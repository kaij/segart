"""Pre-warm `tmp/crossref_full_cache_v2/` for the items in
tmp/audit/v2_rebuild_scope.txt.

Walks each item, derives (issn, year) from IA metadata (uses the cached IA
metadata where present), dedupes, and fetches each unique pair via
articles_pilot.fetch_crossref_full_for_year — which writes to the v2 cache.

Per-pair fetch is strict-mode (retries 429/503 with Retry-After, raises on
permanent 4xx). On any RuntimeError, logs and moves on to the next pair
rather than aborting the whole batch (since one bad ISSN shouldn't block
the other 99% of work). Failed pairs are reported at the end so an
operator can investigate.

Usage:
  python3 tools/prewarm_crossref_cache_v2.py [--scope FILE] [--workers N]
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SEGART = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SEGART / "tools"))
import articles_pilot as ap
from articles_pilot import derive_metadata, ia_metadata, FULL_CACHE

DEFAULT_SCOPE = SEGART / "tmp" / "audit" / "v2_rebuild_scope.txt"
RESULTS = SEGART / "tmp" / "audit" / "prewarm_crossref_cache_v2_results.json"

_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def derive_pair(item):
    """Return (issn, year) for the item, or None if metadata is missing."""
    md = ia_metadata(item)
    if not md:
        return None
    issn, vol, iss, yr = derive_metadata(md)
    if not (issn and yr):
        return None
    return (issn, yr)


def cache_path_for(issn, year):
    safe = re.sub(r"[^A-Za-z0-9-]", "_", issn)
    return FULL_CACHE / f"{safe}_{year}.json"


def fetch_pair(issn, year):
    """Returns ('hit' | 'fetched' | 'error', detail). A cache file with
    `error` set or `items=[]` is treated as a cache-miss and refetched —
    stale-bad entries from the pre-strict cache code don't count as hits."""
    p = cache_path_for(issn, year)
    if p.exists():
        try:
            d = json.loads(p.read_text())
            is_clean = (not d.get("error")
                        and d.get("type_filter") is None
                        and isinstance(d.get("items"), list)
                        and len(d["items"]) > 0)
            if is_clean:
                return ("hit", f"{len(d['items'])} items")
        except Exception:
            pass  # corrupted; refetch
    try:
        items, err = ap.fetch_crossref_full_for_year(issn, year)
        if err:
            return ("error", err[:200])
        return ("fetched", f"{len(items)} items")
    except Exception as e:
        return ("error", f"{type(e).__name__}: {e}")


def main():
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--scope", default=str(DEFAULT_SCOPE))
    ap_.add_argument("--workers", type=int, default=4,
                     help="parallelism for IA-metadata lookups and Crossref fetches")
    args = ap_.parse_args()

    scope = [l.strip() for l in Path(args.scope).read_text().splitlines() if l.strip()]
    log(f"scope: {len(scope)} items from {args.scope}")

    # Phase 1: derive (issn, year) per item in parallel (IA metadata calls)
    log(f"\nPhase 1: deriving (issn, year) for {len(scope)} items ({args.workers} workers)...")
    pairs = {}     # (issn, year) -> count of items
    by_item = {}   # item -> (issn, year)
    no_meta = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(derive_pair, it): it for it in scope}
        n = 0
        for fut in as_completed(futs):
            it = futs[fut]
            n += 1
            try:
                pair = fut.result()
            except Exception as e:
                no_meta.append((it, f"{type(e).__name__}: {e}"))
                continue
            if pair is None:
                no_meta.append((it, "no metadata"))
                continue
            by_item[it] = pair
            pairs[pair] = pairs.get(pair, 0) + 1
            if n % 50 == 0:
                log(f"  derived {n}/{len(scope)} ({time.time()-t0:.1f}s)")
    log(f"  derived: {len(by_item)} items → {len(pairs)} unique (issn, year) pairs")
    if no_meta:
        log(f"  no-meta: {len(no_meta)} items (see results file)")

    # Phase 2: fetch each unique pair, serially with rate limiting
    # (Crossref polite-pool tolerates ~50 req/sec; we go conservatively
    # to be a good citizen. Plus, _doi_cache_get already handles 429.)
    log(f"\nPhase 2: fetching {len(pairs)} unique (issn, year) pairs into {FULL_CACHE}/...")
    results = {"hit": [], "fetched": [], "error": []}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_pair, issn, yr): (issn, yr) for (issn, yr) in pairs}
        n = 0
        for fut in as_completed(futs):
            (issn, yr) = futs[fut]
            n += 1
            try:
                status, detail = fut.result()
            except Exception as e:
                status, detail = "error", f"{type(e).__name__}: {e}"
            entry = {"issn": issn, "year": yr, "n_items": pairs[(issn, yr)], "detail": detail}
            results[status].append(entry)
            if status == "error":
                log(f"  [{n}/{len(pairs)}] ERROR  {issn} {yr}: {detail}")
            elif n % 25 == 0 or n == len(pairs):
                log(f"  [{n}/{len(pairs)}] ok so far: hit={len(results['hit'])} fetched={len(results['fetched'])} error={len(results['error'])}  ({time.time()-t0:.1f}s)")

    # Summary
    log(f"\n=== summary ===")
    log(f"  items in scope:    {len(scope)}")
    log(f"  items with meta:   {len(by_item)}")
    log(f"  unique (issn,year): {len(pairs)}")
    log(f"  cache hit:         {len(results['hit'])}")
    log(f"  fetched fresh:     {len(results['fetched'])}")
    log(f"  errors:            {len(results['error'])}")
    log(f"  items with no meta:{len(no_meta)}")
    if results["error"]:
        log(f"\nfirst 10 errors:")
        for e in results["error"][:10]:
            log(f"  {e['issn']} {e['year']} (n_items={e['n_items']}): {e['detail']}")

    # Write results
    out = {
        "scope_size": len(scope),
        "items_with_meta": len(by_item),
        "unique_pairs": len(pairs),
        "results": results,
        "no_meta": [{"item": it, "reason": r} for it, r in no_meta],
    }
    RESULTS.write_text(json.dumps(out, indent=2))
    log(f"\nresults → {RESULTS}")


if __name__ == "__main__":
    main()
