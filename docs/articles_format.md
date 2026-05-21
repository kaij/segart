# segart per-item articles file: `<item>_articles.json.gz`

A bibliographic-enrichment companion to `<item>_toc.json`. One file per IA periodical issue, gzipped JSON, holding everything we know about each article in the issue from external metadata sources (Crossref, fatcat, OpenAlex, Unpaywall, PubMed) plus per-entry Crossmark / Funder Registry / relation 1-hop / Event Data enrichments.

`_toc.json` answers *"where in the scan is each article."*
`_articles.json.gz` answers *"what does the world know about each article."*

See [`toc_format.md`](./toc_format.md) for the TOC file. The two are linked by **`toc_entry_id`** (the `e1`, `e2`, … ordinals defined in `_toc.json`).

**Schema version: 2** (current). For the v1 → v2 migration plan, see the [Migration](#migration-v1--v2) section.

## Why distinct from `_toc.json`

| | `_toc.json` | `_articles.json.gz` |
|---|---|---|
| Source of truth for | Segmentation (page-index ranges per entry) | Bibliographic metadata per entry |
| Updated when | Re-segmentation pass runs | External dumps refresh, or per-entry enrichment runs |
| Size | ~5–20 KB / issue | ~150–600 KB / issue gzipped; outliers up to several MB |
| Required for an item | Yes | No (optional enrichment) |
| Primary consumers | BookReader, IA UI | Researchers, downstream pipelines |

Bundling them would couple two very different update cadences and force lightweight TOC consumers to download data they don't need.

## Naming and storage

- Filename: `<item>_articles.json.gz` — sibling of `<item>_toc.json` in the IA item file group.
- Compression: gzip on upload (Crossref `reference[]`, `abstract`, and inline Event Data compress ~6×).
- Encoding: UTF-8 JSON.
- Updates: re-derive when either (a) `_toc.json` is regenerated or (b) any source dump is refreshed or (c) any per-entry enrichment is re-run.

## Top-level schema

```jsonc
{
  "schema_version": 2,
  "ia_item": "sim_biological-conservation_2010-11_143_11",

  // Pin which _toc.json snapshot this articles file matches. If these don't
  // agree with the current _toc.json, this file is stale.
  "toc_schema_version": 2,
  "toc_generated_at": "2026-05-21T13:00:00Z",

  // Full Crossref `type:journal-issue` deposit, when one exists. The block IS
  // the raw Crossref record (Crossref field names, no segart wrapping).
  // null when the journal doesn't deposit issue DOIs (~82% of journals).
  "issue_meta": {
    "DOI": "10.1016/s0006-3207(10)00012-3",
    "type": "journal-issue",
    "title": ["Special Issue: Conservation in Fragmented Landscapes"],
    "subtitle": [],
    "editor": [
      { "given": "Lenore", "family": "Fahrig", "sequence": "first" },
      { "given": "Adina", "family": "Merenlender", "sequence": "additional" }
    ],
    "published-print":  { "date-parts": [[2010, 11]] },
    "published-online": { "date-parts": [[2010, 8, 14]] },
    "subject": ["Nature and Landscape Conservation", "Ecology"],
    "link": [{ "URL": "...", "content-type": "text/html" }]
    /* ... every other field Crossref returned ... */
  },

  // Full Crossref `type:journal-volume` deposit, when one exists. Rare; mostly
  // review serials (Advances in X, Annual Review of Y).
  "volume_meta": null,

  // True iff any entry in this issue has `retracted: true`. Lets BookReader /
  // IA UI surface a per-issue badge without walking entries.
  "has_retracted_entries": false,

  "provenance": {
    "generated_at": "2026-05-21T14:00:00Z",
    "generator": { "name": "segart-annotate", "version": "1.1.0" },
    "sources": {
      "crossref": {
        "via": "public_data_file",  // or "live_api_cached" / "openalex_snapshot"
        "dump_date": "2026-01",
        "type_filter": null          // explicit: we fetched ALL types
      },
      "fatcat":          { "via": "release_export_expanded",   "dump_date": "2026-02-18" },
      "openalex":        { "via": "openalex_snapshot",         "dump_date": "2026-04" },
      "unpaywall":       { "via": "snapshot",                  "dump_date": "2026-04" },
      "pubmed":          { "via": "baseline",                  "dump_date": "2026-01" },
      "crossmark":       { "via": "live_api_cached",           "fetched_at": "2026-05-21" },
      "funder_registry": { "via": "live_api_cached",           "fetched_at": "2026-05-21" },
      "event_data":      { "via": "live_api_cached",           "fetched_at": "2026-05-21" }
    }
  },

  "license_notes": { /* see License notes section below */ },

  "entries": { "e1": {...}, "e2": {...}, ... }
}
```

## Per-entry schema

```jsonc
"e7": {
  "toc_entry_id": "e7",

  "ext_ids": {
    "doi":            "10.1016/j.biocon.2010.08.015",
    "pmid":           "...",
    "pmcid":          null,
    "arxiv":          null,
    "fatcat_release": "...",
    "fatcat_work":    "...",
    "openalex":       "W..."
  },

  // Top-level fields surfaced for direct consumer access without drilling into
  // source blobs. Originals stay in their source blobs too — these are
  // convenience copies for entry-level browse / facet / display.
  "entry_type": "editorial",                // derived from crossref.type — primary classifier worth fast access
  "title":      "Conservation in the 21st Century",  // canonical, picked across crossref/openalex/pubmed
  "abstract":   "<jats:p>...</jats:p>",     // canonical, raw JATS from crossref (preferred) or reconstructed from openalex.abstract_inverted_index; DO NOT TRANSFORM
  "subjects":   ["Ecology"],                // copy of crossref.subject[]
  "topics":     [...],                      // copy of openalex.topics[]
  "concepts":   [...],                      // copy of openalex.concepts[]
  "retracted":  false,                      // derived from crossref.update-to + crossref.update-policy

  "match_method": "doi_lookup",
  "match_confidence": 1.0,

  // ----- Crossref: FULL payload, no fields stripped -----
  "crossref": { /* every field /works/{doi} returns */ },

  // ----- fatcat: FULL release record -----
  "fatcat":   { /* every field /release/{ident} returns + files */ },

  // ----- OpenAlex: FULL Work record -----
  "openalex": { /* every field /works/{id} returns */ },

  // ----- Unpaywall: FULL record -----
  "unpaywall": { /* every field /v2/{doi} returns */ },

  // ----- PubMed: FULL XML-parsed record (when biomed) -----
  "pubmed":   { /* every PubMed XML field */ },

  // ----- Crossmark detail (when retracted/corrected) -----
  // Populated only if crossref.update-policy is set or crossref.update-to[] is non-empty.
  "crossmark": {
    "version": "1.2",
    "assertions": [
      { "name": "publication_history",
        "value": "Received: 2010-03-01; Accepted: 2010-08-12; Published: 2010-11-15" },
      { "name": "peer_review",
        "value": "Single-blind external peer review" }
    ],
    "updates": [
      { "type": "correction",
        "DOI":  "10.1016/j.biocon.2011.02.003",
        "updated": { "date-parts": [[2011, 2, 15]] },
        "label": "Erratum to..." }
    ]
  },

  // ----- Funder Registry expansion -----
  // For each funder DOI in crossref.funder[], one-hop expansion.
  "funders_expanded": [
    {
      "DOI":        "10.13039/100000002",
      "name":       "National Institutes of Health",
      "alt_names":  ["NIH", "U.S. National Institutes of Health"],
      "country":    "United States",
      "parent":     null,
      "geonames":   { "id": "6252001", "name": "United States" }
    }
  ],

  // ----- relation[] one-hop traversal -----
  // For each entry in crossref.relation (has-preprint, is-version-of,
  // has-translation, is-supplemented-by, ...), fetch related DOI's metadata.
  // One-hop only — we don't follow the related work's own relations.
  "relations_expanded": [
    {
      "relation_type": "has-preprint",
      "target_doi":    "10.1101/2010.05.20.123456",
      "target_meta":   { "title": [...], "type": "posted-content", "issued": {...} }
    }
  ],

  // ----- Event Data lookup -----
  // Citations / mentions in blog posts, Wikipedia, news, etc.
  // Always inline — every event embedded in the articles file, no sidecars.
  // Outlier entries (e.g. viral COVID papers with 10K+ events) inflate the
  // file size; that's accepted. Counts + breakdown surfaced for fast access;
  // raw events sit in `events[]` for full detail.
  "event_data": {
    "total_events": 47,
    "sources": { "wikipedia": 3, "news": 12, "blog": 8, "twitter": 24 },
    "events": [
      { "source": "wikipedia",
        "obj_url": "https://en.wikipedia.org/wiki/...",
        "occurred_at": "2018-03-15T12:00:00Z",
        "subj_id": "wikipedia:...",
        "evidence_url": "...",
        /* ... every other field Crossref Event Data returned per event ... */
      }
      /* ... up to total_events events ... */
    ]
  }
}
```

## When a source has no data

`null` for any source means "no record found for this entry". Absence vs. null is non-significant — both are valid. Consumers MUST tolerate either.

## `entry_type` values

From `crossref.type`. Values seen in the wild (extensible):

| Value | Notes |
|---|---|
| `journal-article` | the bulk of `entries` |
| `editorial` | editorials, commentaries — routinely DOI'd by medical journals |
| `review-article` | review papers; (in)consistent across publishers |
| `book-review` | journal-published book reviews |
| `book-chapter` | book chapters appearing in journal-issue-style venues |
| `proceedings-article` | conference papers in journal-issue supplements |
| `letter` | letters to the editor |
| `report` | technical reports, "Reports" sections |
| `other` | corrections, news, calls-for-papers, miscellany |

Note: `journal-issue` and `journal-volume` records are NOT in `entries` — they're routed to `issue_meta` and `volume_meta` respectively.

Consumers MUST tolerate unknown `entry_type` values gracefully.

## `match_method` enum

| Value | Meaning |
|---|---|
| `doi_lookup` | TOC entry had a DOI → exact lookup. Confidence = 1.0. |
| `doi_from_crossref` | Crossref `/journals/{issn}/works` enumeration matched by (vol, iss). Confidence = 1.0. |
| `pmid_lookup` | TOC entry had a PMID → exact lookup. Confidence = 1.0. |
| `fuzzy_title_volume_issue` | No exact ID; matched fatcat by `(container, volume, issue)` + fuzzy title. |
| `pubmed_title_match` | Matched PubMed by title within journal+year. |
| `no_match` | Search attempted, nothing found. |
| `skip` | Did not attempt match (ads, frontmatter, etc.). |

`match_method` and `match_confidence` are always required, even when the value is implicit (e.g. confidence = 1.0 for `doi_lookup`). Consumers can rely on these fields always being present.

## Sidecar files

None — all data lives inline in `<item>_articles.json.gz`. Outlier issues (viral papers with 10K+ Event Data events, heavily-cited articles with 80 KB `reference[]`) inflate file size; accepted as the cost of "everything in one file."

## Size implications

Typical v2 issue file: **~150–600 KB compressed**, driven by:

- Per-entry full Crossref blob (including reference[] and abstract)
- Per-entry full OpenAlex Work record
- Per-entry full fatcat / unpaywall / pubmed records
- `issue_meta` (~3–5 KB raw / ~1 KB compressed) when present
- `volume_meta` (~3–5 KB raw / ~1 KB compressed) when present
- Crossmark per retracted/corrected article (~3 KB raw, sparse)
- `funders_expanded` (~1 KB per funder × few funders per article)
- `relations_expanded` (~5 KB per related work × sparse)
- `event_data` inline events (~0.3 KB per event × per-entry total; typical entries 0–50 events)
- Convenience flat fields (small — they're copies of data already in the blob)

**Outliers:** medical/biomed journals with rich Crossmark + many funders + heavy citation activity push toward ~1 MB. An issue containing viral-era papers (e.g. early-2020 COVID issues) with 10K+ Event Data events per entry could reach **several MB**. Accepted as the cost of "all data in one file."

## License notes

The `license_notes` field is informational; it does not override per-record `license[]` fields where present (notably Crossref's per-article license[] array stays intact inside the `crossref` subobject).

- **Bibliographic shell** (everything except abstracts): CC0 / public domain. Crossref treats bibliographic metadata as facts; per their own posture and US law, this is freely redistributable.
- **Abstracts**: deposited by publishers, retain original copyright. We store them as an indexing convenience; downstream redistribution inherits per-publisher terms.
- **Fatcat, OpenAlex, Unpaywall**: CC0.
- **PubMed**: US government work, public domain.
- **Crossref Event Data**: CC0 per Crossref's Event Data terms.
- **Funder Registry**: CC0.

## Update cadence and coupling

The articles file is a **function** of (current `_toc.json`) × (current source dumps + enrichments). Re-derive when any input changes.

- **Re-segmentation invalidates this file.** When `_toc.json` is regenerated, re-run the bibliographic join. The DOI lookups are O(1) hash lookups against locally-cached dumps; cost is negligible compared to OCR or LLM extraction.
- **Dump refresh.** Crossref and OpenAlex publish snapshots; mirrored to IA annually (see issue #8). Refresh cadence is a deployment policy decision, not a schema concern.
- **Per-entry enrichments (Crossmark / Funder Registry / relation / Event Data) cache by DOI.** Re-fetch policy: refresh on next regeneration if older than N days (TBD).
- **Staleness detection.** A consumer comparing `toc_generated_at` here vs. the live `_toc.json` `generated_at` can tell at a glance whether the articles file is current.

## Extension policy

- Extra top-level fields and extra per-entry fields are allowed; consumers MUST ignore unknown fields.
- Adding a new value to `match_method` or `entry_type` is non-breaking.
- Adding a new source (e.g. `semantic_scholar`) is a new top-level entry under `provenance.sources` and a new per-entry subobject; non-breaking.
- Removing or renaming an existing field bumps `schema_version`.

## What changed from v1

v2 is a hard cutover — every existing v1 file is rebuilt at v2 and replaced. No backward-compat work.

| Change | Rationale |
|---|---|
| `schema_version: 1 → 2` | makes the cutover explicit; consumers branch on version |
| Drop `type:journal-article` fetch filter | capture `journal-issue`, `journal-volume`, editorials, book-reviews, proceedings-articles, errata — every DOI registered for the issue |
| Drop `strip_periodical()` | keep `container-title`, `short-container-title`, `ISSN`, `issn-type`, `publisher`, `member`, `prefix`, `source` per-article so they're aggregable to `pub_*` collection level (see issue #1) |
| Remove v1's `issue_doi`/`special_issue_title`/`issue_editors` thin fields | replaced by full `issue_meta` block |
| New top-level `issue_meta` block | populated from Crossref `type:journal-issue` deposit |
| New top-level `volume_meta` block | populated from Crossref `type:journal-volume` deposit |
| New top-level `has_retracted_entries` flag | per-issue retraction badge without walking entries |
| New per-entry `entry_type` field | echoes `crossref.type` at entry top level |
| New per-entry convenience flat fields | `title`, `abstract` (raw JATS), `subjects`, `topics`, `concepts`, `retracted` — copies/derivations from source blobs for direct access |
| Per-source blobs no longer "slim" | v1 docs claimed openalex/unpaywall/pubmed/fatcat were slim projections; implementation has been storing full blobs since v1.0.2. v2 codifies "full blob per source." |
| New per-entry enrichments | Crossmark detail, Funder Registry expansion, relation 1-hop traversal, Event Data lookup — "we want everything Crossref has" (see issue #1 scope decision) |
| Abstract stored as raw JATS XML, never transformed | preserve structure; downstream renders/strips as needed |
| All upstream dates kept in their native format | `_articles.json.gz` is supposed to preserve raw; segart's own dates use ISO-8601 |
| Drop v1's per-entry `match_signature` | v1 used it to re-tie entries when TOC IDs reshuffled; v2's hard cutover rebuilds from scratch, recovery path moot |
| Drop v1's per-entry free-text `_note` | rarely populated, awkward to consume; if a human annotation is needed in the future, add a structured field rather than freeform text |
| `entries` stays a dict keyed by `e1`, `e2`, … | stable IDs let `_toc.json` reference entries reliably across rebuilds |
| `match_method` and `match_confidence` stay required even for `doi_lookup` | uniformity beats 4-byte savings per entry |
| `journal_meta` lives on `pub_*` collection items, NOT in `_articles.json.gz` | journal-level data belongs at journal level, not per-issue — see issue #1 |

Out of scope for v2:
- ❌ Crossref full-text content (publisher-URL gated, not our role)
- ❌ Crossref cited-by full list (Plus account required, defer)
- ❌ Per-issue author-record expansion (use `pub_*`-level author cache instead)

## Migration: v1 → v2

Hard cutover. Every existing v1 `_articles.json.gz` on IA gets rebuilt at v2 and replaced. Consumers branch on `schema_version` and the v1 branch can be deleted once the rebuild completes.

Steps:

1. Refetch year-level `crossref_full_cache/` with no type filter
2. Build v2 `_articles.json.gz` for new items
3. Rebuild v2 files for the existing 911 articles_pilot + 24 heur_xref-fix items, replacing them on IA atomically with a paired review post per the three-file provenance rule
4. Update consumers to read v2 (surface convenience fields, display `issue_meta.title` and `issue_meta.editor[]`, handle non-`journal-article` entry types)
5. Delete v1-only code paths

No grace period for mixed v1/v2 files in production — the rebuild step is the cutover.

## Open questions

1. **Multi-DOI entries**: a TOC entry mapping to multiple DOIs (multi-part article, very rare). v1's `ext_ids.doi` is singular; v2 keeps it singular. Defer until a real case shows up.

2. **Continuations across issues**: an article split across two issues currently appears in both `_toc.json` files. Whether the articles file should mark `is_continuation_of: <other_item>/<other_entry_id>` is unresolved.

3. **JSON Schema document**: should we publish a JSON Schema for v2 alongside this prose doc? Useful for downstream validation, but adds maintenance burden. Decision deferred until v2 ships and we see actual consumer needs.
