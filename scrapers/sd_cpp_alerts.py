"""
Monitor the San Diego Commission on Police Practices (CPP) meetings page.

Written with AI assistance (Claude, Anthropic). The original version used
plain dictionaries to represent monitored items; this improved version
uses Python's dataclass module for a more concise, readable way to get at the data.

Purpose: Automatically scrapes the CPP meetings page on a schedule, compares
the current snapshot to the previous one, logs any newly posted agendas,
minutes, videos, or text notices to a CSV file, and optionally sends an
email alert to the journalist.

Typical usage:
    python monitor_cpp.py
    python monitor_cpp.py --state-file cpp_monitor_state.json --log-file cpp_monitor_log.csv
    python monitor_cpp.py --dry-run
    python monitor_cpp.py --baseline

Optional email alert configuration via environment variables:
    CPP_ALERT_SMTP_HOST, CPP_ALERT_SMTP_PORT, CPP_ALERT_SMTP_USER,
    CPP_ALERT_SMTP_PASS, CPP_ALERT_FROM, CPP_ALERT_TO
"""

import argparse
import csv
import hashlib
import json
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Iterable, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://www.sandiego.gov"
MAIN_PAGE = f"{BASE_URL}/cpp/meetingsagendas"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; StanfordJournalism-CPPMonitor/1.0)"
}
REQUEST_DELAY = 1

DEFAULT_STATE_FILE = "cpp_monitor_state.json"
DEFAULT_LOG_FILE = "cpp_monitor_log.csv"

CSV_FIELDS = [
    "detected_at_utc",
    "item_id",
    "date",
    "category",
    "link_text",
    "item_type",
    "section",
    "source_url",
    "document_url",
    "notes",
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class MonitorItem:
    item_id: str
    date: str
    category: str
    link_text: str
    item_type: str
    section: str
    source_url: str
    document_url: str
    notes: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fetch_soup(url: str) -> BeautifulSoup:
    print(f"Fetching: {url}")
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY)
    return BeautifulSoup(resp.text, "html.parser")


