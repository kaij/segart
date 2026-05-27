"""Build + upload _articles.json.gz (full enrichment) for items in a
clean-tier (publisher, year-bucket) scope.

Reuses build_articles_companion's source-fetch helpers but adds:
  - Explicit retry + exponential backoff on each source fetch
  - Don't upload a partial articles file — if any source fails
    permanently (after retries), skip the item entirely
  - Adaptive: track 429 rate; back off if seen
  - Resume-from-checkpoint via per-item status JSONL

Usage:
  python3 tools/articles_pilot.py <items.txt> [--workers 8] [--limit 100]
"""
from __future__ import annotations
import argparse, gzip, json, random, re, subprocess, sys, time, uuid
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SEGART = Path("/Users/brewster/tmp/segart")
sys.path.insert(0, str(SEGART / "tools"))
from segart_version import software_versions  # noqa
from articles_v2_common import (  # noqa
    SOURCE_VIA, SOURCE_LICENSE,
    add_convenience_fields, route_by_type, derive_crossmark,
    expand_funders, expand_relations, expand_event_data,
)

# ---------------------------------------------------------------- caches/fetch
CACHE_ROOT = SEGART / "tmp"
# v2: separate cache dir so we don't read v1's type-filtered caches.
# v1's `crossref_full_cache/` (type:journal-article only) stays untouched.
FULL_CACHE = CACHE_ROOT / "crossref_full_cache_v2"
FATCAT_CACHE = CACHE_ROOT / "fatcat_doi_cache"
FATCAT_FILES_CACHE = CACHE_ROOT / "fatcat_files_cache"
OPENALEX_CACHE = CACHE_ROOT / "openalex_doi_cache"
UNPAYWALL_CACHE = CACHE_ROOT / "unpaywall_doi_cache"
PUBMED_CACHE = CACHE_ROOT / "pubmed_doi_cache"
CROSSREF_FUNDER_CACHE = CACHE_ROOT / "crossref_funder_cache"  # v2 Pass A
CROSSREF_WORK_CACHE   = CACHE_ROOT / "crossref_work_cache"    # v2 Pass A (relation traversal)
EVENT_DATA_CACHE      = CACHE_ROOT / "event_data_cache"       # v2 Pass A (Event Data)
EMAIL = "brewster@archive.org"
HEADERS = {"User-Agent": f"segart-articles/1.1 (mailto:{EMAIL})"}


def fetch_crossref_full_for_year(issn, year, max_retries=6):
    """v2: no type filter — pull every DOI Crossref has for the (issn, year)
    including journal-issue, journal-volume, editorial, review-article,
    book-review, book-chapter, proceedings-article, errata, etc. Caller
    routes records by `type`.

    Strict mode (this is archival ETL — see memory note
    [never_overwrite_good_data_with_bad]):
      - On 429/503 mid-pagination: retry with backoff honoring Retry-After
      - On network/SSL/timeout: retry with exponential backoff
      - On permanent 4xx/5xx: raise immediately
      - On post-retry transient: raise
      - NEVER caches a partial-or-errored fetch — old good data preserved
    """
    safe = re.sub(r"[^A-Za-z0-9-]", "_", issn)
    p = FULL_CACHE / f"{safe}_{year}.json"
    if p.exists():
        try:
            d = json.loads(p.read_text())
            # v2 cache always has type_filter:null + populated items. If we
            # see an error or empty result with no error context, treat as
            # cache-miss and refetch — never trust a stale-bad entry.
            if not d.get("error") and "items" in d:
                return d["items"], None
        except Exception:
            pass  # corrupted; refetch

    items = []
    cursor = "*"
    pages = 0
    while True:
        qs = urllib.parse.urlencode({
            "rows": 200,
            "filter": f"from-pub-date:{year}-01,until-pub-date:{year}-12",
            "cursor": cursor, "mailto": EMAIL})
        url = f"https://api.crossref.org/journals/{issn}/works?{qs}"

        # Per-page retry loop with strict-mode error handling.
        last_err = None
        page_data = None
        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=60) as fh:
                    page_data = json.load(fh)
                break  # success
            except urllib.error.HTTPError as e:
                if e.code in (429, 503):
                    ra = e.headers.get("Retry-After")
                    delay = _parse_retry_after(ra) if ra else min(5 * (2 ** attempt), 120)
                    last_err = f"HTTP {e.code} (attempt {attempt+1}/{max_retries}, Retry-After={ra!r})"
                    print(f"  backoff {delay}s: {last_err}  {issn} {year} cursor={cursor[:8]}",
                          file=sys.stderr)
                    if attempt < max_retries - 1:
                        time.sleep(delay)
                        continue
                    raise RuntimeError(f"{last_err} — gave up on {url}")
                # Permanent 4xx/5xx — raise immediately, do NOT cache.
                raise RuntimeError(f"HTTP {e.code} (permanent) on {url}")
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_err = f"{type(e).__name__}: {e} (attempt {attempt+1}/{max_retries})"
                delay = min(5 * (2 ** attempt), 120) * (0.5 + random.random())
                print(f"  backoff {delay:.1f}s: {last_err}  {issn} {year}", file=sys.stderr)
                if attempt < max_retries - 1:
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"{last_err} — gave up on {url}")

        msg = page_data.get("message", {})
        page_items = msg.get("items", [])
        items.extend(page_items); pages += 1
        nc = msg.get("next-cursor")
        if not page_items or not nc or nc == cursor: break
        cursor = nc
        if pages >= 50: break

    # Only reach here on success. Cache + return.
    FULL_CACHE.mkdir(parents=True, exist_ok=True)
    p_tmp = p.with_suffix(f".json.tmp.{uuid.uuid4().hex}")
    p_tmp.write_text(json.dumps(
        {"items": items, "paginated": True, "pages": pages,
         "type_filter": None}))
    p_tmp.replace(p)
    return items, None


