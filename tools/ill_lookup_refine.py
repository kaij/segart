"""Claude-as-orchestrator refinement layer for tools/ill_lookup.py.

Given an ILL request and a baseline (start_leaf, end_leaf) prediction from
ill_lookup.lookup(), Claude can call hOCR / docling / scandata / printed→leaf
/ page-image tools (see tools/ill_lookup_tools.py) to confirm or refine the
prediction. Refinement is bounded by:

  - a small trigger gate (only call the LLM on uncertain baselines)
  - a hard tool-call budget per refinement
  - safety-tolerant reconciliation (per [[asymmetric_leaf_tolerance]]:
    Δend ≥ 0 preferred, Δstart ≤ 0 preferred; when the model is uncertain
    we never *shrink* the baseline)

Cost posture: Sonnet 4.6 default, system prompt cached, ~5–15% of rows
trigger refinement. Anticipated $1–4 for the 1,068-row local subset.

Library + CLI. As a library:

    from ill_lookup_refine import refine, should_refine
    from ill_lookup import lookup, LookupRequest

    base = lookup(req)
    if should_refine(req, base):
        refined = refine(req, base)
        # refined.start / refined.end / refined.confidence / refined.evidence

As a CLI (debug one row at a time):

    python3 tools/ill_lookup_refine.py --item <item> --title "..." \\
        --issn 0001-2345 --pages "141-169" --base-start 24 --base-end 29
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from pathlib import Path

import anthropic

SEGART = Path("/Users/brewster/tmp/segart")
sys.path.insert(0, str(SEGART / "tools"))

from ill_lookup import LookupRequest, LookupResult, ItemContext, lookup  # noqa: E402
from ill_lookup_tools import TOOLS, dispatch_tool, get_ctx  # noqa: E402


DEFAULT_MODEL = "claude-sonnet-4-6"

# --- trigger gate ---------------------------------------------------------

def should_refine(req: LookupRequest, base: LookupResult) -> tuple[bool, str]:
    """Decide whether to spend an LLM call on this row.

    Returns (do_refine: bool, reason: str). Reason is a short string that
    gets attached to the refinement record for debugging.
    """
    if not base.picked_item:
        return True, "no_item"
    if base.start is None or base.end is None:
        return True, "partial_pages"
    if base.confidence < 90:
        return True, f"confidence<90:{base.confidence}"
    if base.start == base.end:
        # Could be a 1-page article (Letter / Errata) — but if the request
        # implies multi-page, that's suspicious.
        if req.pages and "-" in str(req.pages):
            return True, "single_leaf_but_request_is_range"
    # A heuristic-strategy result with an end_method that's a fallback
    # (forward_scan, span_fallback) is shakier than a page-map hit.
    for ev in (base.evidence or []):
        ev_s = str(ev)
        if "no_end_signal" in ev_s or "title_too_short" in ev_s:
            return True, f"weak_evidence:{ev_s[:40]}"
    # Request lacks a parseable end-page (e.g. pp="141-", "NA", "710")
    pages = str(req.pages or "")
    if pages and "-" in pages:
        tail = pages.split("-", 1)[1].strip()
        if not tail or not any(c.isdigit() for c in tail):
            return True, "no_parseable_end_page"
    return False, "ok"


# --- output schema --------------------------------------------------------

REFINED_SCHEMA = {
    "type": "object",
    "properties": {
        "refined_start": {
            "type": ["integer", "null"],
            "description": "Refined start page-index (BR nN integer) or null if unchanged from baseline.",
        },
        "refined_end": {
            "type": ["integer", "null"],
            "description": "Refined end page-index (BR nN integer) or null if unchanged from baseline.",
        },
        "confidence": {
            "type": "integer",
            "description": "Your confidence in the final answer, integer 0-100.",
        },
        "decision": {
            "type": "string",
            "enum": ["confirm_baseline", "refine", "abstain"],
            "description": (
                "confirm_baseline: baseline is correct as-is; "
                "refine: returning a better (start, end); "
                "abstain: can't tell — leave baseline alone."
            ),
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Short bullet strings citing what you saw (which tool, which leaf, what evidence).",
        },
    },
    "required": ["decision", "confidence", "evidence",
                 "refined_start", "refined_end"],
    "additionalProperties": False,
}


# --- system prompt --------------------------------------------------------

SYSTEM_PROMPT = """You are an Interlibrary-Loan (ILL) fulfillment auditor for the Internet \
Archive. Each task gives you ONE article request (title, authors, printed page \
range, ISSN, vol, iss, year) and ONE proposed answer from a heuristic system: \
the IA item identifier, plus (start_leaf, end_leaf) — BookReader 0-indexed \
page-index integers covering the article in that scanned issue.

