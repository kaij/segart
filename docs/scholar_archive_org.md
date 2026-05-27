# scholar.archive.org / fatcat — what segart can use

IA Scholar (`scholar.archive.org`) finds, indexes, and serves open-access research as PDFs. Underneath it sits **fatcat**, the companion bibliographic database that tracks metadata gleaned from upstream sources (Crossref, PubMed, etc.). Scholar itself is essentially a wrapper around Elasticsearch; fatcat additionally exposes a full REST API.

**The purpose of consulting fatcat from segart is to support the article-segmentation work itself** — candidate lists, cross-references, prioritization — not to feed back into ILL. (Better segmentation may improve ILL eventually, but that is a downstream consequence; segart reads fatcat for its own segmentation pipeline.)

For segart, fatcat is a strong *indicator* of which articles a given journal issue should contain — useful as a candidate set and consistency check, but not as ground truth. The bibliographic records come from upstream metadata and can disagree with the actual IA scan: articles may be assigned to the wrong volume/issue, page ranges may differ, articles printed in the scan may have no fatcat record, and fatcat records may exist for articles never scanned by IA. ILL fulfillment logs remain the closer-to-ground-truth source because a human physically located the article in the IA scan; fatcat is the much-larger, lower-confidence companion signal.

## Data model (lightweight FRBR)

The scholar/fatcat world uses a specific vocabulary for academic works. Entities cross-reference by `ident`, not revision, so metadata can evolve while persistent links stay stable.

| Entity | What it is | Fields most relevant to segart |
|---|---|---|
| **Release** | A specific published version of a work — most often a paper as a PDF, but can be a dataset or conference proceedings. | `ident`, `work_id`, `container_id`, `title`, `release_date`, `release_year`, `release_stage`, `volume`, `issue`, `pages`, `contribs[]` / `contrib_names`, `ext_ids` (`doi`, `pmid`, `pmcid`, `arxiv`, `jstor`, `mag`, `wikidata_qid`, …) |
| **Work** | An abstract grouping of releases; multiple versions (preprint, published, …) share one `work_id`. | `ident` |
| **Container** | The journal or book series. **Not** an issue in the Crossref sense. | `ident`, `name`, `issnl`, `issne`, `issnp`, `publisher`, `wikidata_qid`, `container_type`, `extra.ia.sim.sim_pubid`, `extra.ia.sim.year_spans` |
| **File** | A digital artifact (usually a PDF) associated with a release. Some records are just checksums; others carry URLs to full files. | `ident`, `md5`, `sha1`, `sha256`, `size` / `size_bytes`, `mimetype`, `urls[]` (often empty for "dark" preservation-only), `release_ids[]` |
| **Creator** | An author persona credited on a release, hopefully tied to an ORCID. | `ident`, names, external IDs |
| **Fileset**, **Webcapture** | Datasets, web snapshots. | Less relevant here. |

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

## Fatcat REST API

The current API is **v2**, at `https://scholar.archive.org/api/fatcat/v2/`. All read operations are public — no API key, and (unlike the `_es` endpoint below) reachable without the IA VPN. Write operations are gated behind an auth token; writing to fatcat is out of scope for segart.

The human-facing docs at `…/v2/docs` are a JS-rendered Swagger UI — fetching them programmatically returns the app shell, not the endpoint list. For the machine-readable source of truth (paths + parameters), fetch the OpenAPI spec:

```
curl 'https://scholar.archive.org/api/fatcat/v2/openapi.json'
```

Lookups by external identifier use an `id_type`/`id_value` query convention; the allowed `id_type` values come straight from that spec. A wrong key or a bare hash returns HTTP 422.

