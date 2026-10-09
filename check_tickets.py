#!/usr/bin/env python3
"""
Monitor buytickets.gi for relevant football ticket listings.

Designed for scheduled execution through cron or GitHub Actions.

Features:
- Scan the /events page
- Match event titles and URLs against configurable rules
- Notify through ntfy and/or Telegram
- Track previously alerted events in seen.json
- Track consecutive failures in .failures
- Send alerts after repeated failures
- Send recovery notifications
- Continue monitoring after failures
- Log HTTP response diagnostics

Environment variables:
    NTFY_TOPIC
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    FAIL_ALERT_AFTER (default: 3)

Exit codes:
    0 = normal execution or handled soft failure
    1 = failure threshold reached or critical notification failure

Usage:
    python check_tickets.py
    python check_tickets.py --test-notify
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = "https://www.buytickets.gi"

# Monitor only the event listings page.
PAGES_TO_SCAN = [
    f"{BASE_URL}/events",
]

STATE_FILE = Path(__file__).with_name("seen.json")
FAIL_FILE = Path(__file__).with_name(".failures")

# Configure via environment variable if desired.
FAIL_ALERT_AFTER = max(1, int(os.getenv("FAIL_ALERT_AFTER", "3")))

EXIT_SOFT_FAIL = 1
EXIT_HARD_FAIL = 2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}


# ============================================================
# EVENT MATCHING
# ============================================================

# An event matches when every term in at least one rule
# appears in its normalized title or URL.

MATCH_RULES: list[set[str]] = [
    # Opponent
    {"hajduk"},
    {"hnk"},
    {"split", "croatia"},
    {"split", "hrvatska"},
    # Home club
    {"lincoln"},
    {"red imps"},
    {"imps"},
    {"lri"},
    # Competitions
    {"uefa"},
    {"conference league"},
    {"uecl"},
    {"europa", "conference"},
    # Venues
    {"europa point stadium"},
    {"victoria stadium"},
]

ALL_KEYWORDS = sorted({term for rule in MATCH_RULES for term in rule})


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def term_in(term: str, hay: str) -> bool:
    if len(term) <= 4:
        return re.search(rf"\b{re.escape(term)}\b", hay) is not None

    return term in hay


@dataclass(frozen=True)
class Event:
    title: str
    url: str
    date: str = ""

    @property
    def haystack(self) -> str:
        return normalize(f"{self.title} {self.url.replace('-', ' ')}")


def is_match(event: Event) -> bool:
    return any(
        all(term_in(term, event.haystack) for term in rule) for rule in MATCH_RULES
    )


EVENT_URL_RE = re.compile(r"/events?/[^/]+-\d+/?$", re.I)

# Preserve HTTP responses for diagnostics.
RAW_PAGES: list[requests.Response] = []


# ============================================================
# HTTP DIAGNOSTICS
# ============================================================


def diagnose() -> None:
    """
    Display diagnostic information from received pages.

    This does not make additional HTTP requests.
    """

    if not RAW_PAGES:
        print("Diagnostics: no HTTP responses available.")
        return

    for response in RAW_PAGES:
        soup = BeautifulSoup(response.text, "html.parser")

        title = soup.title.get_text(strip=True) if soup.title else "(no title)"

        hrefs = [a["href"] for a in soup.find_all("a", href=True)]

        print("--- HTTP DIAGNOSTICS ---")
        print(f"URL: {response.url}")
        print(f"Status: {response.status_code}")
        print(f"Page title: {title}")
        print(f"Body length: {len(response.content)} bytes")
        print(f"Links found: {len(hrefs)}")

        print("Server:", response.headers.get("Server", "unknown"))

        print("Content-Type:", response.headers.get("Content-Type", "unknown"))

        print("Retry-After:", response.headers.get("Retry-After", "not provided"))

        print(
            "X-Proxy-Cache-Info:",
            response.headers.get("X-Proxy-Cache-Info", "not provided"),
        )

        print("Sample links:", hrefs[:15])

        if response.status_code >= 400 or len(hrefs) < 5:
            print("Body preview:", " ".join(response.text.split())[:500])

        print("--- END DIAGNOSTICS ---")


def is_bot_challenge(response: requests.Response) -> bool:
    """
    Detect known challenge-page indicators.

    Other access restrictions may not be detected.
    """

    return "sgcaptcha" in response.text.lower() or (
        response.status_code == 202 and len(response.text) < 1000
    )


# ============================================================
# FETCH EVENTS
# ============================================================


def fetch_events(session: requests.Session) -> list[Event]:

    events: dict[str, Event] = {}

    for page in PAGES_TO_SCAN:

        print(f"Fetching: {page}")

        response = session.get(page, headers=HEADERS, timeout=20)

        # Preserve response BEFORE raising HTTP errors.
        RAW_PAGES.append(response)

        print(
            f"HTTP {response.status_code} | "
            f"{response.url} | "
            f"{len(response.content)} bytes | "
            f"Server: {response.headers.get('Server', 'unknown')}"
        )

        # Raise an exception on 4xx or 5xx responses.
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        for a in soup.find_all("a", href=True):

            href = urljoin(response.url, a["href"]).split("#")[0].split("?")[0]

            if not EVENT_URL_RE.search(href):
                continue

            title = (a.get("title") or a.get_text(" ", strip=True)).strip()

            if not title:
                continue

            h5 = a.find_parent("h5")

            date = str(h5.next_sibling).strip() if h5 and h5.next_sibling else ""

            # Deduplicate event links.
            # Prefer entries containing a date.
            if href not in events or (date and not events[href].date):
                events[href] = Event(title=title, url=href, date=date)

    return list(events.values())


# ============================================================
# NOTIFICATIONS
# ============================================================


def notify(title: str, body: str, url: str = "") -> bool:

    sent = False

    # Support topic name or full ntfy URL.
    topic = (os.getenv("NTFY_TOPIC") or "").strip().rstrip("/").split("/")[-1]

    if not topic:
        print("NTFY_TOPIC is empty. " "Skipping ntfy notification.")

    else:
        print(f"ntfy topic: {topic[:4]}..." f"{topic[-2:]} (len {len(topic)})")

        headers = {
            "Title": title,
            "Priority": "urgent",
            "Tags": "soccer,ticket",
        }

        if url:
            headers["Click"] = url

        try:
            response = requests.post(
                f"https://ntfy.sh/{topic}",
                data=body.encode(),
                headers=headers,
                timeout=10,
            )

            print(f"ntfy response: HTTP " f"{response.status_code}")

            sent |= response.ok

        except requests.RequestException as e:
            print(f"ntfy request failed: {e}")

    # Optional Telegram notifications.
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if token and chat_id:

        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": (f"{title}\n\n" f"{body}\n{url}").strip(),
                },
                timeout=10,
            )

            print(f"Telegram response: HTTP " f"{response.status_code}")

            sent |= response.ok

        except requests.RequestException as e:
            print(f"Telegram request failed: {e}")

    print(f"[notify sent={sent}] " f"{title} | {body} | {url}")

    return sent


# ============================================================
# PREVIOUSLY SEEN EVENTS
# ============================================================


def load_seen() -> set[str]:

    try:
        return set(json.loads(STATE_FILE.read_text()))

    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen: set[str]) -> None:

    STATE_FILE.write_text(json.dumps(sorted(seen), indent=2) + "\n")


# ============================================================
# FAILURE TRACKING
# ============================================================


def track_failures(rc: int) -> int:
    """
    Count consecutive unsuccessful checks.

    Send one failure alert upon reaching the threshold.

    Continue scheduled monitoring after the alert.

    Send a recovery notification once a successful
    check follows an alerted failure sequence.
    """

    try:
        n = int(FAIL_FILE.read_text().strip()) if FAIL_FILE.exists() else 0

    except ValueError:
        n = 0

    # Successful check.
    if rc == 0:

        if n >= FAIL_ALERT_AFTER:
            notify(
                "Hajduk monitor recovered",
                "Checks are working again.",
                f"{BASE_URL}/events",
            )

        if n > 0:
            print(f"Monitoring recovered after " f"{n} failed check(s).")

        FAIL_FILE.write_text("0")

        return 0

    # Failed check.
    n += 1

    FAIL_FILE.write_text(str(n))

    print(f"Consecutive failures: " f"{n}/{FAIL_ALERT_AFTER}")

    if n == FAIL_ALERT_AFTER:

        if any(is_bot_challenge(r) for r in RAW_PAGES):
            reason = "The site appears to be " "showing a bot challenge."

        elif any(r.status_code == 403 for r in RAW_PAGES):
            reason = "The site returned HTTP 403 Forbidden."

        else:
            reason = "The event listing could not " "be retrieved or parsed."

        notify(
            "Hajduk monitor is FAILING",
            (
                f"{n} failed checks in a row.\n"
                f"{reason}\n"
                "The monitor will continue checking "
                "on its normal schedule."
            ),
            f"{BASE_URL}/events",
        )

    elif n > FAIL_ALERT_AFTER:
        print(
            "Failure alert was already triggered. " "Continuing scheduled monitoring."
        )

    return n


# ============================================================
# MAIN MONITORING CHECK
# ============================================================


def run_check() -> int:

    # Clear diagnostics from any previous invocation.
    RAW_PAGES.clear()

    try:
        with requests.Session() as session:
            events = fetch_events(session)

    except requests.RequestException as e:

        print(f"Fetch failed: {e}")

        if RAW_PAGES:
            diagnose()

        else:
            print(
                "No HTTP response received. " "Possible connection or timeout failure."
            )

        return EXIT_SOFT_FAIL

    seen = load_seen()

    # No events found.
    if not events:

        if any(is_bot_challenge(r) for r in RAW_PAGES):
            print("Possible bot-protection challenge. " "Not attempting to bypass it.")

        else:
            print("Parsed 0 events. " "The page layout may have changed.")

        diagnose()

        # Raw-text fallback from the original script.
        for response in RAW_PAGES:

            key = f"raw:{response.url}"

            if "hajduk" in normalize(response.text) and key not in seen:

                if notify(
                    "Possible Hajduk listing on buytickets.gi",
                    (
                        "Keyword found on the page, "
                        "but event cards could not be parsed."
                    ),
                    response.url,
                ):
                    seen.add(key)
                    save_seen(seen)

        return EXIT_SOFT_FAIL

    # Find newly matching events.
    hits = [event for event in events if is_match(event) and event.url not in seen]

    print(f"Scanned {len(events)} events, " f"{len(hits)} new match(es).")

    for event in hits:

        keywords = ", ".join(
            keyword for keyword in ALL_KEYWORDS if term_in(keyword, event.haystack)
        )

        when = f" ({event.date})" if event.date else ""

        message = f"{event.title}{when}\n" f"Matched: {keywords}"

        if notify("Hajduk tickets are LIVE on buytickets.gi", message, event.url):
            # Record only after successful notification.
            seen.add(event.url)

    if hits:
        save_seen(seen)

    # Check for notifications that weren't delivered.
    undelivered = [event for event in hits if event.url not in seen]

    if undelivered:

        print(
            "MATCH FOUND but notification failed for:",
            [event.url for event in undelivered],
        )

        return EXIT_HARD_FAIL

    return 0


# ============================================================
# ENTRY POINT
# ============================================================


def main() -> int:

    parser = argparse.ArgumentParser()

    parser.add_argument("--test-notify", action="store_true")

    args = parser.parse_args()

    if args.test_notify:

        success = notify(
            "Test: Hajduk monitor", "Notifications work.", f"{BASE_URL}/events"
        )

        return 0 if success else 1

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] " "Starting monitoring check.")

    print(f"Failure alert threshold: " f"{FAIL_ALERT_AFTER}")

    rc = run_check()

    # Preserve critical notification-failure behavior.
    if rc == EXIT_HARD_FAIL:

        print("Critical: matching event detected " "but notification failed.")

        return 1

    n = track_failures(rc)

    if rc == 0:
        print("Check completed successfully.")
        return 0

    if n == FAIL_ALERT_AFTER:
        print("Failure threshold reached. " "Notification triggered.")
        return 1

    print(
        f"::warning::Check skipped "
        f"({n} in a row); "
        "will retry next scheduled run."
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
