# Edinburgh Cancer Informatics — publications pipeline

Builds `publications.json` for the ECI publications page on
https://cancer-data.ecrc.ed.ac.uk/.

## How it works

1. A vetted Zotero group collection is the allowlist (the source of truth for
   what appears). Group `4536042`, collection *Web-Publications* (`X3G67CXM`).
2. `scripts/build_publications.py` reads that collection and enriches each item
   from OpenAlex by DOI (citation count, open-access status and PDF link, venue).
   It writes `publications.json`.
3. `scripts/discover_candidates.py` queries OpenAlex by the ORCIDs in
   `authors.json`, drops anything already in the collection, and writes
   `candidates_for_review.md` — your vetting queue. It never writes to Zotero.
4. `.github/workflows/build.yml` runs both daily and commits the results.
5. WordPress reads the committed `publications.json` and renders the page.

## Vetting workflow

- Read `candidates_for_review.md` after each run.
- Add the genuine entries to the *Web-Publications* collection in Zotero.
- The next build picks them up automatically.


## Secrets / variables

| Name                 | Value                          |
|----------------------|--------------------------------|
| ZOTERO_COLLECTION_ID | X3G67CXM                       |
| ZOTERO_API_KEY       | a read key (optional, group is public) |
| OPENALEX_MAILTO      | email, for the polite pool |

