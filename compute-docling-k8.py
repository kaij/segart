#!/usr/bin/env python3
"""Submit a single IA item to the docling-API service, poll until done,
and save the gzipped JSON result(s) under tmp/items/<item>/ following
segart conventions (matches output of local make_more_doclings.py:
tmp/items/<item>/<item>_docling.json.gz).

Per-item job-status response is saved alongside as <item>_docling_job.json
for debugging.

Usage:
  ./compute-docling-k8.py <item>
  ./compute-docling-k8.py --help

The API is always told filename=<item>.pdf so it processes exactly the
canonical Text PDF and skips any encrypted siblings (*.lcpdf,
*_encrypted.pdf, *.acspdf). This matches the IA convention for ~99.83%
of items in scope; the rare multi-PDF case is intentionally not handled
here — use a different tool if you need fanout.

Defaults (each overridable via flag or env var):
  - output:       tmp/items/<item>/  (SEGART_CACHE)
  - timeout:      1800s  (DOCLING_API_TIMEOUT_SEC)
  - poll cadence: 5s  (POLL_INTERVAL_SEC)
  - credentials:  ~/.config/internetarchive/ia.ini [s3] access/secret,
                  overridable via IA_S3_ACCESS_KEY/IA_S3_SECRET_KEY env or
                  --access-key/--secret-key flags. --no-creds suppresses
                  sending them entirely (relies on server's default).

Prints "submit→running: <s>" once when the job leaves "pending" — useful
for detecting worker-pool saturation when running multiple jobs in
parallel (e.g. via GNU parallel).

Exit codes:
  0    succeeded
  1    job ended in failed/partial, or unexpected status
  124  timeout (job continues server-side)
"""
from __future__ import annotations

import argparse
import configparser
import gzip
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_API = "https://docling-api.svc.prod.ca-west-1a.archive.org"
DEFAULT_TMP = Path.home() / "tmp" / "segart" / "tmp"
DEFAULT_IA_INI = Path.home() / ".config" / "internetarchive" / "ia.ini"


def load_ia_credentials(ini_path: Path) -> tuple[str | None, str | None]:
    """Read [s3] access/secret from ~/.config/internetarchive/ia.ini."""
    if not ini_path.exists():
        return None, None
    cp = configparser.ConfigParser()
    try:
        cp.read(ini_path)
    except Exception:
        return None, None
    if not cp.has_section("s3"):
        return None, None
    return cp.get("s3", "access", fallback=None), cp.get("s3", "secret", fallback=None)


def post_json(url: str, body: dict, timeout: int = 30) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        return json.loads(fh.read())


def get_json(url: str, timeout: int = 30) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as fh:
        return json.loads(fh.read())


