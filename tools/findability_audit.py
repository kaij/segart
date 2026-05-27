"""Per-anchor findability evaluator — would heur_xref's TOC pipeline have
included this ILL-attested article in this IA item?

For each ILL anchor:
  1. anchor.identifier → IA item; pull IA metadata for the item.
  2. (issn, year) from IA metadata → fetch Crossref cache (v2 strict-mode,
     no type filter).
  3. Title + author-surname fuzzy match scoped to the journal-year:
     - 0 candidates  →  not_in_crossref
     - ≥1 candidate that the heur_xref routing predicate would place into
       (vol_IA, iss_IA)  →  success
     - otherwise  →  wrong_voliss

Aggregates per anchor → per (journal-issn, year-bucket) and
(publisher, year-bucket). Outputs ranked tier tables.

Caches IA metadata under tmp/ia_metadata_cache/ to make re-runs cheap.

Usage:
  python3 tools/findability_audit.py [--scope FILE] [--workers N]
                                      [--min-anchors-per-bucket N]
"""
from __future__ import annotations
import argparse
import gzip
import json
import re
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SEGART = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SEGART / "tools"))

# Reuse strict-mode fetch + matchers
from articles_pilot import fetch_crossref_full_for_year, derive_metadata
from journal_xref_audit import (
    fuzzy_first_n_match, author_surname_match,
    is_junk_anchor, year_bucket as audit_year_bucket,
)

CORPUS = SEGART / "tmp" / "qa_corpus.jsonl"
IA_META_CACHE = SEGART / "tmp" / "ia_metadata_cache"
RESULTS_DIR = SEGART / "tmp" / "audit"
PER_ANCHOR_JSONL = RESULTS_DIR / "findability_per_anchor.jsonl"
BY_JOURNAL_JSONL = RESULTS_DIR / "findability_by_journal_year.jsonl"
BY_PUBLISHER_JSONL = RESULTS_DIR / "findability_by_publisher_year.jsonl"

_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def label_matches(crossref_label, ia_label):
    """Same predicate heur_xref uses to place a Crossref record into an
    (IA-vol, IA-iss). Handles combined-issue dashes."""
    a = str(crossref_label or "").strip()
    b = str(ia_label or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    if "-" in a:
        if b in [p.strip() for p in a.split("-")]:
            return True
    if "-" in b:
        if a in [p.strip() for p in b.split("-")]:
            return True
    return False


def ia_metadata_cached(item: str) -> dict | None:
    """File-cached ia metadata. Returns the parsed metadata dict or None
    on failure (logs to caller)."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", item)
    p = IA_META_CACHE / f"{safe}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    try:
        r = subprocess.run(["ia", "metadata", item],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        d = json.loads(r.stdout)
    except Exception:
        return None
    IA_META_CACHE.mkdir(parents=True, exist_ok=True)
    p_tmp = p.with_suffix(".json.tmp")
    p_tmp.write_text(json.dumps(d))
    p_tmp.replace(p)
    return d


def is_skippable_item(item: str, md: dict) -> str | None:
    """Return a skip-reason string if this item shouldn't be scored, else None."""
    if not md:
        return "no_ia_metadata"
    m = md.get("metadata", {}) if isinstance(md, dict) else {}
    if "contents" in item.lower() or "index" in item.lower():
        return "skip_contents_or_index"
    if not (m.get("issn") and (m.get("date") or m.get("year"))):
        return "no_issn_or_year"
    if not (m.get("volume") and m.get("issue")):
        return "no_vol_or_iss"
    return None


def _cr_title(r):
    t = r.get("title")
    return (t or [""])[0] if isinstance(t, list) else (t or "")


_PAGE_NUM_RE = re.compile(r"\d+")


def _page_endpoints(page_str: str | None):
    """Extract first and last integer from a page-range string. Returns
    (start, end) tuple, or (None, None) if no digits."""
    if not page_str:
        return (None, None)
    nums = _PAGE_NUM_RE.findall(str(page_str))
    if not nums:
        return (None, None)
    start = int(nums[0])
    end = int(nums[-1]) if len(nums) > 1 else start
    return (start, end)


def _pages_overlap(anchor_pages: str, crossref_page: str, tol: int = 1) -> bool:
    """Check if anchor's printed_pages overlap with crossref's page field
    within `tol` pages (default ±1, per [ill_data_semantics] — ILL endpoints
    are unreliable by ±1)."""
    a_s, a_e = _page_endpoints(anchor_pages)
    c_s, c_e = _page_endpoints(crossref_page)
    if a_s is None or c_s is None:
        return False
    # Intervals overlap if max(starts) ≤ min(ends), with tolerance applied
    # to the gap.
    return max(a_s, c_s) - min(a_e, c_e) <= tol


def _title_score(a_title: str, r) -> float:
    """SequenceMatcher ratio on lowercased titles."""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a_title.lower(), _cr_title(r).lower()).ratio()


