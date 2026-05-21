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
EMAIL = "brewster@archive.org"
HEADERS = {"User-Agent": f"segart-articles/1.1 (mailto:{EMAIL})"}


def fetch_crossref_full_for_year(issn, year):
    """v2: no type filter — pull every DOI Crossref has for the (issn, year)
    including journal-issue, journal-volume, editorial, review-article,
    book-review, book-chapter, proceedings-article, errata, etc. Caller
    routes records by `type`."""
    safe = re.sub(r"[^A-Za-z0-9-]", "_", issn)
    p = FULL_CACHE / f"{safe}_{year}.json"
    if p.exists():
        try:
            d = json.loads(p.read_text())
            return d.get("items", []), d.get("error")
        except Exception: pass
    items, error = [], None
    cursor = "*"; pages = 0
    while True:
        qs = urllib.parse.urlencode({
            "rows": 200,
            # v2: filter on date range only; no `type:` constraint.
            "filter": f"from-pub-date:{year}-01,until-pub-date:{year}-12",
            "cursor": cursor, "mailto": EMAIL})
        url = f"https://api.crossref.org/journals/{issn}/works?{qs}"
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=60) as fh:
                data = json.load(fh)
        except Exception as e:
            error = str(e); break
        msg = data.get("message", {})
        page_items = msg.get("items", [])
        items.extend(page_items); pages += 1
        nc = msg.get("next-cursor")
        if not page_items or not nc or nc == cursor: break
        cursor = nc
        if pages >= 50: break
    FULL_CACHE.mkdir(parents=True, exist_ok=True)
    # Per-worker tmp filename so concurrent writers to the same (issn,year)
    # don't race on the rename.
    p_tmp = p.with_suffix(f".json.tmp.{uuid.uuid4().hex}")
    p_tmp.write_text(json.dumps(
        {"items": items, "error": error, "paginated": True, "pages": pages,
         "type_filter": None}))
    p_tmp.replace(p)
    return items, error


def _doi_cache_get(cache_dir, doi, url):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", doi)
    p = cache_dir / f"{safe}.json"
    if p.exists():
        try:
            d = json.loads(p.read_text())
            if not d.get("error"):
                return d.get("data")
        except Exception: pass
    req = urllib.request.Request(url, headers=HEADERS)
    data = None; error = None
    try:
        with urllib.request.urlopen(req, timeout=30) as fh:
            data = json.load(fh)
    except urllib.error.HTTPError as e:
        if e.code == 404: data = None
        else: error = f"HTTP {e.code}"
    except Exception as e:
        error = str(e)
    cache_dir.mkdir(parents=True, exist_ok=True)
    p_tmp = p.with_suffix(f".json.tmp.{uuid.uuid4().hex}")
    p_tmp.write_text(json.dumps({"data": data, "error": error, "url": url}))
    p_tmp.replace(p)
    if error: raise RuntimeError(error)
    return data


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

SOURCE_VIA = {
    "crossref":  "live_api_cached",
    "fatcat":    "fatcat_release_lookup",
    "openalex":  "openalex_works_doi_lookup",
    "unpaywall": "unpaywall_v2_doi_lookup",
    "pubmed":    "europe_pmc_search_by_doi",
}
SOURCE_LICENSE = {
    "crossref":  "CC0 (bibliographic shell); abstracts retain publisher copyright",
    "fatcat":    "CC0",
    "openalex":  "CC0",
    "unpaywall": "CC0",
    "pubmed":    "US government work, public domain",
}


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


def _pick_title(rec):
    """Multi-source title pick: prefer Crossref, fall back to OpenAlex, then
    PubMed. Returns None if no source has a title."""
    cr = rec.get("crossref") or {}
    t = cr.get("title")
    if isinstance(t, list) and t:
        return t[0]
    if isinstance(t, str) and t:
        return t
    oa = rec.get("openalex") or {}
    t = oa.get("title") or oa.get("display_name")
    if t: return t
    pm = rec.get("pubmed") or {}
    return pm.get("title")


def _pick_abstract(rec):
    """Multi-source abstract pick: prefer Crossref's raw JATS, fall back to
    OpenAlex's inverted-index reconstruction, then PubMed's structured form.
    Raw JATS preserved (no transform). Returns None if none available."""
    cr = rec.get("crossref") or {}
    a = cr.get("abstract")
    if a: return a
    oa = rec.get("openalex") or {}
    inv = oa.get("abstract_inverted_index")
    if inv:
        # Reconstruct plain text from OpenAlex inverted index: {word: [positions]}.
        pairs = [(pos, w) for w, ps in inv.items() for pos in (ps or [])]
        pairs.sort()
        return " ".join(w for _, w in pairs)
    pm = rec.get("pubmed") or {}
    return pm.get("abstract") or pm.get("structured_abstract")


def _is_retracted(rec):
    """Crossref's update-to[] non-empty OR update-policy set both signal
    that the record has been revised/retracted/corrected. Only `update-to`
    with type=='retraction' is a true retraction; we surface the broader
    'has-been-updated' signal here and let consumers refine."""
    cr = rec.get("crossref") or {}
    updates = cr.get("update-to") or []
    for u in updates:
        if (u.get("type") or "").lower() == "retraction":
            return True
    return False


def _add_convenience_fields(rec):
    """v2: populate entry top-level convenience fields from source blobs.
    Originals stay in their source blobs unchanged — these are copies/picks
    surfaced for direct access. Mutates rec in place."""
    cr = rec.get("crossref") or {}
    oa = rec.get("openalex") or {}
    rec["entry_type"] = cr.get("type")
    rec["title"] = _pick_title(rec)
    rec["abstract"] = _pick_abstract(rec)
    rec["subjects"] = cr.get("subject") or []
    rec["topics"] = oa.get("topics") or []
    rec["concepts"] = oa.get("concepts") or []
    rec["retracted"] = _is_retracted(rec)


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


def article_already_uploaded(md, item):
    files = {f.get("name") for f in (md.get("files") or [])}
    return f"{item}_articles.json.gz" in files


def process_item(item):
    """Returns dict with status + counts."""
    out = {"item": item, "ok": False}
    md = ia_metadata(item)
    if not md:
        out["status"] = "ia_metadata_fail"; return out
    if article_already_uploaded(md, item):
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
    issue_meta = None
    volume_meta = None
    article_works = []
    for w in issue_works:
        t = w.get("type")
        if t == "journal-issue" and issue_meta is None:
            issue_meta = w
        elif t == "journal-volume" and volume_meta is None:
            volume_meta = w
        else:
            article_works.append(w)

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
        for sname, fn in (("fatcat", fetch_fatcat_by_doi),
                          ("openalex", fetch_openalex_by_doi),
                          ("unpaywall", fetch_unpaywall_by_doi),
                          ("pubmed", fetch_pubmed_by_doi)):
            data, err = fetch_with_retry(fn, doi)
            if err:
                out["status"] = f"{sname}_fail_for_{doi}: {err[:80]}"
                return out
            if data:
                rec[sname] = data
                src_hits[sname] += 1

        # v2 convenience fields: derived/picked from the source blobs and
        # surfaced at entry top level for direct consumer access. Originals
        # remain in their source blobs unchanged.
        _add_convenience_fields(rec)
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
    args = ap.parse_args()

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