def _parse_retry_after(value):
    """Retry-After header value → seconds to wait (clamped to [1, 300]).
    Supports integer-seconds form and HTTP-date form. Returns 5 on parse fail."""
    value = (value or "").strip()
    if not value:
        return 5
    try:
        return max(1, min(int(value), 300))
    except ValueError:
        pass
    try:
        import email.utils
        from datetime import datetime, timezone
        ts = email.utils.parsedate_to_datetime(value)
        delta = (ts - datetime.now(timezone.utc)).total_seconds()
        return max(1, min(int(delta), 300))
    except Exception:
        return 5


def _doi_cache_get(cache_dir, doi, url, max_retries=6):
    """Cached single GET. Returns parsed JSON or None for legitimate 404.

    Strict-mode behavior (this is archival ETL, not best-effort):
      - 200: cache and return the payload
      - 404: cache as definitive null, return None
      - 429 / 503: retry with backoff honoring Retry-After header
      - network / SSL / timeout: retry with exponential backoff
      - other 4xx / 5xx: raise immediately (permanent)
      - after max_retries exhausted on transient: raise
    Never caches an error — a future re-run sees a fresh fetch."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", doi)
    p = cache_dir / f"{safe}.json"
    if p.exists():
        try:
            d = json.loads(p.read_text())
            # Cache stores definitive results only; treat presence as truth.
            return d.get("data")
        except Exception:
            pass  # corrupted; refetch

    last_err = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as fh:
                data = json.load(fh)
            cache_dir.mkdir(parents=True, exist_ok=True)
            p_tmp = p.with_suffix(f".json.tmp.{uuid.uuid4().hex}")
            p_tmp.write_text(json.dumps({"data": data, "url": url}))
            p_tmp.replace(p)
            return data

        except urllib.error.HTTPError as e:
            if e.code == 404:
                # Legitimate "no record" — cache as definitive null.
                cache_dir.mkdir(parents=True, exist_ok=True)
                p_tmp = p.with_suffix(f".json.tmp.{uuid.uuid4().hex}")
                p_tmp.write_text(json.dumps({"data": None, "url": url, "status": 404}))
                p_tmp.replace(p)
                return None
            if e.code in (429, 503):
                ra = e.headers.get("Retry-After")
                delay = _parse_retry_after(ra) if ra else min(5 * (2 ** attempt), 120)
                last_err = f"HTTP {e.code} (attempt {attempt+1}/{max_retries}, Retry-After={ra!r})"
                print(f"  backoff {delay}s: {last_err}  {url[:100]}", file=sys.stderr)
                if attempt < max_retries - 1:
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"{last_err} — gave up on {url}")
            # Other 4xx / 5xx: permanent — fail immediately
            raise RuntimeError(f"HTTP {e.code} (permanent) on {url}")

        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = f"{type(e).__name__}: {e} (attempt {attempt+1}/{max_retries})"
            delay = min(5 * (2 ** attempt), 120) * (0.5 + random.random())
            print(f"  backoff {delay:.1f}s: {last_err}  {url[:100]}", file=sys.stderr)
            if attempt < max_retries - 1:
                time.sleep(delay)
                continue
            raise RuntimeError(f"{last_err} — gave up on {url}")


def fetch_fatcat_by_doi(doi):
    url = ("https://scholar.archive.org/api/fatcat/v2/release/lookup"
           f"?id_type=doi&id_value={urllib.parse.quote(doi)}")
    rec = _doi_cache_get(FATCAT_CACHE, doi, url)
    if not rec or not rec.get("id"): return rec
    rid = rec["id"]
    files_url = f"https://scholar.archive.org/api/fatcat/v2/release/{rid}/files"
    files_resp = _doi_cache_get(FATCAT_FILES_CACHE, rid, files_url)
    rec["_files"] = (files_resp or {}).get("items") or []
    return rec


def fetch_openalex_by_doi(doi):
    url = (f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi,safe='')}"
           f"?mailto={EMAIL}")
    return _doi_cache_get(OPENALEX_CACHE, doi, url)


def fetch_unpaywall_by_doi(doi):
    url = (f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi,safe='')}"
           f"?email={EMAIL}")
    return _doi_cache_get(UNPAYWALL_CACHE, doi, url)


def fetch_crossref_funder(funder_doi):
    """v2 Pass A: fetch full /funders/{funder-doi} record. Returns the
    `message` payload (the funder's own metadata: name, alt-names, location,
    hierarchy, descendants, work counts)."""
    quoted = urllib.parse.quote(funder_doi)
    url = f"https://api.crossref.org/funders/{quoted}?mailto={EMAIL}"
    data = _doi_cache_get(CROSSREF_FUNDER_CACHE, funder_doi, url)
    if not data: return None
    return data.get("message")


def fetch_crossref_work(doi):
    """v2 Pass A: fetch full /works/{doi} record for relation traversal.
    Distinct from the year-level cache because related DOIs commonly point
    at preprints / translations / versions in OTHER journals (or in
    repositories like bioRxiv) that the year-level fetch wouldn't have."""
    quoted = urllib.parse.quote(doi)
    url = f"https://api.crossref.org/works/{quoted}?mailto={EMAIL}"
    data = _doi_cache_get(CROSSREF_WORK_CACHE, doi, url)
    if not data: return None
    return data.get("message")


