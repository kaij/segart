"""Drill into near-clean (90-94%) buckets to classify the misses.

For each (issn, year-bucket):
  1. Walk tmp/qa_corpus.jsonl and pull all anchors in that bucket
  2. For each anchor's (vol, iss): pull Crossref records from the audit's
     journal_year_cache + filter to that issue
  3. Apply the audit's matcher (fuzzy_first_n_match on title OR
     author_surname_match) to identify which anchors went unmatched
  4. For each unmatched anchor: query Crossref live by title within journal
     (NO type filter, NO vol/iss filter) — see what the article actually is
  5. Classify the miss:
       - `wrong_type`: article exists in Crossref with type != journal-article
       - `wrong_voliss`: exists in Crossref but with different vol/iss labels
       - `not_in_crossref`: title-match returns nothing → genuinely missing
       - `weak_title`: ILL title too short/generic for confident match
       - `ill_noise`: looks like ILL metadata noise (year wrong, issn wrong, etc.)

Usage:
  python3 tools/diagnose_near_clean_misses.py [--limit-per-bucket N]
"""
from __future__ import annotations
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

SEGART = Path("/Users/brewster/tmp/segart")
sys.path.insert(0, str(SEGART / "tools"))

# Re-use the audit's matcher for fidelity with how `match_pct` was computed.
from journal_xref_audit import (
    fuzzy_first_n_match, author_surname_match,
    is_junk_anchor, normalize, extract_surnames,
)

EMAIL = "brewster@archive.org"
UA = f"segart-diagnose/0.1 (mailto:{EMAIL})"
AUDIT_CACHE = SEGART / "tmp" / "crossref_journal_year_cache"
CORPUS = SEGART / "tmp" / "qa_corpus.jsonl"
OUT = SEGART / "tmp" / "audit" / "near_clean_misses_diagnosed.json"


def cache_path(issn, year):
    safe = re.sub(r"[^A-Za-z0-9-]", "_", issn)
    return AUDIT_CACHE / f"{safe}_{year}.json"


def load_anchors_for_bucket(issn, bucket_lo, bucket_hi):
    """Return list of (vol, iss, year, title, author, journal_title) for all
    ILL anchors in this (issn, year-bucket)."""
    out = []
    with CORPUS.open() as fh:
        for line in fh:
            line = line.strip()
            if not line: continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            for a in rec.get("anchors", []):
                if (a.get("issn") or "").strip() != issn: continue
                yr_s = (a.get("year") or "").strip()[:4]
                if not yr_s.isdigit(): continue
                y = int(yr_s)
                if not (bucket_lo <= y <= bucket_hi): continue
                title = (a.get("article_title") or "").strip()
                jt = (a.get("journal_title") or "").strip()
                if not title or is_junk_anchor(title, jt): continue
                vol = (a.get("volume") or "").strip()
                iss = (a.get("issue") or "").strip()
                if not (vol and iss): continue
                out.append({
                    "vol": vol, "iss": iss, "year": yr_s,
                    "title": title,
                    "author": (a.get("article_author") or "").strip(),
                    "journal_title": jt,
                })
    return out


def fetch_year_cache(issn, year):
    """Return the audit-cached Crossref items for (issn, year), or []."""
    p = cache_path(issn, year)
    if not p.exists(): return []
    try:
        d = json.loads(p.read_text())
        return d.get("items") or []
    except Exception:
        return []


def label_matches(crossref_label, ia_label):
    a = str(crossref_label or "").strip()
    b = str(ia_label or "").strip()
    if not a or not b: return False
    if a == b: return True
    for s in (a, b):
        if "-" in s and (a in s.split("-") or b in s.split("-")): return True
    return False


def find_issue_works(items, vol, iss):
    """Filter Crossref records to the issue (vol, iss). Same logic as audit."""
    return [r for r in items
            if label_matches(r.get("volume"), vol)
            and label_matches(r.get("issue"), iss)]


def is_matched(anchor, xref_records):
    """Audit's match rule: fuzzy title OR author surname against the issue's
    Crossref records. (Audit always considers this an anchor *of the issue*,
    so we only look at xref_records that already filter to vol/iss.)"""
    t = anchor["title"]
    for r in xref_records:
        title_field = r.get("title")
        cr_title = (title_field or [""])[0] if isinstance(title_field, list) else (title_field or "")
        if fuzzy_first_n_match(t, cr_title):
            return True
    if author_surname_match(anchor["author"], xref_records):
        return True
    return False


def crossref_title_search(issn, title, rows=5):
    """Live query: search the journal's works by title text. Returns Crossref
    items (full record). NO type filter, NO vol/iss filter."""
    qs = urllib.parse.urlencode({
        "query.title": title,
        "rows": rows,
        "mailto": EMAIL,
    })
    url = f"https://api.crossref.org/journals/{issn}/works?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as fh:
        d = json.load(fh)
    return d.get("message", {}).get("items") or []


def best_title_match(query_title, candidates):
    """Pick the best fuzzy-match Crossref candidate for the ILL anchor title,
    or None if nothing close."""
    q_norm = " ".join(normalize(query_title))
    if not q_norm: return None, 0.0
    best = None
    best_score = 0.0
    for c in candidates:
        t = c.get("title")
        if isinstance(t, list): t = t[0] if t else ""
        c_norm = " ".join(normalize(t or ""))
        if not c_norm: continue
        score = SequenceMatcher(None, q_norm, c_norm).ratio()
        if score > best_score:
            best_score = score
            best = c
    return best, best_score


