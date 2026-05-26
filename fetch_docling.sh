#!/bin/bash
# Submit a single IA item to the docling-API service, poll until done,
# and save the gzipped JSON result(s) under tmp/items/<item>/ following
# segart conventions (matches output of local make_more_doclings.py:
# tmp/items/<item>/<item>_docling.json.gz).
#
# Per-item job-status response is saved alongside as <item>_docling_job.json
# for debugging.
#
# Usage:
#   ./fetch_docling.sh <item>
#
# Honors SEGART_CACHE to override the items cache root (default
# ~/tmp/segart/tmp/items).
#
# Timeout: DOCLING_API_TIMEOUT_SEC (default 1800 = 30 min). If the job
# hasn't reached a terminal status by then, the script exits non-zero
# without downloading; the job continues server-side. POLL_INTERVAL_SEC
# (default 5) controls poll cadence.

set -eo pipefail

item="$1"

if [ -z "$item" ]; then
  echo "Usage: $0 <item>" >&2
  exit 1
fi

API="https://docling-api.svc.prod.ca-west-1a.archive.org"
TMP="${HOME}/tmp/segart/tmp"
ITEMS_DIR="${SEGART_CACHE:-${TMP}/items}"
ITEM_DIR="${ITEMS_DIR}/${item}"
JOB_OUTFILE="${ITEM_DIR}/${item}_docling_job.json"
TIMEOUT_SEC="${DOCLING_API_TIMEOUT_SEC:-1800}"
POLL_INTERVAL_SEC="${POLL_INTERVAL_SEC:-5}"

mkdir -p "$ITEM_DIR"

JOB=$(curl -sS -X POST "${API}/v1/jobs/archive-item" \
  -H 'content-type: application/json' \
  -d "{\"item\": \"${item}\"}" \
  | tee /dev/stderr | jq -r .job_id)

echo
echo "job: $JOB"

START_TS=$(date +%s)
while true; do
  NOW=$(date +%s)
  ELAPSED=$((NOW - START_TS))
  if [ "$ELAPSED" -ge "$TIMEOUT_SEC" ]; then
    echo "timeout after ${ELAPSED}s (limit ${TIMEOUT_SEC}s); job ${JOB} may still be running server-side" >&2
    exit 124
  fi

  RESPONSE=$(curl -sS "${API}/v1/jobs/${JOB}")
  echo "$RESPONSE" | json_pp

  STATUS=$(echo "$RESPONSE" | jq -r .status)

  case "$STATUS" in
    succeeded|failed|partial)
      echo "$RESPONSE" | json_pp > "$JOB_OUTFILE"
      echo "saved job response to $JOB_OUTFILE"

      # Download presigned URLs for any files that succeeded, gzip on
      # the way to disk, name as <base>_docling.json.gz to match the
      # local pipeline's output convention.
      echo "$RESPONSE" \
        | jq -r '.files[]? | select(.status == "succeeded") | "\(.filename)\t\(.result_url)"' \
        | while IFS=$'\t' read -r filename url; do
            [ -z "$url" ] && continue
            # Strip .pdf (case-insensitive) and add _docling.json.gz
            base="${filename%.[Pp][Dd][Ff]}"
            dest="${ITEM_DIR}/${base}_docling.json.gz"
            wget -q -O - "$url" | gzip -c > "$dest"
            echo "wrote $dest"
          done

      exit 0
      ;;
    pending|running)
      sleep "$POLL_INTERVAL_SEC"
      ;;
    *)
      echo "unexpected status: $STATUS" >&2
      exit 1
      ;;
  esac
done