def make_absolute(href: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    return urljoin(BASE_URL, href)


def clean_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_date_string(text: str) -> bool:
    return bool(re.fullmatch(r"\d{2}/\d{2}/\d{4}", clean_text(text)))


def normalize_date(text: str) -> str:
    text = clean_text(text)
    return datetime.strptime(text, "%m/%d/%Y").date().isoformat()


def sha1_key(*parts: str) -> str:
    joined = "||".join(part or "" for part in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def infer_item_type(link_text: str, url: str) -> str:
    t = link_text.lower()
    u = url.lower()

    if "agenda" in t:
        return "agenda"
    if "minutes" in t:
        return "minutes"
    if "video" in t or "youtube.com" in u or "youtu.be" in u:
        return "video"
    if "calendar" in t:
        return "calendar"
    return "link"


def infer_category(section: str, link_text: str, notes: str = "") -> str:
    haystack = f"{section} {link_text} {notes}".lower()

    if "business meeting" in haystack:
        return "business_meeting"
    if "standing committee" in haystack:
        return "standing_committee"
    if "outreach" in haystack or "training" in haystack:
        return "outreach_training"
    if "draft calendar" in haystack:
        return "calendar"
    return "general"


def append_csv(rows: list[dict], path: str) -> None:
    file_exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {"source_url": MAIN_PAGE, "last_checked_utc": None, "items": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path: str, items: list[MonitorItem]) -> None:
    payload = {
        "source_url": MAIN_PAGE,
        "last_checked_utc": utc_now_iso(),
        "items": [asdict(item) for item in items],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def find_cpp_calendar_h1(soup: BeautifulSoup) -> Optional[Tag]:
    for h1 in soup.find_all(["h1", "h2", "h3"]):
        if clean_text(h1.get_text(" ", strip=True)) == "CPP Calendar":
            return h1
    return None


def collect_calendar_section_nodes(cpp_header: Tag) -> list[Tag]:
    """
    Collect sibling nodes after 'CPP Calendar' until the next major heading
    'Board Meetings & Agendas' or the end of the relevant content block.
    """
    nodes = []
    for sib in cpp_header.find_next_siblings():
        if sib.name in {"h1", "h2"}:
            heading_text = clean_text(sib.get_text(" ", strip=True))
            if heading_text == "Board Meetings & Agendas":
                break
        nodes.append(sib)
    return nodes


def parse_monitor_items(soup: BeautifulSoup, source_url: str) -> list[MonitorItem]:
    cpp_header = find_cpp_calendar_h1(soup)
    if not cpp_header:
        print("Warning: could not find 'CPP Calendar' section.")
        return []

    nodes = collect_calendar_section_nodes(cpp_header)
    items: list[MonitorItem] = []

    current_section = "CPP Calendar"
    current_date = ""
    seen_ids: set[str] = set()

    for node in nodes:
        text = clean_text(node.get_text(" ", strip=True))
        if not text:
            continue

        # Section headings inside CPP Calendar
        if node.name in {"h2", "h3", "h4", "strong"}:
            current_section = text
            continue

        # Some pages render section labels as plain text blocks.
        if text in {
            "Upcoming Outreach/Meetings/Trainings",
            "Business Meeting Agendas",
            "Standing Committee Meeting Agendas",
        }:
            current_section = text
            continue

        if is_date_string(text):
            current_date = normalize_date(text)
            continue

        # Capture important plain-text notices in the CPP section,
        # especially anything that may signal a newly scheduled meeting.
        # Ignore boilerplate description lines unless they contain some
        # scheduling clue or a link we care about.
        node_links = node.find_all("a", href=True)

        if node_links:
            for a in node_links:
                link_text = clean_text(a.get_text(" ", strip=True))
                href = clean_text(a.get("href", ""))

                if not href or href == "#" or not link_text:
                    continue

                abs_url = make_absolute(href)
                item_type = infer_item_type(link_text, abs_url)
                category = infer_category(current_section, link_text)

                # If date is not explicitly set for this block, leave blank.
                item_date = current_date

                item_id = sha1_key(item_date, current_section, link_text, abs_url, item_type)
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)

                items.append(
                    MonitorItem(
                        item_id=item_id,
                        date=item_date,
                        category=category,
                        link_text=link_text,
                        item_type=item_type,
                        section=current_section,
                        source_url=source_url,
                        document_url=abs_url,
                        notes="",
                    )
                )
        else:
            # Plain text notices: keep only meeting-relevant text to reduce noise.
            lowered = text.lower()
            keywords = [
                "meeting",
                "agenda",
                "minutes",
                "training",
                "outreach",
                "calendar",
                "closed session",
                "virtual meeting",
                "72 hours",
                "cancelled",
            ]
            if any(keyword in lowered for keyword in keywords):
                category = infer_category(current_section, "", text)
                item_type = "text_notice"
                item_id = sha1_key(current_date, current_section, text, item_type)

                if item_id not in seen_ids:
                    seen_ids.add(item_id)
                    items.append(
                        MonitorItem(
                            item_id=item_id,
                            date=current_date,
                            category=category,
                            link_text=text,
                            item_type=item_type,
                            section=current_section,
                            source_url=source_url,
                            document_url="",
                            notes="plain text notice",
                        )
                    )

    return items


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------

def diff_items(old_items: list[dict], new_items: list[MonitorItem]) -> list[MonitorItem]:
    old_ids = {item["item_id"] for item in old_items}
    return [item for item in new_items if item.item_id not in old_ids]


def format_alert_subject(new_items: list[MonitorItem]) -> str:
    count = len(new_items)
    if count == 1:
        return "CPP monitor alert: 1 new item detected"
    return f"CPP monitor alert: {count} new items detected"


def format_alert_body(new_items: list[MonitorItem]) -> str:
    lines = []
    lines.append("New items detected on the San Diego CPP meetings page.")
    lines.append("")
    lines.append(f"Source page: {MAIN_PAGE}")
    lines.append(f"Detected at: {utc_now_iso()}")
    lines.append("")

    for item in new_items:
        lines.append("-" * 72)
        lines.append(f"Date:        {item.date or '(none shown on page)'}")
        lines.append(f"Section:     {item.section}")
        lines.append(f"Category:    {item.category}")
        lines.append(f"Item type:   {item.item_type}")
        lines.append(f"Link text:   {item.link_text}")
        if item.document_url:
            lines.append(f"URL:         {item.document_url}")
        else:
            lines.append("URL:         (text-only notice)")
        if item.notes:
            lines.append(f"Notes:       {item.notes}")
    lines.append("-" * 72)
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email alerts
# ---------------------------------------------------------------------------

def send_email_alert(subject: str, body: str) -> bool:
    host = os.getenv("CPP_ALERT_SMTP_HOST")
    port = os.getenv("CPP_ALERT_SMTP_PORT")
    user = os.getenv("CPP_ALERT_SMTP_USER")
    password = os.getenv("CPP_ALERT_SMTP_PASS")
    from_addr = os.getenv("CPP_ALERT_FROM")
    to_addr = os.getenv("CPP_ALERT_TO")

    if not all([host, port, user, password, from_addr, to_addr]):
        print("Email alert skipped: SMTP environment variables not fully configured.")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)

    try:
        port_num = int(port)
        with smtplib.SMTP(host, port_num, timeout=30) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
        print(f"Alert email sent to: {to_addr}")
        return True
    except Exception as e:
        print(f"Email alert failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Monitor the San Diego CPP meetings page for newly posted items."
    )
    parser.add_argument(
        "--state-file",
        default=DEFAULT_STATE_FILE,
        help=f"Path to JSON state file (default: {DEFAULT_STATE_FILE})",
    )
    parser.add_argument(
        "--log-file",
        default=DEFAULT_LOG_FILE,
        help=f"Path to CSV log file (default: {DEFAULT_LOG_FILE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write state, do not append CSV, do not send alerts.",
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="Create or refresh baseline state without logging or alerting.",
    )
    args = parser.parse_args()

    print()
    print("=" * 60)
    print("  CPP Main Page Monitor")
    print(f"  Monitoring: {MAIN_PAGE}")
    print("=" * 60)
    print()

    try:
        soup = fetch_soup(MAIN_PAGE)
    except Exception as e:
        print(f"Error fetching page: {e}")
        return 1

    current_items = parse_monitor_items(soup, MAIN_PAGE)
    print(f"Parsed {len(current_items)} structured items from page.")

    if not current_items:
        print("No items parsed. State not updated.")
        return 1

    if args.baseline:
        print("Baseline mode: saving snapshot only, with no CSV log and no alerts.")
        if not args.dry_run:
            save_state(args.state_file, current_items)
            print(f"Saved baseline state to: {args.state_file}")
        else:
            print("Dry run enabled. No files written.")
        return 0

    previous_state = load_state(args.state_file)
    previous_items = previous_state.get("items", [])

    if not previous_items:
        print("No previous state found. Saving current snapshot as baseline.")
        if not args.dry_run:
            save_state(args.state_file, current_items)
            print(f"Saved baseline state to: {args.state_file}")
        else:
            print("Dry run enabled. No files written.")
        return 0

    new_items = diff_items(previous_items, current_items)

    if not new_items:
        print("No new items detected.")
        if not args.dry_run:
            save_state(args.state_file, current_items)
            print(f"Updated state file: {args.state_file}")
        else:
            print("Dry run enabled. No files written.")
        return 0

    print(f"Detected {len(new_items)} new item(s):")
    for item in new_items:
        print(f"  - [{item.item_type}] {item.link_text}")
        if item.document_url:
            print(f"    {item.document_url}")

    detected_at = utc_now_iso()
    csv_rows = []
    for item in new_items:
        row = {
            "detected_at_utc": detected_at,
            "item_id": item.item_id,
            "date": item.date,
            "category": item.category,
            "link_text": item.link_text,
            "item_type": item.item_type,
            "section": item.section,
            "source_url": item.source_url,
            "document_url": item.document_url,
            "notes": item.notes,
        }
        csv_rows.append(row)

    if args.dry_run:
        print("Dry run enabled. No CSV append, no state update, no alert sent.")
        return 0

    append_csv(csv_rows, args.log_file)
    print(f"Appended {len(csv_rows)} new row(s) to: {args.log_file}")

    subject = format_alert_subject(new_items)
    body = format_alert_body(new_items)
    send_email_alert(subject, body)

    save_state(args.state_file, current_items)
    print(f"Updated state file: {args.state_file}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())