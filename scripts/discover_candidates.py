#!/usr/bin/env python3
"""
Find candidate publications and add them to a Zotero Review collection for vetting.

Reads authors.json, queries OpenAlex by ORCID for each person, then by any
OpenAlex author entity IDs configured for them (openalex_id / openalex_ids),
within a per-author window. Drops anything already in the approved
(Web-Publications), Review or Rejected collections, matching on DOI and then on
normalised title, and writes the remainder into the Review collection as
journalArticle items tagged 'needs-review'. You triage them in Zotero: drag
keepers into Web-Publications, move rejects into Rejected. Both moves suppress
the item from future runs, so Review stays a clean pending queue. Nothing is
ever deleted automatically.

A work whose title matches a filed item under a different DOI is usually a
preprint that now has a published version. It is held back from Review and
reported in the logs, because replacing a DOI on a filed item is a decision for
a person.

Also writes candidates.json / candidates_for_review.md as a log, including a
per-author table of works returned per identifier. An author whose identifiers
return nothing at all is flagged at the top of the markdown: OpenAlex matches
author.orcid against its own author entity, so an ORCID in authors.json
guarantees nothing.

If OpenAlex answers 429 because the daily prepaid budget is spent, the script
exits non-zero rather than retrying and reporting an empty run.

Env vars:
  ZOTERO_GROUP_ID               4536042
  ZOTERO_COLLECTION_ID          approved collection key (Web-Publications)
  ZOTERO_REVIEW_COLLECTION_ID   review/inbox collection key (required to write)
  ZOTERO_REJECTED_COLLECTION_ID rejected collection key (suppressed)
  ZOTERO_API_KEY                must have WRITE access to the group
  OPENALEX_MAILTO               your email, for the polite pool
  SINCE_YEAR                    default scan window (default 2015)
"""

import difflib
import json
import os
import re
import sys
import time
import urllib.error
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
CROSSREF_BASE = "https://api.crossref.org"
USER_AGENT = "eci-publications-builder/1.0 (mailto:%s)" % (OPENALEX_MAILTO or "unknown")
MAX_CREATORS = 50

# Same threshold as build_publications.py's title-match fallback.
TITLE_MATCH_THRESHOLD = 0.92


class OpenAlexBudgetExhausted(RuntimeError):
    """OpenAlex refused the request because the daily prepaid budget is spent.

    Backoff cannot help with this, unlike an ordinary 429 rate limit.
    """


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


