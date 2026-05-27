# Matching IA `pub_*` collections to fatcat containers

Two parallel catalogs of periodicals exist:

- **IA `pub_*` collections** — `archive.org/details/pub_<slug>`, ~28,002 of them as of 2026-05-03. They group all the scanned issues IA holds for a given periodical, regardless of source (microfilm, donor scans, partner libraries, NOAA, etc.).
- **Fatcat containers** — `scholar.archive.org/fatcat`, ~199,060 worldwide journals and series. About 21% have IA-side identifiers (`extra.ia.sim.sim_pubid`, etc.).

For segart these need to be linked bidirectionally: starting from an IA periodical issue we want the fatcat container to pull a candidate article list; starting from a fatcat release we want the IA pub collection to find the scanned issue.

## Approach

`build_pub_collections_index.py` harvests every `pub_*` IA collection via the scrape API and emits compact JSONL with `identifier`, `title`, `issn`, `sim_pubid`, `external-identifier`, `publisher`, `collection`, `pub_type`, `peer_reviewed`, `scholarly`.

`match_pub_to_fatcat.py` builds three indexes from the fatcat `container_export.json.gz` dump:

1. **ISSN** → container_id (using `issnl`, `issne`, `issnp`).
2. **`sim_pubid`** → container_id (where present).
3. **Normalized title** → list of `(container_id, publisher, …identifiers)`. Normalization lowercases, strips non-alphanumeric characters, and drops a small stopword list.

For each pub, it tries those keys in priority order and records the match method plus the evidence used. When a normalized title matches multiple fatcat containers, it tries to disambiguate on publisher; if that fails, it falls back to a container-completeness tie-break — preferring the record with more identifiers populated (`issnl`/`issne`/`issnp`, `sim_pubid`, `wikidata_qid`, `publisher`), which is empirically the canonical container — and records it as `title+completeness`. Only when completeness *also* ties is the match flagged `title_ambiguous` and not used.

The ident decode, title normalization, and the completeness tie-break live in the shared `fatcat_match.py` module (also used by `fuzzy_fatcat_match.py` for article→release matching).

OCLC URNs from `external-identifier` are intentionally not used — that data is too noisy for a reliable join.

## Match rates (2026-05-03 against the 2024-02-18 fatcat dump)

| Method | Count | Share |
|---|---:|---:|
| ISSN | 16,037 | 57.3% |
| Title (unique) | 387 | 1.4% |
| `sim_pubid` (after ISSN had its turn) | 73 | 0.3% |
| Title + publisher | 5 | 0.02% |
| **Total matched** | **16,502** | **58.9%** |
| Title ambiguous (multiple candidates, no publisher tie-break) | 154 | 0.5% |
| Unmatched | 11,346 | 40.5% |

The unmatched gap is real and worth working on: foreign-language journals with diacritic differences, non-scholarly publications fatcat doesn't track (general-interest magazines, newspapers), pubs with no ISSN and an ambiguous title, and titles deposited after the fatcat dump's 2024-02-18 cutoff.

## Output schema

`match_pub_to_fatcat.py` emits one JSONL line per pub collection — matched or not — so the gap is visible:

```json
{
  "pub_id": "pub_new-england-journal-of-medicine",
  "pub_title": "The New England Journal of Medicine",
  "container_id": "td5cjnem25b35nugn4qftmwcna",
  "match_method": "issn",
  "evidence": "0028-4793"
}
```

`match_method` is one of `issn | sim_pubid | title | title+publisher | title+completeness | title_ambiguous | null`. `title+completeness` is a title match where multiple containers tied and publisher didn't disambiguate, resolved by preferring the more fully populated container — it carries a real `container_id` but is lower-confidence than the methods above it. `null` means no candidate was found at all.

## How to use the result

- **Pub → fatcat**: filter for `match_method ∈ {issn, sim_pubid, title, title+publisher}` and use `container_id` to look up release candidates for any IA issue under that pub. `title+completeness` rows also carry a resolved `container_id`; include them only when you want broader recall and accept the title was ambiguous.
- **Fatcat → pub**: invert the join (group by `container_id`) to find the IA pub collection — and via that, the scanned issues — for a given fatcat container.
- **Coverage tracking**: the `null` and `title_ambiguous` rows are the gap to chip away at — better title normalization, transliteration, and per-language stopword lists would all help.

## Caveats

- Both sides are moving targets. Re-run when the fatcat bulk dump is refreshed (currently 2024-02-18) or when IA adds significant new pub collections.
- `sim_pubid` only catches a few extra matches because ISSN already covers most cases where both sides have IA SIM coverage. It's mostly useful as a sanity check rather than a primary join key.
- A successful pub ↔ container match does **not** mean every fatcat release in that container has a scan in that pub. That's a per-issue question the segmenter still has to answer.
