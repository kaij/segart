"""Tool implementations + JSON schemas for the Claude-driven ILL leaf-prediction
refinement layer (see tools/ill_lookup_refine.py).

Each tool takes (item: str, **inputs) and returns a JSON-serializable dict.
Item context (PageIndex, scandata, pn.json, hOCR, docling) is cached per item
across multiple tool calls on the same item via `_CTX_CACHE`.

The five tools mirror the five signal sources the refiner can consult:

  hocr_text                — plain-text hOCR for a leaf range
  docling_blocks           — structured docling page_header/title/section_header
                              blocks for a leaf range
  printed_pages_map        — translate printed page strings to BR leaf integers
                              (merges scandata assertions + pn.json + docling)
  scandata_printed_pagenumbers — per-leaf cataloger pageNumber from scandata.xml
                              <page> elements (different from <assertion> blocks)
  fetch_page_image         — JPEG bytes for one leaf, base64-encoded (Anthropic
                              vision input form). Last-resort use only.

TOOLS[] is the API-shape list to pass to messages.create(tools=...).
dispatch_tool(item, name, input_dict, ctx_cache=...) executes a tool call.
"""
from __future__ import annotations

import base64
import gzip
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

SEGART = Path("/Users/brewster/tmp/segart")
ITEMS = SEGART / "tmp" / "items"
sys.path.insert(0, str(SEGART))
sys.path.insert(0, str(SEGART / "tools"))

# Reuse ItemContext from ill_lookup — it already caches docling/hOCR/scandata.
from ill_lookup import ItemContext  # noqa: E402


# ---------------------------------------------------------------------------
# per-item context cache (one ItemContext per item, reused across tool calls)
# ---------------------------------------------------------------------------

def get_ctx(item: str, cache: dict | None = None) -> ItemContext:
    if cache is not None and item in cache:
        return cache[item]
    ctx = ItemContext(item)
    if cache is not None:
        cache[item] = ctx
    return ctx


# ---------------------------------------------------------------------------
# tool implementations
# ---------------------------------------------------------------------------

def hocr_text(ctx: ItemContext, leaf_lo: int, leaf_hi: int,
              max_chars_per_leaf: int = 2500) -> dict[str, Any]:
    """Return plain-text hOCR for leaves in [leaf_lo, leaf_hi]."""
    pix = ctx.pix
    if not pix:
        return {"error": "no_hocr_pageindex"}
    text = ctx.text or ""
    lo = max(0, int(leaf_lo))
    hi = min(len(pix) - 1, int(leaf_hi))
    if hi < lo:
        return {"error": f"invalid_range_lo>{hi}"}
    out = []
    for leaf in range(lo, hi + 1):
        s, e, *_ = pix[leaf]
        snippet = text[s:e]
        if max_chars_per_leaf and len(snippet) > max_chars_per_leaf:
            snippet = snippet[:max_chars_per_leaf] + "…[truncated]"
        out.append({"leaf": leaf, "text": snippet})
    return {"leaves": out}


def docling_blocks(ctx: ItemContext, leaf_lo: int, leaf_hi: int) -> dict[str, Any]:
    """Return structured docling blocks (headers + substantive text) per leaf
    in [leaf_lo, leaf_hi]. Excludes paragraph-content blocks for prompt
    efficiency — the model should call hocr_text for paragraph content."""
    doc = ctx.docling
    if not doc:
        return {"error": "no_docling"}
    keep_labels = {"page_header", "page_footer", "title", "section_header",
                   "paragraph_header"}
    lo, hi = int(leaf_lo), int(leaf_hi)
    # docling page_no is 1-indexed; BR leaf is 0-indexed.
    pg_lo, pg_hi = lo + 1, hi + 1
    by_leaf: dict[int, list[dict]] = {}
    for t in doc.get("texts") or []:
        if t.get("label") not in keep_labels:
            continue
        pr = (t.get("prov") or [{}])[0]
        pn = pr.get("page_no")
        if pn is None or pn < pg_lo or pn > pg_hi:
            continue
        txt = (t.get("text") or "").strip()
        if not txt:
            continue
        bbox = pr.get("bbox") or {}
        leaf = pn - 1
        by_leaf.setdefault(leaf, []).append({
            "label": t.get("label"),
            "text": txt[:200],
            "y_top": bbox.get("t"),
        })
    # Sort each leaf's blocks top-down (y_top descending — docling bbox is
    # BOTTOMLEFT, so higher y = higher on page).
    for leaf, blocks in by_leaf.items():
        blocks.sort(key=lambda b: -(b.get("y_top") or 0))
    return {
        "leaves": [
            {"leaf": leaf, "blocks": by_leaf.get(leaf, [])}
            for leaf in range(lo, hi + 1)
        ]
    }