def fetch_event_data(doi):
    """v2 Pass A: fetch ALL Crossref Event Data events for a DOI, paginated.
    Returns {total_events, sources, events[]} dict or None.

    SSL note: api.eventdata.crossref.org has had an expired cert (as of
    2026-05). We pass an unverified SSL context for this endpoint
    specifically — it's a public open-data API with no auth secrets, so
    skipping verification just means we accept whatever the host serves.
    Worst case: a man-in-the-middle could feed us fake events; for
    archival enrichment that's an acceptable risk."""
    import ssl
    from collections import Counter
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", doi)
    p = EVENT_DATA_CACHE / f"{safe}.json"
    if p.exists():
        try:
            d = json.loads(p.read_text())
            if not d.get("error"):
                return d.get("data")
        except Exception: pass

    # Unverified SSL context — see docstring.
    insecure_ctx = ssl.create_default_context()
    insecure_ctx.check_hostname = False
    insecure_ctx.verify_mode = ssl.CERT_NONE

    all_events = []
    cursor = None
    error = None
    pages = 0
    while True:
        params = {"obj-id": f"https://doi.org/{doi}",
                  "rows": 1000, "mailto": EMAIL}
        if cursor:
            params["cursor"] = cursor
        url = ("https://api.eventdata.crossref.org/v1/events?"
               + urllib.parse.urlencode(params))
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=30, context=insecure_ctx) as fh:
                d = json.load(fh)
        except Exception as e:
            error = str(e); break
        msg = d.get("message", {})
        events = msg.get("events") or []
        all_events.extend(events)
        nc = msg.get("next-cursor")
        pages += 1
        if not events or not nc or nc == cursor or pages >= 50:
            break
        cursor = nc

    if error:
        # Don't cache transient errors (especially the SSL-cert-expired one);
        # a future re-run can succeed once Crossref renews the cert.
        return None

    sources = Counter(e.get("source-id") or e.get("source") or "unknown"
                      for e in all_events)
    data = {"total_events": len(all_events),
            "sources":      dict(sources),
            "events":       all_events}

    EVENT_DATA_CACHE.mkdir(parents=True, exist_ok=True)
    p_tmp = p.with_suffix(f".json.tmp.{uuid.uuid4().hex}")
    p_tmp.write_text(json.dumps({"data": data, "error": None, "doi": doi}))
    p_tmp.replace(p)
    return data


