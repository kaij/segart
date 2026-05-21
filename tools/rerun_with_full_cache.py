"""Local re-run of the heur_xref pipeline using the year-level full
Crossref cache instead of the bug-truncated `crossref_cache/*_full.json`.

Does not modify production code or the shared `crossref_cache/`. Writes
outputs to `tmp/tocs_fixed/` and `tmp/audit/pilot_fixed_*/`.

Usage:
  python3 tools/rerun_with_full_cache.py <ident> [<ident> ...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

SEGART = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SEGART))

# Point at the v2 cache so a fresh re-fetch (now no-type-filter) feeds
# both this script and articles_pilot from one source. The type-filter on
# line ~49 below stays — the TOC pipeline only wants article DOIs, not
# issue/volume deposits.
FULL_CACHE = SEGART / "tmp" / "crossref_full_cache_v2"
OUT_TOCS = SEGART / "tmp" / "tocs_fixed"
OUT_AUDIT = SEGART / "tmp" / "audit"
OUT_TOCS.mkdir(parents=True, exist_ok=True)


def fetch_from_full_cache(issn, year, vol, iss):
    """Replacement for heuristic_toc_crossref.fetch_crossref_full that
    reads from the year-level full cache instead of HTTP. Returns the
    same normalized shape (list of {doi, title, authors, page})."""
    try:
        y = int(str(year)[:4])
    except Exception:
        return None
    p = FULL_CACHE / f"{issn}_{y}.json"
    if not p.exists():
        print(f"  WARN: no full cache at {p.name}", file=sys.stderr)
        return None
    data = json.loads(p.read_text())
    items = data.get("items") if isinstance(data, dict) else data
    if not items:
        return None

    # Import the label-matching helper from the production module so we
    # use identical combined-issue logic ("21-22" matches "21").
    import heuristic_toc_crossref as htc
    out = []
    for r in items:
        if r.get("type") != "journal-article":
            continue
        v = str(r.get("volume", "")).strip()
        i = str(r.get("issue", "")).strip()
        if not htc._label_matches(v, vol) or not htc._label_matches(i, iss):
            continue
        ttl = r.get("title")
        if isinstance(ttl, list):
            ttl = ttl[0] if ttl else ""
        authors = []
        for a in r.get("author") or []:
            given = (a.get("given") or "").strip()
            family = (a.get("family") or "").strip()
            name = " ".join(p for p in (given, family) if p)
            if name:
                authors.append({"name": name})
        out.append({
            "doi": r.get("DOI"),
            "title": ttl,
            "authors": authors,
            "page": (r.get("page") or "").strip() or None,
        })
    return out


def run_one(ident):
    """Stage 1: heuristic_toc_crossref → tmp/tocs_fixed/{ident}_toc_heur_xref.json
    Stage 2: heur_xref_to_legacy → tmp/tocs_fixed/{ident}_toc.json"""
    import heuristic_toc_crossref as htc
    import heur_xref_to_legacy as hxl

    stage1_out = OUT_TOCS / f"{ident}_toc_heur_xref.json"
    stage2_out = OUT_TOCS / f"{ident}_toc.json"

    # ---- Stage 1
    print(f"\n=== {ident} (stage 1) ===")
    sys.argv = ["heuristic_toc_crossref.py", ident, "--out", str(stage1_out)]
    # Patch fetch_crossref_full and the cache-write line. We don't want
    # to clobber the production crossref_cache/*_full.json files.
    with patch.object(htc, "fetch_crossref_full", fetch_from_full_cache):
        # Intercept the cache-write: divert the per-issue cache write
        # to a parallel dir under tmp/tocs_fixed/.
        original_write_text = Path.write_text
        production_cache_dir = htc.CACHE_DIR
        def safe_write(self, *args, **kw):
            # Redirect any writes inside production cache dir to /dev/null-ish
            try:
                self.resolve().relative_to(production_cache_dir)
                return None
            except (ValueError, FileNotFoundError):
                return original_write_text(self, *args, **kw)
        with patch.object(Path, "write_text", safe_write):
            try:
                htc.main()
            except SystemExit as e:
                if e.code: raise

    # ---- Stage 2
    print(f"=== {ident} (stage 2) ===")
    sys.argv = ["heur_xref_to_legacy.py", str(stage1_out), "--out", str(stage2_out)]
    try:
        hxl.main()
    except SystemExit as e:
        if e.code: raise

    return stage2_out


if __name__ == "__main__":
    idents = sys.argv[1:] or []
    if not idents:
        # Default: the 24 calibration items
        idents = [
            'sim_biological-conservation_2010-11_143_11',
            'sim_biological-conservation_2010-04_143_4',
            'sim_biological-conservation_2010-03_143_3',
            'sim_marine-biology_1991-02_108_1',
            'sim_american-journal-of-sports-medicine_march-april-1989_17_2',
            'sim_biological-conservation_2004-11_120_1_0',
            'sim_personality-and-individual-differences_2002-04-05_32_5_0',
            'sim_clinics-in-perinatology_2010-12_37_4',
            'sim_biological-conservation_2004-08_118_5',
            'sim_biological-conservation_2010-12_143_12',
            'sim_biological-conservation_biological-conservation_2013-06_162',
            'sim_biological-conservation_1990_51_1',
            'sim_journal-american-academy-child-adolescent-psychiatry_2013-06_52_6',
            'sim_nursing-research_may-june-1991_40_3',
            'sim_ans_1978-10_1_1',
            'sim_international-journal-of-intercultural-relations-ijir_1985_9',
            'sim_journal-of-obstetric-gynecologic-neonatal-nursing-jognn_1995-09_24_7',
            'sim_journal-of-clinical-pharmacology_2002-11_42_11',
            'sim_acta-radiologica_2005-10_46_6',
            'sim_canadian-entomologist_1981-08_113_8',
            'sim_human-communication-research_2002-04_28_2',
            'sim_biological-conservation_2010-01_143_1',
            'sim_biological-conservation_2010-07_143_7',
            'sim_biological-conservation_biological-conservation_2013-10_166',
        ]

    failures = []
    for ident in idents:
        try:
            out = run_one(ident)
            print(f"  → wrote {out}")
        except Exception as e:
            print(f"  FAIL {ident}: {type(e).__name__}: {e}", file=sys.stderr)
            failures.append((ident, str(e)))

    print(f"\n{len(idents) - len(failures)} ok, {len(failures)} failed")
    for i, e in failures:
        print(f"  {i}: {e}")
