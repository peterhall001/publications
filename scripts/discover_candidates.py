#!/usr/bin/env python3
"""
Find candidate publications and add them to a Zotero Review collection for vetting.

Reads authors.json, queries OpenAlex by ORCID for each (per-author window),
drops anything already in the approved (Web-Publications), Review or Rejected
collections, and writes the remainder into the Review collection as
journalArticle items tagged 'needs-review'. You triage them in Zotero: drag
keepers into Web-Publications, move rejects into Rejected. Both moves suppress
the item from future runs, so Review stays a clean pending queue. Nothing is
ever deleted automatically.

Also writes candidates.json / candidates_for_review.md as a log.

Env vars:
  ZOTERO_GROUP_ID               4536042
  ZOTERO_COLLECTION_ID          approved collection key (Web-Publications)
  ZOTERO_REVIEW_COLLECTION_ID   review/inbox collection key (required to write)
  ZOTERO_REJECTED_COLLECTION_ID rejected collection key (suppressed)
  ZOTERO_API_KEY                must have WRITE access to the group
  OPENALEX_MAILTO               your email, for the polite pool
  SINCE_YEAR                    default scan window (default 2015)
"""

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from pyzotero import zotero

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS_DIR)

GROUP_ID = os.environ.get("ZOTERO_GROUP_ID", "4536042")
APPROVED_COLL = os.environ.get("ZOTERO_COLLECTION_ID", "")
REVIEW_COLL = os.environ.get("ZOTERO_REVIEW_COLLECTION_ID", "")
REJECTED_COLL = os.environ.get("ZOTERO_REJECTED_COLLECTION_ID", "")
API_KEY = os.environ.get("ZOTERO_API_KEY", "").strip()
OPENALEX_MAILTO = os.environ.get("OPENALEX_MAILTO", "").strip()
DEFAULT_SINCE_YEAR = int(os.environ.get("SINCE_YEAR", 2015))

OPENALEX_BASE = "https://api.openalex.org"
USER_AGENT = "eci-publications-builder/1.0 (mailto:%s)" % (OPENALEX_MAILTO or "unknown")
MAX_CREATORS = 50


def normalise_doi(raw):
    if not raw:
        return None
    doi = raw.strip().lower()
    for p in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(p):
            doi = doi[len(p):]
    return doi or None


def split_name(display):
    parts = (display or "").strip().split()
    if not parts:
        return {"creatorType": "author", "name": display or ""}
    if len(parts) == 1:
        return {"creatorType": "author", "firstName": "", "lastName": parts[0]}
    return {"creatorType": "author", "firstName": " ".join(parts[:-1]), "lastName": parts[-1]}


def oa_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def works_for_orcid(orcid, since_year):
    mailto = ("&mailto=" + urllib.parse.quote(OPENALEX_MAILTO)) if OPENALEX_MAILTO else ""
    cursor = "*"
    found = []
    while cursor:
        url = (
            f"{OPENALEX_BASE}/works"
            f"?filter=author.orcid:{orcid},from_publication_date:{since_year}-01-01,type:article"
            f"&select=doi,display_name,publication_year,cited_by_count,authorships,primary_location"
            f"&per-page=100&cursor={cursor}{mailto}"
        )
        data = oa_get(url)
        for w in data.get("results", []):
            doi = normalise_doi(w.get("doi"))
            if not doi:
                continue
            loc = w.get("primary_location") or {}
            src = (loc.get("source") or {}) if loc else {}
            authors = [a.get("author", {}).get("display_name", "")
                       for a in (w.get("authorships") or [])]
            found.append({
                "doi": doi,
                "title": w.get("display_name", "") or "",
                "year": w.get("publication_year"),
                "venue": src.get("display_name") or "",
                "cited_by_count": w.get("cited_by_count", 0),
                "authors": [a for a in authors if a][:MAX_CREATORS],
            })
        cursor = data.get("meta", {}).get("next_cursor")
        time.sleep(0.2)
    return found


