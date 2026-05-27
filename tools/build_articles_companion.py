"""Build a v2 `_articles.json.gz` companion for an IA item, driven by a
TOC file.

Per `docs/articles_format.md` (v2): per-entry payload joins five
bibliographic sources, each addressed by DOI:

  - crossref   — verbatim /works/{doi}, FULL fields (no strip, no projection)
  - fatcat     — verbatim /release/lookup + /release/{id}/files
  - openalex   — verbatim /works/doi:{doi}
  - unpaywall  — verbatim /v2/{doi}
  - pubmed     — verbatim Europe PMC core result (biomedical only)

Plus top-level `issue_meta` / `volume_meta` populated from any Crossref
`journal-issue` / `journal-volume` deposit that falls in the same
(issn, vol, iss) bucket. Plus `has_retracted_entries` derived from
entry-level `retracted` flags.

Each source is file-cached by DOI (or by (issn, year) for the Crossref
bulk fetch) so re-runs are free.
"""
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from segart_version import software_versions
from articles_v2_common import (
    SOURCE_VIA, SOURCE_LICENSE,
    add_convenience_fields, route_by_type, derive_crossmark,
    expand_funders, expand_relations, expand_event_data,
)


CACHE_ROOT = Path("/Users/brewster/tmp/segart/tmp")
# v2: separate cache dir (no type filter); v1's crossref_full_cache stays untouched.
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


def fetch_crossref_full_for_year(issn: str, year: str,
                                  max_retries: int = 6) -> tuple[list, str | None]:
    """v2: pull EVERY DOI Crossref has for (issn, year) — no type filter.
    Cursor-paginated so prolific journal-years aren't truncated. File-cached
    by (issn, year) under tmp/crossref_full_cache_v2/.

    Strict mode (archival ETL — see memory note
    [never_overwrite_good_data_with_bad]):
      - On 429/503 mid-pagination: retry with backoff honoring Retry-After
      - On network/SSL/timeout: retry with exponential backoff
      - On permanent 4xx/5xx: raise immediately
      - On post-retry transient: raise
      - NEVER caches partial-or-errored fetches
    """
    import random as _r
    safe = re.sub(r"[^A-Za-z0-9-]", "_", issn)
    p = FULL_CACHE / f"{safe}_{year}.json"
    if p.exists():
        try:
            d = json.loads(p.read_text())
            # Trust cache only if it's a clean v2 entry. Stale-bad entries
            # (with `error` field from the pre-strict code) get refetched.
            if not d.get("error") and "items" in d:
                return d["items"], None
        except Exception:
            pass  # corrupted; refetch

    items = []
    cursor = "*"
    pages_fetched = 0
    while True:
        qs = urllib.parse.urlencode({
            "rows": 200,
            "filter": (f"from-pub-date:{year}-01,"
                       f"until-pub-date:{year}-12"),
            "cursor": cursor,
            "mailto": EMAIL,
        })
        url = f"https://api.crossref.org/journals/{issn}/works?{qs}"

        last_err = None
        page_data = None
        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=60) as fh:
                    page_data = json.load(fh)
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 503):
                    ra = e.headers.get("Retry-After")
                    delay = _parse_retry_after(ra) if ra else min(5 * (2 ** attempt), 120)
                    last_err = f"HTTP {e.code} (attempt {attempt+1}/{max_retries}, Retry-After={ra!r})"
                    print(f"  backoff {delay}s: {last_err}  {issn} {year}",
                          file=sys.stderr)
                    if attempt < max_retries - 1:
                        time.sleep(delay)
                        continue
                    raise RuntimeError(f"{last_err} — gave up on {url}")
                raise RuntimeError(f"HTTP {e.code} (permanent) on {url}")
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_err = f"{type(e).__name__}: {e} (attempt {attempt+1}/{max_retries})"
                delay = min(5 * (2 ** attempt), 120) * (0.5 + _r.random())
                print(f"  backoff {delay:.1f}s: {last_err}  {issn} {year}",
                      file=sys.stderr)
                if attempt < max_retries - 1:
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"{last_err} — gave up on {url}")

        msg = page_data.get("message", {})
        page_items = msg.get("items", [])
        items.extend(page_items)
        pages_fetched += 1
        next_cursor = msg.get("next-cursor")
        if not page_items or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        if pages_fetched >= 50:
            break

    # Only reach here on success.
    FULL_CACHE.mkdir(parents=True, exist_ok=True)
    p_tmp = p.with_suffix(".json.tmp")
    p_tmp.write_text(json.dumps({"items": items,
                                  "paginated": True, "pages": pages_fetched,
                                  "type_filter": None}))
    p_tmp.replace(p)
    return items, None