def printed_pages_map(ctx: ItemContext, pages: list[str]) -> dict[str, Any]:
    """Look up each printed page string in ctx.printed_to_br (merged scandata
    assertions + pn.json + docling-derived map)."""
    pmap = ctx.printed_to_br or {}
    return {"map": {p: pmap.get(str(p)) for p in pages}}


def scandata_printed_pagenumbers(ctx: ItemContext, leaf_lo: int, leaf_hi: int) -> dict[str, Any]:
    """Per-leaf cataloger pageNumber from scandata.xml <page><pageNumber>
    elements (distinct from the <assertion> blocks ItemContext already merges).
    Reads scandata XML directly."""
    sd_path = ctx.paths.get("_scandata.xml")
    if not sd_path:
        return {"error": "no_scandata"}
    try:
        root = ET.parse(sd_path).getroot()
        pi = ctx.pi
    except Exception as e:
        return {"error": f"parse_failed:{type(e).__name__}"}
    lo, hi = int(leaf_lo), int(leaf_hi)
    out = []
    for pg in root.findall(".//page"):
        leaf_s = pg.get("leafNum")
        if leaf_s is None:
            continue
        try:
            sd_leaf = int(leaf_s)
        except ValueError:
            continue
        br = pi.scandata_to_br(sd_leaf)
        if br is None or br < lo or br > hi:
            continue
        pn_el = pg.find("pageNumber")
        printed = (pn_el.text or "").strip() if pn_el is not None else ""
        out.append({"leaf": br, "scandata_leaf": sd_leaf, "printed": printed or None})
    out.sort(key=lambda r: r["leaf"])
    return {"leaves": out}


def fetch_page_image(ctx: ItemContext, leaf: int) -> dict[str, Any]:
    """Fetch+cache one page image and return Anthropic vision-block content."""
    item = ctx.item
    cache_dir = ITEMS / item / "pages"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"page_{leaf}.jpg"
    if not cache_path.exists():
        url = f"https://archive.org/download/{item}/page/n{int(leaf)}_w400.jpg"
        try:
            req = Request(url, headers={"User-Agent": "segart-ill-refine/1.0"})
            with urlopen(req, timeout=30) as fh:
                data = fh.read()
            cache_path.write_bytes(data)
        except Exception as e:
            return {"error": f"fetch_failed:{type(e).__name__}:{str(e)[:80]}"}
    b64 = base64.b64encode(cache_path.read_bytes()).decode("ascii")
    return {
        "leaf": leaf,
        "image": {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        },
    }


