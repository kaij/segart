#!/usr/bin/env python3
"""Extract journal issue TOC using Docling's document_index table cells.

Docling labels printed Tables of Contents as `document_index` tables. When
`do_table_structure=True` is active (as set by segment_issue_docling.py), the
text in those tables lives in `doc.tables[*].data.table_cells`, not in
`doc.texts`. This script parses those cells directly to extract article titles,
authors, and page numbers, then maps printed page numbers to BookReader nN
indices via PageIndex.

Usage:
    uv run extract_toc_docling.py <item> [-v] [--out PATH] [--min-entries N]
    uv run extract_toc_docling.py <item> --vlm [--vlm-model MODEL] [--vlm-api-url URL]

Output:
    <item>_toc.json  (or <item>_toc_docling.json if _toc.json already exists)
"""
import argparse
import base64
import gzip
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

CACHE = Path(os.environ.get("SEGART_CACHE", "/tmp/segart_items"))
SCHEMA_VERSION = 2
GENERATOR_VERSION = "0.1-docling-toc"

# Matches a standalone 1-4 digit page number
PAGE_RE = re.compile(r"^(\d{1,4})$")
# Splits "Title text by Author Name, Author2" on the " by " separator
BY_RE = re.compile(r"\s+by\s+", re.IGNORECASE)
# Matches TOC leader-dot lines: "Some Title ........ 42"
LEADER_LINE_RE = re.compile(r"^(.{10,}?)\s*[.\s]{3,}\s*(\d{1,4})\s*$")
# Match only when the text IS "Contents" / "Table of Contents" (not mid-sentence)
CONTENTS_RE = re.compile(r"^\s*(?:table\s+of\s+)?contents\s*$", re.IGNORECASE)
# Word that looks like title content: lowercase, 4+ chars
CONTENT_WORD_RE = re.compile(r"\b[a-z]{4,}\b")


# ---------------------------------------------------------------- cache load --

def load_docling_cache(item: str, cache_dir: Path) -> dict | None:
    """Load the Docling JSON cache as a raw dict. Returns None on miss or stale cache.

    Uses JSON directly (no Pydantic) since we only need read-only traversal.
    Returns None when the cache predates do_table_structure=True (detectable
    because all table_cells have empty text).
    """
    item_dir = cache_dir / item
    cache_gz = item_dir / f"{item}_docling.json.gz"
    cache_legacy = item_dir / f"{item}_docling.json"
    cache_src = cache_gz if cache_gz.exists() else (
        cache_legacy if cache_legacy.exists() else None
    )
    if cache_src is None:
        return None
    try:
        opener = gzip.open if cache_src.suffix == ".gz" else open
        with opener(cache_src, "rt", encoding="utf-8") as fh:
            d = json.load(fh)
        tables = d.get("tables") or []
        if tables:
            has_text = any(
                (c.get("text") or "").strip()
                for tbl in tables
                for c in ((tbl.get("data") or {}).get("table_cells") or [])
            )
            if not has_text:
                print("  cache predates do_table_structure; re-converting",
                      file=sys.stderr)
                return None
        return d
    except Exception as e:
        print(f"  cache load failed ({e}); will re-run docling", file=sys.stderr)
        return None


def save_docling_cache(item: str, cache_dir: Path, doc_obj) -> dict:
    """Serialize a DoclingDocument to gzip cache and return as raw dict."""
    item_dir = cache_dir / item
    item_dir.mkdir(parents=True, exist_ok=True)
    cache_gz = item_dir / f"{item}_docling.json.gz"
    json_str = doc_obj.model_dump_json()
    try:
        with gzip.open(cache_gz, "wt", encoding="utf-8") as fh:
            fh.write(json_str)
        print(f"  wrote docling cache {cache_gz.name}", file=sys.stderr)
    except Exception as e:
        print(f"  WARN: cache write failed: {e}", file=sys.stderr)
    return json.loads(json_str)


