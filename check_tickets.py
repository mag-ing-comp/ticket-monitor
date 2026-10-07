#!/usr/bin/env python3
"""
One-shot check of buytickets.gi for a Lincoln Red Imps vs Hajduk Split listing.
Designed for a scheduled runner (GitHub Actions cron): fetch, match, alert on new hits, exit.

State: seen.json (URLs already alerted). The workflow commits it back to the repo,
so each event triggers exactly one alert across runs.

Env:
    NTFY_TOPIC          push via https://ntfy.sh (required unless Telegram is set)
    TELEGRAM_BOT_TOKEN  optional
    TELEGRAM_CHAT_ID    optional

Exit codes: 0 = ran fine (hit or no hit), 1 = site fetch/parse failed
(GitHub emails you on failed runs, which doubles as "site down" monitoring).

Local usage:
    python check_tickets.py
    python check_tickets.py --test-notify
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.buytickets.gi"
PAGES_TO_SCAN = [f"{BASE_URL}/events", f"{BASE_URL}/"]

# An event matches if ANY rule is fully present in its title + URL slug.
MATCH_RULES: list[set[str]] = [
    {"hajduk"},
    {"lincoln", "red imps"},
    {"uefa", "conference"},
    {"hush"},  # TEST
]
ALL_KEYWORDS = ["hajduk", "split", "lincoln red imps", "uefa", "conference"]

STATE_FILE = Path(__file__).with_name("seen.json")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}


@dataclass(frozen=True)
class Event:
    title: str
    url: str
    date: str = ""

    @property
    def haystack(self) -> str:
        return normalize(f"{self.title} {self.url.replace('-', ' ')}")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def is_match(e: Event) -> bool:
    return any(all(term in e.haystack for term in rule) for rule in MATCH_RULES)


EVENT_URL_RE = re.compile(r"/events?/[^/]+-\d+/?$", re.I)  # e.g. /events/the-hush-1362
RAW_PAGES: list[requests.Response] = []  # kept for diagnostics and raw-text fallback


def diagnose() -> None:
    """Print what the server actually returned, so a failed run explains itself."""
    for r in RAW_PAGES:
        soup = BeautifulSoup(r.text, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else "(no <title>)"
        hrefs = [a["href"] for a in soup.find_all("a", href=True)]
        print(
            f"--- {r.url} | HTTP {r.status_code} | {len(r.text)} chars | title: {title}"
        )
        print(f"    {len(hrefs)} links, sample: {hrefs[:15]}")
        if len(hrefs) < 5:
            print("    body start:", " ".join(r.text.split())[:600])


def fetch_events(session: requests.Session) -> list[Event]:
    events: dict[str, Event] = {}
    for page in PAGES_TO_SCAN:
        resp = session.get(page, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        RAW_PAGES.append(resp)
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            # Resolve relative links ("events/x-12", "/events/x-12", absolute) the same way.
            href = urljoin(resp.url, a["href"]).split("#")[0].split("?")[0]
            if not EVENT_URL_RE.search(href):
                continue
            title = (a.get("title") or a.get_text(" ", strip=True)).strip()
            if not title:
                continue
            h5 = a.find_parent("h5")
            date = str(h5.next_sibling).strip() if h5 and h5.next_sibling else ""
            # Each card links twice (image + heading); keep the copy that carries the date.
            if href not in events or (date and not events[href].date):
                events[href] = Event(title=title, url=href, date=date)
    return list(events.values())


def notify(title: str, body: str, url: str = "") -> bool:
    sent = False
    if topic := os.getenv("NTFY_TOPIC"):
        headers = {"Title": title, "Priority": "urgent", "Tags": "soccer,ticket"}
        if url:
            headers["Click"] = url
        r = requests.post(
            f"https://ntfy.sh/{topic}", data=body.encode(), headers=headers, timeout=10
        )
        sent |= r.ok
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"{title}\n\n{body}\n{url}".strip()},
            timeout=10,
        )
        sent |= r.ok
    print(f"[notify sent={sent}] {title} | {body} | {url}")
    return sent


def load_seen() -> set[str]:
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-notify", action="store_true")
    args = parser.parse_args()

    if args.test_notify:
        return (
            0
            if notify(
                "Test: Hajduk monitor", "Notifications work.", f"{BASE_URL}/events"
            )
            else 1
        )

    try:
        events = fetch_events(requests.Session())
    except requests.RequestException as e:
        print(f"Fetch failed: {e}", file=sys.stderr)
        return 1

    seen = load_seen()

    if not events:
        # Zero events usually means the layout changed or we got a bot/cookie page.
        print("Parsed 0 events; page structure may have changed.", file=sys.stderr)
        diagnose()
        # Safety net: even if card parsing breaks, never miss the keyword itself.
        for r in RAW_PAGES:
            key = f"raw:{r.url}"
            if "hajduk" in normalize(r.text) and key not in seen:
                if notify(
                    "Possible Hajduk listing on buytickets.gi",
                    "Keyword found on the page (parser could not read event cards).",
                    r.url,
                ):
                    seen.add(key)
                    STATE_FILE.write_text(json.dumps(sorted(seen), indent=2) + "\n")
        return 1
    hits = [e for e in events if is_match(e) and e.url not in seen]
    print(f"Scanned {len(events)} events, {len(hits)} new match(es).")

    for e in hits:
        kws = ", ".join(k for k in ALL_KEYWORDS if k in e.haystack)
        when = f" ({e.date})" if e.date else ""
        if notify(
            "Hajduk tickets are LIVE on buytickets.gi",
            f"{e.title}{when}\nMatched: {kws}",
            e.url,
        ):
            seen.add(e.url)  # only mark seen once the alert actually went out

    if hits:
        STATE_FILE.write_text(json.dumps(sorted(seen), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
