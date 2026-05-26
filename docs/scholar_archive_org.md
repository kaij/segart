# scholar.archive.org / fatcat — what segart can use

`scholar.archive.org` is the Internet Archive's full-text search index over scholarly content. Underneath it sits **fatcat**, the bibliographic database. **The purpose of consulting fatcat from segart is to support the article-segmentation work itself** — candidate lists, cross-references, prioritization — not to feed back into ILL. (Better segmentation may improve ILL eventually, but that is a downstream consequence; segart reads fatcat for its own segmentation pipeline.)

For segart, fatcat is a strong *indicator* of which articles a given journal issue should contain — useful as a candidate set and consistency check, but not as ground truth. The bibliographic records come from upstream metadata (Crossref, PubMed, etc.) and can disagree with the actual IA scan: articles may be assigned to the wrong volume/issue, page ranges may differ, articles printed in the scan may have no fatcat record, and fatcat records may exist for articles never scanned by IA. ILL fulfillment logs remain the closer-to-ground-truth source because a human physically located the article in the IA scan; fatcat is the much-larger, lower-confidence companion signal.

## Data model (lightweight FRBR)

| Entity | What it is | Fields most relevant to segart |
|---|---|---|
| **Release** | A specific published article version. | `ident`, `work_id`, `container_id`, `title`, `release_date`, `release_year`, `release_stage`, `volume`, `issue`, `pages`, `contribs[]`, `ext_ids` (`doi`, `pmid`, `pmcid`, `arxiv`, `jstor`, `mag`, `wikidata_qid`, etc.) |
| **Work** | Abstract grouping; preprint, published, etc. share one `work_id`. | `ident` |
| **Container** | The journal/series. | `ident`, `name`, `issnl`, `issne`, `issnp`, `publisher`, `wikidata_qid`, `extra.ia.sim.sim_pubid`, `extra.ia.sim.year_spans` |
| **File** | A digital artifact (PDF, etc.). | `ident`, `md5`, `sha1`, `sha256`, `size`, `mimetype`, `urls[]` (often empty for "dark" preservation-only), `release_ids[]` |
| **Creator** | Author personas. | `ident`, names, external IDs |
| **Fileset**, **Webcapture** | Datasets, web snapshots. | Less relevant here. |

Entities cross-reference by `ident`, not revision, so metadata can evolve while persistent links remain stable.

## What segart pulls from fatcat

1. **Per-issue candidate article list.** Given an IA periodical issue, the set of fatcat releases with matching `container_id` + `volume` + `issue` gives a dense list of articles the issue *probably* contains — most well-covered journals have a fatcat record per article from Crossref/PubMed harvests. Use it as a candidate set against which to compare segmenter output (overlaps are positive evidence; mismatches need investigation, not assumed-wrong-on-either-side).
2. **DOI / PMID round-trip.** Lookup canonical release by DOI/PMID, recover work_id and container_id.
3. **IA item ↔ container link.** IA SIM item IDs follow the pattern
   ```
   sim_<container-slug>_<YYYY-MM-DD>_<volume>_<issue>
   ```
   e.g. `sim_new-england-journal-of-medicine_1961-12-28_265_26`. Given a fatcat container plus a release's `release_date`/`volume`/`issue`, the expected IA item ID is constructible.
4. **IA's internal pub ID.** `container.extra.ia.sim.sim_pubid` (e.g. `"693"` for NEJM) — IA's own periodical pub identifier, useful when joining against IA's own catalog.
5. **Coverage signal for prioritization.** `container.extra.ia.sim.year_spans` indicates which years IA holds SIM scans for. Combined with `kbart.{hathitrust,lockss,portico}` it gives a rough preservation map — segart can prioritize titles/years where IA has scans but downstream coverage is sparse.

## What fatcat does *not* give us

- **No page-index or printed-page anchors.** `release.pages` is printed-page numbers (e.g. `"1273-1278"`), not page indices. Linking each release to its `start_page_index`/`stop_page_index` inside the IA scan is exactly what segart must produce. Fatcat suggests *which* articles to look for; segart determines *where* in the page stack each lives — and whether each candidate is actually present.
- **`file.urls` is often empty.** Many IA-held files are "dark" preservation-only and carry no public URLs in the fatcat record. The archive.org link rendered on scholar work pages is constructed at render time from container + release metadata, not stored on the file.
- **Patchy ext_id coverage.** Pre-1970 and many non-English titles lack DOIs. ILL log → fatcat matching has to fall back to fuzzy title + container + volume/issue/year matching when no DOI is present.

## Endpoints

OpenAPI spec at `https://scholar.archive.org/api/fatcat/v2/docs`. All endpoints are GET, JSON, no auth.

| Endpoint                                                        | Use                                           |
|-----------------------------------------------------------------|-----------------------------------------------|
| `/api/fatcat/v2/release/lookup?id_type=doi&id_value={doi}`      | Resolve a release by DOI/PMID/PMCID/etc.      |
| `/api/fatcat/v2/container/lookup?id_type=issnl&id_value={issn}` | Resolve a journal container by ISSN-L.        |
| `/api/fatcat/v2/release/{ident}`                                | Full release with embedded container + files. |
| `/api/fatcat/v2/release/files/{ident}`                          | Files attached to a release.                  |
| `/api/fatcat/v2/container/{ident}`                              | Container by fatcat uuid.                     |
| `/api/fatcat/v2/work/{ident}` / `/work/releases/{ident}`        | Work entity and all its release versions.     |
| `/api/fatcat/v2/creator/{ident}` / `/creator/releases/{ident}`  | Author entity and their releases.             |

Fatcat-side full-text/structured search (releases by `container_issnl` + `volume` + `issue`, etc.) is not in this OpenAPI surface; it is exposed within IA's network at https://scholar.archive.org/_es . Fatcat indices: fatcat_file, fatcat_release, fatcat_container. Full text search over releases is in the scholar_fulltext index.

Note that in Fatcat/scholar world, `container` means journal -- not an issue of a journal as in Crossref.

## Bulk dumps (preferred at scale)

Full fatcat metadata is exported as JSONL plus a periodic ~100 GB compressed PostgreSQL dump:

> https://archive.org/details/fatcat_snapshots_and_exports

For segart's evaluation loop, the relevant dumps are `release_export*.json.gz` and `container_export*.json.gz`. A streaming filter on (release_type=`article-journal`, container_id ∈ {SIM-covered containers}) yields a per-issue article ground-truth list far cheaper than per-record API calls.

## Worked example

NEJM 1961-12-28, vol 265, issue 26, page 1273 — the example from the project doc.

| Source | Value |
|---|---|
| ILL log answer | `sim_new-england-journal-of-medicine_1961-12-28_265_26`, leaves `n26`–`n31` |
| Fatcat release | `dtoonyptt5d2layb4nlokwk6he` |
| Fatcat work | `esjkikobxva5hidsopmsygjaie` |
| Fatcat container | `td5cjnem25b35nugn4qftmwcna` |
| ISSN-L | `0028-4793` |
| `sim_pubid` | `693` |
| Release `pages` | `"1273-1278"` (printed pages, not leaves) |
| DOI | `10.1056/nejm196112282652601` |
| PMID | `14462856` |

Segart's job: produce a TOC entry for this IA item that pairs `release dtoonyptt5d2layb4nlokwk6he` (or the bibliographic tuple) with leaves `n26`–`n31`. The ILL log gives one such ground-truth pair; fatcat gives a dense candidate list of other articles the same issue is likely to contain, against which the segmenter's output can be checked.
