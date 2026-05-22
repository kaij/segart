#!/usr/bin/env python3
"""Fuzzy-match a TOC article entry to a fatcat release when no DOI is known.

Identifier note: fatcat's "v1 base32 ident" and "v2 UUID" are two encodings
of the same 128-bit value. The ES index stores the base32 form; we decode
to canonical UUID at the output boundary so downstream code only sees UUIDs.

Strategy (broader-then-rerank):
  1. Resolve the journal container's ident by ISSN-L from the
     fatcat_container ES index, decode to UUID for the caller.
  2. Query fatcat_release ES with a `match` on title, filtered to the
     container_id (base32 — internal to ES) and optionally release_year.
     Volume is applied as a soft `should` clause (boosts retrieval but
     doesn't require). Issue is intentionally NOT used — empirically
     ES has `issue: null` even on releases with a real issue.
  3. Pull top-N candidates and rerank in Python:
        title_sim     (difflib SequenceMatcher ratio on normalized titles)
        author_sim    (Jaccard of last-name token sets)
        volume_match  (1.0 if both sides present and equal, else 0.0)
        page_overlap  (IoU between TOC printed_pages and fatcat `pages`,
                       or first-page-in-range fallback)
        combined  = TITLE_W * title_sim + AUTHOR_W * author_sim
                    + VOLUME_BONUS * volume_match
                    + PAGE_BONUS * page_overlap
  4. Break combined-score ties by record completeness. Two fatcat releases
     for the SAME article (e.g. the canonical Crossref article-DOI form and a
     page-locator-DOI form) routinely tie on combined; prefer the more fully
     populated record, which empirically is the canonical Crossref one. Tied
     non-top candidates are flagged `duplicate_of_top` so downstream sees the
     ambiguity instead of a silent pick.
  5. Print ranked candidates with both ES BM25 and local rerank scores.
     All ident-shaped output is UUID.

Stdlib only. Read-only — no writes back to fatcat or the TOC.

Usage:
  ./fuzzy_fatcat_match.py \\
      --issn 0098-7484 --year 2018 \\
      --title "Costs of Quality Measurement Reply" \\
      --authors "Schuster,Onorato,Meltzer"

  # JSON-fixture mode for batch testing:
  ./fuzzy_fatcat_match.py --fixture fixtures.jsonl
"""
import argparse
import base64
import difflib
import json
import re
import sys
import urllib.parse
import urllib.request
import uuid

ES_BASE = "https://scholar.archive.org/_es"
TOP_N = 10

# Combined-score weights for local rerank. Title carries more weight than
# authors because title strings are richer; author surnames are noisy
# (initials, suffixes, ordering, OCR errors) and best used as a tiebreaker.
# Volume is an ADDITIVE bonus (not part of the weighted sum) so that
# providing it never penalizes a candidate; missing-on-either-side is just 0.
TITLE_W       = 0.70
AUTHOR_W      = 0.30
VOLUME_BONUS  = 0.10
# Page range is more discriminating within an issue than volume is within
# a year, so weight it slightly higher. Still additive (never penalizes).
PAGE_BONUS    = 0.15

# Boost applied to the ES `should` clause for volume. Affects which 10
# candidates come back, not the local rerank score directly.
VOLUME_ES_BOOST = 5.0

# Two candidates that tie on combined score are almost always duplicate
# fatcat records for the same article (canonical article-DOI vs page-locator-
# DOI). We break the tie by `completeness` and flag the losers
# `duplicate_of_top`. EPS guards against float jitter in otherwise-equal sums.
COMBINED_TIE_EPS = 1e-9

# Confidence bands for surface labels — not used to filter, just to flag
# for review. Tunable once we see real-world distribution.
BANDS = [(0.85, "confident"), (0.65, "review"), (0.0, "weak")]


def fcid_to_uuid(fcid):
    """Decode a 26-char fatcat base32 ident to its canonical UUID string."""
    if not fcid:
        return None
    return str(uuid.UUID(bytes=base64.b32decode(fcid.upper() + "=" * 6)))


def es_search(index, body):
    url = f"{ES_BASE}/{index}/_search"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as fh:
        return json.load(fh)


