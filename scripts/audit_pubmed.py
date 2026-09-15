#!/usr/bin/env python3
"""
Audit discovery against PubMed: find team papers that are not filed in Zotero.

Discovery asks OpenAlex, and OpenAlex fails silently: when an author entity has
no ORCID linked, the ORCID filter returns nothing and the run looks identical to
one where nobody published. This script asks a second source the same question.

For each person in authors.json it searches PubMed with

    (<author term>) AND (Edinburgh[Affiliation]) AND (<since_year>:<this year>[PDAT])

then keeps a hit only when Edinburgh appears in the affiliation of that person's
own Author element, not merely somewhere on the paper. The author term is
`pubmed_term` when set, otherwise surname plus first initials.

Every hit is classified against the Web-Publications, Review and Rejected
collections:

  present         DOI already filed (or title matches a filed item with no DOI)
  version_update  title matches a filed item that has a different DOI
  missing         not filed anywhere

Writes audit_findings.json and audit_for_review.md. Files `missing` items into
Review, tagged source-pubmed-audit, only when AUDIT_WRITE_ZOTERO=1, and at most
AUDIT_MAX_ADD of them. Never writes version updates.

Env vars:
  ZOTERO_GROUP_ID               4536042
  ZOTERO_COLLECTION_ID          approved collection key (Web-Publications)
  ZOTERO_REVIEW_COLLECTION_ID   review collection key (required to write)
  ZOTERO_REJECTED_COLLECTION_ID rejected collection key
  ZOTERO_API_KEY                needs write access to file missing items
  OPENALEX_MAILTO               contact address sent to NCBI E-utilities
  NCBI_API_KEY                  optional, raises the rate limit from 3 to 10 per second
  SINCE_YEAR                    default scan window (default 2015)
  AUDIT_WRITE_ZOTERO            set to 1 to file missing items into Review
  AUDIT_MAX_ADD                 cap on items filed per run (default 25)
"""

import difflib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from pyzotero import zotero

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS_DIR)

GROUP_ID = os.environ.get("ZOTERO_GROUP_ID", "4536042")
APPROVED_COLL = os.environ.get("ZOTERO_COLLECTION_ID", "")
REVIEW_COLL = os.environ.get("ZOTERO_REVIEW_COLLECTION_ID", "")
REJECTED_COLL = os.environ.get("ZOTERO_REJECTED_COLLECTION_ID", "")
API_KEY = os.environ.get("ZOTERO_API_KEY", "").strip()
CONTACT = os.environ.get("OPENALEX_MAILTO", "").strip()
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "").strip()
DEFAULT_SINCE_YEAR = int(os.environ.get("SINCE_YEAR", 2015))
WRITE_ZOTERO = os.environ.get("AUDIT_WRITE_ZOTERO", "").strip() == "1"
MAX_ADD = int(os.environ.get("AUDIT_MAX_ADD", "").strip() or 25)

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
USER_AGENT = "eci-publications-audit/1.0 (mailto:%s)" % (CONTACT or "unknown")
AFFILIATION = "edinburgh"
REQUEST_GAP = 0.11 if NCBI_API_KEY else 0.34   # 10 or 3 requests per second
EFETCH_BATCH = 200
MAX_CREATORS = 50
TITLE_MATCH_THRESHOLD = 0.92
AUDIT_TAG = "source-pubmed-audit"


# --- small helpers, duplicated from the other scripts on purpose ---

def normalise_doi(raw):
    if not raw:
        return None
    doi = raw.strip().lower()
    for p in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(p):
            doi = doi[len(p):]
    return doi or None


