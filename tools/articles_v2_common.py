"""Shared helpers for producing v2 `_articles.json.gz` files.

Used by both `articles_pilot.py` (data-first: enumerate Crossref records
and build entries) and `build_articles_companion.py` (TOC-first: iterate
TOC entries and join by DOI).

See `docs/articles_format.md` for the v2 schema.
"""
from __future__ import annotations


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


def pick_title(rec):
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
    if t:
        return t
    pm = rec.get("pubmed") or {}
    return pm.get("title")


def pick_abstract(rec):
    """Multi-source abstract pick: prefer Crossref's raw JATS, fall back to
    OpenAlex's inverted-index reconstruction, then PubMed's structured form.
    Raw JATS preserved (no transform). Returns None if none available."""
    cr = rec.get("crossref") or {}
    a = cr.get("abstract")
    if a:
        return a
    oa = rec.get("openalex") or {}
    inv = oa.get("abstract_inverted_index")
    if inv:
        # Reconstruct plain text from OpenAlex inverted index: {word: [positions]}.
        pairs = [(pos, w) for w, ps in inv.items() for pos in (ps or [])]
        pairs.sort()
        return " ".join(w for _, w in pairs)
    pm = rec.get("pubmed") or {}
    return pm.get("abstract") or pm.get("structured_abstract")


def is_retracted(rec):
    """Crossref's update-to[] containing a type=='retraction' entry. Other
    update-to types (correction, erratum) are flagged via the crossmark
    block but don't set this boolean."""
    cr = rec.get("crossref") or {}
    for u in (cr.get("update-to") or []):
        if (u.get("type") or "").lower() == "retraction":
            return True
    return False


def add_convenience_fields(rec):
    """v2: populate entry top-level convenience fields from source blobs.
    Originals stay in their source blobs unchanged — these are copies/picks
    surfaced for direct access. Mutates rec in place."""
    cr = rec.get("crossref") or {}
    oa = rec.get("openalex") or {}
    rec["entry_type"] = cr.get("type")
    rec["title"] = pick_title(rec)
    rec["abstract"] = pick_abstract(rec)
    rec["subjects"] = cr.get("subject") or []
    rec["topics"] = oa.get("topics") or []
    rec["concepts"] = oa.get("concepts") or []
    rec["retracted"] = is_retracted(rec)


def derive_crossmark(rec):
    """Pass A: lift Crossmark-relevant fields from the per-entry Crossref
    blob into a top-level `crossmark` block. Pure derivation — no extra
    fetch. Returns None when the article has no Crossmark deployment
    (no update-policy, no assertions, no update-to).

    Source fields (already present in rec.crossref):
      - update-policy: publisher's update-policy doc URL
      - assertion[]:   CrossMark assertions (publication history, peer
                       review, change reasons, etc.)
      - update-to[]:   updates this record carries (corrections,
                       retractions, errata pointing at older DOIs)
      - content-domain: publisher-asserted content-host domains
    """
    cr = rec.get("crossref") or {}
    assertion = cr.get("assertion") or []
    updates = cr.get("update-to") or []
    update_policy = cr.get("update-policy")
    content_domain = cr.get("content-domain") or {}
    if not (assertion or updates or update_policy):
        return None
    return {
        "update_policy": update_policy,
        "assertions":    assertion,
        "updates":       updates,
        "content_domain": content_domain,
    }


def expand_funders(rec, fetch_funder_fn):
    """Pass A: for each funder DOI in rec.crossref.funder[], call
    fetch_funder_fn(funder_doi) to get the full Crossref /funders/{doi}
    response, and collect them at rec.funders_expanded[]. fetch_funder_fn
    should be a producer-provided function that handles caching + retries.

    No-op (no funders_expanded key written) when the article has no funders
    or none of them carry a DOI. funders without a DOI are skipped — there's
    nothing to look up. fetch_funder_fn returning None for a DOI is also
    skipped silently."""
    cr = rec.get("crossref") or {}
    funders = cr.get("funder") or []
    expanded = []
    seen = set()
    for f in funders:
        doi = f.get("DOI") or f.get("doi")
        if not doi or doi in seen:
            continue
        seen.add(doi)
        full = fetch_funder_fn(doi)
        if full:
            expanded.append(full)
    if expanded:
        rec["funders_expanded"] = expanded


def expand_relations(rec, fetch_work_fn):
    """Pass A: for each DOI-typed entry in rec.crossref.relation, fetch the
    related work's full /works/{doi} payload (one-hop only, no recursion)
    and collect at rec.relations_expanded[]. fetch_work_fn(doi) handles
    caching + retries.

    crossref.relation shape: dict keyed by relation type (correction,
    has-preprint, is-version-of, is-supplemented-by, has-translation, ...)
    mapping to a list of {id-type, id, asserted-by}.

    Skips non-DOI id-types (rare; e.g. arxiv IDs surface here too and would
    need a separate fetcher). Dedupes by target DOI within an entry."""
    cr = rec.get("crossref") or {}
    relations = cr.get("relation") or {}
    if not relations:
        return
    expanded = []
    seen = set()
    for rel_type, items in relations.items():
        for item in (items or []):
            id_type = (item.get("id-type") or "").lower()
            if id_type != "doi":
                continue
            target_doi = item.get("id")
            if not target_doi or target_doi in seen:
                continue
            seen.add(target_doi)
            target_meta = fetch_work_fn(target_doi)
            if target_meta:
                expanded.append({
                    "relation_type": rel_type,
                    "target_doi":    target_doi,
                    "asserted_by":   item.get("asserted-by"),
                    "target_meta":   target_meta,
                })
    if expanded:
        rec["relations_expanded"] = expanded


def expand_event_data(rec, fetch_event_data_fn):
    """Pass A: fetch Crossref Event Data events for the entry's primary DOI
    and embed at rec.event_data. Always inline (no sidecars), per the v2
    schema decision. fetch_event_data_fn(doi) should return a dict like:

        { "total_events": N, "sources": {<src>: count, ...}, "events": [...] }

    or None on failure / no events.

    Known operational issue (2026-05): the api.eventdata.crossref.org SSL
    cert is expired; fetches fail until Crossref renews it. We surface
    nothing in that case; when the cert is fixed, events start populating
    on next re-derivation without code changes."""
    doi = (rec.get("ext_ids") or {}).get("doi")
    if not doi:
        return
    ed = fetch_event_data_fn(doi)
    if ed and ed.get("total_events", 0) > 0:
        rec["event_data"] = ed


def route_by_type(records):
    """Partition Crossref records by `type`: journal-issue → issue_meta,
    journal-volume → volume_meta, everything else → articles[].
    First record of each special type wins; subsequent ones go to articles."""
    issue_meta = None
    volume_meta = None
    articles = []
    for w in records:
        t = w.get("type")
        if t == "journal-issue" and issue_meta is None:
            issue_meta = w
        elif t == "journal-volume" and volume_meta is None:
            volume_meta = w
        else:
            articles.append(w)
    return issue_meta, volume_meta, articles
