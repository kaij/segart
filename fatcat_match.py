#!/usr/bin/env python3
"""Shared fatcat-matching primitives for segart.

Two segart tools match local catalog records to fatcat entities:

  - match_pub_to_fatcat.py : IA pub_* collection -> fatcat container (journal),
                             deterministic keys against the offline
                             container_export.json.gz bulk dump.
  - fuzzy_fatcat_match.py  : TOC article entry  -> fatcat release (article),
                             fuzzy rerank against the live scholar.archive.org
                             Elasticsearch index.

They sit at different granularities and pull from different sources, but used
to carry duplicated (and subtly divergent) copies of the same helpers. This
module is the single home for the pieces both want:

  - ident encoding      : fcid_to_uuid
  - value utilities     : first_str, present
  - title normalization : normalize_title_key   (aggressive — exact-match keys)
                          normalize_title_fuzzy (light — similarity scoring)
  - duplicate tiebreak  : COMBINED_TIE_EPS, flag_duplicates,
                          release_completeness, container_completeness

Tiebreak convention: when candidates tie on their primary score they are
almost always duplicate fatcat records for the SAME entity (e.g. a canonical
Crossref article-DOI vs a page-locator-DOI; or two container records for one
journal). Prefer the more fully populated record — empirically the canonical
one — and FLAG the tie rather than silently dropping or picking it, so
downstream sees the ambiguity.

Stdlib only.
"""
import base64
import re
import uuid


# ---- ident encoding ----

def fcid_to_uuid(fcid):
    """Decode a 26-char fatcat base32 ident to its canonical UUID string.

    fatcat's "v1 base32 ident" and "v2 UUID" are two encodings of the same
    128-bit value; the ES index and bulk dumps store the base32 form.
    """
    if not fcid:
        return None
    return str(uuid.UUID(bytes=base64.b32decode(fcid.upper() + "=" * 6)))


# ---- value utilities ----

def first_str(v):
    """First element of a list, or the value itself — for fields that fatcat
    / IA sometimes return as a scalar and sometimes as a list."""
    if isinstance(v, list):
        return v[0] if v else None
    return v


def present(v):
    """True if a field carries a meaningful (non-empty) value."""
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, tuple, dict)):
        return len(v) > 0
    return True


# ---- title normalization ----
# Two intentionally different normalizers. Keep them distinct: collapsing them
# into one would regress one of the two callers.

# Aggressive: for exact-match KEYS (journal-name joins). Lowercase, drop all
# non-alphanumerics, drop a small multilingual stopword list. This is the
# behavior documented in docs/pub_fatcat_matching.md and feeds match rates, so
# don't change it without re-measuring.
STOPWORDS = {
    "the", "a", "an", "of", "and", "in", "on", "for", "to",
    "la", "le", "les", "der", "die", "das", "el", "los", "il",
}


def normalize_title_key(t):
    """Aggressive normalization for exact-match keys (journal-name joins).
    Returns the normalized string, or None if nothing survives."""
    if not t:
        return None
    t = t.lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    words = [w for w in t.split() if w and w not in STOPWORDS]
    return " ".join(words) if words else None


# Light: for SIMILARITY scoring (difflib ratio on article titles). Keep
# stopwords and word boundaries; only fold case and punctuation.
_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)


def normalize_title_fuzzy(s):
    """Light normalization for fuzzy similarity (difflib). Lowercase, fold
    punctuation to spaces, collapse whitespace. Keeps stopwords."""
    s = (s or "").lower()
    s = _PUNCT_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ---- duplicate tiebreak ----

# EPS guards against float jitter when two primary scores are computed from
# identical components and should compare equal.
COMBINED_TIE_EPS = 1e-9


def flag_duplicates(ranked, score_key="combined", eps=COMBINED_TIE_EPS):
    """Mark ties in a best-first-sorted candidate list.

    Mutates `ranked` in place: sets `duplicate_of_top` True on every non-top
    entry whose `score_key` ties the top within `eps`, False otherwise (the
    top itself is always False). Returns `ranked` for chaining.
    """
    if not ranked:
        return ranked
    top = ranked[0][score_key]
    for i, r in enumerate(ranked):
        r["duplicate_of_top"] = (i > 0 and abs(r[score_key] - top) <= eps)
    return ranked


# A clean numeric page value ("1782", "1273-1278", "1273-78") vs a locator
# artifact ("1782c-1782", "S1-S10", "1131a-1132"). Only a completeness signal,
# not full parsing — fuzzy_fatcat_match.parse_page_range does the real thing.
_NUMERIC_PAGES_RE = re.compile(r"^\s*\d+\s*(?:[-–]\s*\d+\s*)?$")


def release_completeness(src):
    """How fully populated a fatcat RELEASE record is — tiebreak only, never
    overrides the primary score.

    Page-locator-DOI duplicates tend to drop `first_page`, carry a malformed
    `pages` locator (e.g. '1782c-1782'), and use initials-only author names,
    so they score below the canonical Crossref article record. `ref_count` is
    in the schema but often 0; included because when populated it's a strong
    canonical-record signal.
    """
    score = float(len(src.get("contrib_names") or []))      # author count
    if present(src.get("issue")):
        score += 1.0
    score += float(src.get("ref_count") or 0)               # references present
    if present(src.get("first_page")):
        score += 1.0
    if _NUMERIC_PAGES_RE.match(str(src.get("pages") or "")):  # well-formed range
        score += 1.0
    return score


def container_completeness(c):
    """How fully populated a fatcat CONTAINER record is — tiebreak for the
    title-ambiguous case (two journals normalizing to the same name). Prefers
    the record with more identifiers populated; empirically the canonical
    container."""
    score = sum(1 for k in ("issnl", "issne", "issnp") if present(c.get(k)))
    if present(c.get("sim_pubid")):
        score += 1
    if present(c.get("wikidata_qid")):
        score += 1
    if present(c.get("publisher")):
        score += 1
    return score
