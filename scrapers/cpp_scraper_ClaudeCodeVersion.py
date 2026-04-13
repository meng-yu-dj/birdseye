"""
Universal San Diego CPP/CRB Meeting Agendas & Minutes — Link Harvester
Covers all years 2003–present across three URL prefixes:
  - /cpp/meetingsagendas/YYYY              (2004, 2022–present)
  - /communityreviewboard/meetingsagendas/YYYY  (2015–2023)
  - /citizensreviewboard/meetingsagendas/YYYY   (2003–2014)

Prompts for a start date and end date (YYYY-MM-DD).
Output: cpp_STARTDATE_ENDDATE.csv saved next to this script.

CSV columns:
  agency_name, source_page_url, year, meeting_date, section,
  document_type, file_name, file_url, scraped_at
"""

import os
import re
import csv
import time
import requests
from bs4 import BeautifulSoup, NavigableString
from urllib.parse import urljoin, urlparse
from datetime import datetime, date

# ── Configuration ─────────────────────────────────────────────────────────────
BASE_URL  = "https://www.sandiego.gov"
MAIN_PAGE = "https://www.sandiego.gov/cpp/meetingsagendas"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DELAY     = 1.5   # polite delay between requests (seconds)
HEADERS   = {"User-Agent": "Mozilla/5.0 (compatible; research-scraper/1.0)"}

# Agency name by URL prefix
AGENCY_MAP = {
    "/cpp/":                  "City of San Diego – Commission on Police Practices (CPP)",
    "/communityreviewboard/": "City of San Diego – Community Review Board (CRB)",
    "/citizensreviewboard/":  "City of San Diego – Citizens' Review Board (CRB)",
}

# Date patterns to recognise in running text
DATE_PATTERNS = [
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b"),                          # 04/01/2026
    re.compile(r"\b(?:January|February|March|April|May|June|July|"
               r"August|September|October|November|December)"
               r"\s+\d{1,2},\s+\d{4}\b", re.IGNORECASE),               # January 27, 2004
    re.compile(r"\b\d{1,2}[-]\d{1,2}[-]\d{2,4}\b"),                    # 12-03-2025
]

# Known legacy sub-page pattern: /BOARD/meetingsagendas/YYYY/YYMMDD
SUBPAGE_RE = re.compile(
    r"/(cpp|communityreviewboard|citizensreviewboard)/meetingsagendas/\d{4}/\d{6}$"
)

# Keyword filter: internal sandiego.gov links whose text suggests a meeting document
DOC_KEYWORDS_RE = re.compile(
    r"\b(agenda|minutes?|meeting|session|hearing|cancelled|canceled|special meeting)\b",
    re.IGNORECASE,
)

# Nav/footer links to ignore
NAV_SKIP_RE = re.compile(
    r"^(home|about|contact|search|back|next|previous|leisure|library|"
    r"doing business|public safety|city hall|resident resources|"
    r"privacy|accessibility|disclaimer|language|translate)$",
    re.IGNORECASE,
)

# ── Date Input & Normalisation ────────────────────────────────────────────────

def prompt_date(label: str) -> date:
    """Prompt the user for a date in YYYY-MM-DD format, re-asking on invalid input."""
    while True:
        raw = input(f"  {label} (YYYY-MM-DD): ").strip()
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            print(f"    Invalid format '{raw}'. Please use YYYY-MM-DD (e.g. 2023-01-15).")


# Attempt to parse the many date string formats found across pages into a date object.
_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

def parse_meeting_date(date_str: str) -> date | None:
    """
    Convert a meeting_date string (in any of the scraped formats) to a date object.
    Returns None if unparseable.
    """
    if not date_str:
        return None

    date_str = date_str.strip()

    # MM/DD/YYYY
    try:
        return datetime.strptime(date_str, "%m/%d/%Y").date()
    except ValueError:
        pass

    # Month DD, YYYY  (e.g. "January 27, 2004")
    m = re.match(
        r"(january|february|march|april|may|june|july|august|"
        r"september|october|november|december)\s+(\d{1,2}),\s+(\d{4})",
        date_str, re.IGNORECASE,
    )
    if m:
        month = _MONTH_NAMES[m.group(1).lower()]
        day   = int(m.group(2))
        year  = int(m.group(3))
        try:
            return date(year, month, day)
        except ValueError:
            pass

    # MM-DD-YYYY
    try:
        return datetime.strptime(date_str, "%m-%d-%Y").date()
    except ValueError:
        pass

    # MM-DD-YY
    try:
        return datetime.strptime(date_str, "%m-%d-%y").date()
    except ValueError:
        pass

    return None


