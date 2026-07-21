#!/usr/bin/env python3
"""
Checks MFN.se for new press releases and posts them to a Discord channel via webhook.

Runs statelessly on each invocation: reads state.json from the repo checkout,
compares against the current front page of mfn.se, and posts new items to
Discord - but only if the item's OWN publish time falls in a configurable
daily active window. Items published outside the window are simply skipped
(marked seen, never posted) - this is a quiet-hours filter, not a delivery
delay. The window is judged by publish time rather than script-run time
because GitHub's schedule trigger is not reliably every 5 minutes in
practice (it can be delayed by hours) - see README for details.

Environment variables:
  DISCORD_WEBHOOK_URL     required - the Discord webhook for the quiet-hours
                          channel (respects ACTIVE_START/ACTIVE_END below).
  DISCORD_WEBHOOK_URL_24_7 optional - a second Discord webhook that receives
                          every new item around the clock, with no quiet-hours
                          filtering at all. Leave unset to only run the one
                          channel.
  MFN_URL               optional - defaults to the full https://mfn.se/all feed.
                         Point this at e.g. https://mfn.se/all/a/carasent to
                         watch a single company instead of the whole market.
  ACTIVE_START           optional - "HH:MM", start of the daily active window.
  ACTIVE_END             optional - "HH:MM", end of the daily active window.
                         If ACTIVE_START > ACTIVE_END the window wraps past
                         midnight (e.g. 17:30 -> 09:00 means "active in the
                         evening/night/early morning, quiet during the day").
                         Leave both unset to always post immediately (no
                         quiet hours). Only affects DISCORD_WEBHOOK_URL, not
                         DISCORD_WEBHOOK_URL_24_7.
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
WEBHOOK_URL_24_7 = os.environ.get("DISCORD_WEBHOOK_URL_24_7")
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Stockholm"))
ACTIVE_START = os.environ.get("ACTIVE_START")
ACTIVE_END = os.environ.get("ACTIVE_END")
MAX_SEEN_IDS = 2000


def _parse_hhmm(s):
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _in_window(t):
    """True if time-of-day 't' falls in the configured active window.
    No window configured => always active."""
    if not ACTIVE_START or not ACTIVE_END:
        return True
    start = _parse_hhmm(ACTIVE_START)
    end = _parse_hhmm(ACTIVE_END)
    if start <= end:
        return start <= t < end
    # Window wraps past midnight, e.g. 17:30 -> 09:00
    return t >= start or t < end


def in_active_window(now=None):
    """True if 'now' (Europe/Stockholm by default) falls in the configured
    active window. Used only as a fallback when an item has no parseable
    publish time."""
    now = now or datetime.now(TIMEZONE)
    return _in_window(now.time())


def item_published_in_window(item):
    """Judge the window using the press release's OWN publish time (as
    shown on mfn.se, already in Europe/Stockholm), not the time the script
    happens to run. This matters because GitHub's schedule trigger can be
    delayed by hours, so an item published at 16:45 might not be *found*
    until 19:00 - it must still be treated as a daytime (quiet-hours) item,
    not an evening one."""
    try:
        h, m = item["time"].split(":")[:2]
        return _in_window(dtime(int(h), int(m)))
    except (KeyError, ValueError):
        # Malformed timestamp: fall back to current time rather than
        # silently dropping or silently posting.
        return in_active_window()

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


def post_to_discord(item, webhook_url):
    payload = {
        "content": f"**{item['company']}**\n{item['title']}\n{item['url']}",
    }
    r = requests.post(webhook_url, json=payload, timeout=15)
    # Discord rate limit: leave a little breathing room between messages.
    if r.status_code == 429:
        retry_after = r.json().get("retry_after", 1)
        time.sleep(float(retry_after) + 0.5)
        r = requests.post(webhook_url, json=payload, timeout=15)
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
    skipped = 0
    posted_24_7 = 0
    for it in new_items:
        seen.add(it["id"])
        state["seen_ids"].append(it["id"])

        # The 24/7 channel gets every new item, no quiet-hours filter at all.
        if WEBHOOK_URL_24_7:
            post_to_discord(it, WEBHOOK_URL_24_7)
            posted_24_7 += 1

        # The quiet-hours channel is judged by the item's OWN publish time,
        # not by when this script happens to run - items published during
        # quiet hours are marked seen and dropped for good on this channel,
        # never queued or delivered later.
        if item_published_in_window(it):
            post_to_discord(it, WEBHOOK_URL)
            posted += 1
        else:
            skipped += 1

    save_state(state)
    print(
        f"Found {len(new_items)} new item(s). Quiet-hours channel: posted "
        f"{posted}, skipped {skipped} (outside active window). "
        f"24/7 channel: posted {posted_24_7}."
    )


if __name__ == "__main__":
    main()