| Endpoint | Use |
|---|---|
| `release/lookup?id_type=doi&id_value={doi}` | Resolve a release by DOI/PMID/PMCID/… |
| `container/lookup?id_type=issnl&id_value={issn}` | Resolve a journal container by ISSN-L. |
| `file/lookup?id_type=sha1&id_value={hash}` | Resolve a file by checksum (`sha1`/`sha256`/`md5`/`legacy_ident`). |
| `release/{ident}` | Full release record. |
| `release/{ident}/files` | Files (PDFs) attached to a release. |
| `release/{ident}/container` · `/work` · `/contribs` | A release's container, work, contributors. |
| `container/{ident}` · `container/{ident}/releases` | Container record; its releases. |
| `work/{ident}/releases` | All release versions of a work. |
| `creator/{ident}/releases` | An author's releases. |
| `file/{ident}` · `file/{ident}/releases` | File record; releases it belongs to. |

> Note the path order: it's `release/{ident}/files`, `work/{ident}/releases`, etc. — **ident first**, then the sub-resource.

### Lookup by DOI

```
curl 'https://scholar.archive.org/api/fatcat/v2/release/lookup?id_type=doi&id_value=10.11316%2Fjpsgaiyo.66.1.3.0_560_4'
```

### Lookup a file by checksum

`file/lookup` takes the same `id_type`/`id_value` shape; for files `id_type` is a hash type (`sha1`, `sha256`, `md5`, or `legacy_ident`):

```
curl 'https://scholar.archive.org/api/fatcat/v2/file/lookup?id_type=sha1&id_value=05d24d6f34197430d0387ad507dba0c90201364f'
```

The response is a file record (`sha1`, `sha256`, `md5`, `size_bytes`, `mimetype`) plus its `urls` (including `webarchive` Wayback copies) and the full expanded `releases` it belongs to — so a checksum gets you straight to release metadata without a separate `release/lookup`. The sha1 above resolves to a 1.13 MB PDF of "Cobot in LambdaMOO: An Adaptive Social Statistics Agent" (DOI `10.1007/s10458-006-0005-z`), a known-good smoke test.

### Files for a known release

Given a release ident (from a lookup, an ES hit, or a file record's `release_id`), list its associated files:

```
curl 'https://scholar.archive.org/api/fatcat/v2/release/48b27806-a145-4bbc-a97b-1f962965c269/files'
```

The response is `{"items": [...], "count": N}`. Each item is a file record with a `urls` list; each url has a `rel` (`web` = original host, `webarchive` = Wayback copy). A release commonly has several files (different scans/versions of the same PDF); the example above returns 5, most with both a live `web` URL and a `webarchive` fallback. **Prefer the `webarchive` URLs** to fetch actual PDF bytes, since original hosts rot. This is the release→file direction; `file/lookup` (above) is the inverse (checksum→file, plus the releases that file belongs to).

## Elasticsearch (`_es`)

The scholar Elasticsearch is exposed on the IA VPN at `https://scholar.archive.org/_es`. **Read-only — never write to it.** Structured and full-text search the REST API doesn't surface (releases by `container_id` + `volume` + `issue`, fuzzy title/author, OCR'd page text) lives here.

| Index | Contents |
|---|---|
| `fatcat_release` | Release metadata. Fuzzy title/author search. |
| `fatcat_container` | Container metadata. ISSN-L or fuzzy name lookups. |
| `fatcat_file` | File metadata; searchable by checksum. |
| `scholar_fulltext` | Full text extracted from PDFs and OCR'd scanned pages. A subset of `fatcat_release`. |

### Fuzzy title + author (releases)

Use a `bool` query with one fuzzy `match` per input field; the relevant release fields are `title` and `contrib_names`. `"fuzziness": "AUTO"` lets Elasticsearch pick an edit distance based on term length, which tolerates OCR noise and spelling variants.

```
curl -s 'https://scholar.archive.org/_es/fatcat_release/_search' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 5,
    "_source": ["title", "contrib_names", "release_year", "doi", "container_name"],
    "query": {
      "bool": {
        "must": [
          { "match": { "title":        { "query": "GEORGE DARLEY", "fuzziness": "AUTO" } } },
          { "match": { "contrib_names": { "query": "DONALD LANGE", "fuzziness": "AUTO" } } }
        ]
      }
    }
  }'
```

