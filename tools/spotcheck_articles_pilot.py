"""Live-Crossref spot-check for items in tmp/audit/published_not_in_qa.txt.

For each item:
  - Read IA metadata (issn, year, vol, iss)
  - Download _articles.json.gz, count entries
  - Query live Crossref for (issn, year), filter to (vol, iss), count articles
  - Report match / mismatch

Usage:
  python3 tools/spotcheck_articles_pilot.py --sample 30 [--seed 42] [--workers 4]
"""
from __future__ import annotations
import argparse
import gzip
import io
import json
import random
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SEGART = Path(__file__).resolve().parent.parent
SUSPECT_LIST = SEGART / "tmp" / "audit" / "published_not_in_qa.txt"
OUT = SEGART / "tmp" / "audit" / "spotcheck_articles_pilot.json"

UA = "segart-spotcheck/0.1 (mailto:brewster@archive.org)"
EMAIL = "brewster@archive.org"

_print_lock = threading.Lock()
def log(s):
    with _print_lock:
        print(s, flush=True)


def ia_metadata(item):
    r = subprocess.run(["ia", "metadata", item], capture_output=True, text=True, timeout=60)
    return json.loads(r.stdout)


_DL_CACHE = SEGART / "tmp" / "audit" / "spotcheck_dl_cache"

def ia_download_articles(item):
    """Use the `ia` CLI so configured IA credentials are carried (the
    items are not-public and direct urllib hits get 403)."""
    _DL_CACHE.mkdir(parents=True, exist_ok=True)
    fname = f"{item}_articles.json.gz"
    target = _DL_CACHE / fname
    if not target.exists():
        r = subprocess.run(
            ["ia", "download", item, fname,
             "--destdir", str(_DL_CACHE), "--no-directories"],
            capture_output=True, text=True, timeout=180,
        )
        if r.returncode != 0 or not target.exists():
            raise RuntimeError(f"ia download failed: {r.stderr[-300:]}")
    with gzip.open(target, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def crossref_year(issn, year):
    """Fetch all journal-articles for (issn, year) live from Crossref using
    cursor pagination — same approach as the year-level full cache."""
    items = []
    cursor = "*"
    pages = 0
    while True:
        qs = urllib.parse.urlencode({
            "rows": 200,
            "filter": (f"type:journal-article,from-pub-date:{year}-01,"
                       f"until-pub-date:{year}-12"),
            "select": "DOI,volume,issue,page",
            "cursor": cursor,
            "mailto": EMAIL,
        })
        url = f"https://api.crossref.org/journals/{issn}/works?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as fh:
            data = json.load(fh)
        msg = data.get("message", {})
        page_items = msg.get("items", [])
        items.extend(page_items)
        pages += 1
        nc = msg.get("next-cursor")
        if not page_items or not nc or nc == cursor:
            break
        cursor = nc
        if pages >= 50:
            break
    return items


def label_matches(crossref_label, ia_label):
    """Match 'volume' or 'issue' between Crossref and IA. Handles combined
    issues like '21-22' matching '21'. Mirrors heuristic_toc_crossref logic."""
    a = str(crossref_label or "").strip()
    b = str(ia_label or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    # combined-issue: "21-22" matches "21" or "22"
    if "-" in a:
        parts = [p.strip() for p in a.split("-")]
        if b in parts:
            return True
    if "-" in b:
        parts = [p.strip() for p in b.split("-")]
        if a in parts:
            return True
    return False


_YEAR_RE = __import__("re").compile(r"\b(1[89]\d{2}|20\d{2})\b")

def get_vol_iss_from_ia(meta):
    m = meta.get("metadata", {})
    vol = m.get("volume")
    iss = m.get("issue")
    year = m.get("year")
    if not year:
        # IA "date" can be "Summer 1998", "Fall 1997", "1998-01", etc.
        # Pull the first 4-digit year out of it.
        d = m.get("date") or ""
        mo = _YEAR_RE.search(str(d))
        year = mo.group(1) if mo else None
    issn = m.get("issn")
    if isinstance(issn, list):
        issn = issn[0] if issn else None
    if year:
        year = str(year)[:4]
    return issn, year, str(vol or "").strip(), str(iss or "").strip()


def process_one(item):
    try:
        meta = ia_metadata(item)
        issn, year, vol, iss = get_vol_iss_from_ia(meta)
        if not (issn and year):
            return {"item": item, "status": "skip_no_meta",
                    "issn": issn, "year": year, "vol": vol, "iss": iss}

        articles = ia_download_articles(item)
        published_n = len(articles.get("entries") or articles.get("articles") or [])

        cr_items = crossref_year(issn, year)
        cr_filtered = [
            x for x in cr_items
            if label_matches(x.get("volume"), vol)
            and label_matches(x.get("issue"), iss)
        ]
        cr_n = len(cr_filtered)
        delta = cr_n - published_n
        status = "match" if delta == 0 else ("under" if delta > 0 else "over")
        result = {
            "item": item, "issn": issn, "year": year, "vol": vol, "iss": iss,
            "published_n": published_n, "crossref_n": cr_n,
            "crossref_year_n": len(cr_items),
            "delta": delta, "status": status,
        }
        log(f"  {item:<80} pub={published_n:>3}  cr={cr_n:>3}  Δ={delta:+d}  [{status}]")
        return result
    except Exception as e:
        log(f"  {item:<80} FAIL {type(e).__name__}: {e}")
        return {"item": item, "status": "error", "err": f"{type(e).__name__}: {e}"}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sample", type=int, default=30)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args()

    pop = SUSPECT_LIST.read_text().splitlines()
    pop = [x for x in pop if x.strip()]
    rng = random.Random(args.seed)
    sample = rng.sample(pop, min(args.sample, len(pop)))
    log(f"population={len(pop)} sample={len(sample)} workers={args.workers} seed={args.seed}\n")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_one, it): it for it in sample}
        for f in as_completed(futs):
            results.append(f.result())

    # Summary
    by_status = {}
    for r in results:
        by_status.setdefault(r.get("status", "?"), []).append(r)
    log("\n=== summary ===")
    for s, rs in sorted(by_status.items()):
        log(f"  {s:>8}: {len(rs)}")
    mismatches = [r for r in results if r.get("status") in ("under", "over")]
    if mismatches:
        log("\n=== mismatches ===")
        for r in mismatches:
            log(f"  {r['item']}  pub={r.get('published_n')}  cr={r.get('crossref_n')}  Δ={r.get('delta'):+d}")

    OUT.write_text(json.dumps({
        "seed": args.seed,
        "sample_size": args.sample,
        "population_size": len(pop),
        "results": results,
    }, indent=2))
    log(f"\nresults → {OUT}")


if __name__ == "__main__":
    main()