def _best_title_match(a_title: str, records: list):
    """Return (best_record, score) — best title-similarity match across the
    given candidate set, or (None, 0.0) if records is empty."""
    if not records:
        return None, 0.0
    best = None; best_score = 0.0
    for r in records:
        s = _title_score(a_title, r)
        if s > best_score:
            best_score = s; best = r
    return best, best_score


def evaluate_anchor(anchor: dict, crossref_records: list, ia_md_metadata: dict) -> str:
    """The pipeline-equivalence test, "would a person scanning this issue's
    TOC see the article?" Returns 'success' / 'wrong_voliss' /
    'not_in_crossref'.

    Logic:
      1. Filter Crossref records to the IA item's (vol, iss) — the issue's
         actual TOC, typically 5-50 articles.
      2. Within that issue, find the BEST-title match for the anchor.
         If the best is plausibly the right one (score ≥ within-issue floor,
         OR author surname agrees), it's a success — same as a person seeing
         the closest match in a printed TOC.
      3. If no plausible match in-issue, scan the rest of the journal-year
         for a likely match (stricter threshold since the candidate pool is
         100× bigger). If found, that's a wrong_voliss case.
      4. Otherwise, not_in_crossref.

    Author surname is a tiebreaker / corroborator, not a primary filter."""
    issn, vol_ia, iss_ia, year_ia = derive_metadata({"metadata": ia_md_metadata})

    a_title = anchor.get("article_title") or ""
    a_author = anchor.get("article_author") or ""
    a_pages = anchor.get("printed_pages") or ""

    # Step 1: split Crossref records into the issue and the rest of the year.
    in_issue, rest = [], []
    for r in crossref_records:
        if (label_matches(r.get("volume"), vol_ia)
                and label_matches(r.get("issue"), iss_ia)):
            in_issue.append(r)
        else:
            rest.append(r)

    # Step 2: best-within-issue match. Person-scanning-the-TOC model.
    best, score = _best_title_match(a_title, in_issue)
    if best is not None:
        author_ok = author_surname_match(a_author, [best])
        pages_ok = _pages_overlap(a_pages, best.get("page"))
        # Success criteria, any of:
        #   - clear title match on its own (≥ 0.5)
        #   - moderate title (≥ 0.3) + author surname agrees
        #   - moderate title (≥ 0.3) + page-range overlap (±1)
        #   - any title overlap (≥ 0.2) + author surname + small issue
        #     (catches paraphrased / abbreviated ILL-form titles)
        if score >= 0.5:
            return "success"
        if score >= 0.3 and (author_ok or pages_ok):
            return "success"
        if author_ok and score >= 0.2 and len(in_issue) <= 50:
            return "success"

    # Step 3: no plausible in-issue match. Scan the rest of the journal-year.
    best_out, score_out = _best_title_match(a_title, rest)
    if best_out is not None and score_out >= 0.75:
        # Article is in Crossref but registered under a different vol/iss
        # than the IA item carries — pipeline-tunable (combined-issue,
        # supplement, etc.).
        return "wrong_voliss"
    # Lower confidence wrong_voliss when author surname agrees and title
    # has at least token overlap.
    if best_out is not None and score_out >= 0.5 and author_surname_match(a_author, [best_out]):
        return "wrong_voliss"

    return "not_in_crossref"