def fetch_pubmed_by_doi(doi):
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search"
           f"?query=DOI:{urllib.parse.quote(doi)}&resultType=core&format=json")
    data = _doi_cache_get(PUBMED_CACHE, doi, url)
    if not data: return None
    results = (data.get("resultList") or {}).get("result") or []
    return results[0] if results else None
# ---------------------------------------------------------------- end inline

OUT_TMP = SEGART / "tmp" / "articles_pilot"
OUT_TMP.mkdir(parents=True, exist_ok=True)

# SOURCE_VIA / SOURCE_LICENSE imported from articles_v2_common


def fetch_with_retry(fn, *args, retries=6, base_delay=2.0):
    """Retry with exponential backoff + jitter. Longer waits for 429-type
    signals. Returns (data, error)."""
    last_err = None
    for attempt in range(retries):
        try:
            result = fn(*args)
            return result, None
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if "429" in msg or "rate" in msg or "throttle" in msg or "503" in msg:
                wait = base_delay * (4 ** attempt)
            else:
                wait = base_delay * (2 ** attempt)
            # Jitter so concurrent workers don't all retry in lockstep
            wait *= (0.5 + random.random())
            if attempt < retries - 1:
                time.sleep(min(wait, 120))  # cap individual sleep at 2 min
    return None, str(last_err)


def fetch_crossref_year_with_retry(issn, yr):
    """fetch_crossref_full_for_year doesn't raise on 429 — it returns
    (items, error). Wrap it so a 429 triggers retry."""
    for attempt in range(6):
        items, error = fetch_crossref_full_for_year(issn, yr)
        if not error or ("429" not in error and "503" not in error
                          and "Rate" not in error and "Timeout" not in error.lower()):
            return items, error
        wait = 2.0 * (4 ** attempt) * (0.5 + random.random())
        time.sleep(min(wait, 120))
    return items, error


def ia_metadata(item):
    r = subprocess.run(["ia","metadata",item], capture_output=True, text=True, timeout=30)
    if r.returncode != 0: return None
    try: return json.loads(r.stdout)
    except Exception: return None


def derive_metadata(md):
    """Pull (issn, vol, iss, yr) from IA metadata."""
    m = md.get("metadata", {}) if md else {}
    issn = (m.get("issn") or "").strip()
    if isinstance(issn, list): issn = issn[0] if issn else ""
    vol = (m.get("volume") or "").strip()
    iss = (m.get("issue") or "").strip()
    date = (m.get("date") or m.get("year") or "").strip()
    import re
    yr_m = re.search(r"\b(19|20)\d{2}\b", date)
    yr = yr_m.group(0) if yr_m else (date[:4] if date[:4].isdigit() else "")
    return issn, vol, iss, yr


# Read at the start of each process_item call; set once by main() from --force.
FORCE_REBUILD = False


def article_already_uploaded(md, item):
    files = {f.get("name") for f in (md.get("files") or [])}
    return f"{item}_articles.json.gz" in files


