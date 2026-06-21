# AGENTS.md

Guidance for AI agents and developers working in this repository.

## What this repo does

This repo maintains `publications.json`, the data behind the Edinburgh Cancer
Informatics publications page at https://cancer-data.ecrc.ed.ac.uk/. A GitHub
Actions workflow runs daily, reads a vetted Zotero collection, enriches it from
OpenAlex, and commits the result. WordPress reads the committed file over the
jsDelivr CDN. There is no server and no database.

The source of truth is a Zotero group collection that a human curates. OpenAlex
provides discovery and enrichment only. Nothing reaches the page without passing
through the Zotero vetting step.

## Layout

- `scripts/build_publications.py`: reads the approved Zotero collection,
  enriches each DOI from OpenAlex (citation count, open access, volume, issue,
  pages), backfills missing DOIs by confident title match, and writes
  `publications.json`. Standard library only, no dependencies.
- `scripts/discover_candidates.py`: queries OpenAlex by ORCID for each author in
  `authors.json`, removes anything already filed in Zotero, and adds the rest to
  the Review collection for a human to triage. Also writes `candidates.json` and
  `candidates_for_review.md` as logs. Requires `pyzotero`.
- `authors.json`: the team. One entry per person with an `orcid`, plus an
  optional `since_year` to narrow that person's scan window (the default is 2015).
- `.github/workflows/build.yml`: runs both scripts daily and on demand, then
  commits the outputs.
- `publications.json`, `candidates.json`, `candidates_for_review.md`: generated
  and committed outputs. Do not edit by hand.
- The WordPress plugin is not in this repo. It lives on the WordPress server and
  only reads `publications.json`.

## Zotero collections (group 4536042)

- Web-Publications `X3G67CXM`: approved. This is what the page shows.
- Review `69RXNW6D`: the auto-filled inbox. Triage happens here.
- Rejected `5DCC26JV`: suppressed items.

Triage convention: approve by moving an item from Review into Web-Publications,
reject by moving it into Rejected. Discovery dedups across all three collections,
so a sorted item never returns.

## Configuration

All configuration is via GitHub Actions secrets, never in code:

- `ZOTERO_API_KEY`: needs read and write on the group. Reads alone work without
  it because the group is public, but the discovery write needs it.
- `ZOTERO_COLLECTION_ID` = `X3G67CXM`
- `ZOTERO_REVIEW_COLLECTION_ID` = `69RXNW6D`
- `ZOTERO_REJECTED_COLLECTION_ID` = `5DCC26JV`
- `OPENALEX_MAILTO`: an email address, for the OpenAlex polite pool.

The group ID `4536042` is public and is set directly in the workflow and scripts.

## Running locally

Build (read-only, no key needed):

```
OPENALEX_MAILTO=you@example.org ZOTERO_COLLECTION_ID=X3G67CXM \
  python scripts/build_publications.py publications.json
```

Discover (writes to Zotero only when the review collection and a write-scoped key
are both set; otherwise it logs and writes nothing):

```
pip install pyzotero
OPENALEX_MAILTO=you@example.org ZOTERO_COLLECTION_ID=X3G67CXM \
  ZOTERO_REVIEW_COLLECTION_ID=69RXNW6D ZOTERO_REJECTED_COLLECTION_ID=5DCC26JV \
  ZOTERO_API_KEY=... SINCE_YEAR=2024 \
  python scripts/discover_candidates.py
```

Use a recent `SINCE_YEAR` when testing so you do not pull the whole
back-catalogue into Review.

## publications.json schema (version 2)

Top level: `schema_version`, `generated_at`, `source`, `count`,
`doi_backfilled`, `publications`.

Each publication: `zotero_key`, `item_type`, `title`, `authors` (a list of
`{family, given}`), `year`, `venue`, `volume`, `issue`, `pages`, `doi`, `url`,
`cited_by_count`, `is_oa`, `oa_url`, `enriched`, `doi_source` (one of `zotero`,
`openalex-title-match`, or null), and `title_match_score`.

## Conventions and things that will bite you

- Dedup must match on title as well as DOI. Some Zotero items have no DOI, and a
  DOI-only check re-adds them from OpenAlex as duplicates. This has already
  happened once and produced a large number of duplicate records.
- OpenAlex author clustering is unreliable. It mixes our Peter Hall with an
  unrelated urban planner of the same name. This is why discovery feeds a human
  review step rather than the page directly. Never wire OpenAlex straight to the
  published list.
- Enrichment keys on DOI. Items with no DOI in Zotero get no citation count or
  open-access link unless the title-match backfill resolves one. Adding the DOIs
  in Zotero is the durable fix and also closes the dedup blind spot above.
- The build is dependency-free on purpose. Keep it that way. Only discovery uses
  `pyzotero`.
- `generated_at` changes on every run, so every build commits. This is
  deliberate. It keeps the scheduled workflow from being disabled after 60 days
  of no repository activity.
- Be polite to OpenAlex: send `mailto`, batch DOIs in groups of 50 or fewer, and
  keep the retry and backoff on 429 and 5xx responses.
- Do not add an `itemType` filter to the Zotero read URL. The API rejects
  multi-type negation and this broke the build once. Filter item types in code
  instead.
- Citations on the page follow Vancouver style, capped at 20 authors then et al.

## What an agent should not do here

- Do not commit secrets or hard-code API keys. If a key appears in a diff or a
  log, treat it as compromised and have it rotated.
- Do not auto-approve candidates into Web-Publications. The human vetting gate is
  the whole point of the design.
- Do not perform destructive Zotero operations such as deletes or merges from a
  script. Duplicate cleanup is done by hand in the Zotero client.
- Do not edit the generated JSON or markdown outputs directly. Change the scripts
  and let the workflow regenerate them.