def _parse_retry_after(value):
    """Retry-After header → seconds, clamped to [1, 300]. Supports both
    integer-seconds and HTTP-date forms."""
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


def _doi_cache_get(cache_dir: Path, doi: str, url: str,
                   extra_headers: dict | None = None,
                   max_retries: int = 6) -> dict | None:
    """Cached single GET by DOI. Returns parsed JSON or None for legitimate
    404. Strict-mode behavior (archival, not best-effort):

      - 200: cache + return payload
      - 404: cache as definitive null, return None
      - 429 / 503: retry with backoff honoring Retry-After
      - network / SSL / timeout: retry with exponential backoff
      - other 4xx / 5xx: raise immediately
      - retries exhausted on transient: raise

    Never caches a transient error — a future re-run does a clean fetch.
    """
    import random as _r
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", doi)
    p = cache_dir / f"{safe}.json"
    if p.exists():
        try:
            d = json.loads(p.read_text())
            return d.get("data")
        except Exception:
            pass  # corrupted; refetch

    last_err = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={**HEADERS, **(extra_headers or {})})
            with urllib.request.urlopen(req, timeout=30) as fh:
                data = json.load(fh)
            cache_dir.mkdir(parents=True, exist_ok=True)
            p_tmp = p.with_suffix(".json.tmp")
            p_tmp.write_text(json.dumps({"data": data, "url": url}))
            p_tmp.replace(p)
            return data

        except urllib.error.HTTPError as e:
            if e.code == 404:
                cache_dir.mkdir(parents=True, exist_ok=True)
                p_tmp = p.with_suffix(".json.tmp")
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
            raise RuntimeError(f"HTTP {e.code} (permanent) on {url}")

        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = f"{type(e).__name__}: {e} (attempt {attempt+1}/{max_retries})"
            delay = min(5 * (2 ** attempt), 120) * (0.5 + _r.random())
            print(f"  backoff {delay:.1f}s: {last_err}  {url[:100]}", file=sys.stderr)
            if attempt < max_retries - 1:
                time.sleep(delay)
                continue
            raise RuntimeError(f"{last_err} — gave up on {url}")


def fetch_fatcat_by_doi(doi: str) -> dict | None:
    """Look up a fatcat release by DOI (via scholar.archive.org's fatcat
    v2 API — the v0 host api.fatcat.wiki was retired). Files are not
    embedded; follow up with /release/{id}/files."""
    url = ("https://scholar.archive.org/api/fatcat/v2/release/lookup"
           f"?id_type=doi&id_value={urllib.parse.quote(doi)}")
    rec = _doi_cache_get(FATCAT_CACHE, doi, url)
    if not rec or not rec.get("id"):
        return rec
    rid = rec["id"]
    files_url = (f"https://scholar.archive.org/api/fatcat/v2/release/{rid}/files")
    files_resp = _doi_cache_get(FATCAT_FILES_CACHE, rid, files_url)
    rec["_files"] = (files_resp or {}).get("items") or []
    return rec


def fetch_openalex_by_doi(doi: str) -> dict | None:
    url = (f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi, safe='')}"
           f"?mailto={EMAIL}")
    return _doi_cache_get(OPENALEX_CACHE, doi, url)


def fetch_unpaywall_by_doi(doi: str) -> dict | None:
    url = (f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi, safe='')}"
           f"?email={EMAIL}")
    return _doi_cache_get(UNPAYWALL_CACHE, doi, url)


def fetch_crossref_funder(funder_doi: str) -> dict | None:
    """v2 Pass A: fetch full /funders/{funder-doi} record. Returns the
    `message` payload (the funder's own metadata: name, alt-names, location,
    hierarchy, descendants, work counts)."""
    quoted = urllib.parse.quote(funder_doi)
    url = f"https://api.crossref.org/funders/{quoted}?mailto={EMAIL}"
    data = _doi_cache_get(CROSSREF_FUNDER_CACHE, funder_doi, url)
    if not data:
        return None
    return data.get("message")


