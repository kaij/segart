"""Score an ill_eval.py output JSONL with ±N tolerance buckets and
produce a markdown report.

Reads each prediction; categorizes by item-match × leaf-tolerance:
  - hit_exact          item matches AND both endpoints match exactly
  - hit_within_1       item matches AND both endpoints within ±1
  - hit_within_2       item matches AND both endpoints within ±2  (≈ ill_eval's "exact")
  - hit_within_5       item matches AND both endpoints within ±5
  - hit_start_only     item matches AND start within ±2 but end is wrong
  - miss_off_by_3+     item matches but both endpoints are off by ≥3
  - miss_item          predicted item differs from gold (after dupe-collapse)
  - no_item            no candidate yielded a result
  - partial_pages      item matches but start or end is None
  - script_fail        exception during predict

Aggregates by strategy and emits a markdown summary with sample misses.

Usage:
  python3 tools/score_ill_eval.py <eval.jsonl> [--out tmp/audit/ill_eval_report.md]
"""
from __future__ import annotations
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Reuse the dedupe helper from ill_eval
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ill_eval import _is_dupe  # noqa: E402


def leaf_int(s):
    if s is None: return None
    if isinstance(s, int): return s
    s = str(s)
    if s.startswith("n"): s = s[1:]
    try: return int(s)
    except Exception: return None


def classify(rec: dict) -> str:
    cat = rec.get("category")
    if cat == "script_fail": return "script_fail"
    if cat == "no_item": return "no_item"
    if cat == "partial_pages": return "partial_pages"

    s = rec["sample"]
    picked = rec.get("picked_item")
    gs = leaf_int(s.get("ill_start"))
    ge = leaf_int(s.get("ill_stop"))
    ps = rec.get("start")
    pe = rec.get("end")

    if picked is None or ps is None or pe is None:
        return "partial_pages"

    item_match = (picked == s["ill_item"]) or _is_dupe(s["ill_item"], picked)
    if not item_match:
        return "miss_item"

    if gs is None or ge is None:
        return "partial_pages"

    ds = abs(ps - gs)
    de = abs(pe - ge)
    if ds == 0 and de == 0: return "hit_exact"
    if ds <= 1 and de <= 1: return "hit_within_1"
    if ds <= 2 and de <= 2: return "hit_within_2"
    if ds <= 5 and de <= 5: return "hit_within_5"
    if ds <= 2: return "hit_start_only"
    return "miss_off_by_3+"


def is_safety_tolerant_hit(rec: dict, tol: int = 1) -> bool:
    """Asymmetric tolerance per [[asymmetric_leaf_tolerance]]:
    over-extension is OK, under-extension is not.

    Returns True iff:
      - item matches
      - start is within [gold_start - tol, gold_start]  (earlier OK, later not)
      - end   is within [gold_end,        gold_end + tol] (later OK, earlier not)

    With tol=0 this reduces to "must over-extend or hit exactly".
    """
    s = rec.get("sample") or {}
    picked = rec.get("picked_item")
    if not picked: return False
    if not (picked == s.get("ill_item") or _is_dupe(s.get("ill_item",""), picked)):
        return False
    gs = leaf_int(s.get("ill_start"))
    ge = leaf_int(s.get("ill_stop"))
    ps = rec.get("start")
    pe = rec.get("end")
    if None in (gs, ge, ps, pe): return False
    start_ok = (gs - tol) <= ps <= gs        # ps no later than gs, at most tol earlier
    end_ok   = ge <= pe <= (ge + tol)         # pe no earlier than ge, at most tol later
    return start_ok and end_ok