def process_item(item_id: str, anchors_for_item: list) -> list[dict]:
    """Evaluate every anchor for a single IA item. Returns list of per-anchor
    result dicts."""
    md = ia_metadata_cached(item_id)
    skip_reason = is_skippable_item(item_id, md)
    if skip_reason:
        return [{"item": item_id, "skip": skip_reason, "anchor_title": a.get("article_title","")[:80]}
                for a in anchors_for_item]

    m = md["metadata"]
    issn = m.get("issn")
    if isinstance(issn, list):
        issn = issn[0] if issn else None
    date = (m.get("date") or m.get("year") or "").strip()
    yr_m = re.search(r"\b(19|20)\d{2}\b", date)
    year = yr_m.group(0) if yr_m else (date[:4] if date[:4].isdigit() else None)
    if not (issn and year):
        return [{"item": item_id, "skip": "no_issn_or_year"} for _ in anchors_for_item]

    try:
        crossref_records, err = fetch_crossref_full_for_year(issn, year)
    except RuntimeError as e:
        return [{"item": item_id, "skip": f"crossref_fetch_fail: {str(e)[:80]}"}
                for _ in anchors_for_item]
    if err or not crossref_records:
        return [{"item": item_id, "skip": "crossref_no_records"}
                for _ in anchors_for_item]

    results = []
    publisher = (m.get("publisher") or "").strip()
    bucket = audit_year_bucket(year, span=5)
    for a in anchors_for_item:
        verdict = evaluate_anchor(a, crossref_records, m)
        results.append({
            "item": item_id, "issn": issn, "year": year,
            "year_bucket": bucket, "publisher": publisher,
            "vol": m.get("volume"), "iss": m.get("issue"),
            "anchor_title": (a.get("article_title") or "")[:120],
            "verdict": verdict,
        })
    return results


