"""
Scrape San Diego CPP / Community Review Board meeting links (2020–present).

Output format (CSV): one row per link, with columns:
  date, link_text, section, page_title, source_url, document_url

Run:
    python scrape_cpp.py
"""

import csv
import json
import re
import time
from datetime import date, datetime
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag
from dateutil.relativedelta import relativedelta


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://www.sandiego.gov"
MAIN_PAGE = f"{BASE_URL}/cpp/meetingsagendas"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StanfordJournalism-CPPScraper/1.0)"}
REQUEST_DELAY = 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch_soup(url: str) -> BeautifulSoup:
    print(f"  Fetching: {url}")
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY)
    return BeautifulSoup(resp.text, "html.parser")


def make_absolute(href: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    return urljoin(BASE_URL, href)


def prompt_for_date(label: str) -> date:
    while True:
        raw = input(f"{label} (format: 2020-04-01): ").strip()
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            print("  Invalid format. Please enter a date like 2020-04-01.\n")


# ---------------------------------------------------------------------------
# Discover archive page URLs from main page sidebar
# ---------------------------------------------------------------------------

def discover_archive_urls() -> dict[int, str]:
    soup = fetch_soup(MAIN_PAGE)
    archives: dict[int, str] = {}
    for a_tag in soup.find_all("a", href=True):
        text = a_tag.get_text(strip=True)
        m = re.match(r"^(20\d{2})\b", text)
        if m and "Board Meetings" in text:
            archives[int(m.group(1))] = make_absolute(a_tag["href"])
    return archives


def get_pages_for_range(start_date: date, end_date: date) -> dict[int, str]:
    current_year = date.today().year
    archives = discover_archive_urls()

    pages: dict[int, str] = {}
    for year in range(start_date.year, end_date.year + 1):
        if year == current_year:
            pages[year] = MAIN_PAGE
        elif year in archives:
            pages[year] = archives[year]
        else:
            print(f"  Warning: no archive page found for year {year}")
    return pages


# ---------------------------------------------------------------------------
# Parse one year page
# ---------------------------------------------------------------------------

def parse_page(soup: BeautifulSoup, source_url: str) -> list[dict]:
    """
    Returns a flat list of dicts, one per link:
      {date, link_text, section, page_title, source_url, document_url}
    """
    rows = []

    # Find the content <h1>
    content_h1 = None
    for h1 in soup.find_all("h1"):
        text = h1.get_text(strip=True)
        if "Board Meetings" in text or "CPP Calendar" in text:
            content_h1 = h1
            break

    if not content_h1:
        print(f"  Warning: no content <h1> found on {source_url}")
        return rows

    page_title = content_h1.get_text(strip=True)

    # Find the content area (to avoid picking up nav accordions)
    content_area = soup.select_one("div.node__content")
    if not content_area:
        content_area = soup.select_one("article")
    if not content_area:
        print(f"  Warning: no content area found on {source_url}")
        return rows

    # Find accordion links within content area
    acc_links = content_area.find_all("a", class_="accordion__link")

    NAV_WORDS = {"leisure", "resident resources", "doing business", "library",
                 "public safety", "city hall", "site menu", "toggle"}

    for acc_a in acc_links:
        section_name = acc_a.get_text(" ", strip=True)
        section_name = re.sub(r"\s+", " ", section_name).strip()

        if any(w in section_name.lower() for w in NAV_WORDS):
            continue

        # Navigate up to parent <div class="accordion">, then find the drawer
        accordion_div = acc_a.find_parent("div", class_="accordion")
        if not accordion_div:
            continue

        drawer = accordion_div.find("div", class_="accordion__drawer")
        if not drawer:
            continue

        # Each meeting block is a <div class="grid-x ...">
        blocks = drawer.find_all("div", class_="grid-x")

        for block in blocks:
            time_tag = block.find("time", class_="datetime")
            if not time_tag:
                continue

            date_text = time_tag.get_text(strip=True)
            try:
                meeting_date = datetime.strptime(date_text, "%m/%d/%Y").date()
            except ValueError:
                continue

            # Collect every link in this block — each becomes its own row
            seen_urls = set()
            for a in block.find_all("a", href=True):
                href = a["href"]
                if not href or href == "#" or not href.strip():
                    continue

                link_text = a.get_text(" ", strip=True)
                if not link_text:
                    continue

                href = make_absolute(href)

                if href not in seen_urls:
                    seen_urls.add(href)
                    rows.append({
                        "date": meeting_date.isoformat(),
                        "link_text": link_text,
                        "section": section_name,
                        "page_title": page_title,
                        "source_url": source_url,
                        "document_url": href,
                    })

    return rows


# ---------------------------------------------------------------------------
# Filter, dedup, sort
# ---------------------------------------------------------------------------

def filter_by_date(rows, start, end):
    return [r for r in rows
            if start <= date.fromisoformat(r["date"]) <= end]


def deduplicate(rows):
    seen = set()
    unique = []
    for r in rows:
        key = (r["date"], r["document_url"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

CSV_FIELDS = ["date", "link_text", "section", "page_title", "source_url", "document_url"]


def save_json(rows, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def save_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print()
    print("=" * 55)
    print("  CPP Meeting Agenda & Minutes Link Scraper")
    print("  Supported range: 2020 to present")
    print("=" * 55)
    print()

    start_date = prompt_for_date("Enter beginning date")
    end_date = prompt_for_date("Enter end date")

    if start_date > end_date:
        print("\nError: beginning date must be before end date.")
        return

    print(f"\nScraping meetings from {start_date} to {end_date} ...\n")

    pages = get_pages_for_range(start_date, end_date)

    if not pages:
        print("No pages found for the given date range.")
        return

    all_rows = []
    for year, url in sorted(pages.items(), reverse=True):
        soup = fetch_soup(url)
        page_rows = parse_page(soup, url)
        print(f"    Year {year}: scraped {len(page_rows)} links\n")
        all_rows.extend(page_rows)

    filtered = filter_by_date(all_rows, start_date, end_date)
    filtered = deduplicate(filtered)
    filtered.sort(key=lambda r: r["date"], reverse=True)

    print(f"Found {len(filtered)} links in range.\n")

    if not filtered:
        return

    date_range_str = f"{start_date.strftime('%Y%m%d')}to{end_date.strftime('%Y%m%d')}"
    json_filename = f"SD_CPP_{date_range_str}.json"
    csv_filename = f"SD_CPP_{date_range_str}.csv"

    save_json(filtered, json_filename)
    save_csv(filtered, csv_filename)
    print(f"Saved: {json_filename}")
    print(f"Saved: {csv_filename}\n")

    # Print summary grouped by date
    current_date = None
    for r in filtered:
        if r["date"] != current_date:
            current_date = r["date"]
            print(f"  {r['date']}  |  {r['section']}")
        print(f"    -> {r['link_text']}")
        print(f"       {r['document_url']}")


if __name__ == "__main__":
    main()