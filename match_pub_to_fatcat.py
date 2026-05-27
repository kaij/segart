#!/usr/bin/env python3
"""Match IA pub_* collections to fatcat containers.

Inputs:
  --pubs     JSONL from build_pub_collections_index.py
  --fatcat   container_export.json.gz from the fatcat bulk dumps

Match priority:
  1. ISSN — pub.issn against container.{issnl, issne, issnp}
  2. sim_pubid — pub.sim_pubid against container.extra.ia.sim.sim_pubid
  3. Normalized title — exact match after lowercase/punct-strip; with a
     publisher tie-break, then a record-completeness tie-break (emitted as
     method "title+completeness") when multiple containers share the title.

Emits one JSONL line per pub collection (matched or not) so the gap is
visible.

Shared matching primitives (ident decode, title normalization, the
completeness tiebreak) live in fatcat_match.py, alongside fuzzy_fatcat_match.py.
"""
import argparse
import gzip
import json
import sys

from fatcat_match import (
    container_completeness,
    first_str,
    normalize_title_key as normalize_title,
)


def load_fatcat_indexes(path):
    by_issn = {}
    by_sim_pubid = {}
    by_title = {}
    n = 0
    with gzip.open(path, "rt", encoding="utf-8") as fin:
        for line in fin:
            try:
                c = json.loads(line)
            except json.JSONDecodeError:
                continue
            n += 1
            ident = c.get("ident")
            if not ident:
                continue
            for k in ("issnl", "issne", "issnp"):
                v = c.get(k)
                if v:
                    by_issn.setdefault(v, ident)
            sim = ((c.get("extra") or {}).get("ia") or {}).get("sim") or {}
            sim_pubid = sim.get("sim_pubid")
            if sim_pubid:
                by_sim_pubid.setdefault(str(sim_pubid), ident)
            nt = normalize_title(c.get("name"))
            if nt:
                by_title.setdefault(nt, []).append({
                    "ident": ident,
                    "publisher": c.get("publisher"),
                    "issnl": c.get("issnl"),
                    "issne": c.get("issne"),
                    "issnp": c.get("issnp"),
                    "sim_pubid": sim_pubid,
                    "wikidata_qid": c.get("wikidata_qid"),
                })
    return n, by_issn, by_sim_pubid, by_title


def match(pub, by_issn, by_sim_pubid, by_title):
    issn = first_str(pub.get("issn"))
    if issn and issn in by_issn:
        return by_issn[issn], "issn", issn

    spid = first_str(pub.get("sim_pubid"))
    if spid:
        spid = str(spid)
        if spid in by_sim_pubid:
            return by_sim_pubid[spid], "sim_pubid", spid

    nt = normalize_title(first_str(pub.get("title")))
    if nt and nt in by_title:
        candidates = by_title[nt]
        pub_publisher = (first_str(pub.get("publisher")) or "").lower()
        if len(candidates) == 1:
            return candidates[0]["ident"], "title", nt
        if pub_publisher:
            for c in candidates:
                cp = (c.get("publisher") or "").lower()
                if cp and (cp in pub_publisher or pub_publisher in cp):
                    return c["ident"], "title+publisher", nt
        # Multiple containers, no publisher tie-break. Rather than give up,
        # prefer the most fully populated container (empirically the canonical
        # one) when it's a strict winner, under a distinct method label so
        # consumers can choose whether to trust completeness-resolved matches.
        by_completeness = sorted(candidates, key=container_completeness,
                                 reverse=True)
        if container_completeness(by_completeness[0]) > container_completeness(
                by_completeness[1]):
            return by_completeness[0]["ident"], "title+completeness", nt
        return None, "title_ambiguous", nt

    return None, None, None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pubs", required=True, help="pub_collections.jsonl")
    p.add_argument(
        "--fatcat", required=True, help="fatcat container_export.json.gz"
    )
    p.add_argument("-o", "--output", help="Output JSONL (default stdout)")
    args = p.parse_args()

    print("loading fatcat container indexes...", file=sys.stderr)
    n_cont, by_issn, by_spid, by_title = load_fatcat_indexes(args.fatcat)
    print(
        f"  {n_cont} containers indexed: {len(by_issn)} ISSNs, "
        f"{len(by_spid)} sim_pubids, {len(by_title)} unique normalized titles",
        file=sys.stderr,
    )

    fout = open(args.output, "w") if args.output else sys.stdout
    method_counts = {}
    n_in = 0
    with open(args.pubs) as fin:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            n_in += 1
            pub = json.loads(line)
            container_id, method, evidence = match(pub, by_issn, by_spid, by_title)
            method_counts[method] = method_counts.get(method, 0) + 1
            out = {
                "pub_id": pub.get("identifier"),
                "pub_title": first_str(pub.get("title")),
                "container_id": container_id,
                "match_method": method,
                "evidence": evidence,
            }
            fout.write(json.dumps(out, ensure_ascii=False) + "\n")

    if args.output:
        fout.close()
    print(f"\ninput pubs: {n_in}", file=sys.stderr)
    print("match breakdown:", file=sys.stderr)
    for m, c in sorted(method_counts.items(), key=lambda x: -x[1]):
        label = m if m is not None else "(unmatched)"
        print(f"  {c:>6}  {label}", file=sys.stderr)


if __name__ == "__main__":
    main()