# --------------------------------------------------------------- page index --

def build_page_map(item: str, cache_dir: Path):
    """Return (printed_to_br, br_to_printed, pi) for an item.

    Downloads scandata and page_numbers.json if not already cached.
    """
    from segment_issue_docling import fetch_page_numbers, fetch_scandata
    from page_index import PageIndex

    pn_path = fetch_page_numbers(item, cache_dir)
    sd_path = fetch_scandata(item, cache_dir)
    pn_data = json.load(open(pn_path))
    pi = PageIndex.from_scandata_path(sd_path)
    return pi.printed_to_br(pn_data), pi.br_to_printed(pn_data), pi


def interpolate_br(printed_to_br: dict, printed_page_str: str) -> int | None:
    """Map a printed page string to a BR index, interpolating when absent.

    pn.json often omits front-of-issue pages (covers, TOC pages themselves).
    For digit-only strings, extrapolate linearly from the nearest known offset.
    Returns None for non-digit strings (Roman numerals, letter-prefixed pages).
    """
    direct = printed_to_br.get(printed_page_str)
    if direct is not None:
        return direct
    if not printed_page_str.isdigit():
        return None
    target = int(printed_page_str)
    known = [(int(k), v) for k, v in printed_to_br.items() if k.isdigit()]
    if not known:
        return None
    known.sort()
    nearest_pp, nearest_br = min(known, key=lambda x: abs(x[0] - target))
    return max(0, nearest_br + (target - nearest_pp))


# ---------------------------------------------------------- table cell parse --

def _cell_row_col(c: dict) -> tuple[int, int]:
    r = c.get("start_row_offset_idx") if c.get("start_row_offset_idx") is not None \
        else c.get("row_offset_idx", 0)
    col = c.get("start_col_offset_idx") if c.get("start_col_offset_idx") is not None \
        else c.get("col_offset_idx", 0)
    return r, col