def load_anchors(scope_path: Path | None):
    """Walk qa_corpus.jsonl. Yields (item_id, anchor_dict). Optionally filter
    to anchors whose item is in scope_path's set."""
    scope_set = None
    if scope_path and scope_path.exists():
        scope_set = set(scope_path.read_text().splitlines())
        scope_set = {x.strip() for x in scope_set if x.strip()}
        log(f"scope filter: {len(scope_set)} items")
    with CORPUS.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            for a in rec.get("anchors", []):
                ident = (a.get("identifier") or "").strip()
                if not ident:
                    continue
                if scope_set is not None and ident not in scope_set:
                    continue
                title = (a.get("article_title") or "").strip()
                jt = (a.get("journal_title") or "").strip()
                if not title:
                    continue
                if is_junk_anchor(title, jt):
                    continue
                yield ident, a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", default=None,
                    help="optional path to a file with IA item identifiers; "
                         "only score anchors whose item is in this set")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--min-anchors-per-bucket", type=int, default=5,
                    help="aggregation threshold; cells with fewer anchors are "
                         "tagged 'low_anchor' rather than tiered")
    args = ap.parse_args()

    scope_path = Path(args.scope) if args.scope else None

    log("loading anchors...")
    by_item = defaultdict(list)
    for ident, a in load_anchors(scope_path):
        by_item[ident].append(a)
    log(f"  {sum(len(v) for v in by_item.values())} anchors across "
        f"{len(by_item)} unique IA items")

    log("\nevaluating anchors (parallel)...")
    all_results = []
    t0 = time.time()
    n = 0
    skip_counter = Counter()
    fh_out = PER_ANCHOR_JSONL.open("w")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_item, it, anchors): it
                for it, anchors in by_item.items()}
        for fut in as_completed(futs):
            n += 1
            try:
                results = fut.result()
            except Exception as e:
                it = futs[fut]
                results = [{"item": it, "skip": f"exc: {type(e).__name__}: {e}"}]
            for r in results:
                fh_out.write(json.dumps(r) + "\n")
                if r.get("skip"):
                    skip_counter[r["skip"].split(":")[0]] += 1
                else:
                    all_results.append(r)
            if n % 25 == 0:
                v_counter = Counter(r["verdict"] for r in all_results)
                log(f"  items {n}/{len(by_item)}  "
                    f"anchors scored: {len(all_results)}  "
                    f"verdicts: {dict(v_counter.most_common(3))}  "
                    f"skips: {sum(skip_counter.values())}  "
                    f"({time.time()-t0:.0f}s)")
    fh_out.close()

    # ===================== aggregate =====================
    log(f"\n=== per-anchor verdict summary ===")
    v_counter = Counter(r["verdict"] for r in all_results)
    total_scored = sum(v_counter.values())
    for v, n in v_counter.most_common():
        pct = 100 * n / total_scored if total_scored else 0
        log(f"  {v:<20}: {n:>6}  ({pct:>5.1f}%)")
    log(f"  --- skips ---")
    for s, n in skip_counter.most_common():
        log(f"  {s:<20}: {n}")

    # Aggregate to (issn, year-bucket)
    by_journal = defaultdict(Counter)
    by_publisher = defaultdict(Counter)
    for r in all_results:
        key_j = (r["issn"], r["year_bucket"])
        key_p = (r["publisher"], r["year_bucket"])
        by_journal[key_j][r["verdict"]] += 1
        by_publisher[key_p][r["verdict"]] += 1

    def tier_of(success_rate, n):
        if n < args.min_anchors_per_bucket:
            return "low_anchor"
        if success_rate >= 0.95: return "clean"
        if success_rate >= 0.80: return "near"
        if success_rate >= 0.50: return "mid"
        return "bad"

    journal_rows = []
    for (issn, bucket), c in by_journal.items():
        n = sum(c.values())
        succ = c["success"]
        rate = (succ / n) if n else 0
        journal_rows.append({
            "issn": issn, "year_bucket": bucket,
            "anchors_scored": n, "success": succ,
            "wrong_voliss": c["wrong_voliss"],
            "not_in_crossref": c["not_in_crossref"],
            "findable_rate": round(rate, 4),
            "tier": tier_of(rate, n),
        })
    pub_rows = []
    for (publisher, bucket), c in by_publisher.items():
        n = sum(c.values())
        succ = c["success"]
        rate = (succ / n) if n else 0
        pub_rows.append({
            "publisher": publisher, "year_bucket": bucket,
            "anchors_scored": n, "success": succ,
            "wrong_voliss": c["wrong_voliss"],
            "not_in_crossref": c["not_in_crossref"],
            "findable_rate": round(rate, 4),
            "tier": tier_of(rate, n),
        })

    journal_rows.sort(key=lambda x: (-x["findable_rate"], -x["anchors_scored"]))
    pub_rows.sort(key=lambda x: (-x["findable_rate"], -x["anchors_scored"]))

    with BY_JOURNAL_JSONL.open("w") as fh:
        for r in journal_rows:
            fh.write(json.dumps(r) + "\n")
    with BY_PUBLISHER_JSONL.open("w") as fh:
        for r in pub_rows:
            fh.write(json.dumps(r) + "\n")

    # Tier summary
    log(f"\n=== journal-year tier counts (min anchors={args.min_anchors_per_bucket}) ===")
    jc = Counter(r["tier"] for r in journal_rows)
    for t in ("clean","near","mid","bad","low_anchor"):
        log(f"  {t:<11}: {jc.get(t,0)}")

    log(f"\n=== publisher-year tier counts (min anchors={args.min_anchors_per_bucket}) ===")
    pc = Counter(r["tier"] for r in pub_rows)
    for t in ("clean","near","mid","bad","low_anchor"):
        log(f"  {t:<11}: {pc.get(t,0)}")

    log(f"\nper-anchor → {PER_ANCHOR_JSONL}")
    log(f"by-journal → {BY_JOURNAL_JSONL}")
    log(f"by-publisher → {BY_PUBLISHER_JSONL}")

    # Show top 10 publisher × year-bucket clean tiers
    clean_pubs = [r for r in pub_rows if r["tier"] == "clean"][:15]
    log(f"\n=== top 15 clean (publisher × year-bucket) ===")
    for r in clean_pubs:
        log(f"  {r['findable_rate']:>6.2%}  n={r['anchors_scored']:>5}  "
            f"{r['publisher']:<50}  {r['year_bucket']}")
    near_pubs = [r for r in pub_rows if r["tier"] == "near"][:10]
    log(f"\n=== top 10 near-clean (publisher × year-bucket) ===")
    for r in near_pubs:
        log(f"  {r['findable_rate']:>6.2%}  n={r['anchors_scored']:>5}  "
            f"{r['publisher']:<50}  {r['year_bucket']}")


if __name__ == "__main__":
    main()
