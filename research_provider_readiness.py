#!/usr/bin/env python3
"""Small, low-rate readiness check for a multi-stock Fubon broker provider.

It deliberately tests three representative instruments only.  It discovers the
static broker roster shipped by the site's own JavaScript, then confirms that a
broker-detail request works even if that broker is absent from a stock's current
Top-15 summary.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from probe_fubon_broker import BASE, SUMMARY_PATH, extract_top_brokers, fetch_probe, make_session, text

ROSTER_PATH = "/z/js/zbrokerjs.djjs"


def extract_roster(source: str) -> list[dict[str, str]]:
    match = re.search(r"var\s+g_BrokerList\s*=\s*'(.*?)';", source, re.S)
    if not match:
        raise ValueError("g_BrokerList was not found in the JavaScript payload")
    records: dict[str, str] = {}
    for group in match.group(1).split(";"):
        for item in group.split("!"):
            if "," not in item:
                continue
            broker_id, name = item.split(",", 1)
            if broker_id and name:
                records[broker_id] = name
    return [{"broker_id": broker_id, "broker_name": name} for broker_id, name in sorted(records.items())]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stocks", nargs="+", default=["2330", "2317", "0050"])
    parser.add_argument("--broker-id", default="1470")
    parser.add_argument("--broker-name", default="台灣摩根士丹利")
    parser.add_argument("--start-date", default="2026-01-01")
    parser.add_argument("--end-date", default="2026-09-15")
    parser.add_argument("--pause-seconds", type=float, default=1.5)
    parser.add_argument("--output-dir", type=Path, default=Path("research/output"))
    args = parser.parse_args()
    output = args.output_dir
    raw = output / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    session = make_session()

    roster_response = session.get(f"{BASE}{ROSTER_PATH}", timeout=(10, 30))
    raw.joinpath("zbrokerjs.djjs").write_bytes(roster_response.content)
    roster = extract_roster(text(roster_response)) if roster_response.ok else []
    output.joinpath("broker_roster.json").write_text(json.dumps(roster, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    roster_match = next((item for item in roster if item["broker_id"] == args.broker_id), None)

    results: list[dict[str, object]] = []
    for stock_id in args.stocks:
        summary = session.get(f"{BASE}{SUMMARY_PATH}", params={"a": stock_id}, timeout=(10, 30))
        raw.joinpath(f"summary_{stock_id}.html").write_bytes(summary.content)
        top = extract_top_brokers(text(summary)) if summary.ok else []
        output.joinpath(f"top_brokers_{stock_id}.json").write_text(json.dumps(top, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        time.sleep(args.pause_seconds)
        probe, rows = fetch_probe(session, stock_id, args.broker_id, args.broker_name, f"detail_{stock_id}", args.start_date, args.end_date, raw, args.pause_seconds)
        results.append({
            "stock_id": stock_id, "summary_status": summary.status_code,
            "summary_link_count": len(top), "selected_broker_in_current_top_links": any(item["broker_id"] == args.broker_id for item in top),
            "detail": probe.__dict__, "first_row": rows[0] if rows else None,
        })

    output.joinpath("readiness_results.json").write_text(json.dumps({"roster_status": roster_response.status_code, "roster_count": len(roster), "selected_broker_in_roster": roster_match, "stocks": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Provider readiness mini-study", "", f"- Roster JavaScript HTTP status: `{roster_response.status_code}`", f"- Unique broker / branch IDs parsed: `{len(roster)}`", f"- Selected broker in roster: `{roster_match}`", f"- Detail interval: `{args.start_date}..{args.end_date}`", "", "| stock | summary HTTP | current summary links | 1470 in current links | detail HTTP | dated rows | earliest | latest | latency ms |", "|---|---:|---:|---|---:|---:|---|---|---:|"]
    for item in results:
        probe = item["detail"]
        lines.append(f"| {item['stock_id']} | {item['summary_status']} | {item['summary_link_count']} | {item['selected_broker_in_current_top_links']} | {probe['status']} | {probe['rows']} | {probe['earliest_date'] or '-'} | {probe['latest_date'] or '-'} | {probe['latency_ms']} |")
    lines.extend(["", "## Conclusion", "", "The site exposes a parseable broker roster in a static public JavaScript payload. A provider can refresh and version this roster periodically, while treating the Top-15 summary only as a discovery/ranking view—not as the complete universe of broker branches. The direct-detail endpoint remains the data path to test per stock and broker."])
    output.joinpath("PROVIDER_READINESS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {output / 'PROVIDER_READINESS.md'}")


if __name__ == "__main__":
    main()
