"""Compute a per-issue confidence score across pilot TOCs.

DRAFT scorer — not yet wired into the pipeline. Lets us spot-check the
gating method against human QA before committing to it.

Inputs:  tmp/audit/pilot_<ident>/<ident>_toc.json
Outputs: tmp/audit/issue_scores_v2.json   (per-item structured data)
         tmp/audit/issue_scores_v2.md     (ranked review report)

Score components (all from existing TOC fields, no re-extraction):
  1. min_art_conf  — min per-entry confidence over articles that aren't
     terminal backmatter (`span_extended_to_end`) or repeated-title
     co-located shorts (`span_co_located_with_siblings`). Those two
     evidence tags mark known-fuzzy boundaries that don't reflect on
     article-body quality.
  2. coverage      — claimed leaves / page_index_count, across ALL
     article entries (including the fuzzy-terminal ones — they still
     occupy real pages).
  3. 1 - max_gap_frac — penalises big interior runs of unclaimed leaves
     that suggest an article is missing from Crossref.

Final score = min(min_art_conf, coverage, 1 - max_gap_frac).

Hard-demote gates (set final = 0.30 when tripped):
  - coverage < 0.75 with >= 10 articles
  - max_gap_pages > 12 with >= 10 articles
These catch supplements / large Crossref holes that the bare min()
doesn't push hard enough on.
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

SEGART = Path(__file__).resolve().parent.parent
PILOTS = SEGART / "tmp" / "audit"
OUT_JSON = PILOTS / "issue_scores_v2.json"
OUT_MD = PILOTS / "issue_scores_v2.md"

# Evidence tags that mark an entry as known-fuzzy at its boundary; we
# exclude them from min_art_conf because the confidence drop reflects the
# pipeline's uncertainty about the *boundary*, not the *article presence*.
FUZZY_BOUNDARY_EV = {
    "span_extended_to_end",
    "span_co_located_with_siblings",
    "frontmatter_positional_default",
    "backmatter_positional_default",
    "crossref_page_unresolvable",
}

HARD_DEMOTE_SCORE = 0.30
COVERAGE_GATE = 0.75
MAX_GAP_GATE = 12
GATE_MIN_ARTICLES = 10


def is_terminal_or_colocated(entry: dict) -> bool:
    ev = set(entry.get("evidence") or [])
    return bool(ev & FUZZY_BOUNDARY_EV)


def claimed_leaves(entries: list[dict]) -> set[int]:
    out: set[int] = set()
    for e in entries:
        for pr in (e.get("page_index_ranges") or []):
            try:
                s = int(pr[0].lstrip("n"))
                en = int(pr[1].lstrip("n"))
            except Exception:
                continue
            for x in range(s, en + 1):
                out.add(x)
    return out


def max_internal_gap(claimed: set[int]) -> int:
    if len(claimed) < 2:
        return 0
    cs = sorted(claimed)
    return max((cs[i] - cs[i - 1] - 1) for i in range(1, len(cs)))


def score_toc(toc: dict) -> dict | None:
    entries = toc.get("entries") or []
    arts = [e for e in entries if e.get("type") == "article"]
    pic = toc.get("page_index_count") or 0
    if not arts or not pic:
        return None

    body_arts = [e for e in arts if not is_terminal_or_colocated(e)]
    if not body_arts:
        body_arts = arts  # don't return None just because everything is fuzzy

    min_art_conf = min(e.get("confidence", 0.0) for e in body_arts)
    confs = sorted(e.get("confidence", 0.0) for e in arts)
    mean_art = sum(confs) / len(confs)

    cl = claimed_leaves(arts)
    coverage = len(cl) / pic if pic else 0.0
    max_gap = max_internal_gap(cl)
    max_gap_frac = max_gap / pic if pic else 0.0

    raw_score = min(min_art_conf, coverage, 1.0 - max_gap_frac)

    gates_tripped = []
    if len(arts) >= GATE_MIN_ARTICLES:
        if coverage < COVERAGE_GATE:
            gates_tripped.append(f"coverage<{COVERAGE_GATE}")
        if max_gap > MAX_GAP_GATE:
            gates_tripped.append(f"max_gap>{MAX_GAP_GATE}")

    final = HARD_DEMOTE_SCORE if gates_tripped else raw_score

    return {
        "min_art_conf": round(min_art_conf, 2),
        "mean_art_conf": round(mean_art, 2),
        "coverage": round(coverage, 3),
        "max_gap_pages": max_gap,
        "max_gap_frac": round(max_gap_frac, 3),
        "page_index_count": pic,
        "articles": len(arts),
        "body_articles": len(body_arts),
        "raw_score": round(raw_score, 3),
        "gates_tripped": gates_tripped,
        "issue_score": round(final, 3),
    }


def load_human_verdicts(csv_path: Path) -> dict[str, Counter]:
    """Read the 1.0.2 QA spreadsheet. Skip TOC-row entries (rows where
    article title is 'Table of Contents') because user said don't QA
    TOC accuracy."""
    rows = list(csv.reader(open(csv_path)))
    data = rows[5:]
    cur = ""
    human: dict[str, list[str]] = defaultdict(list)
    for r in data:
        if not r or len(r) < 11:
            continue
        ident = r[0].strip() or r[1].strip()
        if ident:
            cur = ident
        title = r[2].strip()
        v = r[10].strip()
        if title.lower() == "table of contents":
            continue
        if title == "" and r[3].strip() == "":
            continue
        if v:
            human[cur].append(v)
    return {k: Counter(vs) for k, vs in human.items()}


def status_from_verdicts(c: Counter) -> str:
    if not c:
        return "UNREV"
    if any(c.get(x, 0) for x in ("RED", "BLUE", "ORANGE")):
        return "BAD"
    return "GOOD"


def main():
    csv_path = Path("/tmp/qa_102.csv")
    human = load_human_verdicts(csv_path) if csv_path.exists() else {}

    results = []
    for d in sorted(PILOTS.glob("pilot_sim_*")):
        ident = d.name[len("pilot_"):]
        toc_path = d / f"{ident}_toc.json"
        if not toc_path.exists():
            continue
        toc = json.loads(toc_path.read_text())
        sc = score_toc(toc)
        if sc is None:
            continue
        sc["ident"] = ident
        verdicts = human.get(ident, Counter())
        sc["status"] = status_from_verdicts(verdicts)
        sc["verdicts"] = dict(verdicts)
        results.append(sc)

    results.sort(key=lambda x: (x["issue_score"], x["coverage"]))
    OUT_JSON.write_text(json.dumps(results, indent=2))

    # Markdown report — ranked, ready to skim
    lines = []
    lines.append("# Issue confidence scores — pilot batch (v2 draft)\n")
    lines.append(f"Scored {len(results)} items. Threshold candidate: **0.50**.\n")
    lines.append(f"Hard-demote gates: coverage < {COVERAGE_GATE} or max_gap > "
                 f"{MAX_GAP_GATE} on >= {GATE_MIN_ARTICLES} articles → score = {HARD_DEMOTE_SCORE}.\n")
    lines.append("\n## Confusion @ threshold 0.50\n")

    def split(rows, status, threshold=0.50):
        return (sum(1 for r in rows if r["status"] == status and r["issue_score"] >= threshold),
                sum(1 for r in rows if r["status"] == status and r["issue_score"] < threshold))

    for status in ("GOOD", "BAD", "UNREV"):
        passN, holdN = split(results, status)
        lines.append(f"- **{status}**: {passN} pass / {holdN} hold\n")

    lines.append("\n## All items (lowest score first)\n")
    lines.append("| score | cov | gap | art | status | gates | flags | ident |")
    lines.append("|------:|----:|----:|----:|:------|:------|:------|:------|")
    for r in results:
        flags = " ".join(f"{k}:{v}" for k, v in sorted(r["verdicts"].items()))
        gates = ",".join(r["gates_tripped"]) if r["gates_tripped"] else ""
        ident_link = f"[{r['ident']}](https://archive.org/details/{r['ident']})"
        lines.append(
            f"| {r['issue_score']:.2f} | {r['coverage']:.2f} | {r['max_gap_pages']} | "
            f"{r['articles']} | {r['status']} | {gates} | {flags} | {ident_link} |"
        )

    lines.append("\n## Score formula reminder\n")
    lines.append("```\n"
                 "raw = min(min_art_conf, coverage, 1 - max_gap_frac)\n"
                 "gates: coverage<0.75 OR max_gap>12 (when articles>=10) → 0.30\n"
                 "issue_score = gates_tripped ? 0.30 : raw\n"
                 "```\n")
    lines.append("min_art_conf is computed over article entries EXCLUDING those tagged\n")
    lines.append("`span_extended_to_end`, `span_co_located_with_siblings`,\n")
    lines.append("`frontmatter_positional_default`, `backmatter_positional_default`,\n")
    lines.append("`crossref_page_unresolvable` — those are fuzzy-boundary signals,\n")
    lines.append("not article-body quality signals.\n")

    OUT_MD.write_text("\n".join(lines))
    print(f"wrote {OUT_JSON}")
    print(f"wrote {OUT_MD}")
    print(f"\n{len(results)} items scored.")


if __name__ == "__main__":
    main()