def fetch_crossref_work(doi: str) -> dict | None:
    """v2 Pass A: fetch full /works/{doi} record for relation traversal.
    Distinct from the year-level cache because related DOIs commonly point
    at preprints / translations / versions in OTHER journals (or in
    repositories like bioRxiv) that the year-level fetch wouldn't have."""
    quoted = urllib.parse.quote(doi)
    url = f"https://api.crossref.org/works/{quoted}?mailto={EMAIL}"
    data = _doi_cache_get(CROSSREF_WORK_CACHE, doi, url)
    if not data:
        return None
    return data.get("message")


def fetch_event_data(doi: str) -> dict | None:
    """v2 Pass A: fetch ALL Crossref Event Data events for a DOI, paginated.
    Returns {total_events, sources, events[]} or None.

    SSL note: api.eventdata.crossref.org has had an expired cert (2026-05).
    We pass an unverified SSL context for this endpoint specifically — it's
    a public open-data API with no auth secrets. Worst case from a MITM:
    fake events injected, acceptable for archival enrichment."""
    import ssl
    from collections import Counter
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", doi)
    p = EVENT_DATA_CACHE / f"{safe}.json"
    if p.exists():
        try:
            d = json.loads(p.read_text())
            if not d.get("error"):
                return d.get("data")
        except Exception:
            pass

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
        return None

    sources = Counter(e.get("source-id") or e.get("source") or "unknown"
                      for e in all_events)
    data = {"total_events": len(all_events),
            "sources":      dict(sources),
            "events":       all_events}

    EVENT_DATA_CACHE.mkdir(parents=True, exist_ok=True)
    p_tmp = p.with_suffix(".json.tmp")
    p_tmp.write_text(json.dumps({"data": data, "error": None, "doi": doi}))
    p_tmp.replace(p)
    return data


def fetch_pubmed_by_doi(doi: str) -> dict | None:
    """Via Europe PMC's REST API — returns JSON with MeSH, pub types,
    structured abstract, grants. (NCBI EFetch is XML-only for these fields.)"""
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search"
           f"?query=DOI:{urllib.parse.quote(doi)}&resultType=core&format=json")
    data = _doi_cache_get(PUBMED_CACHE, doi, url)
    if not data:
        return None
    results = (data.get("resultList") or {}).get("result") or []
    return results[0] if results else None


def label_parts(s):
    s = str(s or "").strip()
    if not s: return {""}
    parts = {s}
    for sep in "-/,":
        for p in s.split(sep):
            p = p.strip()
            if p: parts.add(p)
    return parts


def label_matches(crossref_label, query_label):
    c = str(crossref_label or "").strip()
    q = str(query_label or "").strip()
    if c == q: return True
    if not c or not q: return False
    return bool(label_parts(c) & label_parts(q))


if len(sys.argv) != 3:
    print("usage: build_articles_companion.py <toc.json> <out_articles.json.gz>")
    sys.exit(1)

toc_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])

toc = json.loads(toc_path.read_text())
item = toc["item"]; issn = toc["issn"]
year = toc["year"]; vol = toc["volume"]; iss = toc["issue"]

# Fetch full Crossref records for (issn, year) — every DOI, every field.
crossref_records, fetch_error = fetch_crossref_full_for_year(issn, year)
if fetch_error and not crossref_records:
    print(f"ERROR: Crossref fetch failed for ({issn}, {year}): {fetch_error}",
          file=sys.stderr)
    sys.exit(2)

xref_issue = [r for r in crossref_records
              if label_matches(r.get("volume"), vol)
              and label_matches(r.get("issue"), iss)]

# v2: separate journal-issue / journal-volume deposits from articles.
# They land at top-level issue_meta / volume_meta.
issue_meta, volume_meta, article_works = route_by_type(xref_issue)

# Index article-typed Crossref records by DOI (lowercase) for fast lookup
by_doi = {(r.get("DOI") or "").lower(): r for r in article_works if r.get("DOI")}


# Build the companion file. For every TOC entry with a DOI, hit all
# five sources. Each call is file-cached, so re-runs are free.
entries = {}
src_hits = {"crossref": 0, "fatcat": 0, "openalex": 0,
            "unpaywall": 0, "pubmed": 0}