def download_to_gz(url: str, dest: Path) -> None:
    """Stream a URL to a gzipped file on disk."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as src, gzip.open(dest, "wb") as out:
        shutil.copyfileobj(src, out)


def redact(body: dict) -> dict:
    r = dict(body)
    for k in ("access_key", "secret_key"):
        if k in r and r[k]:
            r[k] = "<redacted>"
    return r


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("item", help="IA item identifier (slug from archive.org/details/<item>)")
    ap.add_argument(
        "--cache-dir",
        default=os.environ.get("SEGART_CACHE") or str(DEFAULT_TMP / "items"),
        help="Output items root (default: %(default)s)",
    )
    ap.add_argument(
        "--api",
        default=os.environ.get("DOCLING_API", DEFAULT_API),
        help="docling-API base URL (default: %(default)s)",
    )
    ap.add_argument(
        "--timeout", type=int,
        default=int(os.environ.get("DOCLING_API_TIMEOUT_SEC", 1800)),
        help="Give up after N seconds (default: %(default)s). Job continues server-side.",
    )
    ap.add_argument(
        "--poll-interval", type=int,
        default=int(os.environ.get("POLL_INTERVAL_SEC", 5)),
        help="Seconds between status polls (default: %(default)s)",
    )
    ap.add_argument(
        "--access-key",
        default=os.environ.get("IA_S3_ACCESS_KEY"),
        help="IA S3 access key (default: from ~/.config/internetarchive/ia.ini)",
    )
    ap.add_argument(
        "--secret-key",
        default=os.environ.get("IA_S3_SECRET_KEY"),
        help="IA S3 secret key (default: from ~/.config/internetarchive/ia.ini)",
    )
    ap.add_argument(
        "--no-creds", action="store_true",
        default=bool(os.environ.get("IA_S3_NO_CREDS")),
        help="Don't send credentials; rely on the API's server-configured default.",
    )
    ap.add_argument("--quiet", "-q", action="store_true",
                    help="Suppress per-poll status pretty-print.")
    args = ap.parse_args()

    # Pegged: always tell the API to process exactly <item>.pdf.
    filename = f"{args.item}.pdf"

    # Resolve creds
    if args.no_creds:
        access_key = secret_key = None
    else:
        access_key, secret_key = args.access_key, args.secret_key
        if not (access_key and secret_key):
            ini_a, ini_s = load_ia_credentials(DEFAULT_IA_INI)
            access_key = access_key or ini_a
            secret_key = secret_key or ini_s

    item_dir = Path(args.cache_dir) / args.item
    item_dir.mkdir(parents=True, exist_ok=True)
    job_outfile = item_dir / f"{args.item}_docling_job.json"

    # Build POST body
    body: dict = {"item": args.item, "filename": filename}
    if access_key and secret_key:
        body["access_key"] = access_key
        body["secret_key"] = secret_key

    if not args.quiet:
        print("request body (redacted):", file=sys.stderr)
        print(json.dumps(redact(body), indent=2), file=sys.stderr)

    # Submit
    try:
        accepted = post_json(f"{args.api}/v1/jobs/archive-item", body)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        print(f"POST failed: HTTP {e.code}: {err_body}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"POST failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    job_id = accepted.get("job_id")
    if not job_id:
        print(f"unexpected accept response: {accepted}", file=sys.stderr)
        return 1
    print(f"job: {job_id}", file=sys.stderr)

    # Poll
    start_ts = time.time()
    running_ts = None
    while True:
        elapsed = int(time.time() - start_ts)
        if elapsed >= args.timeout:
            print(
                f"timeout after {elapsed}s (limit {args.timeout}s); "
                f"job {job_id} may still be running server-side",
                file=sys.stderr,
            )
            return 124

        try:
            resp = get_json(f"{args.api}/v1/jobs/{job_id}")
        except Exception as e:
            print(f"poll failed: {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(args.poll_interval)
            continue

        if not args.quiet:
            print(json.dumps(resp, indent=2))

        status = resp.get("status")

        if running_ts is None and status != "pending":
            running_ts = time.time()
            print(f"submit→running: {int(running_ts - start_ts)}s", file=sys.stderr)

        if status in ("succeeded", "failed", "partial"):
            job_outfile.write_text(json.dumps(resp, indent=2))
            print(f"saved job response to {job_outfile}", file=sys.stderr)

            for f in resp.get("files") or []:
                if f.get("status") != "succeeded":
                    continue
                url = f.get("result_url")
                if not url:
                    continue
                src_filename = f.get("filename") or ""
                # Strip .pdf (case-insensitive) and add _docling.json.gz
                if src_filename.lower().endswith(".pdf"):
                    base = src_filename[:-4]
                else:
                    base = src_filename
                dest = item_dir / f"{base}_docling.json.gz"
                download_to_gz(url, dest)
                print(f"wrote {dest}", file=sys.stderr)

            if status == "succeeded":
                return 0
            return 1  # failed / partial

        if status in ("pending", "running"):
            time.sleep(args.poll_interval)
            continue

        print(f"unexpected status: {status!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