Your job: confirm the answer or refine it, using the tools to look at the \
underlying scan. You can call multiple tools, multiple times, and you should \
keep calling tools until you're satisfied or until you've decided you can't \
resolve the question with the signals available.

WHAT MAKES A GOOD ANSWER

- start_leaf is the BR page-index of the FIRST leaf of the article — usually \
the leaf with the article's title at the top and the body beginning. Some \
articles have an unprinted chapter-open leaf with just the title; that IS \
the first leaf.
- end_leaf is the BR page-index of the LAST leaf of the article — the leaf \
that finishes the references / acknowledgments / appendix. If references \
spill onto an otherwise-blank leaf, that leaf is part of the article.
- ASYMMETRIC TOLERANCE — this is the most important rule:
    over-extension (end +1) is HARMLESS — an extra page is fine.
    under-extension (end -1) is HARMFUL — the patron loses content.
  Same for start: too-early (-1) is OK; too-late (+1) cuts off the title.
  When in genuine doubt at the boundary, ERR ON THE SIDE OF INCLUSION.
- Do NOT shrink the baseline unless you have positive evidence the article \
ends earlier than baseline says. "I can't see the end clearly" is NOT \
positive evidence — return decision=abstain.

WHAT THE TOOLS GIVE YOU

- docling_blocks(leaf_lo, leaf_hi): structured layout — titles, section \
headers, page headers, page numbers per leaf. A section_header or title at \
the TOP of a leaf is the strongest signal that a new article starts there.
- hocr_text(leaf_lo, leaf_hi): raw OCR text per leaf. Useful for confirming \
"References" headings, article body continuation, blank-page markers.
- printed_pages_map(pages): translates printed-page strings ("141", "152.e1", \
"S211") to BR leaf integers. Returns null when the page isn't in the merged \
scandata+pn.json+docling map (very common for Elsevier x.eN supplement pages \
and for unprinted chapter-open pages).
- scandata_printed_pagenumbers(leaf_lo, leaf_hi): the cataloger's per-leaf \
pageNumber from scandata.xml. Usually trustworthy when populated, often \
empty.
- fetch_page_image(leaf): a 400px JPEG of one leaf — last resort when the \
text tools leave the question unresolved.

WHEN TO STOP

Stop calling tools and emit your final answer once you've either:
  - confirmed the baseline (decision=confirm_baseline);
  - have a better (start, end) you'd stake your name on (decision=refine);
  - exhausted plausible signals and still can't tell (decision=abstain).

OUTPUT

Once you're done with tools, emit JSON matching the schema. \
refined_start/refined_end can be null when decision is confirm_baseline or \
abstain. Evidence should be a small bulleted list (3–6 strings) naming the \
tools you used and the leaves where you saw signal."""


# --- result type ----------------------------------------------------------

@dataclasses.dataclass
class RefinementResult:
    """Output of refine()."""
    decision: str           # "confirm_baseline" | "refine" | "abstain"
    start: int | None       # final start_leaf after reconciliation
    end: int | None         # final end_leaf after reconciliation
    confidence: int         # 0..100
    evidence: list[str]     # short strings
    raw_refined_start: int | None
    raw_refined_end: int | None
    tools_called: list[str]
    trigger_reason: str
    error: str | None       # set on internal failure (API error, schema fail)
    input_tokens: int       # for cost tracking
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    output_tokens: int


