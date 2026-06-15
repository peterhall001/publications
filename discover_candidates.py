#!/usr/bin/env python3
"""
Find candidate publications for the team and write a review queue.

Reads authors.json, queries OpenAlex by ORCID for each, drops anything already
in the vetted Zotero collection, and writes candidates_for_review.md (+ .json).
It never writes to Zotero. Vetting stays manual: you read the queue, then add
the good ones to the Web-Publications collection by hand.

Env vars: ZOTERO_GROUP_ID, ZOTERO_COLLECTION_ID, ZOTERO_API_KEY (optional),
          OPENALEX_MAILTO, plus optional SINCE_YEAR (default: 3 years back).
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_publications import (  # reuse the verified helpers
    fetch_zotero_collection_items, extract_from_zotero, normalise_doi,
    OPENALEX_BASE, OPENALEX_MAILTO, USER_AGENT,
)

THIS_YEAR = datetime.now(timezone.utc).year
SINCE_YEAR = int(os.environ.get("SINCE_YEAR", THIS_YEAR - 3))


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def works_for_orcid(orcid):
    mailto = ("&mailto=" + urllib.parse.quote(OPENALEX_MAILTO)) if OPENALEX_MAILTO else ""
    cursor = "*"
    found = []
    while cursor:
        url = (
            f"{OPENALEX_BASE}/works"
            f"?filter=author.orcid:{orcid},from_publication_date:{SINCE_YEAR}-01-01,type:article"
            f"&select=doi,display_name,publication_year,cited_by_count,authorships,primary_location"
            f"&per-page=100&cursor={cursor}{mailto}"
        )
        data = get_json(url)
        for w in data.get("results", []):
            doi = normalise_doi(w.get("doi"))
            if not doi:
                continue
            loc = w.get("primary_location") or {}
            src = (loc.get("source") or {}) if loc else {}
            found.append({
                "doi": doi,
                "title": w.get("display_name", ""),
                "year": w.get("publication_year"),
                "venue": src.get("display_name") or "",
                "cited_by_count": w.get("cited_by_count", 0),
            })
        cursor = data.get("meta", {}).get("next_cursor")
        time.sleep(0.2)
    return found


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "authors.json"), encoding="utf-8") as f:
        authors = [a for a in json.load(f)["authors"] if a.get("orcid")]

    items, _ = fetch_zotero_collection_items()
    existing_dois = {extract_from_zotero(i)["doi"] for i in items if extract_from_zotero(i)["doi"]}

    candidates = {}  # doi -> record, deduped across authors
    for a in authors:
        for w in works_for_orcid(a["orcid"]):
            if w["doi"] in existing_dois:
                continue
            rec = candidates.setdefault(w["doi"], dict(w, matched_authors=[]))
            rec["matched_authors"].append(a["name"])

    ranked = sorted(candidates.values(), key=lambda r: (-(r["year"] or 0), -r["cited_by_count"]))

    out_json = os.path.join(here, "candidates.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "since_year": SINCE_YEAR, "count": len(ranked), "candidates": ranked},
                  f, indent=2, ensure_ascii=False)

    out_md = os.path.join(here, "candidates_for_review.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(f"# Candidate publications for review\n\n")
        f.write(f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}. "
                f"Window: {SINCE_YEAR} onwards. {len(ranked)} not yet in the collection.\n\n")
        f.write("Add the genuine ones to the *Web-Publications* Zotero collection. "
                "Ignore mis-attributed entries (OpenAlex author clustering is imperfect).\n\n")
        for r in ranked:
            who = ", ".join(r["matched_authors"])
            f.write(f"- **{r['year']}** — {r['title']}  \n")
            f.write(f"  _{r['venue']}_ · cited {r['cited_by_count']} · "
                    f"[{r['doi']}](https://doi.org/{r['doi']}) · matched: {who}\n")
    print(f"{len(ranked)} candidates written to candidates_for_review.md and candidates.json")


if __name__ == "__main__":
    main()
