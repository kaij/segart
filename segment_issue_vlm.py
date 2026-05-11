#!/usr/bin/env python3
"""segart segmenter v0.1-vlm — VLM-based article segmentation.

Renders each PDF page to a JPEG, sends it to a Vision Language Model via
an OpenAI-compatible API endpoint, then reconciles the per-page results
into a segart v2 TOC using a second LLM call.

Supports any OpenAI-compatible provider: vLLM, OpenRouter, Gemini, etc.
First-run for an item downloads the PDF from IA (~5-50 MB); subsequent
runs skip image rendering and VLM calls for pages already cached.

Usage:
  ./segment_issue_vlm.py sim_academic-medicine_1989-02_64_2 \\
      --api-base-url https://openrouter.ai/api/v1 \\
      --model google/gemini-2.0-flash -v
  ./segment_issue_vlm.py <item> -o /tmp/foo.json
"""

import argparse
import base64
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CACHE = Path(os.environ.get("SEGART_CACHE", "/tmp/segart_items"))
SCHEMA_VERSION = 2
SEGMENTER_VERSION = "0.1-vlm"
DEFAULT_DPI = 200
DEFAULT_MAX_DIM = 1200
# DEFAULT_MODEL = "google/gemini-2.0-flash"
DEFAULT_MODEL = "google/gemini-3.1-flash-lite-preview"

_SCRIPT_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# Shared download helpers (same as segment_issue_docling.py)
# ---------------------------------------------------------------------------


def fetch_pdf(item, cache_dir):
    item_dir = cache_dir / item
    item_dir.mkdir(parents=True, exist_ok=True)
    pdf = item_dir / f"{item}.pdf"
    if not pdf.exists():
        subprocess.run(
            ["ia", "download", item, "--glob", "*.pdf", "--destdir", str(cache_dir)],
            check=True,
            capture_output=True,
        )
    return pdf


def fetch_page_numbers(item, cache_dir):
    item_dir = cache_dir / item
    pn = item_dir / f"{item}_page_numbers.json"
    if not pn.exists():
        subprocess.run(
            [
                "ia",
                "download",
                item,
                "--glob",
                "*page_numbers.json",
                "--destdir",
                str(cache_dir),
            ],
            check=True,
            capture_output=True,
        )
    return pn


def fetch_scandata(item, cache_dir):
    item_dir = cache_dir / item
    sd = item_dir / f"{item}_scandata.xml"
    if not sd.exists():
        subprocess.run(
            [
                "ia",
                "download",
                item,
                "--glob",
                "*scandata.xml",
                "--destdir",
                str(cache_dir),
            ],
            check=True,
            capture_output=True,
        )
    return sd


def page_index_to_printed_map(pn_path, scandata_path):
    from page_index import PageIndex

    pn_data = json.load(open(pn_path))
    pi = PageIndex.from_scandata_path(scandata_path)
    return pi.br_to_printed(pn_data), pn_data, pi


# ---------------------------------------------------------------------------
# PDF → page images
# ---------------------------------------------------------------------------


def render_page_images(pdf_path, item_dir, dpi, max_dim, verbose=False):
    """Render each PDF page to a preprocessed JPEG; return [(page_1indexed, Path)].

    Skips pages whose JPEG cache already exists (resumable).
    """
    import pymupdf
    from image_utils import optimize_microfilm, save_image

    pages_dir = item_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    doc = pymupdf.open(str(pdf_path))
    result = []
    for i in range(len(doc)):
        pdf_page = i + 1
        jpg_path = pages_dir / f"page_{pdf_page:04d}.jpg"
        if not jpg_path.exists():
            page = doc[i]
            mat = pymupdf.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            from PIL import Image

            img = Image.open(io.BytesIO(pix.tobytes("jpeg")))
            img = optimize_microfilm(img, max_dim)
            save_image(img, jpg_path, fmt="jpeg", quality=85)
            if verbose:
                print(f"  rendered page {pdf_page}/{len(doc)}", file=sys.stderr)
        result.append((pdf_page, jpg_path))
    doc.close()
    return result


# ---------------------------------------------------------------------------
# VLM API
# ---------------------------------------------------------------------------


