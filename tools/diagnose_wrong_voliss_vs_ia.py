"""For each `wrong_voliss` case from diagnose_near_clean_misses.py, check
whether Crossref's vol/iss for the article corresponds to an actual IA item
(i.e. would IA-metadata-driven retrieval find this article in some issue).

If yes → the audit's "miss" is just ILL metadata noise; production would
correctly assign the article.

If no → genuine vol/iss disagreement between Crossref and IA's catalog.
"""
from __future__ import annotations
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SEGART = Path("/Users/brewster/tmp/segart")
IN  = SEGART / "tmp" / "audit" / "near_clean_misses_diagnosed.json"
OUT = SEGART / "tmp" / "audit" / "wrong_voliss_vs_ia.json"
EMAIL = "brewster@archive.org"
UA = f"segart-diagnose/0.1 (mailto:{EMAIL})"


def ia_search_for_issue(issn: str, vol: str, iss: str, year: str) -> list[str]:
    """Search IA for items matching this (issn, vol, iss, year). Returns
    list of identifiers, empty if no match. Uses `ia search` CLI."""
    # IA's search syntax accepts field-level constraints. The mediatype
    # filter is loose — texts AND collections both possible.
    q_parts = [f'issn:"{issn}"']
    if vol: q_parts.append(f'volume:"{vol}"')
    if iss: q_parts.append(f'issue:"{iss}"')
    # Don't constrain year strictly — many items have date strings instead
    query = " AND ".join(q_parts)
    r = subprocess.run(
        ["ia", "search", query, "-p", "scope=all"],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line: continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        ident = d.get("identifier")
        if ident: out.append(ident)
    return out


def main():
    data = json.load(open(IN))
    cases = []
    for bucket in data:
        for d in bucket["diagnoses"]:
            if d["verdict"] != "wrong_voliss":
                continue
            cases.append({
                "issn":           bucket["issn"],
                "bucket":         bucket["bucket"],
                "title":          d["title"],
                "ill_vol":        d["ill_voliss"][0],
                "ill_iss":        d["ill_voliss"][1],
                "ill_year":       d.get("ill_year"),
                "crossref_vol":   d["crossref_voliss"][0],
                "crossref_iss":   d["crossref_voliss"][1],
                "crossref_doi":   d.get("crossref_doi"),
                "crossref_title": d.get("crossref_title"),
            })

    print(f"checking {len(cases)} wrong_voliss cases against IA catalog...\n")
    results = []
    counts = {"ia_finds_at_crossref_voliss": 0,
              "ia_finds_at_ill_voliss":      0,
              "ia_finds_neither":            0,
              "ia_finds_both":               0}
    for i, c in enumerate(cases, 1):
        ia_at_cr = ia_search_for_issue(c["issn"], c["crossref_vol"], c["crossref_iss"], c["ill_year"])
        ia_at_ill = ia_search_for_issue(c["issn"], c["ill_vol"], c["ill_iss"], c["ill_year"])
        c["ia_items_at_crossref_voliss"] = ia_at_cr
        c["ia_items_at_ill_voliss"]      = ia_at_ill

        has_cr = bool(ia_at_cr)
        has_ill = bool(ia_at_ill)
        if has_cr and has_ill: cat = "ia_finds_both"
        elif has_cr:           cat = "ia_finds_at_crossref_voliss"
        elif has_ill:          cat = "ia_finds_at_ill_voliss"
        else:                  cat = "ia_finds_neither"
        c["category"] = cat
        counts[cat] += 1

        # Display
        cr_sample = ia_at_cr[0] if ia_at_cr else "(none)"
        ill_sample = ia_at_ill[0] if ia_at_ill else "(none)"
        print(f"  [{i}/{len(cases)}] {c['issn']} {c['bucket']}  ill=({c['ill_vol']},{c['ill_iss']})  cr=({c['crossref_vol']},{c['crossref_iss']})")
        print(f"     title: {c['title'][:75]!r}")
        print(f"     IA@Crossref: {cr_sample}")
        print(f"     IA@ILL:      {ill_sample}")
        print(f"     → {cat}")
        results.append(c)

    print(f"\n=== summary ===")
    for cat, n in counts.items():
        pct = (100*n/len(cases)) if cases else 0
        print(f"  {cat:<35}: {n:>2}  ({pct:.1f}%)")

    OUT.write_text(json.dumps({"counts": counts, "cases": results}, indent=2))
    print(f"\nresults → {OUT}")


if __name__ == "__main__":
    main()