def _empty_result(trigger: str, error: str | None = None) -> RefinementResult:
    return RefinementResult(
        decision="abstain", start=None, end=None,
        confidence=0, evidence=[], raw_refined_start=None, raw_refined_end=None,
        tools_called=[], trigger_reason=trigger, error=error,
        input_tokens=0, cache_read_input_tokens=0,
        cache_creation_input_tokens=0, output_tokens=0,
    )


def _serialize_evidence(ev: list) -> list[str]:
    out: list[str] = []
    for e in ev or []:
        if isinstance(e, str): out.append(e)
        else:
            try: out.append(json.dumps(e, default=str))
            except Exception: out.append(str(e))
    return out


def _reconcile(req: LookupRequest, base: LookupResult,
               decision: str,
               raw_start: int | None, raw_end: int | None
               ) -> tuple[int | None, int | None]:
    """Apply safety-tolerant clamping per [[asymmetric_leaf_tolerance]].

    confirm_baseline / abstain -> return baseline.
    refine -> use refined values, but ensure we don't shrink the baseline
    (refined_end >= base.end; refined_start <= base.start). Under-extension
    is the harmful direction; we silently clamp back to the baseline rather
    than letting the model shrink.
    """
    if decision in ("confirm_baseline", "abstain"):
        return base.start, base.end
    # decision == refine
    s = raw_start if raw_start is not None else base.start
    e = raw_end if raw_end is not None else base.end
    if base.start is not None and s is not None and s > base.start:
        s = base.start  # don't move start later
    if base.end is not None and e is not None and e < base.end:
        e = base.end    # don't move end earlier
    if s is not None and e is not None and e < s:
        e = s
    return s, e


# --- the orchestrator -----------------------------------------------------