def in_range(meeting_date_str: str, start: date, end: date) -> bool:
    """Return True if the meeting date falls within [start, end] inclusive."""
    d = parse_meeting_date(meeting_date_str)
    if d is None:
        return False
    return start <= d <= end


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_page(url: str) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as e:
        print(f"  [ERROR] {url}: {e}")
        return None


def extract_date(text: str) -> str:
    """Return the first date-like string found in text, or ''."""
    for pat in DATE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0)
    return ""


def agency_from_url(url: str) -> str:
    for prefix, name in AGENCY_MAP.items():
        if prefix in url:
            return name
    return "City of San Diego"


def doc_type_from(text: str, url: str) -> str:
    combined = (text + " " + url).lower()
    if "minute" in combined:
        return "minutes"
    if "agenda" in combined:
        return "agenda"
    if "youtube.com" in combined or "youtu.be" in combined:
        return "video"
    if ".pdf" in combined:
        return "pdf"
    return "webpage"


def should_collect(link_text: str, full_url: str) -> bool:
    if NAV_SKIP_RE.match(link_text.strip()):
        return False
    is_pdf      = ".pdf" in full_url.lower()
    is_video    = "youtube.com" in full_url or "youtu.be" in full_url
    is_subpage  = bool(SUBPAGE_RE.search(urlparse(full_url).path))
    is_doc_page = BASE_URL in full_url and bool(DOC_KEYWORDS_RE.search(link_text))
    return is_pdf or is_video or is_subpage or is_doc_page


# ── Core Parser ───────────────────────────────────────────────────────────────

def parse_page(soup: BeautifulSoup, page_url: str, year: str) -> list[dict]:
    records    = []
    agency     = agency_from_url(page_url)
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    content = (
        soup.find("div", id="main-content")
        or soup.find("div", class_="field-items")
        or soup.find("article")
        or soup.body
    )
    if not content:
        return records

    current_section = "General"
    current_date    = ""

    for node in content.descendants:

        if node.name in ("h2", "h3", "h4", "h5"):
            text = node.get_text(" ", strip=True)
            d    = extract_date(text)
            if d:
                current_date = d
            elif text:
                current_section = text

        elif isinstance(node, NavigableString):
            text = node.strip()
            if text:
                d = extract_date(text)
                if d:
                    current_date = d

        elif node.name == "a":
            href      = (node.get("href") or "").strip()
            link_text = node.get_text(" ", strip=True)

            if not href or href.startswith("#") or href.startswith("mailto:"):
                continue

            full_url = urljoin(BASE_URL, href) if not href.startswith("http") else href

            if not should_collect(link_text, full_url):
                continue

            link_date      = extract_date(link_text) or extract_date(full_url)
            effective_date = current_date or link_date
            dtype          = doc_type_from(link_text, full_url)
            file_name      = (
                os.path.basename(urlparse(full_url).path)
                if ".pdf" in full_url.lower()
                else link_text or os.path.basename(urlparse(full_url).path)
            )

            records.append({
                "agency_name":     agency,
                "source_page_url": page_url,
                "year":            year,
                "meeting_date":    effective_date,
                "section":         current_section,
                "document_type":   dtype,
                "file_name":       file_name,
                "file_url":        full_url,
                "scraped_at":      scraped_at,
            })

    seen, unique = set(), []
    for r in records:
        if r["file_url"] not in seen:
            seen.add(r["file_url"])
            unique.append(r)
    return unique


# ── Year-link Discovery ───────────────────────────────────────────────────────

def discover_year_urls(soup: BeautifulSoup) -> list[tuple[str, str]]:
    pattern = re.compile(
        r"/(cpp|communityreviewboard|citizensreviewboard)/meetingsagendas/(20\d{2}|199\d)$"
    )
    seen, years = set(), []
    for a in soup.find_all("a", href=True):
        m = pattern.search(a["href"])
        if m:
            year     = m.group(2)
            full_url = urljoin(BASE_URL, a["href"])
            if full_url not in seen:
                seen.add(full_url)
                years.append((year, full_url))
    years.sort(key=lambda x: x[0])
    return years


# ── Menu & Scrape Orchestration ───────────────────────────────────────────────

def prompt_menu() -> str:
    """Ask the user which mode to run and return '1' or '2'."""
    print("Choose an option:\n")
    print("  1. Scrape entire site (all years)")
    print("  2. Scrape by date range\n")
    while True:
        choice = input("Enter 1 or 2: ").strip()
        if choice in ("1", "2"):
            return choice
        print("  Please enter 1 or 2.")