def _strip_json_fences(text):
    """Remove markdown code fences if the model wrapped its JSON output."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _make_client(api_base_url, api_key):
    from openai import OpenAI

    return OpenAI(base_url=api_base_url, api_key=api_key or "no-key")


def call_vlm(image_path, prompt_text, client, model, verbose=False):
    """Send one page image to the VLM; return parsed JSON dict."""
    with open(image_path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()

    raw = None
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        },
                        {"type": "text", "text": prompt_text},
                    ],
                }
            ],
            max_tokens=1024,
        )
        raw = resp.choices[0].message.content or ""
        if verbose:
            print(f"    raw response: {raw[:200]!r}", file=sys.stderr)
        return json.loads(_strip_json_fences(raw))
    except json.JSONDecodeError as e:
        print(f"  WARN: JSON parse error: {e}", file=sys.stderr)
        print(f"  WARN: raw response was: {raw!r}", file=sys.stderr)
        return {
            "category": "unknown",
            "error": f"json_parse: {e}",
            "raw_response": raw,
            "articles": [],
            "table_of_contents": [],
        }
    except Exception as e:
        # API-level error (auth, rate limit, model not found, network, …)
        err_type = type(e).__name__
        print(f"  WARN: VLM API error ({err_type}): {e}", file=sys.stderr)
        return {
            "category": "unknown",
            "error": f"{err_type}: {e}",
            "articles": [],
            "table_of_contents": [],
        }


def process_pages(pages, prompt_text, client, model, item_dir, verbose=False):
    """Call VLM for every page; cache per-page JSON; return all results."""
    vlm_dir = item_dir / "vlm_pages"
    vlm_dir.mkdir(parents=True, exist_ok=True)
    results = []
    errors = 0
    for pdf_page, jpg_path in pages:
        cache = vlm_dir / f"page_{pdf_page:04d}_vlm.json"
        if cache.exists():
            data = json.loads(cache.read_text())
            if verbose:
                cat = data.get("category", "?")
                err = data.get("error")
                suffix = f" [cached, ERROR: {err}]" if err else f" [cached, {cat}]"
                print(f"  page {pdf_page}{suffix}", file=sys.stderr)
        else:
            print(
                f"  VLM page {pdf_page}/{len(pages)} ({jpg_path.name}) …",
                file=sys.stderr,
                flush=True,
            )
            data = call_vlm(jpg_path, prompt_text, client, model, verbose=verbose)
            data["pdf_page"] = pdf_page
            if data.get("error"):
                errors += 1
                # Don't cache errors — allow a re-run to retry failed pages.
                if verbose:
                    print(
                        f"    not caching error result for page {pdf_page}",
                        file=sys.stderr,
                    )
            else:
                cache.write_text(json.dumps(data, indent=2))
            cat = data.get("category", "unknown")
            err = data.get("error")
            if err:
                print(f"    → ERROR: {err}", file=sys.stderr)
            elif verbose:
                print(f"    → {cat}", file=sys.stderr)
        data.setdefault("pdf_page", pdf_page)
        results.append(data)
    if errors:
        print(
            f"  WARN: {errors}/{len(pages)} pages returned errors (re-run to retry)",
            file=sys.stderr,
        )
    return results


# ---------------------------------------------------------------------------
# Postprocessing step 1 — collect TOC entries and article starts
# ---------------------------------------------------------------------------


def collect_toc_and_articles(page_results):
    """Split page results into TOC entries and article-start records."""
    toc_entries = []
    article_starts = []

    for res in page_results:
        pdf_page = res.get("pdf_page")
        category = (res.get("category") or "").lower().strip()

        if category == "table_of_contents":
            for entry in res.get("table_of_contents") or []:
                if entry.get("title"):
                    toc_entries.append(
                        {
                            "title": entry["title"],
                            "page": entry.get("page") or "n/a",
                        }
                    )

        if category == "article_title_start":
            for art in res.get("articles") or []:
                if art.get("title"):
                    article_starts.append(
                        {
                            "pdf_page": pdf_page,
                            "title": art["title"],
                            "authors": art.get("authors"),
                            "doi": art.get("doi"),
                            "printed_page": art.get("printed_page") or res.get("page"),
                        }
                    )

    return toc_entries, article_starts


# ---------------------------------------------------------------------------
# Postprocessing step 2 — LLM TOC matcher
# ---------------------------------------------------------------------------


def match_with_llm(toc_entries, article_starts, matcher_template, client, model):
    """Ask the LLM to merge TOC entries with article starts; return entry list."""
    if not toc_entries:
        # No TOC found — return article_starts as skeleton entries directly.
        out = []
        for a in article_starts:
            authors_raw = a.get("authors") or ""
            authors = _parse_authors_string(authors_raw)
            out.append(
                {
                    "pdf_page": a["pdf_page"],
                    "title": a["title"],
                    "authors": authors,
                    "type": "article",
                    "doi": a.get("doi"),
                }
            )
        return out

    prompt = matcher_template.replace(
        "<toc>", json.dumps(toc_entries, indent=2)
    ).replace("<articles>", json.dumps(article_starts, indent=2))
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4096,
        )
        raw = resp.choices[0].message.content or ""
        entries = json.loads(_strip_json_fences(raw))
        if not isinstance(entries, list):
            raise ValueError("matcher did not return a JSON array")
        return entries
    except Exception as e:
        print(
            f"  WARN: matcher LLM call failed ({e}); falling back to raw article starts",
            file=sys.stderr,
        )
        return [
            {
                "pdf_page": a["pdf_page"],
                "title": a["title"],
                "authors": _parse_authors_string(a.get("authors") or ""),
                "type": "article",
                "doi": a.get("doi"),
            }
            for a in article_starts
        ]


def _parse_authors_string(authors_raw):
    """Convert a comma/and-separated author string to [{name: ...}] list."""
    if not authors_raw or not isinstance(authors_raw, str):
        return []
    cred_re = re.compile(
        r"\b(?:M\.?\s*D\.?|Ph\.?\s*D\.?|MA|MS|MPH|MSc|R\.?\s*N\.?|D\.?\s*O\.?|"
        r"RD|PhD|Sc\.?\s*D\.?|Jr\.?|Sr\.?|II|III|FRCP|FRCS|FACP|MHA|EdD|DSc)\.?\b",
        re.IGNORECASE,
    )
    body = re.sub(r"^by\s+", "", authors_raw, flags=re.IGNORECASE).strip()
    parts = re.split(r"\s*,\s*|\s+and\s+|\s*&\s*|\s*;\s*", body)
    out = []
    for part in parts:
        part = re.sub(r"^(?:and|or|&)\s+", "", part, flags=re.IGNORECASE)
        name = cred_re.sub("", part).strip(" ,.;-")
        if name and len(name) > 1 and any(c.isalpha() for c in name):
            if name.isupper():
                name = name.title()
            out.append({"name": name})
    return out


# ---------------------------------------------------------------------------
# Build final segart v2 TOC
# ---------------------------------------------------------------------------


def build_toc(matched_entries, leaf_to_page, leaf_count, item):
    # Sort by pdf_page; deduplicate on (pdf_page, title).
    seen = set()
    deduped = []
    for e in sorted(matched_entries, key=lambda x: x.get("pdf_page", 0)):
        key = (e.get("pdf_page"), (e.get("title") or "").lower())
        if key not in seen:
            seen.add(key)
            deduped.append(e)

    entries = []
    for i, e in enumerate(deduped):
        start_leaf = e["pdf_page"] - 1
        if i + 1 < len(deduped):
            end_leaf = deduped[i + 1]["pdf_page"] - 2
        else:
            end_leaf = max(leaf_count - 1, start_leaf)
        if end_leaf < start_leaf:
            end_leaf = start_leaf

        start_pp = leaf_to_page.get(start_leaf)
        end_pp = leaf_to_page.get(end_leaf)
        if start_pp is None and end_pp is None:
            printed_pages = None
        else:
            printed_pages = [[start_pp, end_pp if end_pp is not None else start_pp]]

        authors = e.get("authors") or []
        if isinstance(authors, list) and authors and isinstance(authors[0], str):
            # Normalise if LLM returned plain strings instead of {name: ...} dicts
            authors = [{"name": a} for a in authors if a]

        entries.append(
            {
                "id": f"e{i + 1}",
                "type": e.get("type") or "article",
                "title": e.get("title") or "",
                "authors": authors if authors else None,
                "page_index_ranges": [[f"n{start_leaf}", f"n{end_leaf}"]],
                "printed_pages": printed_pages,
                "ext_ids": {"doi": e["doi"]} if e.get("doi") else {},
                "confidence": 0.6,
                "evidence": ["vlm"],
                "level": 1,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "item": item,
        "page_index_count": leaf_count,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": {
            "name": "segart",
            "version": SEGMENTER_VERSION,
            "method": "vlm-page-classification",
        },
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("item", help="IA item identifier")
    p.add_argument("-o", "--output", help="Output path (default <item>_toc.json)")
    p.add_argument("--raw-output", help="Dump aggregated per-page VLM results")
    p.add_argument("--cache-dir", default=str(CACHE))
    p.add_argument("--keep-pdf", action="store_true")
    p.add_argument(
        "--dpi",
        type=int,
        default=DEFAULT_DPI,
        help=f"Page render resolution in DPI (default {DEFAULT_DPI})",
    )
    p.add_argument(
        "--max-dim",
        type=int,
        default=DEFAULT_MAX_DIM,
        help=f"Max image dimension in pixels (default {DEFAULT_MAX_DIM})",
    )
    p.add_argument(
        "--api-base-url",
        default=os.environ.get("VLM_API_BASE_URL", "https://openrouter.ai/api/v1"),
    )
    p.add_argument(
        "--api-key",
        default=os.environ.get("VLM_API_KEY") or os.environ.get("OPENAI_API_KEY"),
    )
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"VLM model for page processing (default {DEFAULT_MODEL})",
    )
    p.add_argument(
        "--matcher-model",
        help="LLM model for TOC matching step (default: same as --model)",
    )
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    matcher_model = args.matcher_model or args.model
    cache_dir = Path(args.cache_dir)
    item_dir = cache_dir / args.item

    pn = fetch_page_numbers(args.item, cache_dir)
    sd = fetch_scandata(args.item, cache_dir)
    leaf_to_page, _pn_data, pi = page_index_to_printed_map(pn, sd)
    leaf_count = pi.visible_count

    # Render page images — need the PDF for this step
    pages_dir = item_dir / "pages"
    pages_cached = sorted(pages_dir.glob("page_*.jpg")) if pages_dir.exists() else []

    pdf = None
    if not pages_cached:
        pdf = fetch_pdf(args.item, cache_dir)
        print(
            f"  rendering {pdf.name} at {args.dpi} dpi …", file=sys.stderr, flush=True
        )
    else:
        # Might still need the PDF if page count changed; use cached images.
        pdf_candidate = item_dir / f"{args.item}.pdf"
        pdf = pdf_candidate if pdf_candidate.exists() else None

    if pdf is not None and pdf.exists():
        pages = render_page_images(pdf, item_dir, args.dpi, args.max_dim, args.verbose)
    else:
        # Rebuild list from cached JPEGs
        pages = []
        for jpg in sorted(pages_dir.glob("page_*.jpg")):
            m = re.match(r"page_(\d+)\.jpg", jpg.name)
            if m:
                pages.append((int(m.group(1)), jpg))

    if not pages:
        print("ERROR: no pages to process", file=sys.stderr)
        sys.exit(1)

    print(
        f"  processing {len(pages)} pages via VLM ({args.model}) …",
        file=sys.stderr,
        flush=True,
    )

    vlm_prompt = (_SCRIPT_DIR / "etc" / "vlm-segmentation-prompt.txt").read_text()
    client = _make_client(args.api_base_url, args.api_key)
    page_results = process_pages(
        pages, vlm_prompt, client, args.model, item_dir, args.verbose
    )

    # Write aggregated debug dump
    all_out = item_dir / f"{args.item}_vlm_all.json"
    all_out.write_text(
        json.dumps(
            {
                "item": args.item,
                "generator_version": SEGMENTER_VERSION,
                "model": args.model,
                "page_results": page_results,
            },
            indent=2,
        )
    )
    if args.verbose:
        print(f"  wrote {all_out.name}", file=sys.stderr)

    toc_entries, article_starts = collect_toc_and_articles(page_results)
    print(
        f"  found {len(toc_entries)} TOC entries, {len(article_starts)} article starts",
        file=sys.stderr,
    )

    if not article_starts:
        print(
            "  WARN: no article_title_start pages found; producing empty TOC",
            file=sys.stderr,
        )
        article_starts = []

    matcher_template = (_SCRIPT_DIR / "etc" / "llm-toc-matcher.txt").read_text()
    matched = match_with_llm(
        toc_entries, article_starts, matcher_template, client, matcher_model
    )
    print(f"  matcher produced {len(matched)} entries", file=sys.stderr)

    toc = build_toc(matched, leaf_to_page, leaf_count, args.item)

    out = args.output or f"{args.item}_toc.json"
    with open(out, "w") as fh:
        json.dump(toc, fh, indent=2)
    print(f"  wrote {out}: {len(toc['entries'])} entries", file=sys.stderr)

    raw_out = args.raw_output or (
        out.replace("_toc.json", "_raw.json")
        if out.endswith("_toc.json")
        else f"{out}.raw.json"
    )
    with open(raw_out, "w") as fh:
        json.dump(
            {
                "item": args.item,
                "page_index_count": leaf_count,
                "generator_version": SEGMENTER_VERSION,
                "model": args.model,
                "toc_entries": toc_entries,
                "article_starts": article_starts,
                "matched_entries": matched,
            },
            fh,
            indent=2,
        )
    print(f"  wrote {raw_out}", file=sys.stderr)

    if pdf is not None and pdf.exists() and not args.keep_pdf:
        try:
            sz = pdf.stat().st_size
            pdf.unlink()
            print(f"  deleted pdf to free {sz // 1024 // 1024}MB", file=sys.stderr)
        except Exception as e:
            print(f"  WARN: pdf delete failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