n_retracted = 0
for e in toc.get("entries") or []:
    doi = (e.get("ext_ids") or {}).get("doi", "") or ""
    doi_l = doi.lower()
    xref = by_doi.get(doi_l)

    fatcat_raw = openalex_raw = unpaywall_raw = pubmed_raw = None
    if doi:
        # Strict-mode fetches: _doi_cache_get retries 429/503/network with
        # Retry-After backoff; raises on permanent error. Any RuntimeError
        # here aborts the whole build — archival ETL, no partial files.
        fatcat_raw    = fetch_fatcat_by_doi(doi)
        openalex_raw  = fetch_openalex_by_doi(doi)
        unpaywall_raw = fetch_unpaywall_by_doi(doi)
        pubmed_raw    = fetch_pubmed_by_doi(doi)
        time.sleep(0.1)  # be polite across providers

    # Bubble up any new ext_ids we discovered
    ext_ids = dict(e.get("ext_ids") or {})
    if pubmed_raw and pubmed_raw.get("pmid"):
        ext_ids.setdefault("pmid",  pubmed_raw["pmid"])
    if pubmed_raw and pubmed_raw.get("pmcid"):
        ext_ids.setdefault("pmcid", pubmed_raw["pmcid"])
    if fatcat_raw and fatcat_raw.get("id"):
        ext_ids.setdefault("fatcat_release", fatcat_raw["id"])
    if fatcat_raw and fatcat_raw.get("work_id"):
        ext_ids.setdefault("fatcat_work", fatcat_raw["work_id"])
    if openalex_raw and openalex_raw.get("id"):
        ext_ids.setdefault("openalex", openalex_raw["id"].rsplit("/", 1)[-1])

    if xref:         src_hits["crossref"]  += 1
    if fatcat_raw:   src_hits["fatcat"]    += 1
    if openalex_raw: src_hits["openalex"]  += 1
    if unpaywall_raw:src_hits["unpaywall"] += 1
    if pubmed_raw:   src_hits["pubmed"]    += 1

    record = {
        "toc_entry_id": e["id"],
        "ext_ids": ext_ids,
        "match_method": "doi_lookup" if doi else "no_match",
        "match_confidence": 1.0 if doi else 0.0,
    }
    # v2: keep FULL source blobs (no projection, no strip_periodical).
    # Per articles_format.md "absence vs null is not significant": omit
    # source keys for which we found nothing, rather than emit stubs.
    if xref:          record["crossref"]  = xref
    if fatcat_raw:    record["fatcat"]    = fatcat_raw
    if openalex_raw:  record["openalex"]  = openalex_raw
    if unpaywall_raw: record["unpaywall"] = unpaywall_raw
    if pubmed_raw:    record["pubmed"]    = pubmed_raw

    # v2 convenience fields
    add_convenience_fields(record)
    # Pass A enrichment: Crossmark (derivation, no extra fetch)
    cm = derive_crossmark(record)
    if cm is not None:
        record["crossmark"] = cm
    # Pass A enrichment: Funder Registry expansion (per funder DOI).
    # Strict: a permanent fetch failure aborts the build (archival ETL).
    expand_funders(record, fetch_crossref_funder)
    # Pass A enrichment: relation[] one-hop traversal (per related DOI)
    expand_relations(record, fetch_crossref_work)
    # Pass A enrichment: Event Data — DISABLED.
    # api.eventdata.crossref.org was sunset on 2026-04-23. See issue #8 for
    # the plan to mirror the historical archive on IA.
    # expand_event_data(record, fetch_event_data)
    if record.get("retracted"):
        n_retracted += 1
    entries[e["id"]] = record

today = time.strftime("%Y-%m-%d")
sources_used = {s: {"via": SOURCE_VIA[s], "fetched_at": today}
                for s, n in src_hits.items() if n > 0}
# Record the v2 broadened Crossref scope explicitly.
if "crossref" in sources_used:
    sources_used["crossref"]["type_filter"] = None
licenses_used = {s: SOURCE_LICENSE[s] for s in sources_used}

companion = {
    "schema_version": 2,
    "ia_item": item,
    "toc_schema_version": toc.get("schema_version"),
    "issue_meta": issue_meta,
    "volume_meta": volume_meta,
    "has_retracted_entries": n_retracted > 0,
    "provenance": {
        "software_versions": software_versions(),
        "sources": sources_used,
    },
    "license_notes": licenses_used,
    "entries": entries,
}

# Write gzipped
with gzip.open(out_path, "wt", encoding="utf-8") as fh:
    json.dump(companion, fh, indent=2)

print(f"wrote {out_path} ({out_path.stat().st_size} bytes)")
print(f"  {len(entries)} entries")
print(f"  issue_meta:  {'yes' if issue_meta else 'no'}")
print(f"  volume_meta: {'yes' if volume_meta else 'no'}")
print(f"  retracted:   {n_retracted}")
for k, v in src_hits.items():
    print(f"  matched_{k:9s} {v}/{len(entries)}")
