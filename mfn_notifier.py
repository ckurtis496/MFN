#!/usr/bin/env python3
"""
Checks MFN.se for new press releases and posts them to a Discord channel via webhook.

Runs statelessly on each invocation: reads state.json from the repo checkout,
compares against the current front page of mfn.se, and posts new items to
Discord - but only during a configurable daily active window. Items found
outside the window are simply skipped (marked seen, never posted) - this
is a quiet-hours filter, not a delivery delay.

Environment variables:
  DISCORD_WEBHOOK_URL   required - the Discord webhook to post to
  MFN_URL               optional - defaults to the full https://mfn.se/all feed.
                         Point this at e.g. https://mfn.se/all/a/carasent to
                         watch a single company instead of the whole market.
  ACTIVE_START           optional - "HH:MM", start of the daily active window.
  ACTIVE_END             optional - "HH:MM", end of the daily active window.
                         If ACTIVE_START > ACTIVE_END the window wraps past
                         midnight (e.g. 17:30 -> 09:00 means "active in the
                         evening/night/early morning, quiet during the day").
                         Leave both unset to always post immediately (no
                         quiet hours).
  TIMEZONE               optional - IANA tz name, defaults to Europe/Stockholm.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, time as dtime
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

STATE_FILE = Path(__file__).parent / "state.json"
MFN_URL = os.environ.get("MFN_URL", "https://mfn.se/all")
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Stockholm"))
ACTIVE_START = os.environ.get("ACTIVE_START")
ACTIVE_END = os.environ.get("ACTIVE_END")
MAX_SEEN_IDS = 2000


def _parse_hhmm(s):
    h, m = s.split(":")
    return dtime(int(h), int(m))


def in_active_window(now=None):
    """True if 'now' (Europe/Stockholm by default) falls in the configured
    active window. No window configured => always active."""
    if not ACTIVE_START or not ACTIVE_END:
        return True
    now = now or datetime.now(TIMEZONE)
    start = _parse_hhmm(ACTIVE_START)
    end = _parse_hhmm(ACTIVE_END)
    t = now.time()
    if start <= end:
        return start <= t < end
    # Window wraps past midnight, e.g. 17:30 -> 09:00
    return t >= start or t < end

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

    active = in_active_window()
    posted = 0
    for it in new_items:
        seen.add(it["id"])
        state["seen_ids"].append(it["id"])
        # Outside the active window, items are marked seen and dropped for
        # good - not queued, not delivered later. Only items published
        # while the window is open get posted.
        if active:
            post_to_discord(it)
            posted += 1

    save_state(state)
    window_note = "" if active else " (outside active window, skipped)"
    print(f"Found {len(new_items)} new item(s). Posted {posted}{window_note}.")


if __name__ == "__main__":
    main()
