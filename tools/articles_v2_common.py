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