def normalise_title(s):
    """build_publications.py's normalisation, plus dropping a (preprint) suffix."""
    s = (s or "").lower()
    s = re.sub(r"\s*[\(\[]\s*preprint\s*[\)\]]\s*$", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fold(s):
    """Lowercase and strip accents, for comparing names."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


# --- PubMed ---

_last_request = [0.0]


def eutils(endpoint, params, retries=4):
    params = dict(params, tool="eci-publications-audit")
    if CONTACT:
        params["email"] = CONTACT
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(f"{EUTILS_BASE}/{endpoint}", data=body,
                                 headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        wait = _last_request[0] + REQUEST_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise


def author_term(author):
    """(query term, surname, initials) for one person."""
    term = (author.get("pubmed_term") or "").strip()
    if term:
        bare = re.sub(r"\[[^\]]*\]", "", term).strip()
        parts = bare.split()
        if len(parts) >= 2 and re.fullmatch(r"[A-Za-z]+", parts[-1]) and parts[-1].isupper():
            return term, " ".join(parts[:-1]), parts[-1]
        surname, initials = derive_name(author["name"])
        return term, surname, initials
    surname, initials = derive_name(author["name"])
    return f"{surname} {initials}[Author]", surname, initials


def derive_name(name):
    """'Maxine de Araujo' -> ('de Araujo', 'M'). Surname particles stay with the surname."""
    parts = name.split()
    particles = {"de", "da", "van", "von", "der", "del", "di", "le", "la", "mc", "st"}
    i = len(parts) - 1
    while i > 1 and parts[i - 1].lower() in particles:
        i -= 1
    given, surname = parts[:i], " ".join(parts[i:])
    initials = "".join(g[0].upper() for g in given if g)
    return surname, initials


def search_pmids(term, since_year, until_year):
    query = (f"({term}) AND ({AFFILIATION.title()}[Affiliation]) "
             f"AND ({since_year}:{until_year}[PDAT])")
    data = json.loads(eutils("esearch.fcgi", {
        "db": "pubmed", "term": query, "retmode": "json", "retmax": 10000}))
    return query, data.get("esearchresult", {}).get("idlist", [])


def text_of(el):
    return "".join(el.itertext()).strip() if el is not None else ""


def article_year_and_date(article):
    for path in (".//Article/Journal/JournalIssue/PubDate", ".//Article/ArticleDate"):
        el = article.find(path)
        if el is None:
            continue
        year = text_of(el.find("Year"))
        if not year:
            m = re.search(r"\d{4}", text_of(el.find("MedlineDate")))
            year = m.group(0) if m else ""
        if year:
            month = text_of(el.find("Month"))
            day = text_of(el.find("Day"))
            return int(year), " ".join(p for p in (year, month, day) if p)
    return None, ""


def sortable_date(article):
    for path in (".//Article/ArticleDate", ".//PubmedData/History/PubMedPubDate[@PubStatus='pubmed']"):
        el = article.find(path)
        if el is not None and text_of(el.find("Year")):
            try:
                return "%04d-%02d-%02d" % (int(text_of(el.find("Year"))),
                                           int(text_of(el.find("Month")) or 1),
                                           int(text_of(el.find("Day")) or 1))
            except ValueError:
                pass
    return ""


def parse_article(article):
    citation = article.find("MedlineCitation")
    art = citation.find("Article")
    doi = None
    for aid in article.findall(".//PubmedData/ArticleIdList/ArticleId"):
        if aid.get("IdType") == "doi":
            doi = normalise_doi(aid.text)
    if not doi:
        for loc in art.findall("ELocationID"):
            if loc.get("EIdType") == "doi":
                doi = normalise_doi(loc.text)
    authors = []
    for a in art.findall("AuthorList/Author"):
        authors.append({
            "last": text_of(a.find("LastName")),
            "fore": text_of(a.find("ForeName")),
            "initials": text_of(a.find("Initials")),
            "collective": text_of(a.find("CollectiveName")),
            "affiliations": [text_of(x) for x in a.findall("AffiliationInfo/Affiliation")],
        })
    year, date = article_year_and_date(article)
    journal = art.find("Journal")
    issue = journal.find("JournalIssue") if journal is not None else None
    return {
        "pmid": text_of(citation.find("PMID")),
        "doi": doi,
        "title": re.sub(r"\.$", "", text_of(art.find("ArticleTitle"))),
        "year": year,
        "date": date,
        "sort_date": sortable_date(article) or (f"{year:04d}" if year else ""),
        "venue": text_of(journal.find("Title")) if journal is not None else "",
        "volume": text_of(issue.find("Volume")) if issue is not None else "",
        "issue": text_of(issue.find("Issue")) if issue is not None else "",
        "pages": text_of(art.find("Pagination/MedlinePgn")),
        "publication_types": [text_of(p) for p in art.findall("PublicationTypeList/PublicationType")],
        "pubmed_authors": authors,
    }


def fetch_articles(pmids):
    records = {}
    for batch in chunked(pmids, EFETCH_BATCH):
        root = ET.fromstring(eutils("efetch.fcgi", {
            "db": "pubmed", "id": ",".join(batch), "retmode": "xml"}))
        for article in root.findall("PubmedArticle"):
            rec = parse_article(article)
            records[rec["pmid"]] = rec
    return records


def bound_author(rec, surname, initials):
    """The matching Author element whose own affiliation mentions Edinburgh, or None."""
    want_last, want_init = fold(surname), (initials or "").upper()
    for a in rec["pubmed_authors"]:
        if fold(a["last"]) != want_last:
            continue
        have = (a["initials"] or "".join(w[0] for w in a["fore"].split() if w)).upper()
        if want_init and not have.startswith(want_init):
            continue
        if any(AFFILIATION in fold(aff) for aff in a["affiliations"]):
            return a
    return None


# --- Zotero ---

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
        self.by_doi, self.by_title = {}, {}
        for e in entries:
            if e["doi"]:
                self.by_doi.setdefault(e["doi"], e)
            if e["norm"]:
                self.by_title.setdefault(e["norm"], e)
        self.titles = list(self.by_title)

    def match(self, doi, title):
        """Classify a record. Returns (status, entry, score)."""
        if doi and doi in self.by_doi:
            return "present", self.by_doi[doi], 1.0
        norm = normalise_title(title)
        if norm:
            entry, score = self.by_title.get(norm), 1.0
            if entry is None:
                close = difflib.get_close_matches(norm, self.titles, n=1, cutoff=TITLE_MATCH_THRESHOLD)
                if close:
                    entry = self.by_title[close[0]]
                    score = difflib.SequenceMatcher(None, norm, close[0]).ratio()
            if entry is not None:
                if entry["doi"] and doi and entry["doi"] != doi:
                    return "version_update", entry, round(score, 3)
                return "present", entry, round(score, 3)
        return "missing", None, None


def to_zotero_item(rec):
    creators = []
    for a in rec["pubmed_authors"]:
        if a["last"]:
            creators.append({"creatorType": "author", "firstName": a["fore"] or a["initials"],
                             "lastName": a["last"]})
        elif a["collective"]:
            creators.append({"creatorType": "author", "name": a["collective"]})
        if len(creators) >= MAX_CREATORS:
            break
    return {
        "itemType": "journalArticle",
        "title": rec["title"],
        "creators": creators or [{"creatorType": "author", "firstName": "", "lastName": ""}],
        "publicationTitle": rec["venue"],
        "volume": rec["volume"],
        "issue": rec["issue"],
        "pages": rec["pages"],
        "date": rec["date"],
        "DOI": rec["doi"] or "",
        "url": ("https://doi.org/" + rec["doi"]) if rec["doi"]
               else f"https://pubmed.ncbi.nlm.nih.gov/{rec['pmid']}/",
        "extra": f"PMID: {rec['pmid']}\nAuto-added from PubMed audit. "
                 f"Matched: {', '.join(rec['matched_authors'])}.",
        "tags": [{"tag": "needs-review"}, {"tag": AUDIT_TAG}],
        "collections": [REVIEW_COLL] if REVIEW_COLL else [],
    }


# --- reports ---

def public_record(rec):
    out = {k: rec[k] for k in ("pmid", "doi", "title", "year", "date", "venue", "volume",
                               "issue", "pages", "publication_types", "matched_authors", "status")}
    if rec.get("filed"):
        out["filed"] = rec["filed"]
        out["title_score"] = rec["title_score"]
    if "written_to_zotero" in rec:
        out["written_to_zotero"] = rec["written_to_zotero"]
    return out


def line_for(r):
    link = f"[{r['doi']}](https://doi.org/{r['doi']})" if r["doi"] else "no DOI"
    return (f"- **{r['year']}** {r['title']} — _{r['venue']}_ · {link} "
            f"· [PMID {r['pmid']}](https://pubmed.ncbi.nlm.nih.gov/{r['pmid']}/) "
            f"· {', '.join(r['matched_authors'])}")


def write_reports(generated, indexed, per_author, findings, write_summary):
    by_status = {s: [r for r in findings if r["status"] == s]
                 for s in ("missing", "version_update", "present")}
    with open(os.path.join(ROOT, "audit_findings.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_at": generated.isoformat(timespec="seconds"),
                   "query_template": "(<author term>) AND (Edinburgh[Affiliation]) "
                                     "AND (<since_year>:<this year>[PDAT])",
                   "indexed": indexed,
                   "counts": {s: len(v) for s, v in by_status.items()},
                   "zotero_write": write_summary,
                   "per_author": per_author,
                   "findings": [public_record(r) for r in findings]},
                  f, indent=2, ensure_ascii=False)

    with open(os.path.join(ROOT, "audit_for_review.md"), "w", encoding="utf-8") as f:
        f.write("# PubMed audit\n\n")
        f.write(f"Generated {generated:%Y-%m-%d %H:%M UTC}. Indexed "
                + ", ".join(f"{n} {c}" for c, n in indexed.items()) + " Zotero items.\n\n")
        f.write(f"**{len(by_status['missing'])} missing**, "
                f"{len(by_status['version_update'])} version updates, "
                f"{len(by_status['present'])} already filed.\n\n")
        f.write(f"Zotero: {write_summary['message']}\n\n")
        f.write("Missing means PubMed lists the paper under a team member with an Edinburgh "
                "affiliation on their own author entry, and no Zotero collection holds it. "
                "Namesakes still get through, especially with single-initial author terms; "
                "triage before approving.\n\n")

        f.write("## Missing\n\n")
        for r in by_status["missing"]:
            f.write(line_for(r) + "\n")
        if not by_status["missing"]:
            f.write("None.\n")

        f.write("\n## Version updates (never written)\n\n"
                "Title matches a filed item under a different DOI. Replace the DOI on the "
                "filed item by hand if this is the published version.\n\n")
        for r in by_status["version_update"]:
            filed = r["filed"]
            f.write(line_for(r) + f" · filed in {filed['collection']} as `{filed['key']}` "
                    f"with DOI `{filed['doi']}` (title score {r['title_score']})\n")
        if not by_status["version_update"]:
            f.write("None.\n")

        f.write("\n## Per author\n\n"
                "| Author | Term | From | PubMed hits | Edinburgh on own entry | Present | Version updates | Missing |\n"
                "|---|---|---|---|---|---|---|---|\n")
        for name, p in per_author.items():
            f.write(f"| {name} | `{p['term']}` | {p['since_year']} | {p['hits']} | {p['bound']} "
                    f"| {p['present']} | {p['version_update']} | {p['missing']} |\n")


def main():
    with open(os.path.join(ROOT, "authors.json"), encoding="utf-8") as f:
        authors = json.load(f)["authors"]

    zot = zotero.Zotero(GROUP_ID, "group", API_KEY or None)
    entries = {name: collection_entries(zot, key, name) for name, key in (
        ("Web-Publications", APPROVED_COLL), ("Review", REVIEW_COLL), ("Rejected", REJECTED_COLL))}
    indexed = {name: len(e) for name, e in entries.items() if e}
    filed = FiledIndex([e for es in entries.values() for e in es])
    print("Indexed " + ", ".join(f"{n} {c}" for c, n in indexed.items()) + " Zotero items.")

    this_year = datetime.now(timezone.utc).year
    records, per_author = {}, {}
    for a in authors:
        since = int(a.get("since_year", DEFAULT_SINCE_YEAR))
        term, surname, initials = author_term(a)
        query, pmids = search_pmids(term, since, this_year)
        fetched = fetch_articles(pmids)
        stats = per_author[a["name"]] = {"term": term, "query": query, "since_year": since,
                                         "hits": len(pmids), "bound": 0,
                                         "present": 0, "version_update": 0, "missing": 0}
        for pmid, rec in fetched.items():
            if not bound_author(rec, surname, initials):
                continue
            stats["bound"] += 1
            rec = records.setdefault(pmid, dict(rec, matched_authors=[]))
            rec["matched_authors"].append(a["name"])
        print(f"{a['name']}: {len(pmids)} PubMed hits, {stats['bound']} with Edinburgh on their own entry.")

    findings = []
    for rec in records.values():
        status, entry, score = filed.match(rec["doi"], rec["title"])
        rec["status"] = status
        if entry is not None:
            rec["filed"] = {k: entry[k] for k in ("collection", "key", "doi", "title")}
            rec["title_score"] = score
        for name in rec["matched_authors"]:
            per_author[name][status] += 1
        findings.append(rec)

    order = {"missing": 0, "version_update": 1, "present": 2}
    findings.sort(key=lambda r: r["pmid"])
    findings.sort(key=lambda r: r["sort_date"], reverse=True)
    findings.sort(key=lambda r: (order[r["status"]], -(r["year"] or 0)))

    missing = [r for r in findings if r["status"] == "missing"]
    write_summary = write_to_zotero(zot, missing)
    write_reports(datetime.now(timezone.utc), indexed, per_author, findings, write_summary)
    print(f"{len(missing)} missing, "
          f"{sum(r['status'] == 'version_update' for r in findings)} version updates, "
          f"{sum(r['status'] == 'present' for r in findings)} present. {write_summary['message']}")


def write_to_zotero(zot, missing):
    if not WRITE_ZOTERO:
        return {"enabled": False, "added": 0,
                "message": "not written (AUDIT_WRITE_ZOTERO is not 1). Dry run."}
    if not REVIEW_COLL or not API_KEY:
        return {"enabled": True, "added": 0,
                "message": "not written: ZOTERO_REVIEW_COLLECTION_ID and a write-scoped "
                           "ZOTERO_API_KEY are both required."}
    chosen = missing[:MAX_ADD]
    items = [to_zotero_item(r) for r in chosen]
    zot.check_items(items)
    added, failed = 0, 0
    for batch_recs, batch in zip(chunked(chosen, 50), chunked(items, 50)):
        resp = zot.create_items(batch)
        ok = {int(i) for i in resp.get("successful", {})}
        added += len(ok)
        failed += len(resp.get("failed", {}))
        for i, rec in enumerate(batch_recs):
            rec["written_to_zotero"] = i in ok
        if resp.get("failed"):
            print("Some items failed:", json.dumps(resp["failed"], indent=2)[:1000])
        time.sleep(0.3)
    held = len(missing) - len(chosen)
    return {"enabled": True, "added": added, "failed": failed, "held_back_by_cap": held,
            "max_add": MAX_ADD,
            "message": f"added {added} missing items to Review, tagged {AUDIT_TAG} "
                       f"({failed} failed, {held} held back by AUDIT_MAX_ADD={MAX_ADD})."}


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"ERROR: HTTP {e.code} from {e.url}", file=sys.stderr)
        sys.exit(1)
