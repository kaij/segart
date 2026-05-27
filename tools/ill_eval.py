"""Evaluation driver for tools/ill_lookup.py.

Reads samples JSONL (one ILL fulfilment per line with ground-truth
ill_item / ill_start / ill_stop + request fields) and runs the library's
lookup against each, then prints a categorised + confidence-banded
summary.
"""
from __future__ import annotations
import argparse, json, sys, re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SEGART = Path("/Users/brewster/tmp/segart")
sys.path.insert(0, str(SEGART / "tools"))
from ill_lookup import LookupRequest, lookup  # noqa: E402


def _is_dupe(ill_id: str, mine_id: str) -> bool:
    """sim_X vs X, X_0 vs X, sim_X_X vs sim_X — same content, different slug.

    An ID ending in a single trailing `_N` (single digit) is a scan-variant
    marker (e.g. `sim_X_<vol>_<iss>_0`); the same physical issue may also be
    cataloged without the marker (`sim_X_<vol>_<iss>`). Generate variants
    with and without that trailing `_<digit>` and check for any intersection.
    Single-digit only (not `_\\d+`) so we don't accidentally strip the issue
    number from a base identifier like `sim_X_2010_5_3`.
    """
    if not ill_id or not mine_id: return False

    def variants(s: str) -> set[str]:
        s = re.sub(r"^sim_", "", s)
        out = {s}
        m = re.match(r"(.+)_\d$", s)
        if m:
            out.add(m.group(1))
        # Doubled-name collapse: `X_X_<rest>` -> `X_<rest>`
        for v in list(out):
            parts = v.split("_")
            if len(parts) >= 4 and parts[0] == parts[1]:
                out.add("_".join(parts[1:]))
        return out

    return bool(variants(ill_id) & variants(mine_id))


def evaluate_one(sample: dict) -> dict:
    req = LookupRequest(
        issn=sample["issn"], vol=sample["vol"], iss=sample["iss"],
        yr=sample["yr"], pages=sample["pages"],
        title=sample.get("title",""), author=sample.get("author",""),
        title_journal=sample.get("journal_title", sample.get("title_journal","")),
    )
    try: r = lookup(req)
    except Exception as e:
        return {"sample": sample, "category": "script_fail",
                "confidence": 0, "error": str(e)}
    out = {
        "sample": sample,
        "picked_item": r.picked_item,
        "start": r.start, "end": r.end,
        "strategy": r.strategy, "confidence": r.confidence,
        "evidence": [e for e in r.evidence if isinstance(e, str)],
        "error": r.error,
    }
    # Categorise
    if not r.picked_item:
        out["category"] = "no_item"
    elif r.picked_item == sample["ill_item"] or _is_dupe(sample["ill_item"], r.picked_item):
        if r.start is None or r.end is None:
            out["category"] = "partial_pages"
        else:
            ts = int(sample["ill_start"].lstrip("n")) if sample["ill_start"].startswith("n") else None
            te = int(sample["ill_stop"].lstrip("n")) if sample["ill_stop"].startswith("n") else None
            if ts is None or te is None:
                out["category"] = "partial_pages"
            elif abs(r.start - ts) <= 2 and abs(r.end - te) <= 2:
                out["category"] = "exact"
            else:
                out["category"] = "pages_differ"
        if r.picked_item != sample["ill_item"]:
            out["category"] = "exact_dupe" if out["category"] == "exact" else out["category"]
    else:
        out["category"] = "diff_item"
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("samples", help="JSONL with ground-truth samples")
    ap.add_argument("--out", default=None, help="output JSONL (default: samples.eval.jsonl)")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    samples = [json.loads(l) for l in open(args.samples)]
    out_path = Path(args.out or (args.samples + ".eval.jsonl"))
    print(f"evaluating {len(samples)} samples → {out_path}", flush=True)

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(evaluate_one, s): s for s in samples}
        n = 0
        for fut in as_completed(futs):
            try: r = fut.result()
            except Exception as e:
                r = {"sample": futs[fut], "category": "script_fail",
                     "confidence": 0, "error": str(e)}
            results.append(r); n += 1
            if n % 20 == 0:
                print(f"  {n}/{len(samples)}", flush=True)

    with open(out_path, "w") as fh:
        for r in results: fh.write(json.dumps(r, default=str) + "\n")

    cats = Counter(r.get("category") for r in results)
    total = len(results)
    print(f"\n=== outcome ({total} samples) ===")
    for c in ("exact","exact_dupe","pages_differ","partial_pages",
              "diff_item","no_item","script_fail"):
        n = cats.get(c, 0)
        if n: print(f"  {c:18s} {n:>4d} ({100*n/total:.1f}%)")

    # Strategy distribution
    sd = Counter(r.get("strategy") for r in results if r.get("picked_item"))
    print("\n=== strategy ===")
    for s, n in sd.most_common(): print(f"  {s or '(none)'}: {n}")

    # Confidence band × outcome
    bands = [("≥90", 90, 101), ("75-89", 75, 90), ("50-74", 50, 75), ("<50", 0, 50)]
    print("\n=== confidence band × outcome ===")
    print(f"{'band':>6s}  {'count':>5s}  {'exact':>5s}  {'dupe':>5s}  "
          f"{'pgs_dif':>7s}  {'partial':>7s}  {'diff_it':>7s}  {'fail':>4s}")
    for name, lo, hi in bands:
        rs = [r for r in results if lo <= r.get("confidence", 0) < hi]
        c = Counter(r.get("category") for r in rs)
        eff = c.get("exact", 0) + c.get("exact_dupe", 0)
        print(f"{name:>6s}  {len(rs):>5d}  {c.get('exact',0):>5d}  "
              f"{c.get('exact_dupe',0):>5d}  {c.get('pages_differ',0):>7d}  "
              f"{c.get('partial_pages',0):>7d}  {c.get('diff_item',0):>7d}  "
              f"{c.get('script_fail',0):>4d}")


if __name__ == "__main__":
    main()