def process_item(item):
    """Returns dict with status + counts."""
    out = {"item": item, "ok": False}
    md = ia_metadata(item)
    if not md:
        out["status"] = "ia_metadata_fail"; return out
    if not FORCE_REBUILD and article_already_uploaded(md, item):
        out["status"] = "skip_already_uploaded"; out["ok"] = True; return out
    issn, vol, iss, yr = derive_metadata(md)
    if not (issn and yr):
        out["status"] = f"skip_no_meta(issn={issn!r},yr={yr!r})"; return out
    out["issn"] = issn; out["vol"] = vol; out["iss"] = iss; out["yr"] = yr

    # Crossref for the year (cached file), with retry on 429/503
    works, err = fetch_crossref_year_with_retry(issn, yr)
    if err:
        out["status"] = f"crossref_year_fail: {err[:100]}"; return out
    issue_works = [w for w in works
                    if str(w.get("volume","")) == str(vol)
                    and (not iss or str(w.get("issue","")) == str(iss))]
    if not issue_works:
        out["status"] = "skip_no_crossref_articles"; return out

    # v2: separate records by Crossref `type`. journal-issue / journal-volume
    # records hold issue/volume-level metadata (special-issue title, editors,
    # subjects); they are NOT articles, so they go to top-level blocks rather
    # than `entries`.
    issue_meta, volume_meta, article_works = route_by_type(issue_works)

    if not article_works and issue_meta is None and volume_meta is None:
        out["status"] = "skip_no_crossref_articles"; return out
    out["n_articles"] = len(article_works)

    # Fetch other sources for each DOI; ANY permanent error → skip item
    entries = {}
    src_hits = {"crossref":0,"fatcat":0,"openalex":0,"unpaywall":0,"pubmed":0}
    n_retracted = 0
    for i, w in enumerate(article_works):
        doi = w.get("DOI")
        if not doi: continue
        eid = f"e{i+1}"
        # v2: full Crossref blob — no strip_periodical. The 8 periodical
        # fields (container-title, ISSN, publisher, member, prefix, ...)
        # are kept so a pub_*-level aggregator can recover them later.
        rec = {"toc_entry_id": eid,
               "ext_ids": {"doi": doi},
               "match_method": "doi_from_crossref",
               "match_confidence": 1.0,
               "crossref": w}
        src_hits["crossref"] += 1
        # Strict-mode source fetch: _doi_cache_get internally retries 429/503
        # and network errors with backoff honoring Retry-After. Any permanent
        # error (4xx/5xx other than 404) or post-retry transient raises
        # RuntimeError — we catch per-source, fail the item, and let an
        # operator re-run after the upstream is healthy. No silent best-effort.
        for sname, fn in (("fatcat", fetch_fatcat_by_doi),
                          ("openalex", fetch_openalex_by_doi),
                          ("unpaywall", fetch_unpaywall_by_doi),
                          ("pubmed", fetch_pubmed_by_doi)):
            try:
                data = fn(doi)
            except RuntimeError as fetch_e:
                out["status"] = f"{sname}_fail_for_{doi}: {str(fetch_e)[:120]}"
                print(f"  FAIL item={item} {sname}: {fetch_e}", file=sys.stderr)
                return out
            if data:
                rec[sname] = data
                src_hits[sname] += 1

        # v2 convenience fields: derived/picked from the source blobs and
        # surfaced at entry top level for direct consumer access. Originals
        # remain in their source blobs unchanged.
        add_convenience_fields(rec)
        # Pass A enrichment: Crossmark (derivation, no extra fetch)
        cm = derive_crossmark(rec)
        if cm is not None:
            rec["crossmark"] = cm
        # Pass A enrichment: Funder Registry expansion (per funder DOI).
        # Strict: a permanent funder-fetch failure fails the item, same as
        # main-source failure. No silent skip.
        try:
            expand_funders(rec, fetch_crossref_funder)
        except RuntimeError as fund_e:
            out["status"] = f"funder_fail_for_{doi}: {str(fund_e)[:120]}"
            print(f"  FAIL item={item} funder: {fund_e}", file=sys.stderr)
            return out
        # Pass A enrichment: relation[] one-hop traversal (per related DOI)
        try:
            expand_relations(rec, fetch_crossref_work)
        except RuntimeError as rel_e:
            out["status"] = f"relation_fail_for_{doi}: {str(rel_e)[:120]}"
            print(f"  FAIL item={item} relation: {rel_e}", file=sys.stderr)
            return out
        # Pass A enrichment: Event Data — DISABLED.
        # api.eventdata.crossref.org was sunset on 2026-04-23 (per Crossref's
        # deprecation page). Every request returns 403. A one-time historical
        # archive is available on request; once mirrored on IA we'll re-enable
        # this path against the local mirror. See issue #8.
        # expand_event_data(rec, fetch_event_data)
        if rec.get("retracted"):
            n_retracted += 1
        entries[eid] = rec

    if not entries and issue_meta is None and volume_meta is None:
        out["status"] = "skip_no_entries_built"; return out

    today = time.strftime("%Y-%m-%d")
    sources_used = {s: {"via": SOURCE_VIA[s], "fetched_at": today}
                     for s, n in src_hits.items() if n > 0}
    # Crossref always present (we filtered on its output). Record the
    # v2 broadened scope explicitly.
    if "crossref" in sources_used:
        sources_used["crossref"]["type_filter"] = None
    licenses = {s: SOURCE_LICENSE[s] for s in sources_used}
    companion = {
        "schema_version": 2,
        "ia_item": item,
        "toc_schema_version": None,
        "issue_meta": issue_meta,
        "volume_meta": volume_meta,
        "has_retracted_entries": n_retracted > 0,
        "provenance": {
            "software_versions": software_versions(),
            "sources": sources_used,
        },
        "license_notes": licenses,
        "entries": entries,
    }

    out_path = OUT_TMP / f"{item}_articles.json.gz"
    with gzip.open(out_path, "wt", encoding="utf-8") as fh:
        json.dump(companion, fh, indent=2)
    out["src_hits"] = src_hits
    out["bytes"] = out_path.stat().st_size

    # Upload
    r = subprocess.run(
        ["ia","upload",item,str(out_path),"--no-derive","--retries=2"],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        out["status"] = f"upload_fail: {r.stderr.strip()[:200]}"
        return out
    out["ok"] = True
    out["status"] = "ok"
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("items_file", help="path to file with IA identifiers, one per line")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None,
                    help="cap on number of items processed (for pilot)")
    ap.add_argument("--checkpoint", default=None,
                    help="JSONL with per-item statuses; resumable")
    ap.add_argument("--force", action="store_true",
                    help="Rebuild + re-upload even if an _articles.json.gz "
                         "already exists on IA (e.g. for the v1→v2 cutover).")
    args = ap.parse_args()

    # Module-level so process_item (run inside futures) can read it.
    global FORCE_REBUILD
    FORCE_REBUILD = args.force
    if args.force:
        print("FORCE_REBUILD: ignoring existing _articles.json.gz on IA",
              flush=True)

    items = [l.strip() for l in open(args.items_file) if l.strip()]
    if args.limit: items = items[:args.limit]

    checkpoint = args.checkpoint or str(
        SEGART / "tmp/audit" / f"articles_pilot_{Path(args.items_file).stem}.jsonl")
    done = set()
    if Path(checkpoint).exists():
        for line in open(checkpoint):
            try: d = json.loads(line); done.add(d["item"])
            except Exception: pass
    todo = [i for i in items if i not in done]
    print(f"items: {len(items)}, already done: {len(done)}, todo: {len(todo)}",
          flush=True)

    from collections import Counter
    counts = Counter()
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex, \
         open(checkpoint, "a") as fh_ck:
        futs = {ex.submit(process_item, it): it for it in todo}
        n = 0
        for fut in as_completed(futs):
            try: r = fut.result()
            except Exception as e: r = {"item": futs[fut], "status": f"exc:{e}", "ok": False}
            fh_ck.write(json.dumps(r) + "\n"); fh_ck.flush()
            n += 1
            counts["ok" if r.get("ok") else "fail"] += 1
            counts[r.get("status","?").split(":")[0]] += 1
            if n % 10 == 0 or n == len(todo):
                el = time.time() - t0
                print(f"  {n}/{len(todo)}  rate={n/el:.2f}/s  counts={dict(counts.most_common(8))}", flush=True)

    print(f"\ndone in {time.time()-t0:.0f}s")
    print(f"checkpoint: {checkpoint}")


if __name__ == "__main__":
    main()
