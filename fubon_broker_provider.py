#!/usr/bin/env python3
"""SQLite-backed, low-rate Fubon eBrokerDJ broker-data provider.

Designed for personal research: refresh the publicly shipped broker roster, then
upsert dated broker-detail rows.  It intentionally does not launch bulk jobs or
attempt to bypass source-site controls.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator

from probe_fubon_broker import BASE, fetch_probe, make_session, text

ROSTER_PATH = "/z/js/zbrokerjs.djjs"

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS broker_roster (
  snapshot_date TEXT NOT NULL,
  broker_id TEXT NOT NULL,
  broker_name TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  PRIMARY KEY (snapshot_date, broker_id)
);
CREATE TABLE IF NOT EXISTS broker_daily (
  date TEXT NOT NULL,
  stock_id TEXT NOT NULL,
  broker_id TEXT NOT NULL,
  broker_name TEXT NOT NULL,
  buy INTEGER NOT NULL,
  sell INTEGER NOT NULL,
  net INTEGER NOT NULL,
  source_url TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (date, stock_id, broker_id)
);
CREATE INDEX IF NOT EXISTS idx_broker_daily_stock_date ON broker_daily(stock_id, date);
CREATE TABLE IF NOT EXISTS fetch_runs (
  run_id INTEGER PRIMARY KEY,
  fetched_at TEXT NOT NULL,
  stock_id TEXT NOT NULL,
  broker_id TEXT NOT NULL,
  requested_start TEXT NOT NULL,
  requested_end TEXT NOT NULL,
  http_status INTEGER,
  rows_returned INTEGER NOT NULL,
  latency_ms INTEGER NOT NULL,
  error TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def parse_roster(payload: str) -> list[tuple[str, str]]:
    match = re.search(r"var\s+g_BrokerList\s*=\s*'(.*?)';", payload, re.S)
    if not match:
        raise ValueError("Could not find g_BrokerList in roster JavaScript")
    entries: dict[str, str] = {}
    for group in match.group(1).split(";"):
        for item in group.split("!"):
            if "," in item:
                broker_id, broker_name = item.split(",", 1)
                if broker_id and broker_name:
                    entries[broker_id] = broker_name
    return sorted(entries.items())


def refresh_roster(db: sqlite3.Connection, raw_dir: Path) -> None:
    session = make_session()
    response = session.get(f"{BASE}{ROSTER_PATH}", timeout=(10, 30))
    response.raise_for_status()
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.joinpath("zbrokerjs.djjs").write_bytes(response.content)
    snapshot_date = date.today().isoformat()
    digest = hashlib.sha256(response.content).hexdigest()
    rows = parse_roster(text(response))
    db.executemany(
        "INSERT OR REPLACE INTO broker_roster(snapshot_date, broker_id, broker_name, source_sha256) VALUES (?, ?, ?, ?)",
        [(snapshot_date, broker_id, broker_name, digest) for broker_id, broker_name in rows],
    )
    db.commit()
    print(f"Roster snapshot {snapshot_date}: {len(rows)} IDs; sha256={digest}")


def dateranges(start: str, end: str, days: int) -> Iterator[tuple[str, str]]:
    current = datetime.strptime(start, "%Y-%m-%d").date()
    last = datetime.strptime(end, "%Y-%m-%d").date()
    while current <= last:
        finish = min(current + timedelta(days=days - 1), last)
        yield current.isoformat(), finish.isoformat()
        current = finish + timedelta(days=1)


def broker_name(db: sqlite3.Connection, broker_id: str, fallback: str | None) -> str:
    if fallback:
        return fallback
    row = db.execute("SELECT broker_name FROM broker_roster WHERE broker_id=? ORDER BY snapshot_date DESC LIMIT 1", (broker_id,)).fetchone()
    if not row:
        raise ValueError(f"Broker {broker_id} is not in local roster; run refresh-roster or pass --broker-name.")
    return str(row[0])


def fetch(db: sqlite3.Connection, stock_id: str, broker_id: str, name: str, start: str, end: str, chunk_days: int, raw_dir: Path, pause: float) -> None:
    session = make_session()
    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
    total = 0
    for number, (chunk_start, chunk_end) in enumerate(dateranges(start, end, chunk_days), start=1):
        probe, rows = fetch_probe(session, stock_id, broker_id, name, f"{stock_id}_{broker_id}_{chunk_start}_{chunk_end}", chunk_start, chunk_end, raw_dir, pause)
        db.execute(
            "INSERT INTO fetch_runs(fetched_at, stock_id, broker_id, requested_start, requested_end, http_status, rows_returned, latency_ms, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (fetched_at, stock_id, broker_id, chunk_start, chunk_end, probe.status, probe.rows, probe.latency_ms, probe.error),
        )
        db.executemany(
            """INSERT INTO broker_daily(date, stock_id, broker_id, broker_name, buy, sell, net, source_url, fetched_at)
               VALUES (:date, :stock_id, :broker_id, :broker_name, :buy, :sell, :net, :source_url, :fetched_at)
               ON CONFLICT(date, stock_id, broker_id) DO UPDATE SET
                 broker_name=excluded.broker_name, buy=excluded.buy, sell=excluded.sell, net=excluded.net,
                 source_url=excluded.source_url, fetched_at=excluded.fetched_at""",
            [{**row, "source_url": probe.url, "fetched_at": fetched_at} for row in rows],
        )
        db.commit()
        total += len(rows)
        print(f"Chunk {number}: {chunk_start}..{chunk_end}; HTTP {probe.status}; {len(rows)} rows; {probe.latency_ms} ms")
    print(f"Completed: {total} returned rows (upserted; duplicates update in place).")


def enabled_watchlist(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    required = {"stock_id", "enabled", "history_start"}
    if not required.issubset(set(reader.fieldnames or [])):
        raise ValueError("watchlist requires headers: stock_id,enabled,history_start")
    # A header-only file is valid, and produces no work.
    return [row for row in rows if row["enabled"].strip().lower() in {"1", "true", "yes", "y"}]


def sync_watchlist(db: sqlite3.Connection, watchlist: Path, broker_id: str, name: str, end: str, lookback_days: int, chunk_days: int, raw_dir: Path, pause: float) -> None:
    for item in enabled_watchlist(watchlist):
        stock_id = item["stock_id"].strip()
        history_start = item["history_start"].strip()
        last = db.execute("SELECT MAX(date) FROM broker_daily WHERE stock_id=? AND broker_id=?", (stock_id, broker_id)).fetchone()[0]
        if last:
            start = max(datetime.strptime(history_start, "%Y-%m-%d").date(), datetime.strptime(last, "%Y-%m-%d").date() - timedelta(days=lookback_days)).isoformat()
            mode = f"incremental from {last}, with {lookback_days}-day lookback"
        else:
            start = history_start
            mode = "initial backfill"
        if start > end:
            print(f"Skip {stock_id}: start {start} is after end {end}.")
            continue
        print(f"Sync {stock_id}: {mode}; request {start}..{end}")
        fetch(db, stock_id, broker_id, name, start, end, chunk_days, raw_dir, pause)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("provider_data/fubon_broker.sqlite"))
    parser.add_argument("--raw-dir", type=Path, default=Path("provider_data/raw"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("refresh-roster")
    pull = commands.add_parser("fetch")
    pull.add_argument("--stock-id", required=True)
    pull.add_argument("--broker-id", required=True)
    pull.add_argument("--broker-name")
    pull.add_argument("--start-date", required=True)
    pull.add_argument("--end-date", required=True)
    pull.add_argument("--chunk-days", type=int, default=366)
    pull.add_argument("--pause-seconds", type=float, default=1.5)
    sync = commands.add_parser("sync-watchlist", help="Update enabled stocks; new stocks backfill, existing stocks re-fetch a short lookback window.")
    sync.add_argument("--watchlist", type=Path, default=Path("watchlist.csv"))
    sync.add_argument("--broker-id", required=True)
    sync.add_argument("--broker-name")
    sync.add_argument("--end-date", required=True)
    sync.add_argument("--lookback-days", type=int, default=10)
    sync.add_argument("--chunk-days", type=int, default=366)
    sync.add_argument("--pause-seconds", type=float, default=1.5)
    args = parser.parse_args()
    db = connect(args.db)
    try:
        if args.command == "refresh-roster":
            refresh_roster(db, args.raw_dir)
        elif args.command == "fetch":
            if args.chunk_days < 1:
                raise SystemExit("--chunk-days must be positive")
            fetch(db, args.stock_id, args.broker_id, broker_name(db, args.broker_id, args.broker_name), args.start_date, args.end_date, args.chunk_days, args.raw_dir, args.pause_seconds)
        else:
            if args.chunk_days < 1 or args.lookback_days < 0:
                raise SystemExit("--chunk-days must be positive and --lookback-days cannot be negative")
            sync_watchlist(db, args.watchlist, args.broker_id, broker_name(db, args.broker_id, args.broker_name), args.end_date, args.lookback_days, args.chunk_days, args.raw_dir, args.pause_seconds)
    finally:
        db.close()


if __name__ == "__main__":
    main()