def refine(req: LookupRequest, base: LookupResult, *,
           client: anthropic.Anthropic | None = None,
           model: str = DEFAULT_MODEL,
           max_tool_calls: int = 8,
           ctx_cache: dict | None = None,
           verbose: bool = False) -> RefinementResult:
    """Run Claude with the five tools to refine `base` for request `req`.

    Trigger gate is applied — if should_refine() says skip, we return a
    no-op confirm_baseline result.
    """
    do_refine, trigger = should_refine(req, base)
    if not do_refine:
        return RefinementResult(
            decision="confirm_baseline", start=base.start, end=base.end,
            confidence=base.confidence, evidence=[],
            raw_refined_start=None, raw_refined_end=None,
            tools_called=[], trigger_reason=trigger, error=None,
            input_tokens=0, cache_read_input_tokens=0,
            cache_creation_input_tokens=0, output_tokens=0,
        )

    if client is None:
        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    if ctx_cache is None:
        ctx_cache = {}

    # Pre-load the item so the first tool call doesn't pay docling-load latency.
    try:
        get_ctx(base.picked_item or req.title_journal, ctx_cache)
    except Exception:
        pass  # the tool call will surface the same error

    # Build the user message. Include the ILL request fields plus the
    # baseline so the model knows what to confirm/refine.
    user_payload = {
        "ill_request": {
            "title": req.title,
            "author": req.author,
            "journal": req.title_journal,
            "issn": req.issn,
            "vol": req.vol,
            "iss": req.iss,
            "year": req.yr,
            "printed_pages": req.pages,
        },
        "ia_item": base.picked_item,
        "baseline_prediction": {
            "start_leaf": base.start,
            "end_leaf": base.end,
            "strategy": base.strategy,
            "confidence": base.confidence,
            "evidence": _serialize_evidence(base.evidence),
        },
        "trigger_reason": trigger,
    }
    messages: list[dict] = [{
        "role": "user",
        "content": [{"type": "text",
                     "text": json.dumps(user_payload, ensure_ascii=False, indent=2)}],
    }]

    system = [{"type": "text", "text": SYSTEM_PROMPT,
               "cache_control": {"type": "ephemeral"}}]

    output_format = {"type": "json_schema", "schema": REFINED_SCHEMA}

    tools_called: list[str] = []
    totals = {"input_tokens": 0, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0, "output_tokens": 0}

    for _turn in range(max_tool_calls + 1):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system,
                tools=TOOLS,
                output_config={"format": output_format},
                messages=messages,
            )
        except anthropic.APIError as e:
            if verbose: print(f"  api error: {e}", file=sys.stderr)
            return _empty_result(trigger, error=f"api:{type(e).__name__}:{str(e)[:120]}")
        except Exception as e:
            return _empty_result(trigger, error=f"err:{type(e).__name__}:{str(e)[:120]}")

        u = resp.usage
        totals["input_tokens"] += getattr(u, "input_tokens", 0) or 0
        totals["cache_read_input_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
        totals["cache_creation_input_tokens"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        totals["output_tokens"] += getattr(u, "output_tokens", 0) or 0

        # Tool-use turn?
        if resp.stop_reason == "tool_use":
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                return _empty_result(trigger, error="tool_use_stop_but_no_blocks")
            # Append assistant turn verbatim
            messages.append({"role": "assistant", "content": resp.content})
            # Execute each tool
            results_payload = []
            for tu in tool_uses:
                tools_called.append(tu.name)
                if verbose: print(f"  tool: {tu.name} {tu.input}", file=sys.stderr)
                try:
                    out = dispatch_tool(base.picked_item, tu.name, tu.input, ctx_cache)
                except Exception as e:
                    out = {"error": f"dispatch:{type(e).__name__}:{str(e)[:80]}"}
                # fetch_page_image returns an image block; others return JSON
                if (tu.name == "fetch_page_image" and isinstance(out, dict)
                        and "image" in out):
                    results_payload.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": [out["image"]],
                    })
                else:
                    results_payload.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": json.dumps(out, default=str)[:8000],
                    })
            messages.append({"role": "user", "content": results_payload})
            continue

        # Terminal — model emitted structured output
        if resp.stop_reason == "end_turn":
            text = next((b.text for b in resp.content if b.type == "text"), "")
            try:
                data = json.loads(text)
            except Exception as e:
                return _empty_result(trigger, error=f"json_parse:{type(e).__name__}")
            decision = data.get("decision") or "abstain"
            raw_s = data.get("refined_start")
            raw_e = data.get("refined_end")
            evidence = _serialize_evidence(data.get("evidence") or [])
            confidence = int(data.get("confidence") or 0)
            start, end = _reconcile(req, base, decision, raw_s, raw_e)
            return RefinementResult(
                decision=decision, start=start, end=end,
                confidence=confidence, evidence=evidence,
                raw_refined_start=raw_s, raw_refined_end=raw_e,
                tools_called=tools_called, trigger_reason=trigger, error=None,
                **totals,
            )

        # Unexpected stop (max_tokens, refusal, pause_turn)
        return _empty_result(trigger, error=f"stop:{resp.stop_reason}")

    return _empty_result(trigger, error="tool_call_budget_exhausted")


# --- CLI ------------------------------------------------------------------

def _main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--item", required=True)
    ap.add_argument("--issn", required=True)
    ap.add_argument("--vol", default="")
    ap.add_argument("--iss", default="")
    ap.add_argument("--yr", required=True)
    ap.add_argument("--pages", default="")
    ap.add_argument("--title", required=True)
    ap.add_argument("--author", default="")
    ap.add_argument("--journal", default="")
    ap.add_argument("--base-start", type=int, required=True)
    ap.add_argument("--base-end", type=int, required=True)
    ap.add_argument("--base-strategy", default="external")
    ap.add_argument("--base-confidence", type=int, default=50)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-tool-calls", type=int, default=8)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    req = LookupRequest(
        issn=args.issn, vol=args.vol, iss=args.iss, yr=args.yr,
        pages=args.pages, title=args.title, author=args.author,
        title_journal=args.journal,
    )
    base = LookupResult(
        picked_item=args.item, start=args.base_start, end=args.base_end,
        strategy=args.base_strategy, confidence=args.base_confidence,
        evidence=["external_baseline"], error=None,
    )
    t0 = time.time()
    out = refine(req, base, model=args.model, max_tool_calls=args.max_tool_calls,
                 verbose=args.verbose)
    elapsed = time.time() - t0
    print(json.dumps(dataclasses.asdict(out), indent=2, default=str))
    print(f"\nelapsed: {elapsed:.1f}s", file=sys.stderr)


if __name__ == "__main__":
    _main()
