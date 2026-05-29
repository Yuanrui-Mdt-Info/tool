#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List

from email_gateway import build_email_message, load_smtp_config_from_env, send_via_smtp
from monitor_core import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PRODUCTS_CSV,
    run_monitor,
)


def parse_alert_targets(raw: str) -> List[str]:
    parts = [p.strip() for p in (raw or "").replace(";", ",").split(",")]
    return [p for p in parts if p]


def build_email_subject(summary: dict) -> str:
    run_id = summary.get("run_id", "")
    alerts_count = int(summary.get("alerts_count", 0))
    errors = int(summary.get("error_count", 0))
    return f"[Competitor Monitor] {alerts_count} alert(s), {errors} error(s) | {run_id}"


def build_email_body(summary: dict) -> str:
    lines = [
        "Competitor Product Monitor - Run Summary",
        "",
        f"Run ID: {summary.get('run_id', '')}",
        f"Status: {summary.get('status', '')}",
        f"Quota Mode: {summary.get('quota_mode', '')}",
        f"Weekly Quota (%): {summary.get('weekly_quota_pct', '')}",
        f"Products Processed: {summary.get('products_processed', 0)}",
        f"Success Count: {summary.get('success_count', 0)}",
        f"Error Count: {summary.get('error_count', 0)}",
        f"Alert Count: {summary.get('alerts_count', 0)}",
    ]
    if summary.get("alerts_by_type"):
        lines.append(f"Alerts By Type: {json.dumps(summary.get('alerts_by_type', {}), ensure_ascii=False)}")
    if summary.get("alert_digest_path"):
        lines.append(f"Alert Digest: {summary['alert_digest_path']}")
    if summary.get("run_snapshot_md_path"):
        lines.append(f"Run Snapshot: {summary['run_snapshot_md_path']}")
    if summary.get("details_csv_path"):
        lines.append(f"Run Details CSV: {summary['details_csv_path']}")
    if summary.get("summary_path"):
        lines.append(f"Summary JSON: {summary['summary_path']}")
    failures = summary.get("failures", [])
    if failures:
        lines.append("")
        lines.append("Failures:")
        for item in failures[:10]:
            lines.append(f"- {item.get('product_id')}: {item.get('error_message')}")
        if len(failures) > 10:
            lines.append(f"- ... and {len(failures) - 10} more")
    alerts = summary.get("alerts", [])
    if alerts:
        lines.append("")
        lines.append("Top Alerts:")
        for item in alerts[:12]:
            lines.append(
                f"- {item.get('product_id')} | {item.get('alert_type')} | "
                f"{item.get('previous_value')} -> {item.get('current_value')}"
            )
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run competitor product monitor MVP.")
    parser.add_argument("--products-csv", default=str(DEFAULT_PRODUCTS_CSV), help="Path to products.csv.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Path to sqlite db.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Output directory.")
    parser.add_argument("--config-path", default=str(DEFAULT_CONFIG_PATH), help="Path to monitor_config.json.")
    parser.add_argument("--weekly-quota-pct", type=float, default=100.0, help="Current weekly quota remaining percent.")
    parser.add_argument(
        "--price-change-threshold-pct",
        type=float,
        default=None,
        help="Optional CLI override for price alert threshold percentage.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=25, help="HTTP timeout seconds.")
    parser.add_argument("--mail-mode", choices=["prepare", "live"], default="prepare", help="Email mode.")
    parser.add_argument("--alert-to", default="", help="Comma-separated target emails.")
    parser.add_argument(
        "--send-on-no-alerts",
        action="store_true",
        help="Send summary email even when no alerts are found.",
    )
    args = parser.parse_args()

    summary = run_monitor(
        products_csv=Path(args.products_csv),
        db_path=Path(args.db_path),
        output_dir=Path(args.output_dir),
        weekly_quota_pct=args.weekly_quota_pct,
        price_change_threshold_pct=args.price_change_threshold_pct,
        timeout_seconds=args.timeout_seconds,
        config_path=Path(args.config_path),
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))

    targets = parse_alert_targets(args.alert_to)
    should_send = bool(targets) and (summary.get("alerts_count", 0) > 0 or args.send_on_no_alerts)
    if not should_send:
        return 0

    if args.mail_mode == "prepare":
        print("MAIL_PREPARED: set --mail-mode live to send.")
        return 0

    config, missing = load_smtp_config_from_env(default_from_name="Competitor Monitor")
    if config is None:
        print(f"MAIL_CONFIG_MISSING:{','.join(missing)}")
        return 2

    subject = build_email_subject(summary)
    body = build_email_body(summary)
    has_error = False
    for target in targets:
        msg = build_email_message(config, target, subject, body)
        result = send_via_smtp(config, msg)
        print(f"MAIL_RESULT to={target} status={result.get('status')} error={result.get('error_message', '')}")
        if result.get("status") != "SENT":
            has_error = True
    return 3 if has_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