CATEGORIES_ORDER = [
    "hit_exact", "hit_within_1", "hit_within_2", "hit_within_5",
    "hit_start_only", "miss_off_by_3+", "miss_item",
    "no_item", "partial_pages", "script_fail",
]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("eval_jsonl", help="ill_eval.py output (.eval.jsonl)")
    ap.add_argument("--out", default=None, help="markdown output path")
    ap.add_argument("--samples-per-miss-cat", type=int, default=10,
                    help="how many sample misses to show per category")
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.eval_jsonl)]
    print(f"loaded {len(rows)} predictions from {args.eval_jsonl}", file=sys.stderr)

    cat_counts = Counter()
    by_strategy = defaultdict(Counter)
    misses_by_cat = defaultdict(list)
    delta_pairs = []  # (ds, de) for item-match cases

    for r in rows:
        c = classify(r)
        cat_counts[c] += 1
        strat = r.get("strategy") or "(none)"
        by_strategy[strat][c] += 1

        # Capture sample misses for the report
        if c in ("hit_start_only", "miss_off_by_3+", "miss_item",
                 "no_item", "partial_pages", "script_fail"):
            misses_by_cat[c].append(r)

        # Δ tracking
        s = r.get("sample") or {}
        gs = leaf_int(s.get("ill_start"))
        ge = leaf_int(s.get("ill_stop"))
        ps = r.get("start")
        pe = r.get("end")
        picked = r.get("picked_item")
        item_match = picked and ((picked == s.get("ill_item")) or _is_dupe(s.get("ill_item",""), picked))
        if item_match and ps is not None and pe is not None and gs is not None and ge is not None:
            delta_pairs.append((ps - gs, pe - ge))

    n = len(rows)
    cumulative = {
        "exact":      cat_counts["hit_exact"],
        "within_1":   cat_counts["hit_exact"] + cat_counts["hit_within_1"],
        "within_2":   cat_counts["hit_exact"] + cat_counts["hit_within_1"] + cat_counts["hit_within_2"],
        "within_5":   cat_counts["hit_exact"] + cat_counts["hit_within_1"] + cat_counts["hit_within_2"] + cat_counts["hit_within_5"],
    }
    # Safety-tolerant: under-extension is bad; over-extension is OK
    safety_0 = sum(1 for r in rows if is_safety_tolerant_hit(r, tol=0))
    safety_1 = sum(1 for r in rows if is_safety_tolerant_hit(r, tol=1))
    safety_2 = sum(1 for r in rows if is_safety_tolerant_hit(r, tol=2))
    safety_5 = sum(1 for r in rows if is_safety_tolerant_hit(r, tol=5))

    # ----- markdown -----
    lines = []
    lines.append(f"# ILL-anchored leaf prediction — eval report\n")
    lines.append(f"Source: `{args.eval_jsonl}` ({n} predictions)\n")

    lines.append("## Hit rate — safety-tolerant (per [[asymmetric_leaf_tolerance]])\n")
    lines.append("Predicted start ≤ gold start, predicted end ≥ gold end (over-extension OK; "
                 "under-extension is real content loss).\n")
    lines.append("| Tolerance | Count | % of all |")
    lines.append("|---|---:|---:|")
    lines.append(f"| ±0 (exact only) | {safety_0} | {100*safety_0/n:.1f}% |")
    lines.append(f"| Δstart∈[−1,0] & Δend∈[0,+1] | {safety_1} | {100*safety_1/n:.1f}% |")
    lines.append(f"| Δstart∈[−2,0] & Δend∈[0,+2] | {safety_2} | {100*safety_2/n:.1f}% |")
    lines.append(f"| Δstart∈[−5,0] & Δend∈[0,+5] | {safety_5} | {100*safety_5/n:.1f}% |")

    lines.append("\n## Hit rate — symmetric (legacy)\n")
    lines.append("Both endpoints within ±N regardless of direction.\n")
    lines.append("| Tolerance | Count | % of all |")
    lines.append("|---|---:|---:|")
    for k in ("exact", "within_1", "within_2", "within_5"):
        c = cumulative[k]
        lines.append(f"| ±0 (exact) | {cumulative['exact']} | {100*cumulative['exact']/n:.1f}% |"
                     if k == "exact" else
                     f"| ±{k.split('_')[1]} | {c} | {100*c/n:.1f}% |")

    lines.append("\n## Outcome breakdown\n")
    lines.append("| Category | Count | % |")
    lines.append("|---|---:|---:|")
    for cat in CATEGORIES_ORDER:
        c = cat_counts.get(cat, 0)
        if not c: continue
        lines.append(f"| `{cat}` | {c} | {100*c/n:.1f}% |")

    lines.append("\n## Per-strategy breakdown\n")
    lines.append("| Strategy | Total | Exact | ≤±1 | ≤±2 | ≤±5 | Misses (item+offset+no+partial+fail) |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for strat in sorted(by_strategy, key=lambda s: -sum(by_strategy[s].values())):
        cs = by_strategy[strat]
        tot = sum(cs.values())
        ex = cs.get("hit_exact", 0)
        w1 = ex + cs.get("hit_within_1", 0)
        w2 = w1 + cs.get("hit_within_2", 0)
        w5 = w2 + cs.get("hit_within_5", 0)
        misses = (cs.get("miss_item", 0) + cs.get("miss_off_by_3+", 0)
                  + cs.get("no_item", 0) + cs.get("partial_pages", 0)
                  + cs.get("script_fail", 0) + cs.get("hit_start_only", 0))
        lines.append(f"| {strat} | {tot} | {ex} | {w1} | {w2} | {w5} | {misses} |")

    # Δ distribution
    if delta_pairs:
        lines.append("\n## Δ-distribution on item-match cases (predicted − gold)\n")
        lines.append("Signed: negative = predicted before gold (start earlier OR end shorter); "
                     "positive = predicted after gold.\n")
        ds_only = sorted(d[0] for d in delta_pairs)
        de_only = sorted(d[1] for d in delta_pairs)
        def med(xs): return xs[len(xs)//2] if xs else None
        nN = len(delta_pairs)

        # Counts in signed buckets
        def signed_breakdown(xs, label):
            buckets = {
                "≤-3":    sum(1 for x in xs if x <= -3),
                "-2":     sum(1 for x in xs if x == -2),
                "-1":     sum(1 for x in xs if x == -1),
                "0":      sum(1 for x in xs if x == 0),
                "+1":     sum(1 for x in xs if x == 1),
                "+2":     sum(1 for x in xs if x == 2),
                "≥+3":    sum(1 for x in xs if x >= 3),
            }
            row = " | ".join(f"{buckets[k]} ({100*buckets[k]/nN:.0f}%)"
                             for k in ("≤-3","-2","-1","0","+1","+2","≥+3"))
            return f"| {label} | {row} |"

        lines.append(f"N item-matched (with leaves): {nN}\n")
        lines.append("| | ≤−3 | −2 | −1 | 0 | +1 | +2 | ≥+3 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        lines.append(signed_breakdown(ds_only, "Δstart"))
        lines.append(signed_breakdown(de_only, "Δend"))
        lines.append("")
        lines.append(f"- Δstart median = {med(ds_only)}, p10 = {ds_only[nN*1//10]}, p90 = {ds_only[nN*9//10]}")
        lines.append(f"- Δend   median = {med(de_only)}, p10 = {de_only[nN*1//10]}, p90 = {de_only[nN*9//10]}")
        n_start_safe = sum(1 for d in delta_pairs if d[0] <= 0)
        n_end_safe   = sum(1 for d in delta_pairs if d[1] >= 0)
        lines.append(f"- Δstart ≤ 0 (no content cut off front): {n_start_safe} ({100*n_start_safe/nN:.1f}%)")
        lines.append(f"- Δend   ≥ 0 (no content cut off back):  {n_end_safe} ({100*n_end_safe/nN:.1f}%)")

    # Sample misses
    lines.append(f"\n## Sample misses (first {args.samples_per_miss_cat} per category)\n")
    for cat in ("miss_off_by_3+", "hit_start_only", "miss_item",
                "no_item", "partial_pages", "script_fail"):
        rs = misses_by_cat.get(cat, [])
        if not rs: continue
        lines.append(f"\n### {cat}  ({len(rs)} total)\n")
        for r in rs[:args.samples_per_miss_cat]:
            s = r.get("sample") or {}
            gs = s.get("ill_start"); ge = s.get("ill_stop")
            ps = r.get("start"); pe = r.get("end")
            ps_s = f"n{ps}" if ps is not None else "?"
            pe_s = f"n{pe}" if pe is not None else "?"
            strat = r.get("strategy") or "(none)"
            conf = r.get("confidence", "?")
            title = (s.get("title") or "")[:65]
            pages = s.get("pages") or "?"
            picked = r.get("picked_item") or "(none)"
            lines.append(
                f"- `{s.get('ill_item','?')}` → pred `{picked}`\n"
                f"  - title: `{title}` (pp `{pages}`)\n"
                f"  - gold `{gs}-{ge}` vs predicted `{ps_s}-{pe_s}`   strategy=`{strat}` conf={conf}"
            )

    out_path = Path(args.out or "/tmp/ill_eval_report.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")
    print(f"wrote {out_path}", file=sys.stderr)

    # Print a one-line headline to stdout for quick CI/console use
    print(f"exact={100*cumulative['exact']/n:.1f}%  "
          f"safety_tol1={100*safety_1/n:.1f}%  "
          f"sym±1={100*cumulative['within_1']/n:.1f}%  "
          f"sym±2={100*cumulative['within_2']/n:.1f}%  "
          f"miss_item={cat_counts.get('miss_item',0)}  "
          f"no_item={cat_counts.get('no_item',0)}  "
          f"n={n}")


if __name__ == "__main__":
    main()