def _parse_table_cells(tbl: dict, verbose: bool = False) -> tuple[list, list]:
    """Parse one document_index table into (anchored, unanchored) entry dicts.

    Column identification:
    - pg_col: rightmost column with the most PAGE_RE-matching cells
    - content_col: non-pg_col column with the most cells of len > 10

    anchored entries have: {title, authors_raw, printed_page (int), src_docling_page}
    unanchored entries have: {title, authors_raw, src_docling_page}
    """
    data = tbl.get("data") or {}
    cells = data.get("table_cells") or []
    prov = (tbl.get("prov") or [{}])[0]
    src_page = prov.get("page_no")

    # Build rows: row_idx -> {col_idx: text}
    rows: dict[int, dict[int, str]] = {}
    for c in cells:
        r, col = _cell_row_col(c)
        txt = (c.get("text") or "").strip()
        if r is None or not txt:
            continue
        rows.setdefault(r, {})[col] = txt

    if not rows:
        return [], []

    # Identify page column: rightmost col with most PAGE_RE hits
    col_page_hits: dict[int, int] = {}
    for row_data in rows.values():
        for col, txt in row_data.items():
            if PAGE_RE.match(txt):
                col_page_hits[col] = col_page_hits.get(col, 0) + 1

    if not col_page_hits:
        # No page-number column — extract title-containing cells as unanchored
        # so body search can locate their start pages.
        unanchored_no_pg = []
        seen_u: set = set()
        for r_idx in sorted(rows):
            for col in sorted(rows[r_idx]):
                txt = rows[r_idx][col]
                # Filter: need actual title content words (not author-only rows)
                if len(CONTENT_WORD_RE.findall(txt)) < 1:
                    continue
                if len(txt) < 15:
                    continue
                sig = re.sub(r"\s+", " ", txt.lower())[:40]
                if sig in seen_u:
                    continue
                seen_u.add(sig)
                unanchored_no_pg.append({
                    "title": txt.strip(),
                    "authors_raw": "",
                    "src_docling_page": src_page,
                })
        if verbose and unanchored_no_pg:
            print(f"    table p{src_page}: no page col, {len(unanchored_no_pg)} unanchored",
                  file=sys.stderr)
        return [], unanchored_no_pg

    # Among columns with max hits, pick the rightmost
    max_hits = max(col_page_hits.values())
    pg_col = max(c for c, h in col_page_hits.items() if h == max_hits)

    # Identify content column: non-pg_col with most cells having len > 10
    col_content_hits: dict[int, int] = {}
    for row_data in rows.values():
        for col, txt in row_data.items():
            if col != pg_col and len(txt) > 10:
                col_content_hits[col] = col_content_hits.get(col, 0) + 1

    if not col_content_hits:
        return [], []

    content_col = max(col_content_hits, key=lambda c: (col_content_hits[c], c))

    if verbose:
        print(f"    table p{src_page}: pg_col={pg_col} content_col={content_col} "
              f"rows={len(rows)}", file=sys.stderr)

    anchored = []
    unanchored = []

    for r_idx in sorted(rows):
        row_data = rows[r_idx]
        pg_txt = row_data.get(pg_col, "")
        content_txt = row_data.get(content_col, "")

        if not content_txt or len(content_txt) < 5:
            continue
        if not re.search(r"[A-Za-z]{3,}", content_txt):
            continue

        m = BY_RE.search(content_txt)
        if m:
            title = content_txt[:m.start()].strip()
            authors_raw = content_txt[m.end():].strip()
        else:
            title = content_txt.strip()
            authors_raw = ""

        if not title or len(title) < 5:
            continue

        if PAGE_RE.match(pg_txt):
            anchored.append({
                "title": title,
                "authors_raw": authors_raw,
                "printed_page": int(pg_txt),
                "src_docling_page": src_page,
            })
        else:
            unanchored.append({
                "title": title,
                "authors_raw": authors_raw,
                "src_docling_page": src_page,
            })

    return anchored, unanchored


# ------------------------------------------------ cluster scoring and select --

def extract_toc_from_tables(d: dict, min_entries: int = 3,
                             verbose: bool = False) -> tuple[list, list, set]:
    """Find the best document_index table cluster and extract TOC entries.

    Uses _cluster_pages / _monotonic_score / _has_contents_heading from
    recall_toc_seeded for cluster selection (same scoring as the text-based
    seeded pass), but parses table_cells instead of doc.texts.

    Returns (anchored, unanchored, toc_pages).
    """
    from recall_toc_seeded import (
        _cluster_pages, _monotonic_score, _has_contents_heading,
    )

    by_page: dict[int, list] = {}
    for t in d.get("texts", []):
        prov = (t.get("prov") or [{}])[0]
        pn = prov.get("page_no")
        if pn is not None:
            by_page.setdefault(pn, []).append(t)

    tables_by_page: dict[int, list] = {}
    for tbl in d.get("tables", []):
        if tbl.get("label") != "document_index":
            continue
        pn = (tbl.get("prov") or [{}])[0].get("page_no")
        if pn is not None:
            tables_by_page.setdefault(pn, []).append(tbl)

    if verbose:
        print(f"  document_index tables on pages: {sorted(tables_by_page)}", file=sys.stderr)

    runs = _cluster_pages(list(tables_by_page.keys()))
    if not runs:
        return [], [], set()

    best_run = None
    best_score = 0.0
    best_anchored: list = []
    best_unanchored: list = []

    for first, last in runs:
        run_pages = [p for p in tables_by_page if first <= p <= last]
        anchored: list = []
        unanchored: list = []
        for p in run_pages:
            for tbl in tables_by_page[p]:
                a, u = _parse_table_cells(tbl, verbose=verbose)
                anchored.extend(a)
                unanchored.extend(u)

        n_anchored = len(anchored)
        n_unanchored = len(unanchored)

        if n_anchored > 0:
            mono = _monotonic_score([e["printed_page"] for e in anchored])
        else:
            mono = 0.0

        heading_bonus = 5.0 if _has_contents_heading(by_page, run_pages) else 0.0
        # Unanchored-only clusters score at half weight (body search needed)
        score = n_anchored * (1.0 + mono) + (n_unanchored * 0.5 if n_anchored == 0 else 0) + heading_bonus

        if verbose:
            print(f"  cluster p{first}-{last}: {n_anchored} anchored "
                  f"{n_unanchored} unanchored mono={mono:.2f} score={score:.1f}",
                  file=sys.stderr)

        if score > best_score and (n_anchored >= min_entries or n_unanchored >= min_entries):
            best_score = score
            best_run = (first, last)
            best_anchored = anchored
            best_unanchored = unanchored

    if not best_run:
        return [], [], set()

    toc_pages = {p for p in tables_by_page if best_run[0] <= p <= best_run[1]}
    return best_anchored, best_unanchored, toc_pages


