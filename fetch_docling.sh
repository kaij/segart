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
#
# Filename restriction: by default we pass filename="<item>.pdf" so the
# API processes exactly the canonical Text PDF, NOT any sibling
# *.lcpdf / *_encrypted.pdf / *.acspdf variants that would otherwise
# match the "every PDF in the item" fanout (per OpenAPI spec). About
# 4.3% of cached items have such DRM-wrapped sibling PDFs; processing
# those is wasted compute and would overwrite the good output. Override
# via DOCLING_FILENAME=<name> (or DOCLING_FILENAME="" to opt back in to
# the every-PDF fanout, e.g. if you want to process all files in a
# multi-PDF item).
#
# Credentials: for items the API's default service credential can't read
# (private/dark items), we forward IA S3 credentials in the POST body.
# Source (highest precedence first):
#   1. IA_S3_ACCESS_KEY + IA_S3_SECRET_KEY env vars
#   2. ~/.config/internetarchive/ia.ini  ([s3] access=... secret=...)
# Set IA_S3_NO_CREDS=1 to suppress sending creds even if they're
# available (relies on server default). Credentials are passed to curl
# via stdin (--data @-) so they don't appear in `ps` listings.
#
# Saturation metric: prints "submit→running: <s>" the first time status
# transitions out of "pending". Long values indicate the API's worker
# pool is saturated and your job sat in queue.

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
# Empty string = let the API process every PDF in the item (fanout).
# Default = restrict to the canonical Text PDF only.
FILENAME="${DOCLING_FILENAME-${item}.pdf}"
IA_INI="${HOME}/.config/internetarchive/ia.ini"

mkdir -p "$ITEM_DIR"

# --- credential loading ---
if [ -z "${IA_S3_NO_CREDS:-}" ]; then
  if [ -z "${IA_S3_ACCESS_KEY:-}" ] && [ -f "$IA_INI" ]; then
    IA_S3_ACCESS_KEY=$(awk -F'=' '/^\[s3\]/{f=1;next} /^\[/{f=0} f && /^access[[:space:]]*=/{gsub(/^[[:space:]]+|[[:space:]]+$/,"",$2); print $2; exit}' "$IA_INI")
  fi
  if [ -z "${IA_S3_SECRET_KEY:-}" ] && [ -f "$IA_INI" ]; then
    IA_S3_SECRET_KEY=$(awk -F'=' '/^\[s3\]/{f=1;next} /^\[/{f=0} f && /^secret[[:space:]]*=/{gsub(/^[[:space:]]+|[[:space:]]+$/,"",$2); print $2; exit}' "$IA_INI")
  fi
fi

# --- build POST body ---
build_body() {
  local args=(-n --arg item "$item")
  local filter='{item: $item}'
  if [ -n "$FILENAME" ]; then
    args+=(--arg fn "$FILENAME"); filter="${filter} | . + {filename: \$fn}"
  fi
  if [ -n "${IA_S3_ACCESS_KEY:-}" ] && [ -n "${IA_S3_SECRET_KEY:-}" ]; then
    args+=(--arg ak "$IA_S3_ACCESS_KEY" --arg sk "$IA_S3_SECRET_KEY")
    filter="${filter} | . + {access_key: \$ak, secret_key: \$sk}"
  fi
  jq "${args[@]}" "$filter"
}

POST_BODY=$(build_body)

# Log a redacted summary of what we're sending (don't print the body itself —
# it may contain a secret key). Hash the body so reruns are diffable.
REDACTED=$(echo "$POST_BODY" | jq 'if has("secret_key") then .secret_key = "<redacted>" else . end | if has("access_key") then .access_key = "<redacted>" else . end')
echo "request body (redacted):"
echo "$REDACTED"

# Pass via stdin so creds don't appear in ps
JOB=$(curl -sS -X POST "${API}/v1/jobs/archive-item" \
  -H 'content-type: application/json' \
  --data @- <<<"$POST_BODY" \
  | tee /dev/stderr | jq -r .job_id)

echo
echo "job: $JOB"

START_TS=$(date +%s)
RUNNING_TS=""
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

  # Saturation metric: print the first time we leave "pending"
  if [ -z "$RUNNING_TS" ] && [ "$STATUS" != "pending" ]; then
    RUNNING_TS=$(date +%s)
    DELAY=$((RUNNING_TS - START_TS))
    echo "submit→running: ${DELAY}s" >&2
  fi

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