def normalise_title(s):
    """build_publications.py's normalisation, plus dropping a (preprint) suffix."""
    s = (s or "").lower()
    s = re.sub(r"\s*[\(\[]\s*preprint\s*[\)\]]\s*$", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def is_budget_exhausted(code, body):
    """True when a 429 says the prepaid budget is spent rather than rate-limited."""
    if code != 429:
        return False
    text = (body or "").lower()
    return "budget" in text or "prepaid" in text


def get_json(url, retries=4):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = ""
                if is_budget_exhausted(e.code, body):
                    raise OpenAlexBudgetExhausted(
                        f"429 from {urllib.parse.urlsplit(url).netloc}: {body.strip()[:300]}") from e
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise


def openalex_entity_ids(author):
    """Configured OpenAlex author entity IDs for one person, as bare A-numbers."""
    raw = []
    if author.get("openalex_id"):
        raw.append(author["openalex_id"])
    raw.extend(author.get("openalex_ids") or [])
    ids = []
    for value in raw:
        value = str(value).strip().rstrip("/").rsplit("/", 1)[-1].upper()
        if value and value not in ids:
            ids.append(value)
    return ids


def author_filters(author):
    """Every OpenAlex filter to try for one person: ORCID first, then entity IDs.

    Returns (label, filter) pairs. The label is what the per-author table shows.
    """
    filters = []
    if author.get("orcid"):
        filters.append(("orcid", f"author.orcid:{author['orcid'].strip()}"))
    for entity in openalex_entity_ids(author):
        filters.append((f"openalex_id:{entity}", f"author.id:{entity}"))
    return filters


def works_for_filter(author_filter, since_year):
    mailto = ("&mailto=" + urllib.parse.quote(OPENALEX_MAILTO)) if OPENALEX_MAILTO else ""
    cursor = "*"
    found = []
    while cursor:
        url = (
            f"{OPENALEX_BASE}/works"
            f"?filter={author_filter},from_publication_date:{since_year}-01-01,type:article"
            f"&select=doi,display_name,publication_year,cited_by_count,authorships,primary_location,biblio"
            f"&per-page=100&cursor={cursor}{mailto}"
        )
        data = get_json(url)
        for w in data.get("results", []):
            doi = normalise_doi(w.get("doi"))
            if not doi:
                continue
            loc = w.get("primary_location") or {}
            src = (loc.get("source") or {}) if loc else {}
            bib = w.get("biblio") or {}
            fp, lp = bib.get("first_page"), bib.get("last_page")
            pages = f"{fp}-{lp}" if fp and lp else (fp or lp or "")
            authors = [a.get("author", {}).get("display_name", "")
                       for a in (w.get("authorships") or [])]
            found.append({
                "doi": doi,
                "title": w.get("display_name", "") or "",
                "year": w.get("publication_year"),
                "venue": src.get("display_name") or "",
                "volume": bib.get("volume") or "",
                "issue": bib.get("issue") or "",
                "pages": pages,
                "cited_by_count": w.get("cited_by_count", 0),
                "authors": [a for a in authors if a][:MAX_CREATORS],
                "metadata_source": "openalex",
            })
        cursor = data.get("meta", {}).get("next_cursor")
        time.sleep(0.2)
    return found


def first(value):
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def crossref_year(msg):
    for key in ("published-print", "published-online", "published", "issued"):
        parts = (msg.get(key) or {}).get("date-parts") or []
        if parts and parts[0]:
            return parts[0][0]
    return None


def crossref_authors(msg):
    creators = []
    for a in msg.get("author") or []:
        family = (a.get("family") or "").strip()
        given = (a.get("given") or "").strip()
        name = (a.get("name") or "").strip()
        if family or given:
            creators.append({"creatorType": "author", "firstName": given, "lastName": family})
        elif name:
            creators.append({"creatorType": "author", "name": name})
        if len(creators) >= MAX_CREATORS:
            break
    return creators


def crossref_metadata(doi):
    """Return publisher metadata for a DOI from Crossref, or {} if unavailable."""
    mailto = ("?mailto=" + urllib.parse.quote(OPENALEX_MAILTO)) if OPENALEX_MAILTO else ""
    url = f"{CROSSREF_BASE}/works/{urllib.parse.quote(doi, safe='')}{mailto}"
    try:
        data = get_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        print(f"Crossref lookup failed for {doi}: HTTP {e.code}; using OpenAlex metadata")
        return {}
    except Exception as e:
        print(f"Crossref lookup failed for {doi}: {e}; using OpenAlex metadata")
        return {}
    msg = data.get("message") or {}
    pages = msg.get("page") or first(msg.get("article-number")) or ""
    return {
        "title": first(msg.get("title")),
        "year": crossref_year(msg),
        "venue": first(msg.get("container-title")),
        "volume": msg.get("volume") or "",
        "issue": msg.get("issue") or "",
        "pages": pages,
        "creators": crossref_authors(msg),
        "metadata_source": "crossref",
    }


def prefer_metadata(rec, metadata, fields):
    for field in fields:
        if metadata.get(field):
            rec[field] = metadata[field]


def enrich_candidates_from_crossref(records):
    """Prefer Crossref publisher metadata, keeping OpenAlex as fallback."""
    for rec in records:
        md = crossref_metadata(rec["doi"])
        if md:
            prefer_metadata(rec, md, ("title", "year", "venue", "volume", "issue", "pages"))
            rec["metadata_source"] = "crossref+openalex"
            if md.get("creators"):
                rec["creators"] = md["creators"]
        time.sleep(0.1)


def collection_entries(zot, coll_key, coll_name):
    """Every top-level item in a collection as {collection, key, doi, title, norm}."""
    if not coll_key:
        return []
    entries = []
    for it in zot.everything(zot.collection_items_top(coll_key)):
        data = it.get("data", {})
        if data.get("itemType") in ("attachment", "note", "annotation"):
            continue
        entries.append({
            "collection": coll_name,
            "key": data.get("key") or it.get("key"),
            "doi": normalise_doi(data.get("DOI")),
            "title": data.get("title") or "",
            "norm": normalise_title(data.get("title")),
        })
    return entries


class FiledIndex:
    """Lookup of everything already filed, by DOI and by normalised title."""

    def __init__(self, entries):
        self.count = len(entries)
        self.by_doi, self.by_title = {}, {}
        for e in entries:
            if e["doi"]:
                self.by_doi.setdefault(e["doi"], e)
            if e["norm"]:
                self.by_title.setdefault(e["norm"], e)
        self.titles = list(self.by_title)

    def match(self, doi, title):
        """Classify a work against filed items. Returns (status, entry, score).

        present: DOI already filed, or title matches a filed item with no DOI.
        version_update: title matches a filed item that has a different DOI.
        None: not filed.
        """
        if doi and doi in self.by_doi:
            return "present", self.by_doi[doi], 1.0
        norm = normalise_title(title)
        if not norm:
            return None, None, None
        entry, score = self.by_title.get(norm), 1.0
        if entry is None:
            close = difflib.get_close_matches(norm, self.titles, n=1, cutoff=TITLE_MATCH_THRESHOLD)
            if not close:
                return None, None, None
            entry = self.by_title[close[0]]
            score = difflib.SequenceMatcher(None, norm, close[0]).ratio()
        if entry["doi"] and entry["doi"] != doi:
            return "version_update", entry, round(score, 3)
        return "present", entry, round(score, 3)


def to_zotero_item(rec):
    creators = rec.get("creators") or [split_name(a) for a in rec["authors"]] or [
        {"creatorType": "author", "firstName": "", "lastName": ""}]
    matched = ", ".join(rec.get("matched_authors", []))
    return {
        "itemType": "journalArticle",
        "title": rec["title"],
        "creators": creators,
        "publicationTitle": rec["venue"],
        "volume": rec.get("volume", ""),
        "issue": rec.get("issue", ""),
        "pages": rec.get("pages", ""),
        "date": str(rec["year"]) if rec["year"] else "",
        "DOI": rec["doi"],
        "url": "https://doi.org/" + rec["doi"],
        "extra": f"Auto-added from OpenAlex. Matched: {matched}. "
                 f"Metadata: {rec.get('metadata_source', 'openalex')}. "
                 f"cited_by_count at add: {rec['cited_by_count']}.",
        "tags": [{"tag": "needs-review"}, {"tag": "auto-added"}],
        "collections": [REVIEW_COLL] if REVIEW_COLL else [],
    }


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def load_authors():
    with open(os.path.join(ROOT, "authors.json"), encoding="utf-8") as f:
        return [a for a in json.load(f)["authors"] if author_filters(a)]


def write_logs(ranked, version_updates, windows, per_author):
    generated = datetime.now(timezone.utc)
    silent = [name for name, p in per_author.items() if not any(p["works_by_identifier"].values())]
    with open(os.path.join(ROOT, "candidates.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_at": generated.isoformat(timespec="seconds"),
                   "default_since_year": DEFAULT_SINCE_YEAR, "author_windows": windows,
                   "count": len(ranked), "candidates": ranked,
                   "version_update_count": len(version_updates),
                   "version_updates": version_updates,
                   "authors_with_no_works": silent,
                   "per_author": per_author}, f, indent=2, ensure_ascii=False)
    with open(os.path.join(ROOT, "candidates_for_review.md"), "w", encoding="utf-8") as f:
        f.write("# Candidate publications added to Zotero Review\n\n")
        if silent:
            f.write("> **Warning: no works returned for "
                    + ", ".join(silent) + ".** Every configured identifier for these people "
                    "returned nothing from OpenAlex. That usually means their OpenAlex author "
                    "entity has no ORCID linked, not that they have not published. Check the "
                    "authorships of a recent paper in OpenAlex and add an `openalex_id`.\n\n")
        f.write(f"Generated {generated:%Y-%m-%d %H:%M UTC}. "
                f"{len(ranked)} new this run, {len(version_updates)} possible version "
                f"update{'' if len(version_updates) == 1 else 's'} held back.\n\n")
        f.write("Windows scanned: "
                + ", ".join(f"{n} from {y}" for n, y in windows.items()) + ".\n\n")
        for r in ranked:
            details = ", ".join(part for part in (r.get("volume"), r.get("issue"), r.get("pages")) if part)
            details = f" · {details}" if details else ""
            f.write(f"- **{r['year']}** {r['title']} — _{r['venue']}_{details} "
                    f"· [{r['doi']}](https://doi.org/{r['doi']}) · {', '.join(r['matched_authors'])}\n")

        if version_updates:
            f.write("\n## Possible version updates (not added to Review)\n\n"
                    "The title matches an item already filed under a different DOI, usually a "
                    "preprint with a published version. Decide by hand whether to replace the "
                    "DOI on the filed item.\n\n")
            for v in version_updates:
                filed = v["filed"]
                f.write(f"- **{v['year']}** {v['title']} · [{v['doi']}](https://doi.org/{v['doi']}) "
                        f"· {', '.join(v['matched_authors'])} · filed in {filed['collection']} as "
                        f"`{filed['key']}` with DOI `{filed['doi']}` (title score {v['title_score']})\n")

        f.write("\n## Per-author discovery\n\n"
                "Works returned by OpenAlex for each identifier in the scan window. A zero "
                "against every identifier means discovery is blind for that person.\n\n"
                "| Author | From | Works per identifier | Already filed | New candidates | Version updates |\n"
                "|---|---|---|---|---|---|\n")
        for name, p in per_author.items():
            ids = " · ".join(f"{label}: {n}" for label, n in p["works_by_identifier"].items())
            f.write(f"| {name} | {p['since_year']} | {ids} | {p['already_filed']} "
                    f"| {p['new_candidates']} | {p['version_updates']} |\n")


def main():
    authors = load_authors()

    zot = zotero.Zotero(GROUP_ID, "group", API_KEY or None)
    filed = FiledIndex(collection_entries(zot, APPROVED_COLL, "Web-Publications")
                       + collection_entries(zot, REVIEW_COLL, "Review")
                       + collection_entries(zot, REJECTED_COLL, "Rejected"))
    print(f"Indexed {filed.count} filed Zotero items.")

    windows, candidates, updates, per_author = {}, {}, {}, {}
    for a in authors:
        name = a["name"]
        since = int(a.get("since_year", DEFAULT_SINCE_YEAR))
        windows[name] = since
        stats = per_author[name] = {"since_year": since, "works_by_identifier": {},
                                    "already_filed": 0, "new_candidates": 0, "version_updates": 0}
        person_works = {}
        for label, author_filter in author_filters(a):
            works = works_for_filter(author_filter, since)
            stats["works_by_identifier"][label] = len(works)
            for w in works:
                person_works.setdefault(w["doi"], w)

        for doi, w in person_works.items():
            status, entry, score = filed.match(doi, w["title"])
            if status == "present":
                stats["already_filed"] += 1
                continue
            if status == "version_update":
                stats["version_updates"] += 1
                rec = updates.setdefault(doi, {
                    "doi": doi, "title": w["title"], "year": w["year"], "venue": w["venue"],
                    "matched_authors": [], "title_score": score,
                    "filed": {k: entry[k] for k in ("collection", "key", "doi", "title")}})
            else:
                stats["new_candidates"] += 1
                rec = candidates.setdefault(doi, dict(w, matched_authors=[]))
            if name not in rec["matched_authors"]:
                rec["matched_authors"].append(name)

    enrich_candidates_from_crossref(candidates.values())

    ranked = sorted(candidates.values(),
                    key=lambda r: (-(r["year"] or 0), -r["cited_by_count"]))
    version_updates = sorted(updates.values(), key=lambda r: (-(r["year"] or 0), r["doi"]))

    write_logs(ranked, version_updates, windows, per_author)
    for v in version_updates:
        print(f"Version update held back: {v['doi']} matches {v['filed']['collection']} item "
              f"{v['filed']['key']} (DOI {v['filed']['doi']}, title score {v['title_score']})")
    for name, p in per_author.items():
        if not any(p["works_by_identifier"].values()):
            print(f"WARNING: no works returned for {name} "
                  f"({', '.join(p['works_by_identifier'])}). Discovery is blind for this person.")

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
    try:
        main()
    except OpenAlexBudgetExhausted as e:
        print(f"ERROR: OpenAlex budget exhausted, not rate-limited. Retrying will not help "
              f"until the allowance resets. Nothing was discovered or written. {e}", file=sys.stderr)
        sys.exit(2)
