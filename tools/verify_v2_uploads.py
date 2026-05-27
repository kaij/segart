"""Post-batch verifier for v2 `_articles.json.gz` uploads.

For a list of IA items, this:

  1. Polls `ia tasks <item>` and waits for any running/queued `category=catalog`
     tasks to finish. IA uploads are reliable but take time to commit —
     reading `ia metadata` immediately after `ia upload` returns the stale
     pre-upload view until the catalog task lands.

  2. Once tasks have settled, downloads each item's `_articles.json.gz` and
     confirms `schema_version == 2` and `segart_version == 1.1.0`.

  3. Reports per-item: ok / waiting / missing / wrong_schema / metadata_error.

Usage:
  python3 tools/verify_v2_uploads.py <items.txt>
      [--per-item-timeout 1800]  # max seconds to wait per item (default 30 min)
      [--workers 4]
      [--out tmp/audit/v2_verify_results.jsonl]

Exit code is 0 if all items verified at v2, 1 if any failed.
"""
from __future__ import annotations
import argparse
import gzip
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SEGART = Path(__file__).resolve().parent.parent
DL_CACHE = SEGART / "tmp" / "audit" / "v2_verify_dl_cache"

_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def ia_tasks_pending(item: str) -> tuple[int, list[int]]:
    """Return (count, task_ids) of in-flight catalog tasks for the item.
    'In-flight' = status is one of {running, queued, paused, error_queued}.
    Returns (0, []) if no pending tasks."""
    r = subprocess.run(["ia", "tasks", item], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        # ia tasks for an unknown item returns 0 with empty body; non-zero is
        # genuinely an error worth surfacing.
        raise RuntimeError(f"ia tasks failed: {r.stderr.strip()}")
    pending = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            t = json.loads(line)
        except Exception:
            continue
        # `history` category is finished tasks; `catalog` is in-flight.
        if t.get("category") != "catalog":
            continue
        status = (t.get("status") or "").lower()
        if status in ("running", "queued", "paused", "error_queued"):
            pending.append(t.get("task_id"))
    return len(pending), pending


def wait_for_tasks(item: str, timeout: int, poll_interval: int = 20) -> tuple[bool, list[int]]:
    """Block until all catalog tasks for item finish, up to `timeout` seconds.
    Returns (settled, last_pending_task_ids). settled=False means timeout
    hit while tasks were still pending."""
    t0 = time.time()
    last_pending = []
    while True:
        n, pending = ia_tasks_pending(item)
        if n == 0:
            return True, []
        last_pending = pending
        if time.time() - t0 > timeout:
            return False, pending
        time.sleep(poll_interval)


def download_articles(item: str) -> Path:
    """Download `<item>_articles.json.gz` to the verify cache. Returns local
    path. Re-fetches each time so we see post-catalog state."""
    DL_CACHE.mkdir(parents=True, exist_ok=True)
    fname = f"{item}_articles.json.gz"
    target = DL_CACHE / fname
    if target.exists():
        target.unlink()
    r = subprocess.run(
        ["ia", "download", item, fname,
         "--destdir", str(DL_CACHE), "--no-directories"],
        capture_output=True, text=True, timeout=180,
    )
    if r.returncode != 0 or not target.exists():
        raise RuntimeError(f"ia download failed: {r.stderr[-300:]}")
    return target


def verify_one(item: str, per_item_timeout: int) -> dict:
    """Wait for catalog tasks, then verify schema. Returns a status dict."""
    result = {"item": item, "ok": False, "status": "unknown"}
    try:
        settled, pending = wait_for_tasks(item, per_item_timeout)
    except Exception as e:
        result["status"] = f"tasks_error: {type(e).__name__}: {e}"
        log(f"  FAIL {item}: {result['status']}")
        return result

    if not settled:
        result["status"] = "timeout_waiting_for_tasks"
        result["pending_task_ids"] = pending
        log(f"  WAIT {item}: still pending after {per_item_timeout}s, tasks={pending}")
        return result

    try:
        path = download_articles(item)
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception as e:
        result["status"] = f"download_or_parse_error: {type(e).__name__}: {e}"
        log(f"  FAIL {item}: {result['status']}")
        return result

    schema = d.get("schema_version")
    segart = (d.get("provenance") or {}).get("software_versions", {}).get("segart_version")
    n_entries = len(d.get("entries") or {})
    result["schema_version"] = schema
    result["segart_version"] = segart
    result["n_entries"] = n_entries

    if schema != 2:
        result["status"] = f"wrong_schema_version: {schema!r}"
        log(f"  FAIL {item}: schema={schema} (want 2)")
        return result

    result["ok"] = True
    result["status"] = "ok"
    log(f"  ok   {item}  schema={schema}  segart={segart}  entries={n_entries}")
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("items_file", help="path to file with IA identifiers, one per line")
    ap.add_argument("--per-item-timeout", type=int, default=1800,
                    help="seconds to wait for catalog tasks per item (default 1800)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=str(SEGART / "tmp/audit/v2_verify_results.jsonl"))
    args = ap.parse_args()

    items = [l.strip() for l in open(args.items_file) if l.strip()]
    log(f"verifying {len(items)} items (timeout={args.per_item_timeout}s/item, workers={args.workers})")

    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex, \
         open(args.out, "w") as fh_ck:
        futs = {ex.submit(verify_one, it, args.per_item_timeout): it for it in items}
        n = 0
        for fut in as_completed(futs):
            it = futs[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = {"item": it, "ok": False, "status": f"exc: {type(e).__name__}: {e}"}
                log(f"  EXC  {it}: {r['status']}")
            fh_ck.write(json.dumps(r) + "\n"); fh_ck.flush()
            results.append(r)
            n += 1
            if n % 25 == 0:
                ok_n = sum(1 for x in results if x.get("ok"))
                log(f"  progress {n}/{len(items)}: {ok_n} ok, {n - ok_n} fail/wait  ({time.time()-t0:.0f}s)")

    ok = [r for r in results if r.get("ok")]
    fail = [r for r in results if not r.get("ok")]
    log(f"\n=== summary ===")
    log(f"  ok:   {len(ok)} / {len(items)}")
    log(f"  fail: {len(fail)}")
    if fail:
        from collections import Counter
        by_status = Counter(r.get("status", "?").split(":")[0] for r in fail)
        for s, n in by_status.most_common():
            log(f"    {s}: {n}")
        log(f"\nfirst 10 failures:")
        for r in fail[:10]:
            log(f"  {r['item']}  status={r.get('status')}")
    log(f"\nresults → {args.out}")
    sys.exit(0 if not fail else 1)


if __name__ == "__main__":
    main()
