#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path
from typing import Dict, List


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "monitor.db"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "outputs" / "latest_board.csv"


def read_products(conn: sqlite3.Connection) -> Dict[str, Dict[str, str]]:
    rows = conn.execute(
        """
        SELECT product_id, brand, product_line, product_name, url, site_type, enabled
        FROM products
        """
    ).fetchall()
    out: Dict[str, Dict[str, str]] = {}
    for row in rows:
        out[row["product_id"]] = {
            "brand": row["brand"] or "",
            "product_line": row["product_line"] or "",
            "product_name": row["product_name"] or "",
            "url": row["url"] or "",
            "site_type": row["site_type"] or "",
            "enabled": str(row["enabled"] or 0),
        }
    return out


def latest_snapshots(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.*
        FROM snapshots s
        INNER JOIN (
            SELECT product_id, MAX(id) AS max_id
            FROM snapshots
            GROUP BY product_id
        ) latest
        ON s.id = latest.max_id
        ORDER BY s.product_id
        """
    ).fetchall()


def latest_alert_count(conn: sqlite3.Connection, product_id: str, n: int = 10) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM (
            SELECT id
            FROM alerts
            WHERE product_id = ?
            ORDER BY id DESC
            LIMIT ?
        ) t
        """,
        (product_id, n),
    ).fetchone()
    return int(row["c"] if row else 0)


def latest_success_snapshot(conn: sqlite3.Connection, product_id: str) -> sqlite3.Row:
    return conn.execute(
        """
        SELECT *
        FROM snapshots
        WHERE product_id = ? AND success = 1
        ORDER BY id DESC
        LIMIT 1
        """,
        (product_id,),
    ).fetchone()


def gtm_score_for_row(site_type: str, title: str, price_value, features_count: int, specs_count: int, market: Dict[str, object]) -> Dict[str, str]:
    checks = [
        ("title", bool((title or "").strip())),
        ("price", price_value is not None),
        ("features", features_count > 0),
        ("specs", specs_count > 0),
    ]
    if site_type == "amazon":
        checks.extend(
            [
                ("asin", bool(str(market.get("asin", "") or "").strip())),
                ("category", bool(str(market.get("category_path", "") or "").strip())),
                ("brand", bool(str(market.get("brand_name", "") or "").strip())),
                ("rating", market.get("rating_value") is not None),
                ("reviews", market.get("review_count") is not None),
                ("availability", bool(str(market.get("availability", "") or "").strip())),
                ("seller", bool(str(market.get("sold_by", "") or "").strip())),
                ("delivery", bool(str(market.get("delivery_message", "") or "").strip())),
                ("bsr", market.get("bsr_rank") is not None),
                ("model", bool(str(market.get("item_model_number", "") or "").strip())),
            ]
        )
    else:
        checks.extend(
            [
                ("brand", bool(str(market.get("brand_name", "") or "").strip())),
                ("availability", bool(str(market.get("availability", "") or "").strip())),
                ("sku", bool(str(market.get("sku", "") or "").strip())),
            ]
        )
    total = len(checks)
    hit = sum(1 for _, ok in checks if ok)
    pct = round((hit / total) * 100.0, 1) if total > 0 else 0.0
    missing = " | ".join([name for name, ok in checks if not ok])
    return {"score": f"{hit}/{total}", "pct": str(pct), "missing": missing}


