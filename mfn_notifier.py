#!/usr/bin/env python3
"""
Checks MFN.se for new press releases and posts them to a Discord channel via webhook.

Runs statelessly on each invocation: reads state.json (list of already-notified
item ids) from the repo checkout, compares against the current front page of
mfn.se, posts any new items to Discord, then rewrites state.json so the
GitHub Actions workflow can commit it back.

Environment variables:
  DISCORD_WEBHOOK_URL   required - the Discord webhook to post to
  MFN_URL               optional - defaults to the full https://mfn.se/all feed.
                         Point this at e.g. https://mfn.se/all/a/carasent to
                         watch a single company instead of the whole market.
"""

import json
import os
import re
import sys
import time
from html import unescape
from pathlib import Path

import requests

STATE_FILE = Path(__file__).parent / "state.json"
MFN_URL = os.environ.get("MFN_URL", "https://mfn.se/all")
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
MAX_SEEN_IDS = 1000
# Safety cap: never blast more than this many messages in a single run
# (protects against a bad parse or a huge backlog flooding the channel).
MAX_POSTS_PER_RUN = 25

ITEM_RE = re.compile(
    r'<div class="short-item[^"]*"\s+id="([0-9a-fA-F-]{36})"[^>]*'
    r"onclick=\"goToNewsItem\(event, '([^']+)'\)\"[\s\S]*?"
    r'<span class="compressed-date">([^<]+)</span>\s*'
    r'<span class="compressed-time">([^<]+)</span>[\s\S]*?'
    r'author="([^"]*)">([^<]*)</a>[\s\S]*?'
    r'<a class="title-link item-link" href="[^"]*" title="([^"]*)"',
    re.MULTILINE,
)


def fetch_items():
    resp = requests.get(
        MFN_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; mfn-discord-notifier/1.0)"},
        timeout=30,
    )
    resp.raise_for_status()
    html = resp.text

    items = []
    for m in ITEM_RE.finditer(html):
        item_id, path, date, time_, company_slug, company_name, title = m.groups()
        items.append(
            {
                "id": item_id,
                "url": "https://mfn.se" + path,
                "date": date,
                "time": time_,
                "company": unescape(company_name).strip(),
                "title": unescape(title).strip(),
            }
        )
    return items


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"seen_ids": [], "bootstrapped": False}


def save_state(state):
    state["seen_ids"] = state["seen_ids"][-MAX_SEEN_IDS:]
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def post_to_discord(item):
    payload = {
        "content": f"**{item['company']}**\n{item['title']}\n{item['url']}",
    }
    r = requests.post(WEBHOOK_URL, json=payload, timeout=15)
    # Discord rate limit: leave a little breathing room between messages.
    if r.status_code == 429:
        retry_after = r.json().get("retry_after", 1)
        time.sleep(float(retry_after) + 0.5)
        r = requests.post(WEBHOOK_URL, json=payload, timeout=15)
    r.raise_for_status()


def main():
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL is not set", file=sys.stderr)
        sys.exit(1)

    items = fetch_items()
    if not items:
        print("Warning: parsed 0 items - MFN.se markup may have changed", file=sys.stderr)
        return

    state = load_state()
    seen = set(state["seen_ids"])

    # First ever run: don't spam the channel with the whole current front
    # page, just record what's already there and start fresh from here.
    if not state.get("bootstrapped"):
        state["seen_ids"] = [it["id"] for it in items]
        state["bootstrapped"] = True
        save_state(state)
        print(f"Bootstrapped with {len(items)} existing items, no messages sent.")
        return

    new_items = [it for it in items if it["id"] not in seen]
    # Oldest first, so the channel reads chronologically.
    new_items.reverse()

    posted = 0
    for it in new_items[:MAX_POSTS_PER_RUN]:
        post_to_discord(it)
        seen.add(it["id"])
        state["seen_ids"].append(it["id"])
        posted += 1

    save_state(state)
    print(f"Posted {posted} new item(s) of {len(new_items)} found.")


if __name__ == "__main__":
    main()
