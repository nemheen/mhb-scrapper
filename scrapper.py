#!/usr/bin/env python3
"""
MSE (Mongolian Stock Exchange, mse.mn) activity-report downloader — v3.

What changed from v2, and why
------------------------------
Your test run showed 0 of 212 trade-history rows had a plain <a href>
at all — meaning the site navigates via JavaScript click handlers, not
real links. v2's design (collect URLs first, then re-visit each one
with page.goto) depended on exactly those hrefs, so it silently never
actually navigated anywhere: it kept re-loading the trade-history page
itself, which has no "Үйл ажиллагааны тайлан" tab — hence every
company timing out waiting for that tab text.

v3 fixes this by never relying on hrefs for navigation. It keeps ONE
continuous browser session open and, for every row:
    click the row -> wait for the tab bar to appear -> scrape ->
    page.go_back() -> click the next row
This is how a person would actually use the site, so it doesn't care
whether the underlying navigation is a real link or a JS router call.

Your second screenshot also showed that "Татах" doesn't trigger a
browser download event — it opens a normal PDF in a new tab, at
issuers.mse.mn/uploads/activityreport/....pdf (note: a DIFFERENT
subdomain than www.mse.mn). So "Татах" is a plain <a href target=
"_blank">, and downloading is just a direct `requests.get()` on that
href — no click-and-capture needed. That's what this version does;
falling back to a click + new-tab capture only if a row's "Татах"
somehow has no usable href.

Install
-------
    pip install playwright requests
    playwright install chromium

Usage
-----
    # Step 1 — sanity check on ONE company before running at scale.
    python mse_scraper.py --inspect ADU --headless false

    # Full run: every company in the stocks table, top 10 reports each
    python mse_scraper.py

    # Just one company (by ticker)
    python mse_scraper.py --company ADU

    # Top 5, watch it work instead of headless
    python mse_scraper.py --top 5 --headless false

    # See what would be downloaded, without downloading
    python mse_scraper.py --list-only
"""

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://www.mse.mn"
TRADE_HISTORY_URL = f"{BASE_URL}/trade-history"

# Reports are served from a DIFFERENT subdomain than www.mse.mn —
# confirmed from a live "Татах" click: issuers.mse.mn/uploads/...
ISSUERS_BASE_URL = "https://issuers.mse.mn"

ACTIVITY_REPORT_TAB_TEXT = "Үйл ажиллагааны тайлан"
DOWNLOAD_BUTTON_TEXT = "Татах"

DOC_EXT_PATTERN = re.compile(r"\.(pdf|docx?|xlsx?)(\?.*)?$", re.IGNORECASE)

OUTPUT_DIR = Path("downloads")
DEFAULT_TOP_N = 10
NAV_TIMEOUT_MS = 30_000
TAB_RENDER_WAIT_MS = 3000
TAB_RENDER_RETRIES = 3
ROW_CLICK_TIMEOUT_MS = 8000
BETWEEN_COMPANIES_DELAY_S = 1.2
HEADLESS_DEFAULT = True


@dataclass
class ReportRow:
    year: str = ""
    quarter: str = ""
    description: str = ""
    date_str: str = ""          # "Огноо" column, ISO format -> sorts correctly as text
    direct_url: str = ""        # resolved absolute URL to the PDF, from the "Татах" href
    row_index: int = -1         # fallback: click this row's Файл cell if direct_url is empty

    def sort_key(self):
        return self.date_str or "0000-00-00"

    def label(self):
        bits = [b for b in [self.year, f"Q{self.quarter}" if self.quarter else "", self.description] if b]
        return " ".join(bits) or f"row_{self.row_index}"


def resolve_report_url(href: str) -> str:
    """Reports live on issuers.mse.mn, not www.mse.mn — resolve relative
    hrefs against the right base, and pass absolute hrefs through as-is."""
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return urljoin(ISSUERS_BASE_URL, href)


# ---------------------------------------------------------------------------
# Navigation: click-based, single continuous session (no href dependency)
# ---------------------------------------------------------------------------

