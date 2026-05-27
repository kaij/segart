"""Pre-warm per-DOI source caches (fatcat / OpenAlex / Unpaywall / PubMed)
for every DOI in a given scope of IA items.

Inputs:
  - One or more scope files (newline-delimited IA item identifiers)

For each scope item:
  1. Read cached IA metadata → (issn, vol, iss, year)
  2. Read cached Crossref year records → filter to (vol, iss) → collect DOIs

Then for each unique DOI:
  - Fetch fatcat (release lookup → files lookup)
  - Fetch OpenAlex (by-DOI works lookup)
  - Fetch Unpaywall (by-DOI)
  - Fetch PubMed (Europe PMC search by DOI)

All four fetches use the strict-mode `_doi_cache_get` helper from
articles_pilot — retries 429/503 with Retry-After backoff, raises on
permanent errors. Cache hits are no-ops; cold cache populates.

Resumable: re-running skips already-cached DOIs.

Usage:
  python3 tools/prewarm_doi_caches.py SCOPE_FILE [SCOPE_FILE...] [--workers N]
"""
from __future__ import annotations
import argparse
import json
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SEGART = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SEGART / "tools"))

from articles_pilot import (
    fetch_fatcat_by_doi, fetch_openalex_by_doi,
    fetch_unpaywall_by_doi, fetch_pubmed_by_doi,
    FATCAT_CACHE, OPENALEX_CACHE, UNPAYWALL_CACHE, PUBMED_CACHE,
)

IA_META_CACHE  = SEGART / "tmp" / "ia_metadata_cache"
CROSSREF_CACHE = SEGART / "tmp" / "crossref_full_cache_v2"
RESULTS_JSON   = SEGART / "tmp" / "audit" / "prewarm_doi_caches_results.json"

_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def safe_issn(s):
    return re.sub(r"[^A-Za-z0-9-]", "_", s or "")


def label_matches(crossref_label, ia_label):
    a = str(crossref_label or "").strip()
    b = str(ia_label or "").strip()
    if not a or not b: return False
    if a == b: return True
    for s in (a, b):
        if "-" in s and (a in s.split("-") or b in s.split("-")): return True
    return False


def collect_dois_for_item(item: str) -> set[str]:
    """Return the DOI set for this item (one entry per Crossref record that
    matches the item's IA vol/iss labels). Empty if any required cache file
    is missing — caller logs+skips."""
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", item)
    meta_p = IA_META_CACHE / f"{safe_id}.json"
    if not meta_p.exists():
        return set()
    try:
        md = json.loads(meta_p.read_text())
    except Exception:
        return set()
    m = md.get("metadata", {})
    issn = m.get("issn")
    if isinstance(issn, list): issn = issn[0] if issn else None
    if not issn: return set()
    date = (m.get("date") or m.get("year") or "")
    yrm = re.search(r"\b(19|20)\d{2}\b", date)
    year = yrm.group(0) if yrm else None
    if not year: return set()
    vol = m.get("volume"); iss = m.get("issue")
    if not (vol and iss): return set()
    cache_p = CROSSREF_CACHE / f"{safe_issn(issn)}_{year}.json"
    if not cache_p.exists(): return set()
    try:
        d = json.loads(cache_p.read_text())
    except Exception:
        return set()
    dois = set()
    for r in d.get("items", []):
        if label_matches(r.get("volume"), vol) and label_matches(r.get("issue"), iss):
            doi = r.get("DOI")
            if doi: dois.add(doi)
    return dois


def is_cached(cache_dir: Path, doi: str) -> bool:
    """Check whether a per-DOI cache file already exists for this source."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", doi)
    return (cache_dir / f"{safe}.json").exists()


SOURCE_FN = {
    "fatcat":    (fetch_fatcat_by_doi,    FATCAT_CACHE),
    "openalex":  (fetch_openalex_by_doi,  OPENALEX_CACHE),
    "unpaywall": (fetch_unpaywall_by_doi, UNPAYWALL_CACHE),
    "pubmed":    (fetch_pubmed_by_doi,    PUBMED_CACHE),
}


def fetch_one(source: str, doi: str) -> tuple[str, str, str]:
    """Returns (source, doi, status) where status ∈ {hit, fetched, miss404, error}."""
    fn, cache_dir = SOURCE_FN[source]
    # The _doi_cache_get implementation behind each fn already file-caches.
    # Pre-check is just to compute hit-vs-fetched stats.
    pre_existed = is_cached(cache_dir, doi)
    try:
        data = fn(doi)
    except Exception as e:
        return (source, doi, f"error:{type(e).__name__}:{str(e)[:60]}")
    if pre_existed:
        return (source, doi, "hit")
    if data is None:
        return (source, doi, "miss404")
    return (source, doi, "fetched")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scope_files", nargs="+",
                    help="newline-delimited IA item identifier files")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    items = []
    seen = set()
    for sf in args.scope_files:
        for line in Path(sf).read_text().splitlines():
            it = line.strip()
            if it and it not in seen:
                items.append(it); seen.add(it)
    log(f"scope: {len(items)} unique items across {len(args.scope_files)} files")

    # Collect DOIs across all items
    log("\nphase 1: collecting DOIs from cached Crossref year records...")
    all_dois = set()
    n_skipped = 0
    for i, item in enumerate(items, 1):
        dois = collect_dois_for_item(item)
        if not dois:
            n_skipped += 1
        else:
            all_dois.update(dois)
        if i % 500 == 0:
            log(f"  {i}/{len(items)}  total unique DOIs: {len(all_dois)}  skipped: {n_skipped}")
    log(f"\n  total unique DOIs: {len(all_dois)}")
    log(f"  items skipped (missing meta or crossref cache): {n_skipped}")

    # Pre-warm: 4 sources × N DOIs. Hit-vs-fetched stats reported per source.
    log(f"\nphase 2: pre-warming 4 sources × {len(all_dois)} DOIs = {4*len(all_dois)} potential fetches")
    t0 = time.time()
    counters = {src: Counter() for src in SOURCE_FN}
    err_examples = defaultdict(list)
    n = 0
    total = 4 * len(all_dois)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = []
        for doi in all_dois:
            for src in SOURCE_FN:
                futs.append(ex.submit(fetch_one, src, doi))
        for fut in as_completed(futs):
            src, doi, status = fut.result()
            n += 1
            key = status.split(":")[0]
            counters[src][key] += 1
            if key == "error" and len(err_examples[src]) < 3:
                err_examples[src].append((doi, status))
            if n % 2000 == 0:
                el = time.time() - t0
                rate = n / el if el > 0 else 0
                log(f"  {n}/{total}  rate={rate:.1f}/s  fatcat={dict(counters['fatcat'])}  "
                    f"openalex={dict(counters['openalex'])}  ({el:.0f}s)")

    el = time.time() - t0
    log(f"\ndone in {el:.0f}s")
    log("\n=== per-source counts ===")
    for src, c in counters.items():
        log(f"  {src:<10}: {dict(c)}")
        if err_examples[src]:
            for d, s in err_examples[src][:3]:
                log(f"     err sample: {d}  {s}")

    report = {
        "scope_items":       len(items),
        "items_skipped":     n_skipped,
        "unique_dois":       len(all_dois),
        "wall_seconds":      int(el),
        "counts_by_source":  {src: dict(c) for src, c in counters.items()},
        "errors_sample":     {src: err_examples[src][:5] for src in SOURCE_FN},
    }
    RESULTS_JSON.write_text(json.dumps(report, indent=2))
    log(f"\nresults → {RESULTS_JSON}")


if __name__ == "__main__":
    main()