def resolve_container_fcid(issn):
    """Return the journal container's base32 ident from ES (for ES-internal
    joins), or None. The CLI / output layer decodes to UUID separately."""
    r = es_search("fatcat_container", {
        "size": 1,
        "query": {
            "bool": {
                "should": [
                    {"term": {"issnl": issn}},
                    {"term": {"issnp": issn}},
                    {"term": {"issne": issn}},
                ],
                "minimum_should_match": 1,
            }
        }
    })
    hits = r.get("hits", {}).get("hits") or []
    if not hits:
        return None
    return hits[0].get("_source", {}).get("ident")


def fetch_candidates(container_ident, title, year, volume=None):
    """ES query: top-N candidates by title within the container.
    `release_year` is a hard filter when given. `volume` is a soft `should`
    boost — present-and-matching pushes a candidate up in the BM25 ranking
    without excluding non-matching ones."""
    must = [{"match": {"title": title}}]
    filt = [{"term": {"container_id": container_ident}}]
    if year is not None:
        filt.append({"term": {"release_year": int(year)}})
    bool_q = {"must": must, "filter": filt}
    if volume:
        bool_q["should"] = [{"term": {"volume": {"value": str(volume),
                                                  "boost": VOLUME_ES_BOOST}}}]
    r = es_search("fatcat_release", {
        "size": TOP_N,
        "query": {"bool": bool_q},
    })
    return [{"score": h.get("_score"), "src": h.get("_source", {})}
            for h in r.get("hits", {}).get("hits", [])]


# ---- local rerank ----

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)


def normalize_title(s):
    s = (s or "").lower()
    s = _PUNCT_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def title_similarity(a, b):
    return difflib.SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


def extract_surnames(authors):
    """Authors can be: list of strings ('Mark A. Schuster'), list of dicts
    ({name: ...}), or a comma-joined string. Return a lowercased set of
    surnames (the LAST token after splitting on whitespace, with trailing
    punctuation stripped)."""
    if not authors:
        return set()
    if isinstance(authors, str):
        authors = [a.strip() for a in authors.split(",") if a.strip()]
    out = set()
    for a in authors:
        if isinstance(a, dict):
            a = a.get("name") or a.get("raw_name") or ""
        a = (a or "").strip().rstrip(",.;:")
        if not a:
            continue
        # Surname heuristic: if "Last, First" use the part before the comma;
        # otherwise the last whitespace-separated token.
        if "," in a:
            sur = a.split(",", 1)[0].strip()
        else:
            sur = a.split()[-1]
        sur = sur.strip(".,;:").lower()
        # Drop suffix-only tokens like "jr", "iii"
        if sur in ("jr", "sr", "ii", "iii", "iv"):
            # try the second-to-last
            parts = a.split()
            if len(parts) >= 2:
                sur = parts[-2].strip(".,;:").lower()
        if sur:
            out.add(sur)
    return out


def author_similarity(a_set, b_set):
    if not a_set or not b_set:
        return 0.0
    inter = len(a_set & b_set)
    union = len(a_set | b_set)
    return inter / union if union else 0.0


def volume_match(a, b):
    """Soft equality on a volume string. Returns 1.0 / 0.0; absent on
    either side → 0.0 (don't reward unknowns, don't penalize either)."""
    if a is None or b is None:
        return 0.0
    return 1.0 if str(a).strip().lower() == str(b).strip().lower() else 0.0


_PAGE_RANGE_RE  = re.compile(r"^\s*(\d+)\s*[-–]\s*(\d+)\s*$")
_PAGE_SINGLE_RE = re.compile(r"^\s*(\d+)\s*$")


def parse_page_range(s):
    """Parse a single page-range string into an (int, int) tuple, or None.

    Handles "1273-1278", "1273-78" (abbreviated end), "1273" (single page).
    Rejects Roman numerals, supplement prefixes ("S1-S10"), and anything
    non-arabic — those are uncommon enough in the SIM corpus to be worth
    ignoring rather than risking false matches on misparsed values.
    """
    if not s:
        return None
    s = str(s).strip()
    if m := _PAGE_RANGE_RE.match(s):
        start, end = int(m.group(1)), int(m.group(2))
        if end < start:
            # Abbreviated end (e.g. "1273-78" meaning 1273-1278).
            ss, es = str(start), str(end)
            if len(es) < len(ss):
                end = int(ss[: len(ss) - len(es)] + es)
        return (start, end) if end >= start else None
    if m := _PAGE_SINGLE_RE.match(s):
        p = int(m.group(1))
        return (p, p)
    return None