def scrape_years(main_soup: BeautifulSoup, year_urls: list[tuple[str, str]],
                 current_year: str) -> list[dict]:
    """Fetch and parse the main page plus every year page in year_urls."""
    all_records = []

    # Main page (current year items)
    print(f"\nFetching main page: {MAIN_PAGE}")
    records = parse_page(main_soup, MAIN_PAGE, current_year)
    print(f"  Main page : {len(records)} links found.")
    all_records.extend(records)

    print(f"\nScraping {len(year_urls)} year archive page(s):\n")
    for year, url in year_urls:
        print(f"  [{year}]  {url}")
        soup = get_page(url)
        if not soup:
            time.sleep(DELAY)
            continue
        recs = parse_page(soup, url, year)
        print(f"          → {len(recs)} links")
        all_records.extend(recs)
        time.sleep(DELAY)

    # Deduplicate
    seen, deduped = set(), []
    for r in all_records:
        if r["file_url"] not in seen:
            seen.add(r["file_url"])
            deduped.append(r)
    return deduped


def write_csv(records: list[dict], csv_path: str) -> None:
    fieldnames = [
        "agency_name", "source_page_url", "year", "meeting_date",
        "section", "document_type", "file_name", "file_url", "scraped_at",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def print_summary(records: list[dict], csv_path: str) -> None:
    by_type = {}
    for r in records:
        by_type[r["document_type"]] = by_type.get(r["document_type"], 0) + 1
    print(f"\n{'='*65}")
    print(f"Done!  {len(records)} links saved.")
    for dtype, count in sorted(by_type.items()):
        print(f"  {dtype:<12}: {count}")
    print(f"\nCSV saved to: {csv_path}")
    print("=" * 65)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("San Diego CPP/CRB Meeting Agendas & Minutes — Link Harvester")
    print("=" * 65 + "\n")

    choice = prompt_menu()

    # ── Fetch main page (needed for both modes to discover year links) ────────
    print(f"\nFetching main page to discover archive links...")
    main_soup = get_page(MAIN_PAGE)
    if not main_soup:
        print("FATAL: Could not load main page. Exiting.")
        return
    time.sleep(DELAY)

    current_year = str(datetime.now().year)
    all_year_urls = discover_year_urls(main_soup)  # sorted oldest → newest

    # ── Option 1: scrape entire site ─────────────────────────────────────────
    if choice == "1":
        csv_path = os.path.join(SCRIPT_DIR, "cpp_all.csv")
        print(f"\nMode       : Full site scrape")
        print(f"Output CSV : {csv_path}")

        records = scrape_years(main_soup, all_year_urls, current_year)
        write_csv(records, csv_path)
        print_summary(records, csv_path)

    # ── Option 2: scrape by date range ───────────────────────────────────────
    else:
        print("\nEnter the date range:\n")
        start_date = prompt_date("Start date")
        end_date   = prompt_date("End date  ")

        if end_date < start_date:
            print("\n  Error: end date is before start date. Please re-run and try again.")
            return

        csv_filename = f"cpp_{start_date}_{end_date}.csv"
        csv_path     = os.path.join(SCRIPT_DIR, csv_filename)

        print(f"\nMode       : Date range scrape")
        print(f"Date range : {start_date} → {end_date}")
        print(f"Output CSV : {csv_path}")

        # Only scrape year pages that overlap with the requested date range
        start_year = start_date.year
        end_year   = end_date.year
        relevant_year_urls = [
            (yr, url) for yr, url in all_year_urls
            if start_year <= int(yr) <= end_year
        ]

        # Include main page only if the current year is in range
        include_main = start_year <= int(current_year) <= end_year

        if not relevant_year_urls and not include_main:
            print("\n  No archive pages found for that date range.")
            return

        print(f"\nYears to scrape: {[yr for yr, _ in relevant_year_urls]}"
              + (f" + main page ({current_year})" if include_main else ""))

        all_records = []

        if include_main:
            print(f"\nParsing main page ({current_year})...")
            recs = parse_page(main_soup, MAIN_PAGE, current_year)
            print(f"  Main page : {len(recs)} links found.")
            all_records.extend(recs)

        if relevant_year_urls:
            print(f"\nScraping {len(relevant_year_urls)} year archive page(s):\n")
            for year, url in relevant_year_urls:
                print(f"  [{year}]  {url}")
                soup = get_page(url)
                if not soup:
                    time.sleep(DELAY)
                    continue
                recs = parse_page(soup, url, year)
                print(f"          → {len(recs)} links")
                all_records.extend(recs)
                time.sleep(DELAY)

        # Deduplicate
        seen, deduped = set(), []
        for r in all_records:
            if r["file_url"] not in seen:
                seen.add(r["file_url"])
                deduped.append(r)

        # Filter to exact date range
        filtered = [r for r in deduped if in_range(r["meeting_date"], start_date, end_date)]

        write_csv(filtered, csv_path)
        print(f"\n  Total scraped from relevant pages : {len(deduped)}")
        print(f"  After date filter                 : {len(filtered)}")
        print_summary(filtered, csv_path)


if __name__ == "__main__":
    main()
