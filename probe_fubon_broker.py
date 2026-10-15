#!/usr/bin/env python3
"""Low-rate, HTTP-only probe for Fubon eBrokerDJ broker trading detail pages.

This is a research PoC, not a production ingestion service.  It records both
successful and unsuccessful HTTP probes so that availability claims are
auditable and repeatable.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://fubon-ebrokerdj.fbs.com.tw"
SUMMARY_PATH = "/z/zc/zco/zco.djhtm"
DETAIL_PATH = "/z/zc/zco/zco0/zco0.djhtm"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)


@dataclass
class Probe:
    label: str
    start: str
    end: str
    status: int | None
    rows: int
    earliest_date: str | None
    latest_date: str | None
    latency_ms: int
    error: str | None
    url: str


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2, connect=2, read=2, status=2,
        backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",), raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    return session


def text(response: requests.Response) -> str:
    # The site currently labels pages ISO-8859-1 even though the content is Big5.
    return response.content.decode("big5", errors="replace")


def clean(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def number(value: str) -> int:
    value = clean(value).replace(",", "")
    if value in {"", "-", "--"}:
        return 0
    return int(float(re.sub(r"[^0-9.-]", "", value)))


def extract_top_brokers(html: str) -> list[dict[str, str]]:
    """Extract the broker label and actual `b` code from the summary's links."""
    soup = BeautifulSoup(html, "html.parser")
    brokers: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.select('a[href*="zco0.djhtm"]'):
        label = clean(anchor.get_text(" ", strip=True))
        query = parse_qs(urlparse(urljoin(BASE, anchor.get("href", ""))).query)
        broker_id = query.get("b", [""])[0]
        if label and broker_id and broker_id not in seen:
            seen.add(broker_id)
            brokers.append({"broker_id": broker_id, "broker_name": label})
    return brokers


def extract_broker_name(html: str, fallback: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    title_cell = soup.select_one("td.t10")
    title = title_cell.get_text(" ", strip=True) if title_cell else ""
    match = re.search(r"(.+?)\s*對\s*.+?\(\d+\)個股", title)
    return clean(match.group(1)) if match else fallback


def extract_detail_rows(html: str, stock_id: str, broker_id: str, broker_name: str) -> list[dict[str, object]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="oMainTable")
    if table is None:
        return []
    rows: list[dict[str, object]] = []
    for tr in table.find_all("tr"):
        cells = [clean(cell.get_text(" ", strip=True)) for cell in tr.find_all("td", recursive=False)]
        if len(cells) < 5 or not re.fullmatch(r"\d{4}/\d{2}/\d{2}", cells[0]):
            continue
        rows.append({
            "date": cells[0].replace("/", "-"), "stock_id": stock_id,
            "broker_id": broker_id, "broker_name": broker_name,
            # The page's fourth numeric column is buy+sell; the fifth is net.
            "buy": number(cells[1]), "sell": number(cells[2]), "net": number(cells[4]),
        })
    return rows


def fetch_probe(session: requests.Session, stock_id: str, broker_id: str, broker_name: str,
                label: str, start: str, end: str, raw_dir: Path, pause_seconds: float) -> tuple[Probe, list[dict[str, object]]]:
    params = {"a": stock_id, "BHID": broker_id[:4], "b": broker_id, "C": "1", "D": start, "E": end, "ver": "V3"}
    url = f"{BASE}{DETAIL_PATH}"
    started = time.perf_counter()
    status: int | None = None
    rows: list[dict[str, object]] = []
    error = None
    try:
        response = session.get(url, params=params, timeout=(10, 30))
        status = response.status_code
        raw_dir.joinpath(f"detail_{label}.html").write_bytes(response.content)
        if response.ok:
            page = text(response)
            actual_name = extract_broker_name(page, broker_name)
            rows = extract_detail_rows(page, stock_id, broker_id, actual_name)
    except requests.RequestException as exc:
        error = f"{type(exc).__name__}: {exc}"
    latency_ms = round((time.perf_counter() - started) * 1000)
    dates = sorted(row["date"] for row in rows)
    probe = Probe(label, start, end, status, len(rows), dates[0] if dates else None,
                  dates[-1] if dates else None, latency_ms, error, requests.Request("GET", url, params=params).prepare().url)
    time.sleep(pause_seconds)
    return probe, rows


def write_csv(path: Path, rows: Iterable[dict[str, object]]) -> None:
    fields = ["date", "stock_id", "broker_id", "broker_name", "buy", "sell", "net"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda row: row["date"]))