def parse_printed_pages_arg(s):
    """Parse a CLI --printed-pages string. Accepts a single range
    ("1273-1278") or comma-separated multi-range
    ("1273-1278,1285-1290")."""
    if not s:
        return None
    out = []
    for part in str(s).split(","):
        r = parse_page_range(part)
        if r:
            out.append(r)
    return out or None


def normalize_printed_pages(x):
    """Accept the TOC v2 form (array of [start, end] string pairs, per
    docs/toc_format.md) or a string. Return list of (int, int) tuples
    or None."""
    if x is None:
        return None
    if isinstance(x, str):
        return parse_printed_pages_arg(x)
    if isinstance(x, list):
        out = []
        for item in x:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                r = parse_page_range(f"{item[0]}-{item[1]}")
                if r:
                    out.append(r)
        return out or None
    return None


def _iou(a, b):
    a_start, a_end = a
    b_start, b_end = b
    inter = max(0, min(a_end, b_end) - max(a_start, b_start) + 1)
    union = (a_end - a_start + 1) + (b_end - b_start + 1) - inter
    return inter / union if union else 0.0


def page_overlap(toc_ranges, candidate_pages, candidate_first):
    """Score how well the candidate's page span matches the TOC entry's.

    - Both sides have parseable ranges: IoU across the best pairing.
    - Candidate has only first_page: 1.0 if it equals a TOC range start,
      0.5 if it lands inside (but not at the start of) one, 0 otherwise.
    - Anything missing on either side: 0.0 — don't reward, don't penalize.
    """
    if not toc_ranges:
        return 0.0
    fc_range = parse_page_range(candidate_pages)
    if fc_range:
        return max(_iou(fc_range, t) for t in toc_ranges)
    if candidate_first is not None:
        try:
            fp = int(candidate_first)
        except (TypeError, ValueError):
            return 0.0
        for t_start, t_end in toc_ranges:
            if t_start <= fp <= t_end:
                return 1.0 if fp == t_start else 0.5
    return 0.0


def _present(v):
    """True if a field carries a meaningful (non-empty) value."""
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, tuple, dict)):
        return len(v) > 0
    return True


def completeness(src):
    """Tiebreaker score: how fully populated a fatcat release record is.

    Only consulted to order candidates that tie on `combined` — never
    overrides it. Page-locator-DOI duplicates tend to drop `first_page`,
    carry a malformed `pages` locator (e.g. '1782c-1782'), and use initials-
    only author names, so they score below the canonical Crossref article
    record. `ref_count` is in the ES schema but often 0; included because
    when populated it's a strong canonical-record signal.
    """
    score = float(len(src.get("contrib_names") or []))   # author count
    if _present(src.get("issue")):
        score += 1.0
    score += float(src.get("ref_count") or 0)            # reference list present
    if _present(src.get("first_page")):
        score += 1.0
    if parse_page_range(src.get("pages")):               # well-formed range
        score += 1.0
    return score


def band(score):
    for thresh, label in BANDS:
        if score >= thresh:
            return label
    return "weak"


def rank(query_title, query_authors, candidates, query_volume=None,
         query_pages=None):
    """Rerank ES candidates locally. `query_pages` is a list of
    (int, int) tuples from normalize_printed_pages, or None."""
    q_surnames = extract_surnames(query_authors)
    ranked = []
    for c in candidates:
        src = c["src"]
        ts = title_similarity(query_title, src.get("title") or "")
        a_sur = extract_surnames(src.get("contrib_names") or [])
        au = author_similarity(q_surnames, a_sur)
        vm = volume_match(query_volume, src.get("volume"))
        po = page_overlap(query_pages, src.get("pages"), src.get("first_page"))
        combined = (TITLE_W * ts + AUTHOR_W * au
                    + VOLUME_BONUS * vm + PAGE_BONUS * po)
        ranked.append({
            "combined":  combined,
            "completeness": completeness(src),
            "es_score":  c["score"],
            "title_sim": ts,
            "author_sim": au,
            "volume_match": vm,
            "page_overlap": po,
            "band":      band(combined),
            "release_id":    fcid_to_uuid(src.get("ident")),
            "title":         src.get("title"),
            "year":      src.get("release_year"),
            "volume":    src.get("volume"),
            "issue":     src.get("issue"),
            "pages":     src.get("pages"),
            "first_page": src.get("first_page"),
            "ref_count": src.get("ref_count"),
            "doi":       src.get("doi"),
            "contrib_names": src.get("contrib_names") or [],
        })
    # Primary sort: combined score. Secondary: completeness — only changes
    # order among combined-score ties (duplicate records for one article).
    ranked.sort(key=lambda r: (r["combined"], r["completeness"]), reverse=True)
    # Flag any non-top candidate that ties the top on combined: downstream
    # sees the ambiguity rather than a silently-picked winner.
    if ranked:
        top_combined = ranked[0]["combined"]
        for i, r in enumerate(ranked):
            r["duplicate_of_top"] = (
                i > 0 and abs(r["combined"] - top_combined) <= COMBINED_TIE_EPS
            )
    return ranked