- `_source` trims the returned fields; drop it to get the full release document.
- Swap `must` for `should` to match either field rather than both.
- **Known-good:** the top hit is DOI `10.1093/nq/23-4-168c` ("GEORGE DARLEY" by DONALD LANGE, in *Notes and Queries*, 1976), with a `_score` (~45) well clear of the runner-up (~34). Verified against index `fatcat_release_v05_20220110`; query latency was ~10s, so budget accordingly when fanning out many lookups.

### Fuzzy container (journal)

Fuzzy match against the `name` field of `fatcat_container`; useful fields are `name`, `publisher`, `issnl`, `container_type`.

```
curl -s 'https://scholar.archive.org/_es/fatcat_container/_search' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 5,
    "_source": ["name", "publisher", "issnl", "container_type"],
    "query": {
      "match": { "name": { "query": "Autonomous Agents and Multi-Agent Systems", "fuzziness": "AUTO" } }
    }
  }'
```

- **Known-good:** the top hit is the journal "Autonomous Agents and Multi-Agent Systems" (publisher Springer-Verlag, ISSN-L `1387-2532`), `_score` ~46 vs ~38 for the runner-up. Verified against index `fatcat_container_v05_20220110`.
- `container_type` is often `null` on journal records (it tends to be populated only on conference-series and similar), so don't filter on it expecting journals to be tagged.
- Fuzzy name matches are broad — `total` caps at the 10,000 reporting limit. Rank by `_score` and/or pin down with `issnl` once you have a candidate.

### Full-text search of scanned pages (`scholar_fulltext`)

`scholar_fulltext` indexes text extracted from PDFs and — directly relevant to this project — OCR'd pages of scanned periodicals. Each document has a `doc_type`; `sim_page` is a single scanned periodical page (other types are PDF/work-level fulltext). `sim_page` docs carry exactly the linkage this project needs to reach the scanned source on archive.org:

- `ia_sim.issue_item` — the archive.org item id for the *issue* (e.g. `sim_automobile-magazine_1900-03_1_6`).
- `access[].access_url` — a direct link to that specific page on archive.org (`https://archive.org/details/<issue_item>/page/<n>`).
- `biblio.container_name`, `biblio.release_year`, `biblio.volume`, `biblio.issue`, `biblio.first_page` — issue/page bibliographic context.

Do **not** search `fulltext.body` directly: it is stored but `index: false`. Its contents (along with titles, abstracts, contributor names, etc.) are `copy_to` the catch-all **`everything`** field, which is what you query. (`biblio_all` and `title_all` are narrower catch-alls for bibliographic and title text respectively.)

```
curl -s 'https://scholar.archive.org/_es/scholar_fulltext/_search' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 3,
    "_source": ["doc_type", "biblio.container_name", "biblio.release_year", "biblio.first_page", "ia_sim.issue_item", "access.access_url"],
    "query": {
      "bool": {
        "must":   [ { "match_phrase": { "everything": "horseless carriage" } } ],
        "filter": [ { "term": { "doc_type": "sim_page" } } ]
      }
    }
  }'
```

- **Known-good:** ~1634 hits; the top hit is page 345 of the *Journal of the Illinois State Historical Society* (Winter 1954), with an `access_url` to that page on archive.org. Verified against index `scholar_fulltext_v01_20211208`.
- `match_phrase` works on `everything` but errors on `fulltext.body` ("indexed without position data" / "not indexed") — another reason to target `everything`.
- Drop the `doc_type: sim_page` filter to include PDF/work-level fulltext as well; keep it to stay within scanned periodical pages.

## Bulk dumps (preferred at scale)

Full fatcat metadata is exported as JSONL plus a periodic ~100 GB compressed PostgreSQL dump:

> https://archive.org/details/fatcat_snapshots_and_exports

For segart's evaluation loop, the relevant dumps are `release_export*.json.gz` and `container_export*.json.gz`. A streaming filter on (release_type=`article-journal`, container_id ∈ {SIM-covered containers}) yields a per-issue article ground-truth list far cheaper than per-record API calls. See [`fatcat_bulk_dumps.md`](./fatcat_bulk_dumps.md) for detail.

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
