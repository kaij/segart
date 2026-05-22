#!/usr/bin/env python3
"""Enrich ILL ground-truth records with fatcat container metadata.

Reads JSONL records from parse_ill_logs.py on stdin (or a path) and, for each
unique ISSN, calls the fatcat lookup_container endpoint at scholar.archive.org.
Adds these fields to each record:

  fatcat_container_id        — fatcat container id (v2 UUID, e.g. 98fa24b4-8cd7-43be-b686-6f2059b2c268)
  fatcat_container_name      — canonical journal name
  fatcat_sim_pubid           — IA SIM publication ID (extra.ia.sim.sim_pubid)
  fatcat_sim_year_spans      — year ranges IA holds SIM scans for
  fatcat_lookup_error        — string set when the lookup failed (otherwise absent)

A release-level lookup (release_id ↔ ILL article) is the natural next step but
requires either DOI in the ILL record (rare) or fuzzy title/volume/issue matching
against a fatcat bulk dump. Not yet implemented.

Usage:
  ./parse_ill_logs.py logs.csv --dedupe | ./fatcat_lookup.py > enriched.jsonl
  ./fatcat_lookup.py records.jsonl --probe-issn 0028-4793
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

FATCAT = "https://scholar.archive.org/api/fatcat/v2"
USER_AGENT = "segart-fatcat-lookup/0.1 (+https://github.com/brewsterkahle/segart)"


def fatcat_get(path, params=None, timeout=15):
    url = f"{FATCAT}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def lookup_container_by_issn(issn):
    return fatcat_get(
        "/container/lookup",
        {"id_type": "issnl", "id_value": issn},
    )


def container_summary(container):
    sim = (container.get("extra") or {}).get("ia", {}).get("sim", {}) or {}
    return {
        "fatcat_container_id": container.get("id"),
        "fatcat_container_name": container.get("name"),
        "fatcat_sim_pubid": sim.get("sim_pubid"),
        "fatcat_sim_year_spans": sim.get("year_spans"),
    }


def enrich_stream(records, throttle=0.1):
    cache = {}  # issn -> summary dict OR {"fatcat_lookup_error": "..."}
    for rec in records:
        issn = rec.get("issn")
        if not issn:
            yield rec
            continue
        if issn not in cache:
            try:
                cache[issn] = container_summary(lookup_container_by_issn(issn))
            except urllib.error.HTTPError as e:
                cache[issn] = {"fatcat_lookup_error": f"HTTP {e.code}"}
            except Exception as e:
                cache[issn] = {"fatcat_lookup_error": str(e)}
            time.sleep(throttle)
        yield {**rec, **cache[issn]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "input",
        nargs="?",
        help="JSONL input from parse_ill_logs.py (default: stdin)",
    )
    p.add_argument("-o", "--output", help="Output JSONL path (default: stdout)")
    p.add_argument(
        "--throttle",
        type=float,
        default=0.1,
        help="Seconds to sleep between API calls (default 0.1)",
    )
    p.add_argument(
        "--probe-issn",
        help="Diagnostic: look up a single ISSN and print container JSON, then exit",
    )
    args = p.parse_args()

    if args.probe_issn:
        json.dump(lookup_container_by_issn(args.probe_issn), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    fin = open(args.input) if args.input else sys.stdin
    fout = open(args.output, "w") if args.output else sys.stdout
    n_in = n_out = n_with = n_err = 0
    issns = set()

    def records():
        nonlocal n_in
        for line in fin:
            line = line.strip()
            if not line:
                continue
            n_in += 1
            yield json.loads(line)

    for rec in enrich_stream(records(), throttle=args.throttle):
        if rec.get("issn"):
            issns.add(rec["issn"])
        if rec.get("fatcat_container_id"):
            n_with += 1
        if rec.get("fatcat_lookup_error"):
            n_err += 1
        fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
        n_out += 1

    if args.input:
        fin.close()
    if args.output:
        fout.close()
    print(
        f"in={n_in} out={n_out} unique_issns={len(issns)} "
        f"resolved={n_with} errors={n_err}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
