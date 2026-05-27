"""Build the rebuild scope for clean + near publisher×year-bucket cells from
the findability audit. Two outputs:

  - tmp/audit/clean_tier_items.txt    (IA items in clean tier cells)
  - tmp/audit/near_tier_items.txt     (IA items in near tier cells)

Also reports Crossref year-cache coverage and identifies (issn, year)
pairs that still need to be pre-warmed.

Sources:
  - tmp/audit/findability_by_publisher_year.jsonl  (the tier table)
  - tmp/ia_metadata_cache/                          (which items belong where)

Each cached IA-metadata JSON is read to extract (publisher, year_bucket).
Items whose (publisher, year_bucket) is in the clean (or near) set are
included.

Usage:
  python3 tools/build_clean_tier_scope.py
"""
from __future__ import annotations
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

SEGART = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SEGART / "tools"))

from journal_xref_audit import year_bucket as audit_year_bucket

AUDIT_PUB_TABLE = SEGART / "tmp" / "audit" / "findability_by_publisher_year.jsonl"
IA_META_CACHE   = SEGART / "tmp" / "ia_metadata_cache"
CROSSREF_CACHE  = SEGART / "tmp" / "crossref_full_cache_v2"

OUT_CLEAN  = SEGART / "tmp" / "audit" / "clean_tier_items.txt"
OUT_NEAR   = SEGART / "tmp" / "audit" / "near_tier_items.txt"
OUT_PAIRS_MISSING = SEGART / "tmp" / "audit" / "clean_tier_crossref_pairs_missing.txt"
OUT_REPORT = SEGART / "tmp" / "audit" / "clean_tier_scope_report.json"


def derive_publisher_year_bucket(md_record: dict) -> tuple[str | None, str | None, str | None, str | None]:
    """From an `ia metadata` JSON dict, derive (publisher, issn, year, year_bucket).
    Returns (None, None, None, None) if any required field is missing."""
    if not isinstance(md_record, dict):
        return (None, None, None, None)
    m = md_record.get("metadata", {})
    publisher = (m.get("publisher") or "").strip()
    issn = m.get("issn")
    if isinstance(issn, list):
        issn = issn[0] if issn else None
    issn = (issn or "").strip() or None
    date = (m.get("date") or m.get("year") or "").strip()
    yr_m = re.search(r"\b(19|20)\d{2}\b", date)
    year = yr_m.group(0) if yr_m else (date[:4] if date[:4].isdigit() else None)
    if not (publisher and year):
        return (None, issn, year, None)
    bucket = audit_year_bucket(year, span=5)
    return (publisher, issn, year, bucket)


def main():
    # 1) Load tier sets from the publisher-table.
    clean_set = set()
    near_set = set()
    n_rows = 0
    with AUDIT_PUB_TABLE.open() as fh:
        for line in fh:
            line = line.strip()
            if not line: continue
            r = json.loads(line)
            n_rows += 1
            key = (r["publisher"], r["year_bucket"])
            if r["tier"] == "clean":
                clean_set.add(key)
            elif r["tier"] == "near":
                near_set.add(key)
    print(f"audit rows: {n_rows}")
    print(f"  clean publisher-year cells: {len(clean_set)}")
    print(f"  near publisher-year cells:  {len(near_set)}")

    # 2) Walk IA metadata cache; classify each item.
    files = list(IA_META_CACHE.glob("*.json"))
    print(f"\nIA-metadata cache: {len(files)} items")

    clean_items = []
    near_items = []
    item_no_meta = item_no_pub_or_year = 0
    pairs_clean = set(); pairs_near = set()

    for p in files:
        try:
            md = json.loads(p.read_text())
        except Exception:
            item_no_meta += 1; continue
        publisher, issn, year, bucket = derive_publisher_year_bucket(md)
        if not (publisher and bucket and issn):
            item_no_pub_or_year += 1; continue
        item_id = p.stem.replace("_", "-")  # the cache filename re-mangling
        # Better: pull from metadata if present.
        m = md.get("metadata", {})
        item_id = m.get("identifier") or item_id
        key = (publisher, bucket)
        if key in clean_set:
            clean_items.append(item_id)
            pairs_clean.add((issn, year))
        elif key in near_set:
            near_items.append(item_id)
            pairs_near.add((issn, year))

    print(f"  no_meta: {item_no_meta}")
    print(f"  no_pub_or_year: {item_no_pub_or_year}")
    print(f"\nitems in clean cells: {len(clean_items)}")
    print(f"items in near cells:  {len(near_items)}")

    # 3) Crossref cache coverage.
    cached_pairs = set()
    if CROSSREF_CACHE.exists():
        for cp in CROSSREF_CACHE.glob("*.json"):
            # filename format: {safe-issn}_{year}.json
            stem = cp.stem
            m = re.match(r"^(.+)_(\d{4})$", stem)
            if not m: continue
            cached_pairs.add((m.group(1), m.group(2)))

    # Some ISSNs have hyphens normalized; we need to compare apples-to-apples
    def safe_issn(s): return re.sub(r"[^A-Za-z0-9-]", "_", s or "")
    pairs_clean_safe = {(safe_issn(i), y) for (i, y) in pairs_clean}
    pairs_near_safe = {(safe_issn(i), y) for (i, y) in pairs_near}

    missing_clean = pairs_clean_safe - cached_pairs
    missing_near = pairs_near_safe - cached_pairs

    print(f"\ncrossref year-cache: {len(cached_pairs)} cached pairs")
    print(f"  unique (issn, year) in clean items: {len(pairs_clean_safe)}  → {len(missing_clean)} missing")
    print(f"  unique (issn, year) in near items:  {len(pairs_near_safe)}  → {len(missing_near)} missing")

    # 4) Write outputs.
    OUT_CLEAN.write_text("\n".join(sorted(clean_items)) + "\n")
    OUT_NEAR.write_text("\n".join(sorted(near_items)) + "\n")
    missing_all = sorted(missing_clean | missing_near)
    OUT_PAIRS_MISSING.write_text("\n".join(f"{i}\t{y}" for i, y in missing_all) + "\n")
    print(f"\nwrote:")
    print(f"  {OUT_CLEAN}  ({len(clean_items)} items)")
    print(f"  {OUT_NEAR}  ({len(near_items)} items)")
    print(f"  {OUT_PAIRS_MISSING}  ({len(missing_all)} (issn, year) pairs to fetch)")

    report = {
        "clean_publisher_year_cells": len(clean_set),
        "near_publisher_year_cells": len(near_set),
        "ia_items_cached": len(files),
        "items_in_clean": len(clean_items),
        "items_in_near": len(near_items),
        "items_no_meta": item_no_meta,
        "items_no_pub_or_year": item_no_pub_or_year,
        "crossref_cache_pairs": len(cached_pairs),
        "unique_pairs_clean": len(pairs_clean_safe),
        "unique_pairs_near": len(pairs_near_safe),
        "missing_pairs_clean": len(missing_clean),
        "missing_pairs_near": len(missing_near),
        "missing_pairs_total": len(missing_all),
    }
    OUT_REPORT.write_text(json.dumps(report, indent=2))
    print(f"  {OUT_REPORT}")


if __name__ == "__main__":
    main()