# ------------------------------------------------ text-based TOC fallback ---

def extract_toc_from_texts(d: dict, verbose: bool = False) -> tuple[list, set]:
    """Text-based TOC detection fallback when no document_index tables exist.

    Finds pages with leader-dot lines ("Title ........ 42") or an explicit
    'Contents' heading, then extracts title + page from each matching line.
    """
    by_page: dict[int, list] = {}
    for t in d.get("texts", []):
        prov = (t.get("prov") or [{}])[0]
        pn = prov.get("page_no")
        if pn is not None:
            by_page.setdefault(pn, []).append(t)

    toc_pages: set = set()
    for page, items in by_page.items():
        leader_hits = sum(
            1 for t in items
            if LEADER_LINE_RE.match((t.get("text") or "").strip())
        )
        has_heading = any(
            CONTENTS_RE.search((t.get("text") or "").strip())
            and len((t.get("text") or "")) <= 60
            for t in items
        )
        if leader_hits >= 3 or has_heading:
            toc_pages.add(page)

    if not toc_pages:
        return [], set()

    if verbose:
        print(f"  text-based TOC pages: {sorted(toc_pages)}", file=sys.stderr)

    anchored = []
    seen: set = set()
    for page in sorted(toc_pages):
        for t in by_page.get(page, []):
            raw = (t.get("text") or "").strip()
            m = LEADER_LINE_RE.match(raw)
            if not m:
                continue
            title_part = m.group(1).strip()
            printed_page = int(m.group(2))

            ma = BY_RE.search(title_part)
            if ma:
                title = title_part[:ma.start()].strip()
                authors_raw = title_part[ma.end():].strip()
            else:
                title = title_part
                authors_raw = ""

            sig = re.sub(r"\s+", " ", title.lower()).strip()[:40]
            if sig in seen:
                continue
            seen.add(sig)

            anchored.append({
                "title": title,
                "authors_raw": authors_raw,
                "printed_page": printed_page,
                "src_docling_page": page,
            })

    return anchored, toc_pages


# -------------------------------------------- body search for unanchored ----

_STOP = frozenset(
    "a an the of for in on to and or but with by at from as is are was were be that this if".split()
)
_WORD = re.compile(r"[a-z0-9]+")
_HEADER_LABELS = frozenset({"section_header", "title", "paragraph_header"})


def _norm(s: str) -> frozenset:
    return frozenset(w for w in _WORD.findall((s or "").lower())
                     if w not in _STOP and len(w) > 2)


