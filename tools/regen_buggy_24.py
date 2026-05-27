"""Regenerate TOC + articles + review for the 24 items hit by the
bug-truncated per-issue Crossref cache, and upload to IA.

Inputs come from tmp/audit/suspect_items_from_bug_cache.json.

Per [[per_item_toc_review_provenance]]: writes all three together
(_toc.json, _articles.json.gz, IA review) atomically per item.

Usage:
  python3 tools/regen_buggy_24.py [--limit N] [--dry-run] [<item>...]
"""
from __future__ import annotations
import argparse
import gzip
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SEGART = Path(__file__).resolve().parent.parent
TOCS_FIXED = SEGART / "tmp" / "tocs_fixed"
ARTS_FIXED = SEGART / "tmp" / "articles_fixed"
ARTS_FIXED.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(SEGART))
sys.path.insert(0, str(SEGART / "tools"))

REVIEW_BODY = (
    "TOC corrected: regenerated from full Crossref data "
    "(prior version had articles missing due to a cache-truncation bug)."
)

SUSPECT_FILE = SEGART / "tmp" / "audit" / "suspect_items_from_bug_cache.json"


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def regen_toc(item: str) -> Path:
    """Run rerun_with_full_cache.py for one item. Returns the produced TOC path."""
    out = TOCS_FIXED / f"{item}_toc.json"
    if out.exists():
        return out
    r = run([sys.executable, str(SEGART / "tools" / "rerun_with_full_cache.py"), item])
    if r.returncode != 0:
        raise RuntimeError(f"rerun_with_full_cache failed: {r.stderr[-500:]}")
    if not out.exists():
        raise RuntimeError(f"expected {out} not produced; stderr: {r.stderr[-500:]}")
    return out


def build_articles(item: str, toc_path: Path) -> Path:
    out = ARTS_FIXED / f"{item}_articles.json.gz"
    if out.exists():
        return out
    r = run([sys.executable, str(SEGART / "tools" / "build_articles_companion.py"),
             str(toc_path), str(out)])
    if r.returncode != 0:
        raise RuntimeError(f"build_articles_companion failed: {r.stderr[-500:]}")
    return out


def entry_count(path: Path) -> int:
    if path.suffix == ".gz":
        d = json.load(gzip.open(path))
    else:
        d = json.load(open(path))
    if isinstance(d, list):
        return len(d)
    return len(d.get("entries") or d.get("articles") or d.get("items") or [])


def upload(item: str, toc_path: Path, art_path: Path, dry_run: bool):
    """Use publish_atomically from tools/publish_toc.py."""
    from publish_toc import publish_atomically
    toc = json.load(open(toc_path))
    arts = json.load(gzip.open(art_path))
    return publish_atomically(
        item, toc, arts,
        review_body=REVIEW_BODY,
        dry_run=dry_run,
    )


_print_lock = threading.Lock()
def log(msg):
    with _print_lock:
        print(msg, flush=True)


def process_one(i, total, item, dry_run):
    try:
        toc_p = regen_toc(item)
        n_toc = entry_count(toc_p)
        art_p = build_articles(item, toc_p)
        n_art = entry_count(art_p)
        log(f"[{i}/{total}] {item}: toc={n_toc} arts={n_art}")
        r = upload(item, toc_p, art_p, dry_run=dry_run)
        log(f"[{i}/{total}] {item}: uploaded toc={r.get('toc_uploaded')} "
            f"arts={r.get('articles_uploaded')} review={r.get('review_posted')}")
        return {"item": item, "n_toc": n_toc, "n_art": n_art, "ok": True, "resp": r}
    except Exception as e:
        log(f"[{i}/{total}] {item}: FAIL {type(e).__name__}: {e}")
        return {"item": item, "ok": False, "err": str(e)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("items", nargs="*")
    args = p.parse_args()

    if args.items:
        items = args.items
    else:
        data = json.load(open(SUSPECT_FILE))
        items = [r["item"] for r in data["uploaded"]]
        items.extend(data.get("local_only", []))
        items.extend(r["item"] for r in data.get("errors", []))
        seen = set(); items = [x for x in items if not (x in seen or seen.add(x))]

    if args.limit:
        items = items[: args.limit]

    print(f"processing {len(items)} items with {args.workers} workers (dry_run={args.dry_run})", flush=True)
    total = len(items)
    results = []
    if args.workers <= 1:
        for i, item in enumerate(items, 1):
            results.append(process_one(i, total, item, args.dry_run))
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(process_one, i, total, item, args.dry_run): item
                    for i, item in enumerate(items, 1)}
            for f in as_completed(futs):
                results.append(f.result())

    out = SEGART / "tmp" / "audit" / "regen_buggy_24_results.json"
    # Merge with existing results (preserves pilot's record)
    existing = []
    if out.exists():
        try: existing = json.load(open(out))
        except Exception: pass
    by_item = {r["item"]: r for r in existing}
    for r in results: by_item[r["item"]] = r
    json.dump(list(by_item.values()), open(out, "w"), indent=2, default=str)
    print(f"\nresults → {out}", flush=True)
    print(f"ok: {sum(1 for r in results if r['ok'])} / {len(results)}", flush=True)


if __name__ == "__main__":
    main()
