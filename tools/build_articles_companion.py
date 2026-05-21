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
    add_convenience_fields, route_by_type,
)


CACHE_ROOT = Path("/Users/brewster/tmp/segart/tmp")
# v2: separate cache dir (no type filter); v1's crossref_full_cache stays untouched.
FULL_CACHE = CACHE_ROOT / "crossref_full_cache_v2"
FATCAT_CACHE = CACHE_ROOT / "fatcat_doi_cache"
FATCAT_FILES_CACHE = CACHE_ROOT / "fatcat_files_cache"
OPENALEX_CACHE = CACHE_ROOT / "openalex_doi_cache"
UNPAYWALL_CACHE = CACHE_ROOT / "unpaywall_doi_cache"
PUBMED_CACHE = CACHE_ROOT / "pubmed_doi_cache"

EMAIL = "brewster@archive.org"
HEADERS = {"User-Agent": f"segart-articles/1.1 (mailto:{EMAIL})"}


def fetch_crossref_full_for_year(issn: str, year: str) -> tuple[list, str | None]:
    """v2: pull EVERY DOI Crossref has for (issn, year) — no type filter.
    Cursor-paginated so prolific journal-years aren't truncated. File-cached
    by (issn, year) under tmp/crossref_full_cache_v2/.

    Distinct from the audit cache (tmp/crossref_journal_year_cache/) — the
    audit used select=DOI,title,page,volume,issue,author to keep its cache
    small. The articles file needs the rest of each record (abstract,
    references, funder, license, dates, ...).
    """
    safe = re.sub(r"[^A-Za-z0-9-]", "_", issn)
    p = FULL_CACHE / f"{safe}_{year}.json"
    if p.exists():
        try:
            d = json.loads(p.read_text())
            return d.get("items", []), d.get("error")
        except Exception:
            pass  # corrupted; refetch

    items, error = [], None
    cursor = "*"
    pages_fetched = 0
    while True:
        qs = urllib.parse.urlencode({
            "rows": 200,
            # v2: filter on date range only; no `type:` constraint.
            "filter": (f"from-pub-date:{year}-01,"
                       f"until-pub-date:{year}-12"),
            "cursor": cursor,
            "mailto": EMAIL,
            # NB: no `select=` — we want everything Crossref has
        })
        url = f"https://api.crossref.org/journals/{issn}/works?{qs}"
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=60) as fh:
                data = json.load(fh)
        except Exception as e:
            error = str(e); break
        msg = data.get("message", {})
        page_items = msg.get("items", [])
        items.extend(page_items)
        pages_fetched += 1
        next_cursor = msg.get("next-cursor")
        if not page_items or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        if pages_fetched >= 50:  # safety cap
            break

    FULL_CACHE.mkdir(parents=True, exist_ok=True)
    p_tmp = p.with_suffix(".json.tmp")
    p_tmp.write_text(json.dumps({"items": items, "error": error,
                                  "paginated": True, "pages": pages_fetched,
                                  "type_filter": None}))
    p_tmp.replace(p)
    return items, error


def _doi_cache_get(cache_dir: Path, doi: str, url: str,
                   extra_headers: dict | None = None) -> dict | None:
    """File-cached single GET by DOI. Returns parsed JSON, None for a
    cached 404 (no record exists). Transient errors are NOT cached —
    re-runs will retry."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", doi)
    p = cache_dir / f"{safe}.json"
    if p.exists():
        try:
            d = json.loads(p.read_text())
            # Only trust the cache when the previous fetch reached the
            # server (status 200 or 404). Transient errors get a retry.
            if not d.get("error"):
                return d.get("data")
        except Exception:
            pass
    req = urllib.request.Request(url, headers={**HEADERS, **(extra_headers or {})})
    data = None
    error = None
    try:
        with urllib.request.urlopen(req, timeout=30) as fh:
            data = json.load(fh)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            data = None  # legitimately no record — cache this
        else:
            error = f"HTTP {e.code}"
    except Exception as e:
        error = str(e)
    cache_dir.mkdir(parents=True, exist_ok=True)
    p_tmp = p.with_suffix(".json.tmp")
    p_tmp.write_text(json.dumps({"data": data, "error": error, "url": url}))
    p_tmp.replace(p)
    return data


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
        try:
            fatcat_raw   = fetch_fatcat_by_doi(doi)
        except Exception as ex:
            print(f"  fatcat fetch failed for {doi}: {ex}", file=sys.stderr)
        try:
            openalex_raw = fetch_openalex_by_doi(doi)
        except Exception as ex:
            print(f"  openalex fetch failed for {doi}: {ex}", file=sys.stderr)
        try:
            unpaywall_raw = fetch_unpaywall_by_doi(doi)
        except Exception as ex:
            print(f"  unpaywall fetch failed for {doi}: {ex}", file=sys.stderr)
        try:
            pubmed_raw   = fetch_pubmed_by_doi(doi)
        except Exception as ex:
            print(f"  pubmed fetch failed for {doi}: {ex}", file=sys.stderr)
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