def _find_in_body(d: dict, title: str, toc_pages: set,
                  exclude: set) -> tuple | None:
    """Body search with max-denominator overlap to avoid single-word false positives.

    Uses inter / max(|query|, |text|) so a 1-word text item can't score 1.0
    against a 12-word query. Returns (page_no, text_idx, matched_text) or None.
    """
    query_words = _norm(title)
    if not query_words:
        return None

    best = None
    for idx, t in enumerate(d.get("texts", [])):
        if t.get("content_layer") == "furniture":
            continue
        prov = (t.get("prov") or [{}])[0]
        page = prov.get("page_no")
        if page is None or page in toc_pages:
            continue
        if (page, idx) in exclude:
            continue
        label = str(t.get("label") or "")
        is_header = label in _HEADER_LABELS
        text = (t.get("text") or "").strip()
        text_words = _norm(text)
        if not text_words:
            continue

        inter = len(query_words & text_words)
        if inter == 0:
            continue

        # Use max denominator: prevents tiny text items from scoring 1.0
        overlap = inter / max(len(query_words), len(text_words))
        threshold = 0.5 if is_header else 0.7
        if overlap < threshold:
            continue

        score = (overlap, 1 if is_header else 0, -page)
        if best is None or score > best[0]:
            best = (score, page, idx, text)

    return (best[1], best[2], best[3]) if best else None


def locate_unanchored(d: dict, unanchored: list, toc_pages: set,
                      verbose: bool = False) -> list:
    """Find body pages for unanchored TOC entries using fuzzy title matching."""
    located = []
    exclude: set = set()
    for entry in unanchored:
        result = _find_in_body(d, entry["title"], toc_pages, exclude)
        if result is None:
            if verbose:
                print(f"  unanchored: no body match for {entry['title'][:50]!r}",
                      file=sys.stderr)
            continue
        page_no, text_idx, matched_text = result
        exclude.add((page_no, text_idx))
        new_entry = dict(entry)
        new_entry["src_docling_page"] = page_no
        new_entry["evidence_method"] = "body_search"
        if verbose:
            print(f"  unanchored body match: {entry['title'][:50]!r} -> p{page_no}",
                  file=sys.stderr)
        located.append(new_entry)

    return located


# ----------------------------------------------------------------- VLM path --

_VLM_SYSTEM = """\
Extract table of contents entries from journal page images.
Return ONLY valid JSON: {"entries": [{"title": "...", "authors": [{"name": "..."}], "page_number": <int or null>}]}
Rules:
- title: exact article title only, no author names
- authors: author names without credentials (Ph.D., R.N., M.D., Ed.D., M.A., M.S. etc.)
- page_number: printed page integer if visible, null if absent
- Skip: section headers, editorials without byline, advertisements, editorial-board tables
"""


def _render_toc_pages(item: str, cache_dir: Path, toc_pages: set) -> list:
    """Render Docling-detected TOC pages to 150 DPI JPEG.

    Returns list of (docling_page_no, Path) tuples.
    Caches renders so repeat runs skip re-rendering.
    """
    import pymupdf
    from segment_issue_docling import fetch_pdf

    pdf = fetch_pdf(item, cache_dir)
    pages_dir = cache_dir / item / "pages"
    pages_dir.mkdir(exist_ok=True)
    doc = pymupdf.open(str(pdf))
    out = []
    try:
        for pg in sorted(toc_pages):  # pg is 1-indexed Docling page
            path = pages_dir / f"toc_p{pg:04d}.jpg"
            if not path.exists():
                page = doc[pg - 1]  # 0-indexed in PyMuPDF
                mat = pymupdf.Matrix(150 / 72, 150 / 72)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                pix.save(str(path), output="jpeg", jpg_quality=85)
            out.append((pg, path))
    finally:
        doc.close()
    return out


