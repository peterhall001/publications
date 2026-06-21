# Edinburgh Cancer Informatics — publications pipeline

Builds `publications.json` for the ECI publications page on
https://cancer-data.ecrc.ed.ac.uk/.

## How it works

1. A vetted Zotero group collection is the allowlist (the source of truth for
   what appears). Group `4536042`, collection *Web-Publications* (`X3G67CXM`).
2. `scripts/build_publications.py` reads that collection and enriches each item
   from OpenAlex by DOI (citation count, open-access status and PDF link, venue,
   volume, issue and pages when they are missing in Zotero).
   It writes `publications.json`.
3. `scripts/discover_candidates.py` queries OpenAlex by the ORCIDs in
   `authors.json`, drops anything already in the collection, enriches candidate
   DOIs from Crossref with OpenAlex as fallback, and writes richer records into
   the Zotero Review collection plus `candidates_for_review.md` — your vetting
   queue.
4. `.github/workflows/build.yml` runs both daily and commits the results.
5. WordPress reads the committed `publications.json` and renders the page.

## Vetting workflow

- Read `candidates_for_review.md` after each run.
- Add the genuine entries to the *Web-Publications* collection in Zotero.
- The next build picks them up automatically.

## Fresh Review rebuild for richer metadata

If the current Zotero contents have not yet been vetted, the cleanest way to get
better volume, issue and page metadata is to rebuild the Review collection rather
than patching individual Zotero records by hand.

Recommended process:

1. In Zotero, manually empty or recreate the Review collection. Do the same for
   Web-Publications and Rejected only if you are sure nothing there needs to be
   preserved.
2. Run discovery with a recent `SINCE_YEAR` first so you can inspect the result
   before pulling in the whole back-catalogue.
3. The discovery script will:
   - discover candidate article DOIs from OpenAlex by ORCID;
   - enrich each DOI from Crossref, preferring publisher bibliographic metadata;
   - fall back to OpenAlex `biblio` fields when Crossref lacks a value;
   - create Zotero Review items with `publicationTitle`, `volume`, `issue`,
     `pages`, `date`, `DOI`, authors and URL where available.
4. Vet in Zotero by moving genuine publications from Review into
   Web-Publications and moving false positives into Rejected.

Do not auto-populate Web-Publications directly from OpenAlex discovery. OpenAlex
author matching can include false positives, so Review remains the safety gate.


## Secrets / variables

| Name                 | Value                          |
|----------------------|--------------------------------|
| ZOTERO_COLLECTION_ID | X3G67CXM                       |
| ZOTERO_API_KEY       | a read key (optional, group is public) |
| OPENALEX_MAILTO      | email, for the polite pool |