def collection_dois(zot, coll_key):
    if not coll_key:
        return set()
    dois = set()
    for it in zot.everything(zot.collection_items_top(coll_key)):
        d = normalise_doi(it.get("data", {}).get("DOI"))
        if d:
            dois.add(d)
    return dois


def to_zotero_item(rec):
    creators = [split_name(a) for a in rec["authors"]] or [
        {"creatorType": "author", "firstName": "", "lastName": ""}]
    matched = ", ".join(rec.get("matched_authors", []))
    return {
        "itemType": "journalArticle",
        "title": rec["title"],
        "creators": creators,
        "publicationTitle": rec["venue"],
        "date": str(rec["year"]) if rec["year"] else "",
        "DOI": rec["doi"],
        "url": "https://doi.org/" + rec["doi"],
        "extra": f"Auto-added from OpenAlex. Matched: {matched}. "
                 f"cited_by_count at add: {rec['cited_by_count']}.",
        "tags": [{"tag": "needs-review"}, {"tag": "auto-added"}],
        "collections": [REVIEW_COLL] if REVIEW_COLL else [],
    }


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def main():
    with open(os.path.join(ROOT, "authors.json"), encoding="utf-8") as f:
        authors = [a for a in json.load(f)["authors"] if a.get("orcid")]

    zot = zotero.Zotero(GROUP_ID, "group", API_KEY or None)
    seen = (collection_dois(zot, APPROVED_COLL)
            | collection_dois(zot, REVIEW_COLL)
            | collection_dois(zot, REJECTED_COLL))

    windows, candidates = {}, {}
    for a in authors:
        since = int(a.get("since_year", DEFAULT_SINCE_YEAR))
        windows[a["name"]] = since
        for w in works_for_orcid(a["orcid"], since):
            if w["doi"] in seen:
                continue
            rec = candidates.setdefault(w["doi"], dict(w, matched_authors=[]))
            if a["name"] not in rec["matched_authors"]:
                rec["matched_authors"].append(a["name"])

    ranked = sorted(candidates.values(),
                    key=lambda r: (-(r["year"] or 0), -r["cited_by_count"]))

    # --- logs ---
    with open(os.path.join(ROOT, "candidates.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "default_since_year": DEFAULT_SINCE_YEAR, "author_windows": windows,
                   "count": len(ranked), "candidates": ranked}, f, indent=2, ensure_ascii=False)
    with open(os.path.join(ROOT, "candidates_for_review.md"), "w", encoding="utf-8") as f:
        f.write("# Candidate publications added to Zotero Review\n\n")
        f.write(f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}. "
                f"{len(ranked)} new this run.\n\n")
        f.write("Windows scanned: "
                + ", ".join(f"{n} from {y}" for n, y in windows.items()) + ".\n\n")
        for r in ranked:
            f.write(f"- **{r['year']}** {r['title']} — _{r['venue']}_ "
                    f"· [{r['doi']}](https://doi.org/{r['doi']}) · {', '.join(r['matched_authors'])}\n")

    # --- write to Zotero Review collection ---
    items = [to_zotero_item(r) for r in ranked]
    zot.check_items(items)  # validate field names against the schema first

    if not REVIEW_COLL:
        print(f"{len(items)} candidates found. ZOTERO_REVIEW_COLLECTION_ID not set, "
              f"so nothing written to Zotero. Logs updated.")
        return
    if not API_KEY:
        print(f"{len(items)} candidates found, but ZOTERO_API_KEY is empty, "
              f"so cannot write. Logs updated.")
        return

    added, failed = 0, 0
    for batch in chunked(items, 50):
        resp = zot.create_items(batch)
        added += len(resp.get("successful", {}))
        failed += len(resp.get("failed", {}))
        if resp.get("failed"):
            print("Some items failed:", json.dumps(resp["failed"], indent=2)[:1000])
        time.sleep(0.3)
    print(f"Added {added} new candidates to the Review collection "
          f"({failed} failed) out of {len(items)} found.")


if __name__ == "__main__":
    main()