def load_trade_history(page):
    print(f"Loading {TRADE_HISTORY_URL} ...")
    page.goto(TRADE_HISTORY_URL, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
    page.wait_for_selector("table tbody tr", timeout=NAV_TIMEOUT_MS)
    page.wait_for_timeout(1500)


def get_row_count(page) -> int:
    return page.locator("table tbody tr").count()


def read_row_basic_info(page, index: int):
    """Ticker + name only — cheap, doesn't require navigating anywhere."""
    rows = page.locator("table tbody tr")
    if index >= rows.count():
        return None, None
    row = rows.nth(index)
    cells = row.locator("td")
    if cells.count() < 3:
        return None, None
    try:
        ticker = cells.nth(1).inner_text().strip()
        name = cells.nth(2).inner_text().strip()
        return ticker, name
    except Exception:
        return None, None


def read_row_full_info(page, index: int) -> list[str]:
    """All cell text for a row, in order — e.g.
    ['1', 'ADU', 'Хөвсгөл алтан дуулга ХК', '294', '294', '294', '294',
     '50', '14,700', '2026-09-10', 'Энгийн арилжаа']
    Used purely for verification, so we can confirm we've matched the
    right row before ever attempting to click anything."""
    rows = page.locator("table tbody tr")
    if index >= rows.count():
        return []
    cells = rows.nth(index).locator("td")
    out = []
    for i in range(cells.count()):
        try:
            out.append(cells.nth(i).inner_text().strip())
        except Exception:
            out.append("")
    return out


def find_row_by_ticker(page, ticker: str):
    """Returns (index, full_row_cells) for the first row whose Символ
    column matches `ticker` (case-insensitive), or (None, []) if not found."""
    row_count = get_row_count(page)
    for i in range(row_count):
        t, _ = read_row_basic_info(page, i)
        if t and t.lower() == ticker.lower():
            return i, read_row_full_info(page, i)
    return None, []


def run_peek(page, ticker: str):
    """Pure verification, no clicking at all: load the list, find the
    row matching `ticker`, and print every cell so we can confirm the
    match is correct before touching navigation."""
    load_trade_history(page)
    row_count = get_row_count(page)
    print(f"Loaded {row_count} rows.\n")

    index, cells = find_row_by_ticker(page, ticker)
    if index is None:
        print(f"No row found with Символ == '{ticker}'.")
        all_tickers = [read_row_basic_info(page, i)[0] for i in range(min(row_count, 20))]
        print(f"First 20 tickers on the page: {all_tickers}")
        return

    print(f"Matched row index {index}:")
    labels = ["№", "Символ", "Компанийн нэр", "Дээд", "Доод", "Нээлт", "Хаалт",
              "Ширхэг", "Үнийн дүн", "Огноо", "Арилжааны төрөл"]
    for i, val in enumerate(cells):
        label = labels[i] if i < len(labels) else f"col_{i}"
        print(f"  {label:20s} = {val}")

    print("\nThis is purely a read — no click was attempted. If this matches "
          "what you see on the live site, row identification is confirmed "
          "correct and the remaining issue (if any) is specifically in "
          "clicking into the company page, not in finding it.")


def click_into_company(list_page, index: int, ticker: str = "", verbose: bool = True):
    """Click row `index` on the trade-history table and get to the
    company's page — which may open as a NEW BROWSER TAB rather than
    navigating list_page in place (confirmed: even a forced click on
    the whole row never changes list_page's URL at all, and we already
    know the 'Татах' download button opens a new tab too — so company
    links very likely do the same).

    Returns (company_page, opened_new_tab: bool) on success, where
    company_page is the Page object to scrape from (this may BE
    list_page, if it turns out some rows navigate in place after all).
    Returns (None, False) if every strategy failed.
    """
    rows = list_page.locator("table tbody tr")
    if index >= rows.count():
        if verbose:
            print(f"    !! row index {index} no longer exists ({rows.count()} rows present)")
        return None, False
    row = rows.nth(index)

    ticker_cell = row.locator("td").nth(1)
    name_cell = row.locator("td").nth(2)

    def cell_or_inner_link(cell):
        link = cell.locator("a")
        return (link.first, "inner <a>") if link.count() > 0 else (cell, "cell itself")

    t1, t1_desc = cell_or_inner_link(ticker_cell)
    t2, t2_desc = cell_or_inner_link(name_cell)

    click_targets = [
        (t1, f"ticker cell ({t1_desc})"),
        (ticker_cell, "ticker <td> (plain)"),
        (t2, f"name cell ({t2_desc})"),
        (name_cell, "name <td> (plain)"),
        (row, "whole row"),
    ]

    for target, desc in click_targets:
        for force in (False, True):
            label = f"[{desc}]{' (forced)' if force else ''}"

            # Attempt 1: does this click open a new tab (popup)?
            popup_page = None
            popup_failed_reason = None
            try:
                with list_page.expect_popup(timeout=4000) as popup_info:
                    target.scroll_into_view_if_needed(timeout=3000)
                    target.click(timeout=ROW_CLICK_TIMEOUT_MS, force=force)
                popup_page = popup_info.value
            except PWTimeout:
                popup_failed_reason = "no popup opened"
            except Exception as e:
                if verbose:
                    print(f"    click {label} raised: {type(e).__name__}: {str(e).splitlines()[0]}")
                continue

            if popup_page is not None:
                try:
                    popup_page.wait_for_load_state("domcontentloaded", timeout=8000)
                    popup_page.wait_for_selector(f"text={ACTIVITY_REPORT_TAB_TEXT}", timeout=8000)
                    if verbose:
                        print(f"    click {label} -> opened new tab at {popup_page.url}")
                    return popup_page, True
                except PWTimeout:
                    if verbose:
                        print(f"    click {label}: new tab opened at {popup_page.url} "
                              f"but no tab bar found there")
                    popup_page.close()
                    continue

            # Attempt 2: no popup — maybe it navigated list_page itself.
            try:
                list_page.wait_for_selector(f"text={ACTIVITY_REPORT_TAB_TEXT}", timeout=3000)
                if verbose:
                    print(f"    click {label} -> navigated in place to {list_page.url}")
                return list_page, False
            except PWTimeout:
                if verbose:
                    print(f"    click {label}: {popup_failed_reason}, and no in-place "
                          f"navigation either (still at {list_page.url})")
                continue

    if verbose:
        print(f"    !! all click strategies failed for row {index} ({ticker})")
    return None, False


def return_to_trade_history(page):
    try:
        page.go_back(timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_selector("table tbody tr", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(1000)
    except Exception:
        # Fallback: just reload the listing outright if back-navigation misbehaves.
        load_trade_history(page)


# ---------------------------------------------------------------------------
# Company page -> activity report table
# ---------------------------------------------------------------------------

def get_header_map(table) -> dict:
    header_cells = table.locator("thead tr").first.locator("th")
    if header_cells.count() == 0:
        header_cells = table.locator("tr").first.locator("th, td")
    mapping = {}
    try:
        for i in range(header_cells.count()):
            text = header_cells.nth(i).inner_text().strip()
            if text:
                mapping[text] = i
    except Exception:
        pass
    return mapping


def find_reports_table(page):
    """The report tab may hold more than one table (there's also a
    'Компанийн засаглалын үнэлгээний тайлан' sub-tab with the same
    shape). Find the one whose header matches Огноо/Файл."""
    tables = page.locator("table")
    n = tables.count()
    for i in range(n):
        table = tables.nth(i)
        header_map = get_header_map(table)
        if "Огноо" in header_map and "Файл" in header_map:
            return table, header_map
    if n > 0:
        return tables.nth(n - 1), {"№": 0, "Он": 1, "Улирал": 2, "Тайлбар": 3, "Огноо": 4, "Файл": 5}
    return None, {}


def open_activity_report_tab(page) -> bool:
    """Click the 'Үйл ажиллагааны тайлан' tab (assumes it's already
    visible — call this only after click_into_company succeeds) and
    wait for its report table to render."""
    try:
        tab = page.get_by_text(ACTIVITY_REPORT_TAB_TEXT, exact=False).first
        tab.click(timeout=5000)
    except Exception as e:
        print(f"    !! couldn't click the activity-report tab: {e}")
        return False

    for _ in range(TAB_RENDER_RETRIES):
        page.wait_for_timeout(TAB_RENDER_WAIT_MS)
        table, header_map = find_reports_table(page)
        if table is not None and "Огноо" in header_map and "Файл" in header_map:
            return True
    return False


def cell_text(cells, idx) -> str:
    if idx is None:
        return ""
    try:
        if idx < cells.count():
            return cells.nth(idx).inner_text().strip()
    except Exception:
        pass
    return ""


def get_activity_reports(page) -> list[ReportRow]:
    table, header_map = find_reports_table(page)
    if table is None:
        return []

    rows = table.locator("tbody tr")
    row_count = rows.count()
    if row_count == 0:
        return []

    year_idx = header_map.get("Он")
    quarter_idx = header_map.get("Улирал")
    desc_idx = header_map.get("Тайлбар")
    date_idx = header_map.get("Огноо")
    file_idx = header_map.get("Файл")

    reports = []
    for i in range(row_count):
        row = rows.nth(i)
        cells = row.locator("td")
        if cells.count() == 0:
            continue

        direct_url = ""
        try:
            file_cell = cells.nth(file_idx) if file_idx is not None else row
            link = file_cell.locator("a[href]").first
            if link.count() > 0:
                href = link.get_attribute("href") or ""
                resolved = resolve_report_url(href)
                if DOC_EXT_PATTERN.search(resolved):
                    direct_url = resolved
        except Exception:
            pass

        reports.append(
            ReportRow(
                year=cell_text(cells, year_idx),
                quarter=cell_text(cells, quarter_idx),
                description=cell_text(cells, desc_idx),
                date_str=cell_text(cells, date_idx),
                direct_url=direct_url,
                row_index=i,
            )
        )

    reports.sort(key=lambda r: r.sort_key(), reverse=True)
    return reports


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------

def safe_name(text: str, max_len: int = 60) -> str:
    text = re.sub(r"\s+", "_", text.strip())
    text = re.sub(r'[\\/*?:"<>|]', "", text)
    return text[:max_len] or "report"


def download_direct(report: ReportRow, dest_dir: Path, session: requests.Session) -> bool:
    dest_dir.mkdir(parents=True, exist_ok=True)
    year_q = f"{report.year}_Q{report.quarter}" if report.quarter else report.year
    ext = Path(report.direct_url.split("?")[0]).suffix or ".pdf"
    filename = safe_name(f"{year_q}_{report.description}") + ext
    dest_path = dest_dir / filename

    if dest_path.exists():
        print(f"    already have {filename}")
        return True
    try:
        resp = session.get(report.direct_url, timeout=30)
        resp.raise_for_status()
        dest_path.write_bytes(resp.content)
        print(f"    downloaded {filename} ({len(resp.content) / 1024:.0f} KB)")
        return True
    except requests.RequestException as e:
        print(f"    !! failed direct download {report.direct_url}: {e}")
        return False


def download_via_new_tab(page, row_index: int, dest_dir: Path, report: ReportRow, session: requests.Session) -> bool:
    """Fallback for the rare row where 'Татах' has no plain href we could
    read: click it, capture the new tab's URL (it's just a normal PDF
    page, per your screenshot — not a native download event), then
    download that URL directly."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    table, header_map = find_reports_table(page)
    if table is None:
        return False
    row = table.locator("tbody tr").nth(row_index)
    file_idx = header_map.get("Файл")
    file_cell = row.locator("td").nth(file_idx) if file_idx is not None else row

    target = file_cell.get_by_text(DOWNLOAD_BUTTON_TEXT, exact=False)
    if target.count() == 0:
        target = file_cell.locator("a, button")
    if target.count() == 0:
        return False

    try:
        with page.expect_popup(timeout=8000) as popup_info:
            target.first.click(timeout=5000)
        popup = popup_info.value
        popup.wait_for_load_state("domcontentloaded", timeout=8000)
        pdf_url = popup.url
        popup.close()
    except PWTimeout:
        print(f"    !! clicking 'Татах' opened neither a link nor a new tab for: {report.label()}")
        return False
    except Exception as e:
        print(f"    !! click-to-new-tab failed for {report.label()}: {e}")
        return False

    report.direct_url = pdf_url
    return download_direct(report, dest_dir, session)


# ---------------------------------------------------------------------------
# Inspect mode
# ---------------------------------------------------------------------------

def run_inspect(page, ticker: str):
    load_trade_history(page)
    target_index, row_cells = find_row_by_ticker(page, ticker)

    if target_index is None:
        row_count = get_row_count(page)
        print(f"Ticker '{ticker}' not found among {row_count} rows. "
              f"Try `--peek {ticker}` first to double check.")
        return

    print(f"Matched row {target_index}: {row_cells}")
    print(f"Clicking row {target_index} ({ticker}) ...")
    company_page, opened_new_tab = click_into_company(page, target_index, ticker=ticker)
    if company_page is None:
        print("!! Could not click into the company page at all (tab bar never appeared "
              "on the list page or in a new tab).")
        Path("inspect_trade_history.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path="inspect_trade_history.png", full_page=True)
        print("Saved inspect_trade_history.html / .png — send these over.")
        return

    print(f"On company page ({'new tab' if opened_new_tab else 'same tab'}): {company_page.url}")
    opened = open_activity_report_tab(company_page)

    reports = []
    if opened:
        reports = get_activity_reports(company_page)
        print(f"\nParsed {len(reports)} report row(s):")
        for r in reports[:5]:
            print(f"  {r.year} Q{r.quarter} | {r.description} | {r.date_str} | "
                  f"{'direct href OK' if r.direct_url else 'NO href found — would use click fallback'}")
        if len(reports) > 5:
            print(f"  ... and {len(reports) - 5} more")
    else:
        print("!! Tab clicked but no report table with Огноо/Файл headers appeared.")

    html_path = Path(f"inspect_{ticker}.html")
    png_path = Path(f"inspect_{ticker}.png")
    html_path.write_text(company_page.content(), encoding="utf-8")
    company_page.screenshot(path=str(png_path), full_page=True)
    print(f"\nSaved {html_path} and {png_path}.")
    if opened_new_tab:
        company_page.close()
    if not opened or not reports:
        print("Send me these two files and I'll fix the selectors precisely.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Download top-N activity reports per MSE company")
    parser.add_argument("--company", help="Only scrape one company, by ticker (e.g. ADU)")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--headless", default="true", choices=["true", "false"])
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--out", default=str(OUTPUT_DIR))
    parser.add_argument("--inspect", metavar="TICKER", help="Debug mode: dump HTML/screenshot for one company and exit")
    parser.add_argument("--peek", metavar="TICKER", help="Pure verification: find and print one company's row, no clicking, no browser navigation beyond loading the list")
    args = parser.parse_args()

    out_dir = Path(args.out)
    headless = args.headless == "true"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)

        if args.peek:
            run_peek(page, args.peek)
            browser.close()
            return

        if args.inspect:
            run_inspect(page, args.inspect)
            browser.close()
            return

        load_trade_history(page)
        row_count = get_row_count(page)
        print(f"Found {row_count} rows in the trade-history table.")

        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; MSEReportFetcher/1.0)"})

        summary = []
        for i in range(row_count):
            ticker, name = read_row_basic_info(page, i)
            if not ticker:
                continue
            if args.company and ticker.lower() != args.company.strip().lower():
                continue

            print(f"\n[{i + 1}/{row_count}] {name} ({ticker})")

            company_page, opened_new_tab = click_into_company(page, i, ticker=ticker)
            if company_page is None:
                print("    !! could not navigate into this company's page, skipping")
                summary.append({"ticker": ticker, "name": name, "found": 0, "downloaded": 0, "error": "navigation failed"})
                # Save a diagnostic snapshot on the FIRST navigation failure only,
                # so a full run doesn't spam dozens of debug files.
                if not any(s.get("error") == "navigation failed" for s in summary[:-1]):
                    try:
                        Path(f"debug_fail_{ticker}.html").write_text(page.content(), encoding="utf-8")
                        page.screenshot(path=f"debug_fail_{ticker}.png", full_page=True)
                        print(f"    saved debug_fail_{ticker}.html / .png for troubleshooting")
                    except Exception:
                        pass
                # We may have ended up somewhere odd — force back to a known state.
                return_to_trade_history(page)
                continue

            try:
                if not open_activity_report_tab(company_page):
                    print("    no activity-report table appeared, skipping")
                    summary.append({"ticker": ticker, "name": name, "found": 0, "downloaded": 0})
                else:
                    reports = get_activity_reports(company_page)
                    if not reports:
                        print("    activity-report table is empty for this company (null) — skipping")
                        summary.append({"ticker": ticker, "name": name, "found": 0, "downloaded": 0})
                    else:
                        print(f"    found {len(reports)} report row(s):")
                        for r in reports:
                            print(f"      {r.year or '?'} Q{r.quarter or '?'} | {r.description or '(no description)'} "
                                  f"| {r.date_str or '(no date)'} | "
                                  f"{'direct link' if r.direct_url else 'no href, will click-through'}")
                        top_reports = reports[: args.top]
                        dl_count = 0
                        if not args.list_only:
                            dest_dir = out_dir / safe_name(f"{ticker}_{name}")
                            for r in top_reports:
                                ok = (
                                    download_direct(r, dest_dir, session)
                                    if r.direct_url
                                    else download_via_new_tab(company_page, r.row_index, dest_dir, r, session)
                                )
                                if ok:
                                    dl_count += 1
                        summary.append({"ticker": ticker, "name": name, "found": len(reports), "downloaded": dl_count})
            except PWTimeout:
                print("    !! timed out mid-scrape, skipping")
                summary.append({"ticker": ticker, "name": name, "found": 0, "downloaded": 0, "error": "timeout"})
            except Exception as e:
                print(f"    !! error: {e}, skipping")
                summary.append({"ticker": ticker, "name": name, "found": 0, "downloaded": 0, "error": str(e)})
            finally:
                if opened_new_tab:
                    try:
                        company_page.close()
                    except Exception:
                        pass
                    # list page (`page`) never left trade-history, so no need to reload it.
                else:
                    return_to_trade_history(page)

            time.sleep(BETWEEN_COMPANIES_DELAY_S)

        browser.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\nDone. Summary written to {out_dir / '_summary.json'}")


if __name__ == "__main__":
    main()