def classify_miss(anchor, issn):
    """For an unmatched anchor, query Crossref live and classify the miss."""
    title = anchor["title"]
    # Weak-title heuristic: < 3 content words
    if len(normalize(title)) < 3:
        return {"verdict": "weak_title", "detail": f"title has {len(normalize(title))} content words", "title": title}

    try:
        candidates = crossref_title_search(issn, title, rows=5)
    except Exception as e:
        return {"verdict": "lookup_error", "detail": f"{type(e).__name__}: {e}", "title": title}

    if not candidates:
        return {"verdict": "not_in_crossref", "detail": "title query returned 0 items in this journal", "title": title}

    best, score = best_title_match(title, candidates)
    if not best or score < 0.7:
        return {"verdict": "not_in_crossref",
                "detail": f"best title fuzzy score {score:.2f} (< 0.70 threshold)",
                "title": title,
                "best_candidate": (best or {}).get("title", [""])[0] if best else None}

    # Found a likely-match candidate in Crossref. Classify why audit missed it.
    cr_type = best.get("type")
    cr_vol = str(best.get("volume", "")).strip()
    cr_iss = str(best.get("issue", "")).strip()
    ill_vol = anchor["vol"]
    ill_iss = anchor["iss"]

    if cr_type and cr_type != "journal-article":
        return {"verdict": "wrong_type",
                "detail": f"Crossref has it as type={cr_type!r} (vol={cr_vol}, iss={cr_iss})",
                "title": title,
                "crossref_doi": best.get("DOI"),
                "crossref_title": (best.get("title") or [""])[0],
                "crossref_type": cr_type,
                "score": round(score, 3)}

    if cr_vol != ill_vol or cr_iss != ill_iss:
        return {"verdict": "wrong_voliss",
                "detail": f"Crossref vol/iss = ({cr_vol},{cr_iss}); ILL says ({ill_vol},{ill_iss})",
                "title": title,
                "crossref_doi": best.get("DOI"),
                "crossref_title": (best.get("title") or [""])[0],
                "ill_voliss": [ill_vol, ill_iss],
                "crossref_voliss": [cr_vol, cr_iss],
                "score": round(score, 3)}

    # Same vol/iss/type but audit's matcher missed it. Likely a title-shape
    # edge case the audit's fuzzy_first_n_match couldn't grok.
    return {"verdict": "audit_matcher_miss",
            "detail": f"vol/iss agree but audit fuzzy_first_n_match didn't fire",
            "title": title,
            "crossref_doi": best.get("DOI"),
            "crossref_title": (best.get("title") or [""])[0],
            "score": round(score, 3)}


def diagnose_bucket(issn, bucket_label, limit=None):
    lo, hi = [int(x) for x in bucket_label.split("-")]
    print(f"\n=== {issn} {bucket_label} ===")
    anchors = load_anchors_for_bucket(issn, lo, hi)
    print(f"  ILL anchors loaded: {len(anchors)}")

    # Group anchors by issue, run the matcher to find unmatched
    by_issue = defaultdict(list)
    for a in anchors:
        by_issue[(a["vol"], a["iss"], a["year"])].append(a)

    unmatched = []
    for (vol, iss, year), aks in by_issue.items():
        items = fetch_year_cache(issn, year)
        if not items:
            for a in aks: unmatched.append(a)
            continue
        xref_issue = find_issue_works(items, vol, iss)
        for a in aks:
            if not is_matched(a, xref_issue):
                unmatched.append(a)

    print(f"  unmatched anchors (audit-matcher-reproduction): {len(unmatched)}")

    if limit:
        unmatched = unmatched[:limit]

    diagnoses = []
    for i, a in enumerate(unmatched, 1):
        d = classify_miss(a, issn)
        d["ill_vol"] = a["vol"]; d["ill_iss"] = a["iss"]; d["ill_year"] = a["year"]
        diagnoses.append(d)
        print(f"  [{i}/{len(unmatched)}] {d['verdict']:<18} title={d['title'][:60]!r}")

    return {
        "issn": issn,
        "bucket": bucket_label,
        "n_anchors": len(anchors),
        "n_unmatched": len(unmatched),
        "diagnoses": diagnoses,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-per-bucket", type=int, default=10,
                    help="cap unmatched anchors examined per bucket")
    args = ap.parse_args()

    # The 5 buckets ranked by miss-count, spread across journals.
    buckets = [
        ("0277-9536", "1990-1994"),  # Social Science & Medicine
        ("0264-0414", "2010-2014"),  # Journal of Sports Sciences
        ("0950-0693", "2005-2009"),  # Int. Journal of Science Education
        ("0014-0139", "2010-2014"),  # Ergonomics
        ("0161-2840", "2005-2009"),  # Issues in Mental Health Nursing
    ]

    results = []
    for issn, bucket in buckets:
        try:
            results.append(diagnose_bucket(issn, bucket, args.limit_per_bucket))
        except Exception as e:
            print(f"  FAIL {issn} {bucket}: {type(e).__name__}: {e}", file=sys.stderr)

    # Summary across all
    from collections import Counter
    verdict_counter = Counter()
    for r in results:
        for d in r["diagnoses"]:
            verdict_counter[d["verdict"]] += 1
    print(f"\n=== verdict summary across {len(results)} buckets ===")
    total = sum(verdict_counter.values())
    for v, n in verdict_counter.most_common():
        pct = (100 * n / total) if total else 0
        print(f"  {v:<22}: {n:>3}  ({pct:>4.1f}%)")

    OUT.write_text(json.dumps(results, indent=2))
    print(f"\nresults → {OUT}")


if __name__ == "__main__":
    main()