def main() -> int:
    parser = argparse.ArgumentParser(description="Export latest product monitor board.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Path to sqlite database.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH), help="Output CSV file path.")
    args = parser.parse_args()

    db_path = Path(args.db_path)
    out_path = Path(args.output)
    if not db_path.exists():
        print(f"DB_NOT_FOUND:{db_path}")
        return 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    products = read_products(conn)
    snapshots = latest_snapshots(conn)

    fieldnames = [
        "product_id",
        "brand",
        "product_line",
        "product_name",
        "site_type",
        "enabled",
        "captured_at",
        "success",
        "data_freshness",
        "last_success_captured_at",
        "http_status",
        "error_message",
        "title",
        "price_value",
        "currency",
        "price_raw",
        "asin",
        "parent_asin",
        "brand_name",
        "category_path",
        "best_sellers_rank_text",
        "bsr_rank",
        "bsr_category",
        "bsr_subrank",
        "bsr_subcategory",
        "rating_value",
        "review_count",
        "question_count",
        "availability",
        "in_stock",
        "delivery_message",
        "is_prime",
        "bought_past_month",
        "buying_options_count",
        "buy_box_suppressed",
        "seller_type",
        "fulfillment_type",
        "sold_by",
        "ships_from",
        "coupon_text",
        "discount_percent",
        "list_price_value",
        "list_price_currency",
        "item_model_number",
        "date_first_available",
        "product_dimensions",
        "item_weight",
        "country_of_origin",
        "manufacturer",
        "variation_count",
        "selected_color",
        "selected_size",
        "sku",
        "mpn",
        "gtin",
        "product_type",
        "price_valid_until",
        "item_condition",
        "badges",
        "image_count",
        "video_count",
        "has_aplus",
        "gtm_signal_score",
        "gtm_signal_score_pct",
        "gtm_missing_fields",
        "features_count",
        "specs_count",
        "recent_alert_count_10",
        "url",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in snapshots:
            product_id = row["product_id"]
            meta = products.get(product_id, {})
            source_row = row
            data_freshness = "fresh" if bool(row["success"]) else "empty"
            last_success_captured_at = ""
            if not bool(row["success"]):
                prev = latest_success_snapshot(conn, product_id)
                if prev is not None:
                    source_row = prev
                    data_freshness = "stale_from_last_success"
                    last_success_captured_at = prev["captured_at"] or ""

            features = []
            specs = {}
            try:
                features = json.loads(source_row["key_features_json"] or "[]")
            except json.JSONDecodeError:
                features = []
            try:
                specs = json.loads(source_row["key_specs_json"] or "{}")
            except json.JSONDecodeError:
                specs = {}
            try:
                market = json.loads(source_row["market_signals_json"] or "{}")
                if not isinstance(market, dict):
                    market = {}
            except json.JSONDecodeError:
                market = {}
            features_count = len(features) if isinstance(features, list) else 0
            specs_count = len(specs) if isinstance(specs, dict) else 0
            score = gtm_score_for_row(
                str(meta.get("site_type", row["site_type"] or "")),
                str(source_row["title"] or ""),
                source_row["price_value"],
                features_count,
                specs_count,
                market,
            )

            writer.writerow(
                {
                    "product_id": product_id,
                    "brand": meta.get("brand", ""),
                    "product_line": meta.get("product_line", ""),
                    "product_name": meta.get("product_name", ""),
                    "site_type": meta.get("site_type", row["site_type"] or ""),
                    "enabled": meta.get("enabled", ""),
                    "captured_at": row["captured_at"] or "",
                    "success": bool(row["success"]),
                    "data_freshness": data_freshness,
                    "last_success_captured_at": last_success_captured_at,
                    "http_status": row["http_status"] or 0,
                    "error_message": row["error_message"] or "",
                    "title": source_row["title"] or "",
                    "price_value": source_row["price_value"],
                    "currency": source_row["currency"] or "",
                    "price_raw": source_row["price_raw"] or "",
                    "asin": market.get("asin", ""),
                    "parent_asin": market.get("parent_asin", ""),
                    "brand_name": market.get("brand_name", ""),
                    "category_path": market.get("category_path", ""),
                    "best_sellers_rank_text": market.get("best_sellers_rank_text", ""),
                    "bsr_rank": market.get("bsr_rank"),
                    "bsr_category": market.get("bsr_category", ""),
                    "bsr_subrank": market.get("bsr_subrank"),
                    "bsr_subcategory": market.get("bsr_subcategory", ""),
                    "rating_value": market.get("rating_value"),
                    "review_count": market.get("review_count"),
                    "question_count": market.get("question_count"),
                    "availability": market.get("availability", ""),
                    "in_stock": market.get("in_stock"),
                    "delivery_message": market.get("delivery_message", ""),
                    "is_prime": market.get("is_prime"),
                    "bought_past_month": market.get("bought_past_month"),
                    "buying_options_count": market.get("buying_options_count"),
                    "buy_box_suppressed": market.get("buy_box_suppressed"),
                    "seller_type": market.get("seller_type", ""),
                    "fulfillment_type": market.get("fulfillment_type", ""),
                    "sold_by": market.get("sold_by", ""),
                    "ships_from": market.get("ships_from", ""),
                    "coupon_text": market.get("coupon_text", ""),
                    "discount_percent": market.get("discount_percent"),
                    "list_price_value": market.get("list_price_value"),
                    "list_price_currency": market.get("list_price_currency", ""),
                    "item_model_number": market.get("item_model_number", ""),
                    "date_first_available": market.get("date_first_available", ""),
                    "product_dimensions": market.get("product_dimensions", ""),
                    "item_weight": market.get("item_weight", ""),
                    "country_of_origin": market.get("country_of_origin", ""),
                    "manufacturer": market.get("manufacturer", ""),
                    "variation_count": market.get("variation_count"),
                    "selected_color": market.get("selected_color", ""),
                    "selected_size": market.get("selected_size", ""),
                    "sku": market.get("sku", ""),
                    "mpn": market.get("mpn", ""),
                    "gtin": market.get("gtin", ""),
                    "product_type": market.get("product_type", ""),
                    "price_valid_until": market.get("price_valid_until", ""),
                    "item_condition": market.get("item_condition", ""),
                    "badges": " | ".join(market.get("badges", [])) if isinstance(market.get("badges", []), list) else "",
                    "image_count": market.get("image_count"),
                    "video_count": market.get("video_count"),
                    "has_aplus": market.get("has_aplus", False),
                    "gtm_signal_score": score["score"],
                    "gtm_signal_score_pct": score["pct"],
                    "gtm_missing_fields": score["missing"],
                    "features_count": features_count,
                    "specs_count": specs_count,
                    "recent_alert_count_10": latest_alert_count(conn, product_id, 10),
                    "url": row["url"] or meta.get("url", ""),
                }
            )
    conn.close()
    print(f"OK:{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