def match_one(issn, title, authors, year=None, volume=None, printed_pages=None):
    """Run the full pipeline for one query. Returns a dict with container
    info (UUID) and ranked candidates (release_ids as UUIDs)."""
    cfcid = resolve_container_fcid(issn)
    if not cfcid:
        return {"error": f"no fatcat container for ISSN {issn}"}
    cands = fetch_candidates(cfcid, title, year, volume=volume)
    q_pages = normalize_printed_pages(printed_pages)
    return {
        "container_id":    fcid_to_uuid(cfcid),
        "candidate_count": len(cands),
        "ranked":          rank(title, authors, cands,
                                 query_volume=volume, query_pages=q_pages),
    }


# ---- CLI ----

def format_result(result):
    lines = []
    if result.get("error"):
        return f"ERROR: {result['error']}"
    lines.append(f"container_id={result['container_id']}  "
                 f"candidates={result['candidate_count']}")
    for r in result["ranked"]:
        dup = "  <DUP of top>" if r.get("duplicate_of_top") else ""
        lines.append(
            f"  combined={r['combined']:.3f} [{r['band']:>9s}]  "
            f"title_sim={r['title_sim']:.2f}  author_sim={r['author_sim']:.2f}  "
            f"vol_match={r['volume_match']:.0f}  page_overlap={r['page_overlap']:.2f}  "
            f"compl={r['completeness']:.0f}  es={r['es_score']:.1f}{dup}"
        )
        lines.append(
            f"    release_id={r['release_id']}  y={r['year']}  vol={r['volume']}  "
            f"iss={r['issue']}  pages={r['pages']!r}  doi={r['doi']}"
        )
        lines.append(f"    title:   {r['title']!r}")
        lines.append(f"    authors: {r['contrib_names']!r}")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--issn", help="ISSN-L (or any of issn-p/issn-e) for the container")
    p.add_argument("--year", type=int, help="release_year filter (optional)")
    p.add_argument("--volume", help="soft volume hint (boost; never excludes)")
    p.add_argument("--printed-pages",
                   help="page-range hint: 'start-end' or comma-list "
                        "'start1-end1,start2-end2' (soft; never excludes)")
    p.add_argument("--title", help="article title (free text)")
    p.add_argument("--authors", help="comma-separated authors (any format)")
    p.add_argument("--fixture", help="JSONL file with one query per line: "
                                     "{issn, year, [volume], [printed_pages], "
                                     "title, authors, [expected_doi]}")
    p.add_argument("--json", action="store_true", help="output JSON instead of human-readable")
    args = p.parse_args()

    if args.fixture:
        with open(args.fixture) as fh:
            queries = [json.loads(line) for line in fh if line.strip()]
    elif args.issn and args.title:
        queries = [{"issn": args.issn, "year": args.year, "volume": args.volume,
                    "printed_pages": args.printed_pages,
                    "title": args.title, "authors": args.authors}]
    else:
        p.error("Provide either --fixture or (--issn AND --title)")

    for q in queries:
        result = match_one(q["issn"], q["title"], q.get("authors"),
                            q.get("year"), q.get("volume"),
                            q.get("printed_pages"))
        if args.json:
            out = {"query": q, "result": result}
            print(json.dumps(out, indent=2))
        else:
            exp = q.get("expected_doi")
            print(f"\n=== query: title={q['title']!r} authors={q.get('authors')!r} "
                  f"issn={q['issn']} year={q.get('year')} "
                  f"{('expected_doi='+exp) if exp else ''} ===")
            print(format_result(result))
            if exp and result.get("ranked"):
                top = result["ranked"][0]
                hit = (top["doi"] or "").lower() == exp.lower()
                print(f"  >> top-1 doi match: {hit}")


if __name__ == "__main__":
    main()
