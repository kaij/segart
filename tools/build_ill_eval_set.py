"""Build the eval set for the ILL-anchored leaf-prediction evaluation.

Filters the raw ILL CSVs to rows that:
  1. were logged on or after 2024-04-01 (structured original_request_params),
  2. belong to an IA item that has docling (default: any item with
     format:docling on IA, from --ia-docling-list; use --local-cache-only
     to restrict to items already downloaded locally),
  3. have all required citation fields populated (issn, vol, iss, year,
     printed pages, title),
  4. have exactly one (n_start, n_stop) leaf range (single-range only;
     multi-range deliveries are out of scope for v1),
  5. are not unfilled,
  6. don't have "contents" or "index" in the IA identifier (cumulative-
     page volumes, not article-bearing issues).

Output schema matches what tools/ill_eval.py expects:
  ill_item, ill_start, ill_stop, issn, vol, iss, yr, pages,
  title, author, journal_title, log_date.

Each emitted row gets an extra "docling_local" boolean — True if the
docling is already cached at tmp/items/<item>/<item>_docling.json.gz,
False if it lives on IA but needs to be downloaded before ill_lookup
can read it.

Refresh the IA docling list with:
  ia search "format:docling" -p scope=all --itemlist > tmp/ia_docling_items.txt

Usage:
  python3 tools/build_ill_eval_set.py \
      --output tmp/audit/ill_eval_set.jsonl

  # Only items whose docling is already local (no downloads needed):
  python3 tools/build_ill_eval_set.py --local-cache-only ...
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import json
import re
import sys
from collections import Counter
from pathlib import Path

SEGART = Path("/Users/brewster/tmp/segart")
sys.path.insert(0, str(SEGART))
sys.path.insert(0, str(SEGART / "tools"))

from parse_ill_logs import parse_row  # noqa: E402

ILL_LOGS_DIR = SEGART / "tmp" / "ill_logs"
ITEMS_DIR = SEGART / "tmp" / "items"
DEFAULT_OUT = SEGART / "tmp" / "audit" / "ill_eval_set.jsonl"
DEFAULT_IA_DOCLING_LIST = SEGART / "tmp" / "ia_docling_items.txt"

CUTOFF_DATE = dt.date(2024, 4, 1)

# Per [[skip_contents_and_index_items]] — cumulative volume pages, not issues.
SKIP_TOKENS = ("contents", "index")

DATE_FROM_FILENAME = re.compile(r"ill_logs_(\d{4})-(\d{2})-(\d{2})\.csv$")


def filename_date(p: Path) -> dt.date | None:
    m = DATE_FROM_FILENAME.search(p.name)
    if not m:
        return None
    try:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def has_local_docling(item: str) -> bool:
    return (ITEMS_DIR / item / f"{item}_docling.json.gz").exists()


def should_skip_identifier(item: str) -> bool:
    return any(tok in item for tok in SKIP_TOKENS)


def load_ia_docling_set(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text().splitlines() if line.strip()}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", "-o", default=str(DEFAULT_OUT),
                    help="output JSONL path (default: %(default)s)")
    ap.add_argument("--ill-dir", default=str(ILL_LOGS_DIR),
                    help="ILL CSV directory (default: %(default)s)")
    ap.add_argument("--cutoff", default=CUTOFF_DATE.isoformat(),
                    help="ISO date; rows on or after this date are kept "
                         "(default: %(default)s)")
    ap.add_argument("--ia-docling-list", default=str(DEFAULT_IA_DOCLING_LIST),
                    help="File of IA items that have format:docling "
                         "(one per line). Built via: "
                         "ia search 'format:docling' -p scope=all --itemlist. "
                         "Default: %(default)s")
    ap.add_argument("--local-cache-only", action="store_true",
                    help="Restrict to items already downloaded to "
                         "tmp/items/. Otherwise: any IA item with "
                         "format:docling is eligible (download required "
                         "before running ill_eval).")
    ap.add_argument("--limit", type=int, default=0,
                    help="If >0, stop after emitting N rows (for smoke tests)")
    args = ap.parse_args()

    cutoff = dt.date.fromisoformat(args.cutoff)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ia_set: set[str] = set()
    if not args.local_cache_only:
        ia_set = load_ia_docling_set(Path(args.ia_docling_list))
        if not ia_set:
            print(f"WARN: --ia-docling-list at {args.ia_docling_list} is empty or "
                  f"missing. Refresh via: "
                  f"ia search 'format:docling' -p scope=all --itemlist > "
                  f"{args.ia_docling_list}", file=sys.stderr)
        else:
            print(f"loaded {len(ia_set):,} items from IA docling list",
                  file=sys.stderr)

    # Walk CSVs whose filename-date is >= cutoff
    csvs = sorted(p for p in Path(args.ill_dir).glob("ill_logs_*.csv")
                  if (d := filename_date(p)) is not None and d >= cutoff)
    print(f"scanning {len(csvs)} ILL CSVs on/after {cutoff} from {args.ill_dir}",
          file=sys.stderr)

    counts = Counter()
    seen_keys: set = set()  # dedupe by (item, title, leaf_ranges)

    n_emitted = 0
    with open(out_path, "w") as out:
        for csv_path in csvs:
            log_date = filename_date(csv_path).isoformat()
            with open(csv_path, newline="") as fh:
                for row in csv.DictReader(fh):
                    counts["row_in"] += 1
                    rec = parse_row(row)
                    if rec is None:
                        counts["parse_fail"] += 1
                        continue
                    if rec["unfill_reason"]:
                        counts["unfilled"] += 1
                        continue

                    item = rec["identifier"]
                    if should_skip_identifier(item):
                        counts["skip_contents_or_index"] += 1
                        continue
                    if len(rec["leaf_ranges"]) != 1:
                        counts["multi_range"] += 1
                        continue

                    # Required citation fields. printed_pages can be missing;
                    # ill_lookup's heuristic copes, but a missing title is fatal.
                    if not rec.get("article_title"):
                        counts["no_title"] += 1
                        continue
                    if not rec.get("issn"):
                        counts["no_issn"] += 1
                        continue
                    if not (rec.get("volume") and rec.get("year")):
                        counts["no_vol_or_year"] += 1
                        continue

                    docling_local = has_local_docling(item)
                    if args.local_cache_only:
                        if not docling_local:
                            counts["no_local_docling"] += 1
                            continue
                    else:
                        # Item must be in the broader IA docling pool
                        if ia_set and item not in ia_set:
                            counts["not_in_ia_docling"] += 1
                            continue
                    if docling_local:
                        counts["docling_local"] += 1
                    else:
                        counts["docling_remote_only"] += 1

                    leaf_start, leaf_stop = rec["leaf_ranges"][0]

                    # Dedupe identical (item, title, leaves) — patrons re-request
                    key = (item, rec["article_title"], leaf_start, leaf_stop)
                    if key in seen_keys:
                        counts["dedup"] += 1
                        continue
                    seen_keys.add(key)

                    sample = {
                        "ill_item": item,
                        "ill_start": leaf_start,
                        "ill_stop": leaf_stop,
                        "issn": rec["issn"],
                        "vol": rec.get("volume") or "",
                        "iss": rec.get("issue") or "",
                        "yr": rec.get("year") or "",
                        "pages": rec.get("printed_pages") or "",
                        "title": rec["article_title"],
                        "author": rec.get("article_author") or "",
                        "journal_title": rec.get("journal_title") or "",
                        "log_date": log_date,
                        "docling_local": docling_local,
                    }
                    out.write(json.dumps(sample, ensure_ascii=False) + "\n")
                    n_emitted += 1
                    counts["emitted"] += 1

                    if args.limit and n_emitted >= args.limit:
                        break
            if args.limit and n_emitted >= args.limit:
                break

    # Summary
    print(f"\nwrote {out_path} ({n_emitted} samples)", file=sys.stderr)
    print("\nfilter counts:", file=sys.stderr)
    for k in ("row_in", "parse_fail", "unfilled", "skip_contents_or_index",
              "multi_range", "no_title", "no_issn", "no_vol_or_year",
              "no_local_docling", "not_in_ia_docling", "dedup", "emitted",
              "docling_local", "docling_remote_only"):
        if k in counts or k in ("emitted",):
            print(f"  {k:24s} {counts.get(k, 0):>8d}", file=sys.stderr)


if __name__ == "__main__":
    main()
