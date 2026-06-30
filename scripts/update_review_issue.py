#!/usr/bin/env python3
"""Create or update the standing GitHub issue for Zotero Review candidates.

The discovery workflow writes candidates.json and candidates_for_review.md. When
new candidates are found, this script updates a single open GitHub issue with the
current review list and adds a comment mentioning the reviewer so GitHub sends a
notification. It uses only the standard library and the GitHub Actions-provided
GITHUB_TOKEN.
"""

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GITHUB_API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_SERVER_URL = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
GITHUB_RUN_ID = os.environ.get("GITHUB_RUN_ID", "").strip()

ISSUE_TITLE = os.environ.get("REVIEW_ISSUE_TITLE", "Zotero review queue").strip()
NOTIFY_USER = os.environ.get("REVIEW_NOTIFICATION_USER", "").strip().lstrip("@")
ZOTERO_REVIEW_URL = os.environ.get("ZOTERO_REVIEW_URL", "").strip()


def github_request(method, path, body=None):
    if not GITHUB_REPOSITORY:
        raise RuntimeError("GITHUB_REPOSITORY is not set")
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN is not set")

    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "User-Agent": "eci-publications-review-notifier",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        f"{GITHUB_API}{path}", data=data, headers=headers, method=method
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        if resp.status == 204:
            return None
        return json.loads(resp.read().decode("utf-8"))


def find_open_issue():
    issues = github_request(
        "GET", f"/repos/{GITHUB_REPOSITORY}/issues?state=open&per_page=100"
    )
    for issue in issues:
        if "pull_request" in issue:
            continue
        if issue.get("title") == ISSUE_TITLE:
            return issue
    return None


def read_candidates():
    with open(os.path.join(ROOT, "candidates.json"), encoding="utf-8") as f:
        return json.load(f)


def read_review_markdown():
    path = os.path.join(ROOT, "candidates_for_review.md")
    if not os.path.exists(path):
        return "_No candidates_for_review.md file was found._\n"
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def run_url():
    if GITHUB_REPOSITORY and GITHUB_RUN_ID:
        return f"{GITHUB_SERVER_URL}/{GITHUB_REPOSITORY}/actions/runs/{GITHUB_RUN_ID}"
    return ""


def build_issue_body(count, review_markdown):
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    review_link = f"\n- Zotero Review collection: {ZOTERO_REVIEW_URL}" if ZOTERO_REVIEW_URL else ""
    workflow_link = f"\n- Workflow run: {run_url()}" if run_url() else ""

    body = f"""# Zotero review queue

There are **{count}** newly discovered candidate publication(s) to review.

Links:{review_link}{workflow_link}

Last updated: {generated}

## Candidates

{review_markdown}
"""
    # GitHub issue bodies are limited to 65,536 characters. Keep a margin for
    # metadata and ask the reviewer to use the committed markdown if truncated.
    limit = 60000
    if len(body) > limit:
        body = body[:limit] + "\n\n_This list was truncated. See candidates_for_review.md in the repository for the full list._\n"
    return body


def build_comment(count):
    mention = f"@{NOTIFY_USER} " if NOTIFY_USER else ""
    review_link = f"\n\nZotero Review collection: {ZOTERO_REVIEW_URL}" if ZOTERO_REVIEW_URL else ""
    workflow_link = f"\nWorkflow run: {run_url()}" if run_url() else ""
    return (
        f"{mention}{count} new candidate publication(s) were added to Zotero Review. "
        "The standing issue body has been updated."
        f"{review_link}{workflow_link}"
    )


def main():
    candidates = read_candidates()
    count = int(candidates.get("count") or 0)
    if count <= 0:
        print("No new candidates found; not updating the review issue.")
        return

    review_markdown = read_review_markdown()
    body = build_issue_body(count, review_markdown)
    issue = find_open_issue()

    if issue:
        number = issue["number"]
        github_request("PATCH", f"/repos/{GITHUB_REPOSITORY}/issues/{number}", {"body": body})
        print(f"Updated existing review issue #{number}.")
    else:
        payload = {"title": ISSUE_TITLE, "body": body}
        issue = github_request("POST", f"/repos/{GITHUB_REPOSITORY}/issues", payload)
        number = issue["number"]
        print(f"Created review issue #{number}.")

    github_request(
        "POST",
        f"/repos/{GITHUB_REPOSITORY}/issues/{number}/comments",
        {"body": build_comment(count)},
    )
    print(f"Posted notification comment on review issue #{number}.")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        details = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API request failed: HTTP {e.code}: {details}") from e
