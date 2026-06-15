#!/usr/bin/env python3
"""
Build publications.json for the Edinburgh Cancer Informatics publications page.

Source of truth: a vetted Zotero group collection (the allowlist).
Enrichment: OpenAlex, by DOI, for live citation counts and open-access links.

The page never calls an API. It reads the committed publications.json.

Environment variables (set as GitHub Actions secrets / env):
  ZOTERO_GROUP_ID      e.g. 4536042            (public, in the group URL)
  ZOTERO_COLLECTION_ID e.g. X3G67CXM           (the collection key)
  ZOTERO_API_KEY       optional for a public group; required if private
  OPENALEX_MAILTO      your email, for the OpenAlex polite pool
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ZOTERO_GROUP_ID = os.environ.get("ZOTERO_GROUP_ID", "4536042")
ZOTERO_COLLECTION_ID = os.environ.get("ZOTERO_COLLECTION_ID", "X3G67CXM")
ZOTERO_API_KEY = os.environ.get("ZOTERO_API_KEY", "").strip()
OPENALEX_MAILTO = os.environ.get("OPENALEX_MAILTO", "").strip()

ZOTERO_BASE = "https://api.zotero.org"
OPENALEX_BASE = "https://api.openalex.org"
USER_AGENT = "eci-publications-builder/1.0 (mailto:%s)" % (OPENALEX_MAILTO or "unknown")


def get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    req.add_header("User-Agent", USER_AGENT)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8")), dict(resp.headers)


def fetch_zotero_collection_items():
    """Page through every top-level item in the vetted collection."""
    headers = {"Zotero-API-Version": "3"}
    if ZOTERO_API_KEY:
        headers["Authorization"] = "Bearer " + ZOTERO_API_KEY

    items = []
    start = 0
    limit = 100
    library_version = None
    while True:
        url = (
            f"{ZOTERO_BASE}/groups/{ZOTERO_GROUP_ID}"
            f"/collections/{ZOTERO_COLLECTION_ID}/items/top"
            f"?format=json&limit={limit}&start={start}&itemType=-attachment||note"
        )
        batch, resp_headers = get_json(url, headers)
        if library_version is None:
            library_version = resp_headers.get("Last-Modified-Version")
        if not batch:
            break
        items.extend(batch)
        start += limit
        if len(batch) < limit:
            break
        time.sleep(0.2)
    return items, library_version


def normalise_doi(raw):
    if not raw:
        return None
    doi = raw.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
    return doi or None


def extract_from_zotero(item):
    d = item.get("data", {})
    creators = d.get("creators", [])
    authors = []
    for c in creators:
        if c.get("creatorType") not in (None, "author"):
            continue
        last = c.get("lastName") or c.get("name") or ""
        first = c.get("firstName") or ""
        authors.append((first + " " + last).strip() if first else last)
    doi = normalise_doi(d.get("DOI"))
    return {
        "zotero_key": d.get("key"),
        "item_type": d.get("itemType"),
        "title": d.get("title", "").strip(),
        "authors": [a for a in authors if a],
        "year": (d.get("date") or "")[:4] if (d.get("date") or "")[:4].isdigit() else None,
        "venue": d.get("publicationTitle") or d.get("bookTitle") or d.get("publisher") or "",
        "doi": doi,
        "url": d.get("url") or (f"https://doi.org/{doi}" if doi else ""),
    }


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def enrich_from_openalex(dois):
    """Batch-query OpenAlex by DOI. Returns {doi: enrichment}."""
    out = {}
    mailto = ("&mailto=" + urllib.parse.quote(OPENALEX_MAILTO)) if OPENALEX_MAILTO else ""
    for batch in chunked(dois, 50):  # docs allow up to 100; 50 is comfortable
        pipe = "|".join(batch)
        url = (
            f"{OPENALEX_BASE}/works"
            f"?filter=doi:{urllib.parse.quote(pipe, safe='|/:')}"
            f"&per-page=100&select=doi,display_name,publication_year,cited_by_count,"
            f"open_access,primary_location,type{mailto}"
        )
        data, _ = get_json(url)
        for w in data.get("results", []):
            doi = normalise_doi(w.get("doi"))
            if not doi:
                continue
            oa = w.get("open_access") or {}
            loc = w.get("primary_location") or {}
            src = (loc.get("source") or {}) if loc else {}
            out[doi] = {
                "openalex_year": w.get("publication_year"),
                "cited_by_count": w.get("cited_by_count", 0),
                "is_oa": bool(oa.get("is_oa")),
                "oa_url": oa.get("oa_url"),
                "openalex_venue": src.get("display_name"),
                "openalex_type": w.get("type"),
            }
        time.sleep(0.2)
    return out


def build():
    items, library_version = fetch_zotero_collection_items()
    records = [extract_from_zotero(i) for i in items]

    dois = [r["doi"] for r in records if r["doi"]]
    enrichment = enrich_from_openalex(dois) if dois else {}

    for r in records:
        e = enrichment.get(r["doi"], {}) if r["doi"] else {}
        r["cited_by_count"] = e.get("cited_by_count")
        r["is_oa"] = e.get("is_oa", False)
        r["oa_url"] = e.get("oa_url")
        if not r["year"] and e.get("openalex_year"):
            r["year"] = str(e["openalex_year"])
        if not r["venue"] and e.get("openalex_venue"):
            r["venue"] = e["openalex_venue"]
        r["enriched"] = bool(e)

    def sort_key(r):
        return (-(int(r["year"]) if r["year"] else 0), r["title"].lower())
    records.sort(key=sort_key)

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {
            "zotero_group": ZOTERO_GROUP_ID,
            "zotero_collection": ZOTERO_COLLECTION_ID,
            "zotero_library_version": library_version,
        },
        "count": len(records),
        "publications": records,
    }
    return payload


if __name__ == "__main__":
    out_path = sys.argv[1] if len(sys.argv) > 1 else "publications.json"
    payload = build()
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"Wrote {out_path}: {payload['count']} publications "
          f"(library version {payload['source']['zotero_library_version']})")