def report(path: Path, stock_id: str, broker: dict[str, str], probes: list[Probe]) -> None:
    successful = [probe for probe in probes if probe.rows]
    earliest = min((probe.earliest_date for probe in successful if probe.earliest_date), default=None)
    all_history = next((probe for probe in probes if probe.label == "all_history"), None)
    lines = [
        "# Fubon eBrokerDJ broker-data HTTP PoC", "",
        f"- Stock: `{stock_id}`", f"- Selected broker: `{broker['broker_name']}` (`{broker['broker_id']}`)",
        f"- Run date: `{date.today().isoformat()}`", "- Transport: Python `requests`; no Selenium.",
        f"- Earliest returned record across probes: `{earliest or 'none'}`.",
        f"- Largest tested single request: `{all_history.start}..{all_history.end}`; {all_history.rows} rows; HTTP {all_history.status}." if all_history else "",
        "", "## Probe results", "",
        "| label | requested interval | HTTP | rows | earliest | latest | latency ms | error |",
        "|---|---|---:|---:|---|---|---:|---|",
    ]
    for probe in probes:
        lines.append(f"| {probe.label} | {probe.start}..{probe.end} | {probe.status or '-'} | {probe.rows} | {probe.earliest_date or '-'} | {probe.latest_date or '-'} | {probe.latency_ms} | {probe.error or '-'} |")
    lines.extend(["", "## Interpretation", "", "HTTP is considered viable only when the raw HTML contains the data table and the parser returns dated rows. This test measures availability, not a provider SLA or reuse permission. Use a conservative rate limit and verify the site terms before any sustained use."])
    path.write_text("\n".join(line for line in lines if line != "") + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stock-id", default="2408")
    parser.add_argument("--broker-id", default=None, help="Optional override; otherwise choose a Top-15 broker containing the preferred name.")
    parser.add_argument("--preferred-broker", default="台灣摩根士丹利")
    parser.add_argument("--end-date", default="2026-09-15", help="Use a known completed trading date for repeatable probes.")
    parser.add_argument("--pause-seconds", type=float, default=1.5)
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    args = parser.parse_args()
    output = args.output_dir
    raw_dir = output / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    session = make_session()
    summary = session.get(f"{BASE}{SUMMARY_PATH}", params={"a": args.stock_id}, timeout=(10, 30))
    raw_dir.joinpath("summary_top15.html").write_bytes(summary.content)
    brokers = extract_top_brokers(text(summary)) if summary.ok else []
    (output / "top15_brokers.json").write_text(json.dumps(brokers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    selected = next((item for item in brokers if args.preferred_broker in item["broker_name"]), None)
    if args.broker_id:
        selected = next((item for item in brokers if item["broker_id"] == args.broker_id), {"broker_id": args.broker_id, "broker_name": args.preferred_broker})
    if selected is None:
        raise SystemExit(f"No matching broker in current Top-15. Available: {brokers}")
    end = args.end_date
    intervals = [(str(year), f"{year}-01-01", f"{year}-12-31") for year in (2026, 2025, 2024, 2022, 2020, 2018)]
    intervals[0] = ("2026", "2026-01-01", end)
    intervals.extend([("all_history", "2000-01-01", end), ("2018_to_end", "2018-01-01", end)])
    probes: list[Probe] = []
    normalized: list[dict[str, object]] = []
    for label, start, finish in intervals:
        probe, rows = fetch_probe(session, args.stock_id, selected["broker_id"], selected["broker_name"], label, start, finish, raw_dir, args.pause_seconds)
        probes.append(probe)
        normalized.extend(rows)
    # Deduplicate overlap from broad-range probes, retaining the most recent parser result.
    unique = {(row["date"], row["stock_id"], row["broker_id"]): row for row in normalized}
    write_csv(output / "normalized_broker_daily.csv", unique.values())
    (output / "probe_results.json").write_text(json.dumps([asdict(item) for item in probes], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report(output / "RESULTS.md", args.stock_id, selected, probes)
    print(f"Wrote {output / 'RESULTS.md'} and {len(unique)} normalized rows.")


if __name__ == "__main__":
    main()