def _call_vlm(image_paths: list, model: str, api_base_url: str,
              api_key: str | None = None, verbose: bool = False) -> list:
    """Send TOC page images to a local OpenAI-compatible VLM and return raw entry dicts."""
    from openai import OpenAI

    client = OpenAI(base_url=api_base_url, api_key=api_key or "no-key")

    content = []
    for _, path in image_paths:
        data = base64.standard_b64encode(path.read_bytes()).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{data}"},
        })
    content.append({"type": "text", "text": "Extract the table of contents entries."})

    resp = client.chat.completions.create(
        model=model,
        max_tokens=2048,
        messages=[
            {"role": "system", "content": _VLM_SYSTEM},
            {"role": "user", "content": content},
        ],
    )
    raw = (resp.choices[0].message.content or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    if verbose:
        print(f"  VLM raw: {raw[:300]!r}", file=sys.stderr)
    try:
        return json.loads(raw).get("entries", [])
    except json.JSONDecodeError as e:
        print(f"  WARN: VLM returned invalid JSON: {e}", file=sys.stderr)
        return []


def vlm_extract_toc_pages(d: dict, toc_pages: set, item: str, cache_dir: Path,
                           model: str, api_base_url: str, api_key: str | None = None,
                           verbose: bool = False) -> tuple[list, list]:
    """Render TOC pages, call VLM, split into (anchored, unanchored).

    VLM entries carry _evidence and _confidence fields for assemble_entries().
    """
    image_paths = _render_toc_pages(item, cache_dir, toc_pages)
    if not image_paths:
        return [], []
    if verbose:
        print(f"  VLM: sending {len(image_paths)} TOC page(s) to {model}",
              file=sys.stderr)

    raw = _call_vlm(image_paths, model=model, api_base_url=api_base_url,
                    api_key=api_key, verbose=verbose)

    anchored, unanchored = [], []
    first_page = image_paths[0][0] if image_paths else None

    for e in raw:
        title = (e.get("title") or "").strip()
        if not title:
            continue
        authors_raw = ", ".join(
            a.get("name", "") for a in (e.get("authors") or [])
        )
        pn = e.get("page_number")
        rec = {
            "title": title,
            "authors_raw": authors_raw,
            "src_docling_page": first_page,
            "_evidence": ["docling_toc", "vlm"],
            "_confidence": 0.90,
        }
        if isinstance(pn, int):
            rec["printed_page"] = pn
            anchored.append(rec)
        else:
            unanchored.append(rec)

    if verbose:
        print(f"  VLM: {len(anchored)} anchored, {len(unanchored)} unanchored",
              file=sys.stderr)
    return anchored, unanchored


# ----------------------------------------------------------- author parsing --

def parse_toc_authors(authors_raw: str) -> list:
    """Parse TOC author string into [{name: str}] list.

    Wraps segment_issue_docling.parse_authors() and filters out credential
    suffixes that slip through (e.g. "M.R.P", "R.D") — a real name needs
    at least one word of 3+ chars that isn't all-caps abbreviation.
    """
    if not authors_raw:
        return []
    from segment_issue_docling import parse_authors
    raw = parse_authors(authors_raw)
    result = []
    for a in raw:
        name = a.get("name", "")
        words = name.split()
        has_real_word = any(
            len(w) >= 3
            and w[0].isupper()
            and not all(c in ".ABCDEFGHIJKLMNOPQRSTUVWXYZ" for c in w)
            for w in words
        )
        if has_real_word:
            result.append({"name": name})
    return result


# --------------------------------------------------- assemble final entries --

def assemble_entries(anchored: list, located_unanchored: list,
                     printed_to_br: dict, br_to_printed: dict,
                     visible_count: int, verbose: bool = False) -> list:
    """Merge and sort entries, assign end-page ranges, build schema-v2 dicts."""
    raw: list = []

    for e in anchored:
        start_br = interpolate_br(printed_to_br, str(e["printed_page"]))
        if start_br is None:
            if verbose:
                print(f"  drop (no BR map): {e['title'][:50]!r} p{e['printed_page']}",
                      file=sys.stderr)
            continue
        raw.append({
            "title": e["title"],
            "authors": parse_toc_authors(e["authors_raw"]),
            "start_br": start_br,
            "start_pp": str(e["printed_page"]),
            "confidence": e.get("_confidence", 0.85),
            "evidence": e.get("_evidence", ["docling_toc"]),
        })

    for e in located_unanchored:
        doc_page = e.get("src_docling_page")
        if doc_page is None:
            continue
        start_br = max(0, doc_page - 1)  # Docling 1-indexed → BR 0-indexed (approx)
        base_ev = e.get("_evidence", ["docling_toc"])
        evidence = base_ev + ["body_search"] if "body_search" not in base_ev else base_ev
        raw.append({
            "title": e["title"],
            "authors": parse_toc_authors(e["authors_raw"]),
            "start_br": start_br,
            "start_pp": br_to_printed.get(start_br),
            "confidence": e.get("_confidence", 0.65),
            "evidence": evidence,
        })

    # Deduplicate on normalized title prefix
    seen_titles: set = set()
    deduped = []
    for e in raw:
        sig = re.sub(r"\s+", " ", e["title"].lower()).strip()[:50]
        if sig not in seen_titles:
            seen_titles.add(sig)
            deduped.append(e)

    deduped.sort(key=lambda e: e["start_br"])

    # Deduplicate by start_br (continuation rows from same article map to same page)
    seen_br: set = set()
    deduped2 = []
    for e in deduped:
        if e["start_br"] not in seen_br:
            seen_br.add(e["start_br"])
            deduped2.append(e)
    deduped = deduped2

    final = []
    for i, e in enumerate(deduped):
        start_br = e["start_br"]
        end_br = deduped[i + 1]["start_br"] - 1 if i + 1 < len(deduped) \
            else max(start_br, visible_count - 1)

        start_pp = e["start_pp"]
        end_pp = br_to_printed.get(end_br)
        printed_pages = [[start_pp, end_pp]] if (start_pp or end_pp) else None

        final.append({
            "id": f"e{i + 1}",
            "type": "article",
            "title": e["title"],
            "subtitle": None,
            "authors": e["authors"] or None,
            "page_index_ranges": [[f"n{start_br}", f"n{end_br}"]],
            "printed_pages": printed_pages,
            "ext_ids": {},
            "confidence": e["confidence"],
            "evidence": e["evidence"],
            "level": 1,
            "label": None,
        })

    return final


# ----------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("item", help="IA item identifier")
    p.add_argument("-o", "--out", default=None,
                   help="Output path (default: <item>_toc.json or _toc_docling.json)")
    p.add_argument("--cache-dir", default=str(CACHE))
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--min-entries", type=int, default=3,
                   help="Minimum anchored entries to accept a TOC cluster (default 3)")
    p.add_argument("--device", choices=("mps", "cpu"), default="mps",
                   help="Docling accelerator for cache-miss conversion")
    p.add_argument("--vlm", action="store_true",
                   help="Use local VLM for TOC extraction via OpenAI-compatible endpoint")
    p.add_argument("--vlm-model", default="granite-vision-4.1", metavar="MODEL",
                   help="VLM model name (default: granite-vision-4.1)")
    p.add_argument("--vlm-api-url",
                   default=os.environ.get("VLM_API_BASE_URL", "http://localhost:8000/v1"),
                   metavar="URL", help="OpenAI-compatible API base URL (default: http://localhost:8000/v1)")
    p.add_argument("--vlm-api-key",
                   default=os.environ.get("VLM_API_KEY") or os.environ.get("OPENAI_API_KEY"),
                   metavar="KEY", help="API key (default: VLM_API_KEY or OPENAI_API_KEY env var)")
    args = p.parse_args()

    cache_dir = Path(args.cache_dir)

    # 1. Load or build Docling cache
    d = load_docling_cache(args.item, cache_dir)
    if d is None:
        from segment_issue_docling import fetch_pdf, docling_convert
        import time
        pdf = fetch_pdf(args.item, cache_dir)
        print(f"converting {pdf} via Docling...", file=sys.stderr, flush=True)
        t0 = time.time()
        doc_obj = docling_convert(pdf, device=args.device)
        print(f"  conversion took {time.time() - t0:.1f}s", file=sys.stderr)
        d = save_docling_cache(args.item, cache_dir, doc_obj)

    # 2. Build page index maps
    printed_to_br, br_to_printed, pi = build_page_map(args.item, cache_dir)
    visible_count = pi.visible_count

    if args.verbose:
        n_doc_idx = sum(1 for t in d.get("tables", []) if t.get("label") == "document_index")
        print(f"  docling tables (document_index): {n_doc_idx}", file=sys.stderr)
        print(f"  visible_count: {visible_count}", file=sys.stderr)

    # 3. Primary: document_index table cells
    anchored, unanchored, toc_pages = extract_toc_from_tables(
        d, min_entries=args.min_entries, verbose=args.verbose)

    # 4. Fallback: leader-dot text detection — only when the table approach
    # found nothing at all (not even unanchored entries), since some TOCs
    # have no printed page numbers (the unanchored entries will be resolved
    # via body search instead).
    if len(anchored) < args.min_entries and len(unanchored) < args.min_entries:
        if args.verbose:
            print(f"  table extraction gave nothing — trying text fallback",
                  file=sys.stderr)
        text_anchored, toc_pages_text = extract_toc_from_texts(d, verbose=args.verbose)
        if len(text_anchored) >= args.min_entries:
            anchored = text_anchored
            toc_pages = toc_pages_text
            unanchored = []

    if not anchored and not unanchored:
        print(f"ERROR: no TOC detected for {args.item}", file=sys.stderr)
        sys.exit(1)

    # 5. Optional VLM re-extraction (replaces table-cell path for title/author quality)
    if args.vlm and toc_pages:
        anchored, unanchored = vlm_extract_toc_pages(
            d, toc_pages, args.item, cache_dir,
            model=args.vlm_model, api_base_url=args.vlm_api_url,
            api_key=args.vlm_api_key, verbose=args.verbose)
        if not anchored and not unanchored:
            print("  WARN: VLM returned no entries; falling back to table-cell results",
                  file=sys.stderr)
            # re-run table extraction to restore original anchored/unanchored
            anchored, unanchored, _ = extract_toc_from_tables(
                d, min_entries=args.min_entries, verbose=False)

    if args.verbose:
        print(f"  anchored: {len(anchored)}, unanchored (for body search): {len(unanchored)}",
              file=sys.stderr)

    # 6. Body-search for unanchored entries
    located = locate_unanchored(d, unanchored, toc_pages, verbose=args.verbose) \
        if unanchored else []

    # 7. Assemble final entries
    entries = assemble_entries(anchored, located, printed_to_br, br_to_printed,
                               visible_count, verbose=args.verbose)

    # 8. Build output
    toc = {
        "schema_version": SCHEMA_VERSION,
        "item": args.item,
        "pub_collection": None,
        "container_id": None,
        "issn": None,
        "journal_title": None,
        "volume": None,
        "issue": None,
        "issue_date": None,
        "year": None,
        "page_index_count": visible_count,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": {
            "name": "segart",
            "version": GENERATOR_VERSION,
            "method": "docling-toc",
        },
        "entries": entries,
    }

    # 9. Determine output path
    if args.out:
        out_path = Path(args.out)
    else:
        default = Path(f"{args.item}_toc.json")
        if default.exists():
            out_path = Path(f"{args.item}_toc_docling.json")
            print(f"  {default.name} exists; writing to {out_path.name}", file=sys.stderr)
        else:
            out_path = default

    with open(out_path, "w") as f:
        json.dump(toc, f, indent=2)
    print(f"wrote {out_path}: {len(entries)} entries", file=sys.stderr)


if __name__ == "__main__":
    main()
