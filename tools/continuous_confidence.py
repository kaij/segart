"""Continuous per-entry confidence — DRAFT replacement for the discrete
{0.15, 0.2, 0.4, 0.5, 0.6, 0.7, 0.8} buckets in heur_xref_to_legacy.

Not wired into the pipeline. Reads published TOCs, re-derives per-entry
confidence from evidence-tag combinations + span-length sanity vs. the
issue's article-length median + 1-pager-adjacency check.

Output:
  tmp/audit/continuous_conf_v1.json   per-entry old/new comparison
  tmp/audit/continuous_conf_v1.md     summary + suggested QA list

Design rationale:
  * 79% of entries are plain `page_numbers` at 0.7 today. We want to
    *separate* these from the 53 entries tagged `title_in_docling+xref_span`,
    which have two independent agreeing signals.
  * `span_inferred_from_next_entry` should drop further when the inferred
    length is wildly longer than the issue's median article — catches the
    JAACAP "Being Bullied" overshoot class.
  * Crossref-deposited entries adjacent to a 1-pager (errata/calendar)
    should get a mild demote — catches the nursing-research off-by-1
    class (Crossref end-page wrong by 1). Asymmetric mild penalty since
    most adjacent-1-pager cases are correct.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path

SEGART = Path(__file__).resolve().parent.parent
PILOTS = SEGART / "tmp" / "audit"
OUT_JSON = PILOTS / "continuous_conf_v1.json"
OUT_MD = PILOTS / "continuous_conf_v1.md"

# Base score from the strongest *positive locator* signal in the evidence.
# Listed in priority order — first matching tag wins as the base.
LOCATOR_BASE = [
    ("title_in_docling+xref_span", 0.85),  # two independent signals agree
    ("docling_section_header",      0.82), # TOC frontmatter detection
    ("page_numbers",                0.75), # Crossref deposited s+e, located
    ("page_numbers+repair",         0.72), # needed minor repair to locate
    ("page_numbers_partial",        0.67), # only start matched cleanly
    ("title_in_docling",            0.72), # docling section_header match
    ("start_from_docling_page_header", 0.65),  # relocated via page header
    ("resolved_via_docling_or_hocr",   0.45),  # fallback locator
    ("crossref_page_unresolvable",     0.20),
    ("frontmatter_positional_default", 0.18),
    ("backmatter_positional_default",  0.18),
]

# Modifiers applied additively after picking the base.
MODIFIERS = {
    "span_inferred_from_next_entry":              -0.18,
    "span_inferred_from_next_entry_shared_page":  -0.10,  # layout-confirmed
    "span_extended_to_end":                       -0.28,
    "span_co_located_with_siblings":              -0.28,
    "trim_trailing_blank":                        +0.02,
}

# Span-length sanity: only applies when end was inferred.
SPAN_RATIO_PENALTIES = [
    (2.5, -0.18),  # wildly long — almost certainly a missed article
    (2.0, -0.12),
    (1.5, -0.06),
]

# 1-pager-adjacency penalty: deposited-end entries followed by a 1-page
# entry are at risk for Crossref-end-off-by-1 (nursing-research class).
ONE_PAGER_ADJACENCY_PENALTY = -0.10


def pick_base(ev: set[str]) -> tuple[float, str]:
    for tag, score in LOCATOR_BASE:
        if tag in ev:
            return score, tag
    return 0.50, "unknown"


def span_pages(entry: dict) -> int | None:
    pr = entry.get("page_index_ranges") or []
    if not pr or not pr[0]:
        return None
    try:
        s = int(pr[0][0].lstrip("n"))
        e = int(pr[0][1].lstrip("n"))
        return max(1, e - s + 1)
    except Exception:
        return None


def start_pi(entry: dict) -> int | None:
    pr = entry.get("page_index_ranges") or []
    if not pr or not pr[0]:
        return None
    try:
        return int(pr[0][0].lstrip("n"))
    except Exception:
        return None


def end_pi(entry: dict) -> int | None:
    pr = entry.get("page_index_ranges") or []
    if not pr or not pr[0]:
        return None
    try:
        return int(pr[0][1].lstrip("n"))
    except Exception:
        return None


def issue_median_article_span(entries: list[dict]) -> int | None:
    """Median *body article* span in the issue. Excludes:
      - non-article entries (TOC, frontmatter/backmatter)
      - spans < 4 pages (editorials, calendar, errata, in-this-issue)
      - terminal backmatter (span_extended_to_end)
      - co-located shorts (span_co_located_with_siblings)
    The intent is to get the typical *real article* length, so the
    span-ratio sanity check can flag wildly-long inferred spans without
    misfiring on issues that happen to contain many short pieces."""
    spans = []
    for e in entries:
        if e.get("type") != "article":
            continue
        sp = span_pages(e)
        if sp is None or sp < 4:
            continue
        ev = set(e.get("evidence") or [])
        if "span_extended_to_end" in ev:
            continue
        if "span_co_located_with_siblings" in ev:
            continue
        spans.append(sp)
    if not spans:
        return None
    return int(statistics.median(spans))


def mark_one_pager_adjacency(entries: list[dict]) -> None:
    """Annotate each entry with `_followed_by_one_pager: True` if the
    next entry (by start_pi) is exactly 1 page starting at this entry's
    end + 1."""
    # Sort by start_pi (skipping unplaced)
    placed = [e for e in entries if start_pi(e) is not None]
    placed.sort(key=lambda e: start_pi(e))
    for i, e in enumerate(placed[:-1]):
        nxt = placed[i + 1]
        e_end = end_pi(e)
        n_start = start_pi(nxt)
        n_end = end_pi(nxt)
        if e_end is None or n_start is None or n_end is None:
            continue
        if n_end == n_start and n_start == e_end + 1:
            e["_followed_by_one_pager"] = True


def continuous_confidence(entry: dict, median_span: int | None) -> dict:
    ev = set(entry.get("evidence") or [])
    base, base_tag = pick_base(ev)
    score = base
    breakdown = [f"base={base:.2f}({base_tag})"]

    # Modifiers
    for tag, delta in MODIFIERS.items():
        if tag in ev:
            score += delta
            breakdown.append(f"{tag}={delta:+.2f}")

    # Span sanity: only if end was inferred
    inferred = ("span_inferred_from_next_entry" in ev
                or "span_inferred_from_next_entry_shared_page" in ev)
    if inferred and median_span:
        sp = span_pages(entry)
        if sp:
            ratio = sp / median_span
            for thresh, penalty in SPAN_RATIO_PENALTIES:
                if ratio >= thresh:
                    score += penalty
                    breakdown.append(
                        f"span_ratio_{thresh}x={penalty:+.2f}({sp}/{median_span})"
                    )
                    break

    # 1-pager adjacency: deposited-end + next-entry-is-1-pager
    deposited_end = (
        ("page_numbers" in ev or "page_numbers+repair" in ev)
        and not inferred
    )
    if deposited_end and entry.get("_followed_by_one_pager"):
        score += ONE_PAGER_ADJACENCY_PENALTY
        breakdown.append(f"adjacent_1pager={ONE_PAGER_ADJACENCY_PENALTY:+.2f}")

    final = max(0.05, min(0.95, round(score, 3)))
    return {"score": final, "breakdown": breakdown}


def main():
    results = []
    distribution_old = Counter()
    distribution_new = Counter()
    biggest_drops = []   # entries whose new conf is much lower than old
    biggest_rises = []   # entries whose new conf is much higher than old

    for d in sorted(PILOTS.glob("pilot_sim_*")):
        ident = d.name[len("pilot_") :]
        toc_path = d / f"{ident}_toc.json"
        if not toc_path.exists():
            continue
        toc = json.loads(toc_path.read_text())
        entries = toc.get("entries") or []
        median = issue_median_article_span(entries)
        mark_one_pager_adjacency(entries)

        rescored = []
        for e in entries:
            new = continuous_confidence(e, median)
            old = e.get("confidence", 0.0)
            delta = new["score"] - old
            rescored.append({
                "id": e.get("id"),
                "title": (e.get("title") or "")[:80],
                "type": e.get("type"),
                "evidence": e.get("evidence") or [],
                "old_conf": round(old, 3),
                "new_conf": new["score"],
                "delta": round(delta, 3),
                "breakdown": new["breakdown"],
                "span_pages": span_pages(e),
            })
            distribution_old[round(old, 2)] += 1
            distribution_new[round(new["score"], 2)] += 1
            if delta <= -0.10:
                biggest_drops.append({
                    "ident": ident, "id": e.get("id"),
                    "title": (e.get("title") or "")[:60],
                    "old": round(old, 2), "new": new["score"],
                    "delta": round(delta, 3),
                    "ev": e.get("evidence") or [],
                    "span": span_pages(e),
                    "median": median,
                })
            elif delta >= 0.05:
                biggest_rises.append({
                    "ident": ident, "id": e.get("id"),
                    "title": (e.get("title") or "")[:60],
                    "old": round(old, 2), "new": new["score"],
                    "delta": round(delta, 3),
                    "ev": e.get("evidence") or [],
                })

        results.append({
            "ident": ident,
            "median_article_span": median,
            "entries": rescored,
        })

    biggest_drops.sort(key=lambda r: r["delta"])
    biggest_rises.sort(key=lambda r: -r["delta"])

    OUT_JSON.write_text(json.dumps(results, indent=2))

    # ---- Markdown summary ----
    lines = []
    lines.append("# Continuous per-entry confidence — draft v1\n")
    lines.append("Standalone re-scoring of all 2329 entries across 149 pilot TOCs.\n")
    lines.append("Not wired into the pipeline.\n")

    lines.append("\n## Distribution shift\n")
    lines.append("| Old bucket | n | New bucket median | New range |")
    lines.append("|--:|--:|--:|---|")
    for old_b in sorted(distribution_old):
        n = distribution_old[old_b]
        # Find new values for entries that had old=old_b
        new_for_old = []
        for r in results:
            for e in r["entries"]:
                if round(e["old_conf"], 2) == old_b:
                    new_for_old.append(e["new_conf"])
        if new_for_old:
            med = statistics.median(new_for_old)
            lo, hi = min(new_for_old), max(new_for_old)
            lines.append(f"| {old_b:.2f} | {n} | {med:.2f} | {lo:.2f}–{hi:.2f} |")

    lines.append("\n## Biggest drops (entries newly flagged as uncertain)\n")
    lines.append("Mostly the JAACAP `span_inferred_from_next_entry` overshoot class — the new span-vs-median sanity check pulls them down.\n")
    lines.append("\n| old → new | Δ | span/median | ident · entry | evidence |")
    lines.append("|---|---|---|---|---|")
    for r in biggest_drops[:25]:
        ratio = f"{r['span']}/{r['median']}" if r["span"] and r["median"] else "—"
        lines.append(
            f"| {r['old']:.2f} → {r['new']:.2f} | {r['delta']:.2f} | {ratio} | "
            f"{r['ident']} · {r['id']} `{r['title']}` | {r['ev']} |"
        )

    lines.append("\n## Biggest rises (entries newly recognised as multi-signal-confirmed)\n")
    lines.append("Mostly entries tagged `title_in_docling+xref_span` — two independent signals agree.\n")
    lines.append("\n| old → new | Δ | ident · entry | evidence |")
    lines.append("|---|---|---|---|")
    for r in biggest_rises[:15]:
        lines.append(
            f"| {r['old']:.2f} → {r['new']:.2f} | +{r['delta']:.2f} | "
            f"{r['ident']} · {r['id']} `{r['title']}` | {r['ev']} |"
        )

    lines.append("\n## Formula summary\n")
    lines.append("```")
    lines.append("Base from strongest locator signal in evidence:")
    for tag, score in LOCATOR_BASE:
        lines.append(f"  {tag:<40} {score:.2f}")
    lines.append("")
    lines.append("Modifiers (additive):")
    for tag, d in MODIFIERS.items():
        lines.append(f"  {tag:<45} {d:+.2f}")
    lines.append("")
    lines.append("Span-length sanity (when end was inferred):")
    for t, p in SPAN_RATIO_PENALTIES:
        lines.append(f"  span >= {t}x issue median article length     {p:+.2f}")
    lines.append("")
    lines.append(f"1-pager adjacency (deposited-end + next is 1-pager): {ONE_PAGER_ADJACENCY_PENALTY:+.2f}")
    lines.append(f"Clipped to [0.05, 0.95].")
    lines.append("```")

    OUT_MD.write_text("\n".join(lines))
    print(f"wrote {OUT_JSON}")
    print(f"wrote {OUT_MD}")
    print(f"\n{sum(distribution_new.values())} entries rescored across {len(results)} items")
    print(f"Biggest drops: {len(biggest_drops)}   biggest rises: {len(biggest_rises)}")


if __name__ == "__main__":
    main()