# ---------------------------------------------------------------------------
# Anthropic-API-shaped tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "hocr_text",
        "description": (
            "Read plain-text hOCR (the IA-supplied full-text OCR) for a "
            "contiguous range of leaves. Useful for confirming what content "
            "lives on a specific leaf — the start of an article, a "
            "'References' header, the title of the next article, blank-page "
            "filler, etc. Returns one entry per leaf with up to "
            "max_chars_per_leaf characters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "leaf_lo": {"type": "integer", "description": "First BookReader leaf (0-indexed)"},
                "leaf_hi": {"type": "integer", "description": "Last BookReader leaf (inclusive)"},
                "max_chars_per_leaf": {
                    "type": "integer",
                    "description": "Cap on returned text per leaf (default 2500)",
                },
            },
            "required": ["leaf_lo", "leaf_hi"],
        },
    },
    {
        "name": "docling_blocks",
        "description": (
            "Get structured docling layout blocks (titles, section headers, "
            "page headers, page footers, paragraph headers) for a leaf range. "
            "Returns the labeled blocks per leaf, sorted top-down. Use this "
            "to detect article boundaries (a section_header at top of a leaf "
            "is usually a new article's title) and to read running headers "
            "and page numbers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "leaf_lo": {"type": "integer"},
                "leaf_hi": {"type": "integer"},
            },
            "required": ["leaf_lo", "leaf_hi"],
        },
    },
    {
        "name": "printed_pages_map",
        "description": (
            "Translate one or more printed page strings (e.g. \"141\", "
            "\"169\", \"152.e1\") into BookReader leaf integers using the "
            "merged scandata-assertions + pn.json + docling printed→leaf "
            "map. Returns null for pages that aren't in the map (e.g. "
            "Elsevier x.eN supplement pages, or pages on unprinted "
            "chapter-opens)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Printed page strings to look up",
                },
            },
            "required": ["pages"],
        },
    },
    {
        "name": "scandata_printed_pagenumbers",
        "description": (
            "Return per-leaf printed pageNumber from scandata.xml's <page> "
            "elements (the cataloger-supplied page numbers — generally more "
            "reliable than pn.json OCR, but missing on items where the "
            "cataloger didn't fill them in). Returns one entry per leaf in "
            "[leaf_lo, leaf_hi] for which scandata records the leaf, with "
            "the printed page string (possibly null/empty)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "leaf_lo": {"type": "integer"},
                "leaf_hi": {"type": "integer"},
            },
            "required": ["leaf_lo", "leaf_hi"],
        },
    },
    {
        "name": "fetch_page_image",
        "description": (
            "Last-resort vision input: fetch a 400-pixel-wide JPEG of one "
            "leaf from archive.org and pass it to you as an image block. "
            "Use this only when the other tools (hOCR, docling, scandata) "
            "leave the question unresolved. Returns one leaf at a time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "leaf": {"type": "integer"},
            },
            "required": ["leaf"],
        },
    },
]


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

_DISPATCH = {
    "hocr_text": hocr_text,
    "docling_blocks": docling_blocks,
    "printed_pages_map": printed_pages_map,
    "scandata_printed_pagenumbers": scandata_printed_pagenumbers,
    "fetch_page_image": fetch_page_image,
}


def dispatch_tool(item: str, name: str, inputs: dict,
                  ctx_cache: dict | None = None) -> Any:
    """Execute a tool call. Returns a JSON-serializable result OR an Anthropic
    content-block (for fetch_page_image, which returns an image block)."""
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown_tool:{name}"}
    ctx = get_ctx(item, ctx_cache)
    try:
        return fn(ctx, **inputs)
    except TypeError as e:
        return {"error": f"bad_inputs:{type(e).__name__}:{str(e)[:120]}"}
    except Exception as e:
        return {"error": f"exec_failed:{type(e).__name__}:{str(e)[:120]}"}


# ---------------------------------------------------------------------------
# smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("item")
    ap.add_argument("--tool", choices=list(_DISPATCH))
    ap.add_argument("--leaf-lo", type=int)
    ap.add_argument("--leaf-hi", type=int)
    ap.add_argument("--pages", nargs="*")
    ap.add_argument("--leaf", type=int)
    args = ap.parse_args()
    cache: dict[str, ItemContext] = {}
    if args.tool == "hocr_text":
        out = dispatch_tool(args.item, "hocr_text",
                            {"leaf_lo": args.leaf_lo, "leaf_hi": args.leaf_hi},
                            ctx_cache=cache)
    elif args.tool == "docling_blocks":
        out = dispatch_tool(args.item, "docling_blocks",
                            {"leaf_lo": args.leaf_lo, "leaf_hi": args.leaf_hi},
                            ctx_cache=cache)
    elif args.tool == "printed_pages_map":
        out = dispatch_tool(args.item, "printed_pages_map",
                            {"pages": args.pages or []}, ctx_cache=cache)
    elif args.tool == "scandata_printed_pagenumbers":
        out = dispatch_tool(args.item, "scandata_printed_pagenumbers",
                            {"leaf_lo": args.leaf_lo, "leaf_hi": args.leaf_hi},
                            ctx_cache=cache)
    elif args.tool == "fetch_page_image":
        out = dispatch_tool(args.item, "fetch_page_image",
                            {"leaf": args.leaf}, ctx_cache=cache)
        # don't print the base64
        if isinstance(out, dict) and "image" in out:
            data = out["image"]["source"]["data"]
            out = {**out, "image": {**out["image"],
                                    "source": {**out["image"]["source"],
                                               "data": f"<{len(data)} chars base64>"}}}
    else:
        out = {"tools": [t["name"] for t in TOOLS]}
    print(json.dumps(out, indent=2, default=str)[:4000])
