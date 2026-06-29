#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import requests


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PRODUCTS_CSV = PROJECT_ROOT / "data" / "products.csv"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "monitor.db"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "data" / "monitor_config.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

PRICE_RE = re.compile(r"([$€£¥])\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
PRICE_CODE_RE = re.compile(r"\b([A-Z]{3})\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)\b")
SCRIPT_LD_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
WHITESPACE_RE = re.compile(r"\s+")
ASIN_RE = re.compile(r"/(?:dp|gp/product|aw/d)/([A-Z0-9]{10})(?:[/?]|$)", re.IGNORECASE)

AMAZON_TITLE_PATTERNS = [
    re.compile(r"<span[^>]+id=[\"']productTitle[\"'][^>]*>(.*?)</span>", re.IGNORECASE | re.DOTALL),
]
AMAZON_PRICE_PATTERNS = [
    re.compile(r"<span[^>]+id=[\"']priceblock_ourprice[\"'][^>]*>(.*?)</span>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<span[^>]+id=[\"']priceblock_dealprice[\"'][^>]*>(.*?)</span>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<span[^>]+id=[\"']priceblock_saleprice[\"'][^>]*>(.*?)</span>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<span[^>]+class=[\"'][^\"']*a-offscreen[^\"']*[\"'][^>]*>(.*?)</span>", re.IGNORECASE | re.DOTALL),
]
AMAZON_BULLET_SECTION_RE = re.compile(
    r"<div[^>]+id=[\"']feature-bullets[\"'][^>]*>(.*?)</div>",
    re.IGNORECASE | re.DOTALL,
)
AMAZON_SPEC_TABLE_PATTERNS = [
    re.compile(r"<table[^>]+id=[\"']productDetails_techSpec_section_1[\"'][^>]*>(.*?)</table>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<table[^>]+id=[\"']productDetails_detailBullets_sections1[\"'][^>]*>(.*?)</table>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<table[^>]+id=[\"']productOverview_detailBullets_sections1[\"'][^>]*>(.*?)</table>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<table[^>]+id=[\"']productOverview_feature_div[\"'][^>]*>(.*?)</table>", re.IGNORECASE | re.DOTALL),
]
AMAZON_DETAIL_BULLETS_RE = re.compile(
    r"<div[^>]+id=[\"']detailBullets_feature_div[\"'][^>]*>(.*?)</div>",
    re.IGNORECASE | re.DOTALL,
)
GENERIC_TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table>", re.IGNORECASE | re.DOTALL)
TABLE_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
TABLE_CELL_RE = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.IGNORECASE | re.DOTALL)
LIST_ITEM_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)
META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)

GENERIC_SPEC_LABELS = {
    "size",
    "color",
    "material",
    "weight",
    "voltage",
    "wattage",
    "dimensions",
    "compatible",
    "capacity",
    "model",
    "sku",
}
GENERIC_NOISE_TOKENS = {
    "cookie",
    "privacy",
    "shipping policy",
    "return policy",
    "subscribe",
    "sign up",
    "javascript",
    "copyright",
    "login",
    "wishlist",
    "cart",
}
SECTION_HEADING_TOKENS = {
    "description",
    "descriptions",
    "specification",
    "specifications",
    "disclaimer",
}
SPEC_SECTION_MARKERS = [
    "PRODUCT SPECIFICATIONS",
    "Product Specifications",
    "Specifications",
]
SPEC_SECTION_END_MARKERS = [
    "Precautions and Contraindications",
    "See Precautions and Contraindications",
    "DISCLAIMER",
    "Add-ons",
    "What’s Included",
    "What's Included",
    "#shopify-section",
    "<style",
    "</style",
]
COMMON_SPEC_LABELS = [
    "Dimensions",
    "Dimension",
    "Size",
    "Weight",
    "Battery life",
    "Battery Life",
    "Battery",
    "Warranty",
    "Material",
    "Materials",
    "Voltage",
    "Wattage",
    "Power",
    "Input",
    "Output",
    "Speed",
    "Speeds",
    "Amplitude",
    "Stall force",
    "Stall Force",
    "Pressure levels",
    "Pressure Levels",
    "Compression levels",
    "Compression Levels",
    "Timer",
    "Runtime",
    "Operating time",
    "Charging time",
    "Charging Time",
    "Attachments",
    "Attachment",
    "Included",
    "Model",
    "SKU",
    "Compatibility",
    "Color",
]
DISCLAIMER_SKIP_PREFIXES = {
    "for household use only",
    "use only while seated",
    "keep away from children",
    "unplug after each use",
    "do not operate with damaged cords",
    "store in a cool, dry place",
}
AMAZON_PARENT_ASIN_RE = re.compile(r'"parentAsin"\s*:\s*"([A-Z0-9]{10})"', re.IGNORECASE)
AMAZON_RATING_TEXT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*out of\s*5", re.IGNORECASE)
AMAZON_REVIEW_COUNT_RE = re.compile(r"([0-9][0-9,]*)")
AMAZON_BADGE_PATTERNS = [
    "Amazon's Choice",
    "Best Seller",
    "Limited time deal",
    "Climate Pledge Friendly",
    "FSA/HSA eligible",
]
AMAZON_DELIVERY_IDS = [
    "deliveryBlockMessage",
    "mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE",
    "mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE",
]
AMAZON_BSR_RANK_RE = re.compile(r"#\s*([0-9][0-9,]*)\s+in\s+([^#(]+)", re.IGNORECASE)
AMAZON_QUESTION_COUNT_RE = re.compile(r"([0-9][0-9,]*)\s+(?:answered\s+)?questions", re.IGNORECASE)
AMAZON_BOUGHT_PAST_MONTH_RE = re.compile(r"([0-9][0-9,]*)\+?\s+bought in past month", re.IGNORECASE)
AMAZON_BUYING_OPTIONS_RE = re.compile(r"(?:new|used)\s*\(([0-9][0-9,]*)\)\s+from", re.IGNORECASE)


@dataclass
class Product:
    product_id: str
    brand: str
    product_line: str
    product_name: str
    url: str
    site_type: str
    watch_keywords: List[str]
    enabled: bool
    relationship: str = "competitor"
    category: str = ""
    channel: str = ""
    country_market: str = "US"
    url_type: str = "pdp"
    priority: str = "B"


@dataclass
class Snapshot:
    product_id: str
    captured_at: str
    url: str
    site_type: str
    success: bool
    http_status: int
    error_message: str
    title: str
    price_value: Optional[float]
    currency: str
    price_raw: str
    key_features: List[str]
    key_specs: Dict[str, str]
    market_signals: Dict[str, Any]
    page_hash: str


def default_monitor_config() -> Dict[str, Any]:
    return {
        "enabled_alert_types": [
            "PRICE_CHANGE",
            "CURRENCY_CHANGE",
            "TITLE_CHANGE",
            "TITLE_KEYWORD_CHANGE",
            "FEATURE_CHANGE",
            "SPEC_CHANGE",
            "MARKET_SIGNAL_CHANGE",
            "BLOCKED_PAGE",
        ],
        "price_change_threshold_pct": 0.1,
        "feature_change_min_items": 1,
        "spec_change_min_items": 1,
        "http_retry_count": 1,
        "http_retry_backoff_seconds": 1.0,
        "amazon_blocked_retry_count": 1,
        "http_max_html_bytes": 1500000,
        "max_feature_items": 12,
        "max_feature_items_in_alert": 6,
        "max_spec_items_in_alert": 8,
    }


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_monitor_config(path: Optional[Path]) -> Dict[str, Any]:
    cfg = default_monitor_config()
    if path is None or not path.exists():
        return cfg
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return cfg
    if not isinstance(loaded, dict):
        return cfg

    if isinstance(loaded.get("enabled_alert_types"), list):
        cfg["enabled_alert_types"] = [str(x).strip().upper() for x in loaded["enabled_alert_types"] if str(x).strip()]
    cfg["price_change_threshold_pct"] = _coerce_float(
        loaded.get("price_change_threshold_pct"),
        cfg["price_change_threshold_pct"],
    )
    cfg["feature_change_min_items"] = max(1, _coerce_int(loaded.get("feature_change_min_items"), 1))
    cfg["spec_change_min_items"] = max(1, _coerce_int(loaded.get("spec_change_min_items"), 1))
    cfg["http_retry_count"] = max(1, _coerce_int(loaded.get("http_retry_count"), 1))
    cfg["http_retry_backoff_seconds"] = max(0.0, _coerce_float(loaded.get("http_retry_backoff_seconds"), 1.0))
    cfg["amazon_blocked_retry_count"] = max(0, _coerce_int(loaded.get("amazon_blocked_retry_count"), 1))
    cfg["http_max_html_bytes"] = max(300000, _coerce_int(loaded.get("http_max_html_bytes"), 1500000))
    cfg["max_feature_items"] = max(4, _coerce_int(loaded.get("max_feature_items"), 12))
    cfg["max_feature_items_in_alert"] = max(1, _coerce_int(loaded.get("max_feature_items_in_alert"), 6))
    cfg["max_spec_items_in_alert"] = max(1, _coerce_int(loaded.get("max_spec_items_in_alert"), 8))
    return cfg


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_space(value: str) -> str:
    return WHITESPACE_RE.sub(" ", (value or "").strip())


def strip_tags(html_fragment: str) -> str:
    if not html_fragment:
        return ""
    text = COMMENT_RE.sub(" ", html_fragment)
    text = SCRIPT_STYLE_RE.sub(" ", text)
    text = TAG_RE.sub(" ", text)
    text = unescape(text)
    return normalize_space(text)


def parse_keywords(value: str) -> List[str]:
    raw = (value or "").replace(",", "|")
    parts = [normalize_space(p).lower() for p in raw.split("|")]
    return [p for p in parts if p]


def infer_site_type(url: str, site_type: str) -> str:
    normalized = (site_type or "").strip().lower()
    if normalized in {"amazon", "independent"}:
        return normalized
    host = (urlparse(url).hostname or "").lower()
    if "amazon." in host:
        return "amazon"
    return "independent"


def ensure_output_dir(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)


def read_products_csv(path: Path) -> List[Product]:
    if not path.exists():
        raise FileNotFoundError(f"Missing products csv: {path}")

    rows: List[Product] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = normalize_space(row.get("url", ""))
            if not url:
                continue
            enabled_raw = normalize_space(row.get("enabled", "1")).lower()
            enabled = enabled_raw not in {"0", "false", "no", "off"}
            product_id = normalize_space(row.get("product_id", ""))
            if not product_id:
                product_id = fallback_product_id(url)
            rows.append(
                Product(
                    product_id=product_id,
                    brand=normalize_space(row.get("brand", "")),
                    product_line=normalize_space(row.get("product_line", "")),
                    product_name=normalize_space(row.get("product_name", "")),
                    url=url,
                    site_type=infer_site_type(url, row.get("site_type", "")),
                    watch_keywords=parse_keywords(row.get("watch_keywords", "")),
                    enabled=enabled,
                    relationship=normalize_space(row.get("relationship", "")) or "competitor",
                    category=normalize_space(row.get("category", "")),
                    channel=normalize_space(row.get("channel", "")),
                    country_market=normalize_space(row.get("country_market", "")) or "US",
                    url_type=normalize_space(row.get("url_type", "")) or "pdp",
                    priority=normalize_space(row.get("priority", "")) or "B",
                )
            )
    return rows


def fallback_product_id(url: str) -> str:
    host = (urlparse(url).hostname or "unknown").replace(".", "-")
    asin = extract_asin_from_url(url)
    if asin:
        return f"amz-{asin.lower()}"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"{host}-{digest}"


def extract_asin_from_url(url: str) -> str:
    match = ASIN_RE.search(url or "")
    if not match:
        return ""
    return match.group(1).upper()


def db_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_table_column(conn: sqlite3.Connection, table_name: str, column_name: str, column_ddl: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    existing = {str(row["name"]) for row in rows}
    if column_name in existing:
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_ddl}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            product_id TEXT PRIMARY KEY,
            brand TEXT NOT NULL DEFAULT '',
            product_line TEXT NOT NULL DEFAULT '',
            product_name TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL,
            site_type TEXT NOT NULL DEFAULT '',
            watch_keywords TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            url TEXT NOT NULL,
            site_type TEXT NOT NULL,
            success INTEGER NOT NULL,
            http_status INTEGER NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            price_value REAL,
            currency TEXT NOT NULL DEFAULT '',
            price_raw TEXT NOT NULL DEFAULT '',
            key_features_json TEXT NOT NULL DEFAULT '[]',
            key_specs_json TEXT NOT NULL DEFAULT '{}',
            market_signals_json TEXT NOT NULL DEFAULT '{}',
            page_hash TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(product_id) REFERENCES products(product_id)
        );

        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            product_id TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            previous_value TEXT NOT NULL DEFAULT '',
            current_value TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(snapshot_id) REFERENCES snapshots(id)
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_product ON snapshots(product_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_alerts_product ON alerts(product_id, id DESC);
        """
    )

    ensure_table_column(conn, "snapshots", "market_signals_json", "TEXT NOT NULL DEFAULT '{}'")
    ensure_table_column(conn, "products", "relationship", "TEXT NOT NULL DEFAULT 'competitor'")
    ensure_table_column(conn, "products", "category", "TEXT NOT NULL DEFAULT ''")
    ensure_table_column(conn, "products", "channel", "TEXT NOT NULL DEFAULT ''")
    ensure_table_column(conn, "products", "country_market", "TEXT NOT NULL DEFAULT 'US'")
    ensure_table_column(conn, "products", "url_type", "TEXT NOT NULL DEFAULT 'pdp'")
    ensure_table_column(conn, "products", "priority", "TEXT NOT NULL DEFAULT 'B'")
    conn.commit()


def upsert_products(conn: sqlite3.Connection, products: Sequence[Product]) -> None:
    now = now_utc_iso()
    for p in products:
        conn.execute(
            """
            INSERT INTO products (
                product_id, brand, product_line, product_name, url, site_type, watch_keywords, enabled,
                relationship, category, channel, country_market, url_type, priority, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(product_id) DO UPDATE SET
                brand=excluded.brand,
                product_line=excluded.product_line,
                product_name=excluded.product_name,
                url=excluded.url,
                site_type=excluded.site_type,
                watch_keywords=excluded.watch_keywords,
                enabled=excluded.enabled,
                relationship=excluded.relationship,
                category=excluded.category,
                channel=excluded.channel,
                country_market=excluded.country_market,
                url_type=excluded.url_type,
                priority=excluded.priority,
                updated_at=excluded.updated_at
            """,
            (
                p.product_id,
                p.brand,
                p.product_line,
                p.product_name,
                p.url,
                p.site_type,
                "|".join(p.watch_keywords),
                1 if p.enabled else 0,
                p.relationship,
                p.category,
                p.channel,
                p.country_market,
                p.url_type,
                p.priority,
                now,
            ),
        )
    conn.commit()


def fetch_html(
    url: str,
    site_type: str = "",
    timeout_seconds: int = 25,
    retry_count: int = 1,
    retry_backoff_seconds: float = 1.0,
    max_html_bytes: int = 1500000,
) -> Tuple[str, int, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if site_type == "amazon":
        headers["Cookie"] = "i18n-prefs=USD; lc-main=en_US;"
    attempts = max(1, int(retry_count))
    last_exc: Optional[Exception] = None
    for idx in range(attempts):
        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=(timeout_seconds, timeout_seconds),
                allow_redirects=True,
                stream=True,
            )
            raw_chunks: List[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                raw_chunks.append(chunk)
                if total >= max_html_bytes:
                    break

            raw = b"".join(raw_chunks)
            encoding = resp.encoding or "utf-8"
            html = raw.decode(encoding, errors="ignore")
            resp.close()
            return resp.url, resp.status_code, html
        except requests.RequestException as exc:
            last_exc = exc
            if idx < attempts - 1:
                time.sleep(max(0.0, retry_backoff_seconds) * (idx + 1))
    if last_exc is not None:
        raise last_exc
    raise requests.RequestException("UNKNOWN_REQUEST_ERROR")


def extract_attr_value(tag_html: str, attr: str) -> str:
    pattern = rf"{re.escape(attr)}\s*=\s*([\"'])(.*?)\1"
    match = re.search(pattern, tag_html, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return normalize_space(unescape(match.group(2)))


def extract_meta_content(html: str, key: str) -> str:
    source = html[:250000]
    head_end = source.lower().find("</head>")
    if head_end != -1:
        source = source[: head_end + len("</head>")]

    key_lower = normalize_space(key).lower()
    for meta_match in re.finditer(r"<meta\b[^>]*>", source, re.IGNORECASE):
        tag = meta_match.group(0)
        name = extract_attr_value(tag, "name").lower()
        prop = extract_attr_value(tag, "property").lower()
        if name != key_lower and prop != key_lower:
            continue
        content = extract_attr_value(tag, "content")
        if content:
            return content
    return ""


def extract_json_ld_products(html: str) -> List[Dict[str, Any]]:
    products: List[Dict[str, Any]] = []
    for match in SCRIPT_LD_RE.finditer(html):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        walk_product_nodes(obj, products)
    return products


def walk_product_nodes(node: Any, out: List[Dict[str, Any]]) -> None:
    if isinstance(node, dict):
        node_type = node.get("@type") or node.get("type")
        if isinstance(node_type, str) and "product" in node_type.lower():
            out.append(node)
        elif isinstance(node_type, list):
            node_type_text = " ".join([str(x).lower() for x in node_type])
            if "product" in node_type_text:
                out.append(node)
        for value in node.values():
            walk_product_nodes(value, out)
    elif isinstance(node, list):
        for item in node:
            walk_product_nodes(item, out)


def parse_price_text(value: str) -> Tuple[Optional[float], str, str]:
    text = normalize_space(strip_tags(value))
    if not text:
        return None, "", ""
    match = PRICE_RE.search(text)
    if match:
        sign = match.group(1)
        number_raw = match.group(2).replace(",", "")
        try:
            amount = float(number_raw)
        except ValueError:
            amount = None
        if amount is not None:
            currency = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}.get(sign, "")
            return amount, currency, f"{sign}{number_raw}"

    # Fallback for prices like HKD626.81 / USD 109.99
    code_match = PRICE_CODE_RE.search(text)
    if code_match:
        code = code_match.group(1).upper()
        number_raw = code_match.group(2).replace(",", "")
        try:
            amount = float(number_raw)
            return amount, code, f"{code}{number_raw}"
        except ValueError:
            pass

    return None, "", ""


def parse_offer_price(offers: Any) -> Tuple[Optional[float], str, str]:
    offer = offers
    if isinstance(offers, list) and offers:
        offer = offers[0]
    if not isinstance(offer, dict):
        return None, "", ""
    price_raw = normalize_space(str(offer.get("price", "") or ""))
    currency = normalize_space(str(offer.get("priceCurrency", "") or ""))
    if price_raw:
        try:
            return float(price_raw.replace(",", "")), currency, price_raw
        except ValueError:
            pass
    return None, currency, price_raw


def offer_url_from_product(product_json: Dict[str, Any]) -> str:
    offers = product_json.get("offers")
    offer = offers[0] if isinstance(offers, list) and offers else offers
    if not isinstance(offer, dict):
        return ""
    return normalize_space(str(offer.get("url", "") or ""))


def same_host_and_path(url_a: str, url_b: str) -> bool:
    try:
        a = urlparse(url_a)
        b = urlparse(url_b)
        if not a.hostname or not b.hostname:
            return False
        if a.hostname.lower() != b.hostname.lower():
            return False
        return normalize_space(a.path) == normalize_space(b.path)
    except Exception:
        return False


def score_product_json(product_json: Dict[str, Any], page_url: str) -> int:
    score = 0
    name = normalize_space(str(product_json.get("name", "") or ""))
    if name:
        score += 10

    desc_len = len(str(product_json.get("description", "") or ""))
    if 40 <= desc_len <= 450:
        score += 20
    elif desc_len > 1400:
        score -= 5

    amount, _, _ = parse_offer_price(product_json.get("offers"))
    if amount is not None:
        score += 30

    offer_url = offer_url_from_product(product_json)
    if offer_url:
        score += 15
        if same_host_and_path(offer_url, page_url):
            score += 80
    return score


def reorder_product_jsons_by_relevance(product_jsons: Sequence[Dict[str, Any]], page_url: str) -> List[Dict[str, Any]]:
    scored = [(score_product_json(p, page_url), idx, p) for idx, p in enumerate(product_jsons)]
    scored.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return [item[2] for item in scored]


def extract_first_title(html: str, site_type: str, product_jsons: Sequence[Dict[str, Any]]) -> str:
    if site_type == "amazon":
        for pattern in AMAZON_TITLE_PATTERNS:
            m = pattern.search(html)
            if m:
                return normalize_space(strip_tags(m.group(1)))

    meta_title = extract_meta_content(html, "og:title")
    if meta_title:
        return meta_title

    for product in product_jsons:
        name = normalize_space(str(product.get("name", "") or ""))
        if name:
            return name

    title_match = TITLE_TAG_RE.search(html)
    if title_match:
        return normalize_space(strip_tags(title_match.group(1)))
    return ""


def extract_price(html: str, site_type: str, product_jsons: Sequence[Dict[str, Any]]) -> Tuple[Optional[float], str, str]:
    if site_type == "amazon":
        for pattern in AMAZON_PRICE_PATTERNS:
            m = pattern.search(html)
            if not m:
                continue
            amount, currency, raw = parse_price_text(m.group(1))
            if amount is not None:
                return amount, currency, raw

    for product in product_jsons:
        amount, currency, raw = parse_offer_price(product.get("offers"))
        if amount is not None:
            return amount, currency, raw

    amount_meta = extract_meta_content(html, "product:price:amount")
    currency_meta = extract_meta_content(html, "product:price:currency")
    if amount_meta:
        try:
            return float(amount_meta.replace(",", "")), currency_meta, amount_meta
        except ValueError:
            pass

    # Generic fallback: first visible currency-style number.
    amount, currency, raw = parse_price_text(html[:120000])
    if amount is None:
        return None, "", ""
    return amount, currency, raw


def looks_like_noise(text: str) -> bool:
    normalized = normalize_space(text).lower()
    if not normalized:
        return True
    if len(normalized) < 8:
        return True
    return any(token in normalized for token in GENERIC_NOISE_TOKENS)


def normalize_rich_text(value: str) -> str:
    text = unescape(str(value or ""))
    text = unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u2028", "\n").replace("\xa0", " ")
    text = text.replace("•", "\n")
    return text


def is_section_heading(text: str) -> bool:
    lowered = normalize_space(text).lower().rstrip(":")
    return lowered in SECTION_HEADING_TOKENS


def looks_like_spec_key(text: str) -> bool:
    normalized = normalize_space(text)
    if not normalized:
        return False
    if len(normalized) > 40:
        return False
    if "." in normalized:
        return False
    lowered = normalized.lower()
    if any(lowered.startswith(prefix) for prefix in DISCLAIMER_SKIP_PREFIXES):
        return False
    if is_section_heading(lowered):
        return False
    return True


def looks_like_spec_value(text: str) -> bool:
    normalized = normalize_space(text)
    if not normalized:
        return False
    if len(normalized) > 180:
        return False
    lowered = normalized.lower()
    if any(token in lowered for token in ["rgba(", "font-", "line-height", "letter-spacing", "display:", "{", "}"]):
        return False
    return True


def dedupe_keep_order(items: Sequence[str], limit: int) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        normalized = normalize_space(item)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
        if len(out) >= limit:
            break
    return out


def normalize_item_for_compare(value: str) -> str:
    lowered = normalize_space(value).lower()
    lowered = re.sub(r"&", " and ", lowered)
    lowered = re.sub(r"[^a-z0-9 ]+", " ", lowered)
    return normalize_space(lowered)


def split_key_value_text(text: str) -> Tuple[str, str]:
    normalized = normalize_space(text)
    if not normalized:
        return "", ""
    for sep in [":", "："]:
        if sep not in normalized:
            continue
        left, right = [normalize_space(part) for part in normalized.split(sep, 1)]
        left = left.rstrip(":：")
        if left and right:
            return left, right
    return "", ""


def get_spec_value_by_keywords(specs: Dict[str, str], keywords: Sequence[str]) -> str:
    if not specs:
        return ""
    lowered_keys = [(k, normalize_space(k).lower()) for k in specs.keys()]
    for kw in keywords:
        kw_l = normalize_space(kw).lower()
        if not kw_l:
            continue
        for key, key_l in lowered_keys:
            if kw_l in key_l:
                return normalize_space(specs.get(key, ""))
    return ""


def extract_feature_lines_from_description(description_raw: str) -> List[str]:
    if not description_raw:
        return []
    raw = normalize_rich_text(description_raw)
    upper = raw.upper()

    cut_pos = len(raw)
    for marker in ["SPECIFICATIONS", "DISCLAIMER"]:
        idx = upper.find(marker)
        if idx != -1:
            cut_pos = min(cut_pos, idx)
    main_block = raw[:cut_pos]

    candidates: List[str] = []
    for line in main_block.splitlines():
        cleaned = normalize_space(line)
        if not cleaned:
            continue
        lowered = cleaned.lower().rstrip(":")
        if is_section_heading(lowered):
            continue
        if any(lowered.startswith(prefix) for prefix in DISCLAIMER_SKIP_PREFIXES):
            continue

        pieces = [cleaned]
        if ". " in cleaned or ";" in cleaned or len(cleaned) > 170:
            pieces = [normalize_space(p) for p in re.split(r"[.;]+", cleaned) if normalize_space(p)]

        for piece in pieces:
            lowered_piece = piece.lower().rstrip(":")
            if is_section_heading(lowered_piece):
                continue
            if any(lowered_piece.startswith(prefix) for prefix in DISCLAIMER_SKIP_PREFIXES):
                continue
            if len(piece) < 12:
                continue
            if looks_like_noise(piece):
                continue
            candidates.append(piece)
    return candidates


def extract_feature_bullets(
    html: str,
    site_type: str,
    product_jsons: Sequence[Dict[str, Any]],
    max_items: int = 12,
) -> List[str]:
    candidates: List[str] = []

    if site_type == "amazon":
        section_match = AMAZON_BULLET_SECTION_RE.search(html)
        if section_match:
            section = section_match.group(1)
            for li in LIST_ITEM_RE.findall(section):
                text = strip_tags(li)
                if not looks_like_noise(text):
                    candidates.append(text)

    for product in product_jsons:
        description_raw = str(product.get("description", "") or "")
        if description_raw:
            candidates.extend(extract_feature_lines_from_description(description_raw))

        additional = product.get("additionalProperty")
        if isinstance(additional, list):
            for item in additional:
                if not isinstance(item, dict):
                    continue
                name = normalize_space(str(item.get("name", "") or ""))
                value = normalize_space(str(item.get("value", "") or ""))
                if name and value:
                    combined = f"{name}: {value}"
                    if not looks_like_noise(combined):
                        candidates.append(combined)

    if not candidates:
        # Fallback for independent pages: first meaningful list items.
        for li in LIST_ITEM_RE.findall(html[:200000]):
            text = strip_tags(li)
            if not looks_like_noise(text):
                candidates.append(text)
            if len(candidates) >= 20:
                break

    return dedupe_keep_order(candidates, limit=max(4, max_items))


def extract_specs_from_description(description_raw: str) -> Dict[str, str]:
    specs: Dict[str, str] = {}
    if not description_raw:
        return specs

    raw = normalize_rich_text(description_raw)
    upper = raw.upper()
    start = upper.find("SPECIFICATIONS")
    if start == -1:
        return specs

    spec_block = raw[start + len("SPECIFICATIONS") :]
    end = spec_block.upper().find("DISCLAIMER")
    if end != -1:
        spec_block = spec_block[:end]

    lines = [normalize_space(line) for line in spec_block.splitlines() if normalize_space(line)]

    # Pattern A: explicit "key: value" lines.
    i = 0
    while i < len(lines):
        line = lines[i]
        lowered = line.lower().rstrip(":")
        if is_section_heading(lowered):
            i += 1
            continue

        left, right = split_key_value_text(line)
        if left and right and looks_like_spec_key(left) and looks_like_spec_value(right):
            specs.setdefault(left, right)
            i += 1
            continue

        if looks_like_spec_key(line) and i + 1 < len(lines):
            nxt = lines[i + 1]
            if looks_like_spec_value(nxt):
                specs.setdefault(line, nxt)
                i += 2
                continue
        i += 1

    # Pattern B: strict alternating key/value lines (common on Shopify PDP schema).
    for idx in range(0, len(lines) - 1, 2):
        key = lines[idx]
        value = lines[idx + 1]
        if is_section_heading(key) or is_section_heading(value):
            continue
        if not looks_like_spec_key(key):
            continue
        if not looks_like_spec_value(value):
            continue
        if key == value:
            continue
        specs.setdefault(key, value)

    return specs


def parse_inline_key_value_specs(text: str, limit: int = 20) -> Dict[str, str]:
    specs: Dict[str, str] = {}
    source = normalize_space(text)
    if not source:
        return specs

    labels = sorted({normalize_space(label) for label in COMMON_SPEC_LABELS if normalize_space(label)}, key=len, reverse=True)
    label_pattern = "|".join(re.escape(label) for label in labels)
    if not label_pattern:
        return specs

    matches = list(re.finditer(rf"\b({label_pattern})\s*[:：]", source, re.IGNORECASE))
    for idx, match in enumerate(matches):
        key = normalize_space(match.group(1)).rstrip(":：")
        value_start = match.end()
        value_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(source)
        value = normalize_space(source[value_start:value_end])
        value = re.sub(r"\s*(?:#shopify-section|<style|</style).*$", "", value, flags=re.IGNORECASE)
        if not key or not value:
            continue
        if len(value) > 260:
            value = normalize_space(value[:260]).rstrip(" ,;")
        if looks_like_spec_key(key) and looks_like_spec_value(value):
            specs.setdefault(key, value)
        if len(specs) >= limit:
            break
    return specs


def extract_specs_from_product_spec_sections(html: str, limit: int = 20) -> Dict[str, str]:
    specs: Dict[str, str] = {}
    source = html or ""
    lower = source.lower()
    starts: List[int] = []
    for marker in SPEC_SECTION_MARKERS:
        start = lower.find(marker.lower())
        while start != -1:
            starts.append(start)
            start = lower.find(marker.lower(), start + len(marker))

    for start in sorted(set(starts)):
        chunk = source[start : start + 14000]
        nearest_end = len(chunk)
        chunk_lower = chunk.lower()
        for marker in SPEC_SECTION_END_MARKERS:
            idx = chunk_lower.find(marker.lower(), len("PRODUCT SPECIFICATIONS"))
            if idx != -1:
                nearest_end = min(nearest_end, idx)
        chunk = chunk[:nearest_end]
        text = strip_tags(chunk)
        text = re.sub(r"(?i)^product specifications\s*", "", text)
        text = normalize_space(text)
        parsed = parse_inline_key_value_specs(text, limit=limit - len(specs))
        for key, value in parsed.items():
            specs.setdefault(key, value)
            if len(specs) >= limit:
                return specs
    return specs


def extract_specs_from_table_fragment(table_html: str, limit: int = 20) -> Dict[str, str]:
    specs: Dict[str, str] = {}
    for row in TABLE_ROW_RE.findall(table_html or ""):
        cells = TABLE_CELL_RE.findall(row)
        if len(cells) < 2:
            continue
        key = normalize_space(strip_tags(cells[0]))
        val = normalize_space(strip_tags(cells[1]))
        if not key or not val:
            continue
        if len(key) > 90 or len(val) > 220:
            continue
        specs.setdefault(key, val)
        if len(specs) >= limit:
            break
    return specs


def extract_amazon_detail_bullet_specs(html: str, limit: int = 20) -> Dict[str, str]:
    specs: Dict[str, str] = {}
    wrapper_ids = [
        "detailBullets_feature_div",
        "detailBulletsWrapper_feature_div",
        "prodDetails",
    ]
    for wrapper_id in wrapper_ids:
        section = extract_element_by_id(html, wrapper_id, max_scan=900000)
        if not section:
            continue
        for li in LIST_ITEM_RE.findall(section):
            text = normalize_space(strip_tags(li).replace("\u200e", ""))
            if not text:
                continue
            key, value = split_key_value_text(text)
            if not key or not value:
                spans = re.findall(r"<span[^>]*>(.*?)</span>", li, re.IGNORECASE | re.DOTALL)
                if len(spans) >= 2:
                    key = normalize_space(strip_tags(spans[0])).rstrip(":：")
                    value = normalize_space(strip_tags(" ".join(spans[1:])))
            if not key or not value:
                continue
            if len(key) > 120 or len(value) > 280:
                continue
            specs.setdefault(key, value)
            if len(specs) >= limit:
                return specs
    return specs


def extract_amazon_specs(html: str, limit: int = 20) -> Dict[str, str]:
    specs: Dict[str, str] = {}
    for pattern in AMAZON_SPEC_TABLE_PATTERNS:
        match = pattern.search(html)
        if not match:
            continue
        parsed = extract_specs_from_table_fragment(match.group(1), limit=limit)
        for key, value in parsed.items():
            specs.setdefault(key, value)
            if len(specs) >= limit:
                return specs

    detail_specs = extract_amazon_detail_bullet_specs(html, limit=limit)
    for key, value in detail_specs.items():
        specs.setdefault(key, value)
        if len(specs) >= limit:
            return specs

    if specs:
        return specs

    detail_match = AMAZON_DETAIL_BULLETS_RE.search(html)
    if detail_match:
        for li in LIST_ITEM_RE.findall(detail_match.group(1)):
            text = normalize_space(strip_tags(li))
            left, right = split_key_value_text(text)
            if not left or not right:
                continue
            if len(left) > 90 or len(right) > 220:
                continue
            specs.setdefault(left, right)
            if len(specs) >= limit:
                break
    return specs


def extract_specs(html: str, product_jsons: Sequence[Dict[str, Any]], site_type: str = "independent") -> Dict[str, str]:
    specs: Dict[str, str] = {}

    if site_type == "amazon":
        specs = extract_amazon_specs(html, limit=20)
        for product in product_jsons:
            additional = product.get("additionalProperty")
            if not isinstance(additional, list):
                continue
            for item in additional:
                if not isinstance(item, dict):
                    continue
                key = normalize_space(str(item.get("name", "") or ""))
                val = normalize_space(str(item.get("value", "") or ""))
                if not key or not val:
                    continue
                specs.setdefault(key, val)
                if len(specs) >= 20:
                    return specs
        # For Amazon pages, avoid noisy generic fallback parsing when structured specs are absent.
        return specs

    for table in GENERIC_TABLE_RE.findall(html[:180000]):
        parsed = extract_specs_from_table_fragment(table, limit=20 - len(specs))
        for key, value in parsed.items():
            specs.setdefault(key, value)
            if len(specs) >= 20:
                return specs

    for product in product_jsons:
        additional = product.get("additionalProperty")
        if not isinstance(additional, list):
            continue
        for item in additional:
            if not isinstance(item, dict):
                continue
            key = normalize_space(str(item.get("name", "") or ""))
            val = normalize_space(str(item.get("value", "") or ""))
            if not key or not val:
                continue
            specs.setdefault(key, val)
            if len(specs) >= 20:
                return specs

    for product in product_jsons:
        desc_specs = extract_specs_from_description(str(product.get("description", "") or ""))
        for key, value in desc_specs.items():
            specs.setdefault(key, value)
            if len(specs) >= 20:
                return specs

    section_specs = extract_specs_from_product_spec_sections(html, limit=20 - len(specs))
    for key, value in section_specs.items():
        specs.setdefault(key, value)
        if len(specs) >= 20:
            return specs

    if specs:
        return specs

    # Lightweight fallback: infer spec-like lines from list items.
    for li in LIST_ITEM_RE.findall(html[:650000]):
        text = normalize_space(strip_tags(li))
        left, right = split_key_value_text(text)
        if not left or not right:
            continue
        if left.lower() not in GENERIC_SPEC_LABELS and len(left) > 24:
            continue
        specs.setdefault(left, right)
        if len(specs) >= 12:
            break
    return specs


def parse_first_float(value: str) -> Optional[float]:
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", value or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def parse_first_int(value: str) -> Optional[int]:
    m = re.search(r"([0-9][0-9,]*)", value or "")
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def extract_element_by_id(html: str, element_id: str, max_scan: int = 350000) -> str:
    source = html[:max_scan]
    id_esc = re.escape(element_id)
    pattern = rf"<([a-zA-Z0-9]+)[^>]*\bid=[\"']{id_esc}[\"'][^>]*>(.*?)</\1>"
    m = re.search(pattern, source, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return m.group(2)


def extract_amazon_breadcrumbs(html: str) -> List[str]:
    section = extract_element_by_id(html, "wayfinding-breadcrumbs_feature_div", max_scan=280000)
    if not section:
        return []
    items: List[str] = []
    for li in LIST_ITEM_RE.findall(section):
        text = normalize_space(strip_tags(li))
        if text and text not in {"›", ">", "/"}:
            items.append(text)
    return dedupe_keep_order(items, limit=10)


def extract_amazon_rating_and_reviews(html: str) -> Tuple[Optional[float], Optional[int], str, str]:
    rating_text = ""
    review_text = ""

    rating_section = extract_element_by_id(html, "averageCustomerReviews", max_scan=550000)
    if rating_section:
        m = re.search(r"<span[^>]*class=[\"'][^\"']*a-icon-alt[^\"']*[\"'][^>]*>(.*?)</span>", rating_section, re.IGNORECASE | re.DOTALL)
        if m:
            rating_text = normalize_space(strip_tags(m.group(1)))

    if not rating_text:
        m = re.search(r"<i[^>]*data-hook=[\"']average-star-rating[\"'][^>]*>\s*<span[^>]*>(.*?)</span>", html[:650000], re.IGNORECASE | re.DOTALL)
        if m:
            rating_text = normalize_space(strip_tags(m.group(1)))

    reviews_section = extract_element_by_id(html, "acrCustomerReviewText", max_scan=650000)
    if reviews_section:
        review_text = normalize_space(strip_tags(reviews_section))

    if not review_text:
        m = re.search(r"id=[\"']acrCustomerReviewText[\"'][^>]*>(.*?)</", html[:650000], re.IGNORECASE | re.DOTALL)
        if m:
            review_text = normalize_space(strip_tags(m.group(1)))

    rating_value = parse_first_float(rating_text or "")
    review_count = parse_first_int(review_text or "")
    return rating_value, review_count, rating_text, review_text


def extract_amazon_availability(html: str) -> str:
    availability = extract_element_by_id(html, "availability", max_scan=650000)
    msg = normalize_space(strip_tags(availability))
    if msg:
        return msg
    if "currently unavailable" in html[:900000].lower():
        return "Currently unavailable"
    return ""


def extract_amazon_delivery_message(html: str) -> str:
    for element_id in AMAZON_DELIVERY_IDS:
        block = extract_element_by_id(html, element_id, max_scan=700000)
        text = normalize_space(strip_tags(block))
        if text:
            return text
    m = re.search(
        r"<[^>]*id=[\"'][^\"']*DELIVERY[^\"']*MESSAGE[^\"']*[\"'][^>]*>(.*?)</[^>]+>",
        html[:700000],
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        return normalize_space(strip_tags(m.group(1)))
    return ""


def extract_amazon_merchant_and_fulfillment(html: str) -> Dict[str, str]:
    result = {
        "ships_from": "",
        "sold_by": "",
        "seller_type": "",
        "fulfillment_type": "",
        "merchant_id": "",
        "merchant_info_text": "",
    }

    merchant_html = extract_element_by_id(html, "merchantInfoFeature_feature_div", max_scan=850000)
    if not merchant_html:
        merchant_html = extract_element_by_id(html, "merchant-info", max_scan=850000)

    merchant_text = normalize_space(strip_tags(merchant_html))
    result["merchant_info_text"] = merchant_text

    merchant_id_match = re.search(
        r"id=[\"']ftSelectMerchant[\"'][^>]*value=[\"']([^\"']+)[\"']",
        html[:900000],
        re.IGNORECASE,
    )
    if merchant_id_match:
        result["merchant_id"] = merchant_id_match.group(1).strip()

    ships_match = re.search(r"Ships from\s*([^.;]+)", merchant_text, re.IGNORECASE)
    sold_match = re.search(r"Sold by\s*([^.;]+)", merchant_text, re.IGNORECASE)
    if ships_match:
        result["ships_from"] = normalize_space(ships_match.group(1))
    if sold_match:
        result["sold_by"] = normalize_space(sold_match.group(1))

    sold_lower = result["sold_by"].lower()
    ships_lower = result["ships_from"].lower()
    if "amazon" in sold_lower:
        result["seller_type"] = "amazon"
    elif result["sold_by"]:
        result["seller_type"] = "third_party"

    if "amazon" in ships_lower and result["seller_type"] == "third_party":
        result["fulfillment_type"] = "fba"
    elif "amazon" in ships_lower and result["seller_type"] == "amazon":
        result["fulfillment_type"] = "amazon_retail"
    elif result["ships_from"]:
        result["fulfillment_type"] = "fbm"

    return result


def extract_amazon_badges(html: str) -> List[str]:
    source = html[:900000]
    badges: List[str] = []
    for phrase in AMAZON_BADGE_PATTERNS:
        if phrase.lower() in source.lower():
            badges.append(phrase)
    return dedupe_keep_order(badges, limit=8)


def extract_amazon_media_counts(html: str) -> Tuple[Optional[int], Optional[int]]:
    source = html[:1000000]

    image_count: Optional[int] = None
    alt_images_block = extract_element_by_id(html, "altImages", max_scan=1000000)
    if alt_images_block:
        thumbnails = re.findall(r"<li[^>]*>", alt_images_block, re.IGNORECASE)
        if thumbnails:
            image_count = len(thumbnails)

    if image_count is None:
        thumb_hits = len(re.findall(r"id=[\"']ivImage_[^\"']+[\"']", source, re.IGNORECASE))
        if thumb_hits > 0:
            image_count = thumb_hits

    video_hits = len(re.findall(r"ivVideo|videoThumbnail|video-block", source, re.IGNORECASE))
    video_count: Optional[int] = None
    if video_hits > 0:
        # Keywords may appear multiple times for same tile; coarse de-dup ratio.
        video_count = max(1, min(20, video_hits // 2))
    return image_count, video_count


def extract_amazon_coupon_text(html: str) -> str:
    for element_id in ["couponTextpctch", "couponText", "vpcButton"]:
        block = extract_element_by_id(html, element_id, max_scan=850000)
        text = normalize_space(strip_tags(block))
        if text and "coupon" in text.lower():
            return text
    m = re.search(r"(Save\s+[^\n<]{1,80}coupon|Apply\s+[^\n<]{1,80}coupon)", html[:850000], re.IGNORECASE)
    if m:
        return normalize_space(m.group(1))
    return ""


def extract_amazon_list_price(html: str) -> Tuple[Optional[float], str, str]:
    source = html[:900000]
    patterns = [
        r"<span[^>]*class=[\"'][^\"']*a-price a-text-price[^\"']*[\"'][^>]*>\s*<span[^>]*class=[\"']a-offscreen[\"'][^>]*>(.*?)</span>",
        r"<span[^>]*class=[\"'][^\"']*basisPrice[^\"']*[\"'][^>]*>(.*?)</span>",
    ]
    for pattern in patterns:
        m = re.search(pattern, source, re.IGNORECASE | re.DOTALL)
        if not m:
            continue
        amount, currency, raw = parse_price_text(m.group(1))
        if amount is not None:
            return amount, currency, raw
    return None, "", ""


def extract_amazon_discount_percent(html: str) -> Optional[float]:
    source = html[:950000]
    m = re.search(r"([0-9]{1,2})\s*%[^<]{0,20}(off|savings)", source, re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def extract_amazon_parent_asin(html: str) -> str:
    m = AMAZON_PARENT_ASIN_RE.search(html[:1000000])
    if not m:
        return ""
    return m.group(1).upper()


def extract_amazon_sales_rank(specs: Dict[str, str], html: str) -> Dict[str, Any]:
    rank_text = get_spec_value_by_keywords(specs, ["best sellers rank", "amazon best sellers rank"])
    if not rank_text:
        m = re.search(r"Best Sellers Rank\s*[:：]\s*([^<\n]+)", html[:1000000], re.IGNORECASE)
        if m:
            rank_text = normalize_space(strip_tags(m.group(1)))
    matches = list(AMAZON_BSR_RANK_RE.finditer(rank_text))
    if not matches:
        return {
            "best_sellers_rank_text": rank_text,
            "bsr_rank": None,
            "bsr_category": "",
            "bsr_subrank": None,
            "bsr_subcategory": "",
        }

    def _rank_int(group_text: str) -> Optional[int]:
        try:
            return int(group_text.replace(",", "").strip())
        except ValueError:
            return None

    main = matches[0]
    sub = matches[1] if len(matches) > 1 else None
    return {
        "best_sellers_rank_text": rank_text,
        "bsr_rank": _rank_int(main.group(1)),
        "bsr_category": normalize_space(main.group(2)),
        "bsr_subrank": _rank_int(sub.group(1)) if sub else None,
        "bsr_subcategory": normalize_space(sub.group(2)) if sub else "",
    }


def extract_amazon_question_count(html: str) -> Optional[int]:
    source = html[:900000]
    m = AMAZON_QUESTION_COUNT_RE.search(source)
    if not m:
        return None
    return parse_first_int(m.group(1))


def extract_amazon_prime_flag(html: str, delivery_message: str, badges: Sequence[str]) -> bool:
    source = html[:900000].lower()
    if "a-icon-prime" in source:
        return True
    if " prime " in normalize_space(delivery_message).lower():
        return True
    for badge in badges:
        if "prime" in normalize_space(badge).lower():
            return True
    return False


def extract_amazon_bought_past_month(html: str) -> Optional[int]:
    m = AMAZON_BOUGHT_PAST_MONTH_RE.search(html[:900000])
    if not m:
        return None
    return parse_first_int(m.group(1))


def extract_amazon_buying_options(html: str) -> Tuple[Optional[int], bool]:
    source = html[:950000]
    m = AMAZON_BUYING_OPTIONS_RE.search(source)
    count = parse_first_int(m.group(1)) if m else None
    lowered = source.lower()
    suppressed = ("see all buying options" in lowered) and ("add to cart" not in lowered)
    return count, suppressed


def extract_amazon_byline_brand(html: str) -> str:
    byline = extract_element_by_id(html, "bylineInfo", max_scan=650000)
    text = normalize_space(strip_tags(byline))
    if not text:
        return ""
    cleaned = re.sub(r"^(visit the|brand:)\s+", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+store$", "", cleaned, flags=re.IGNORECASE)
    return normalize_space(cleaned)


def extract_amazon_variation_count(html: str) -> Optional[int]:
    section = extract_element_by_id(html, "twister_feature_div", max_scan=1200000)
    if not section:
        return None
    hits = len(re.findall(r"data-defaultasin=|twisterSwatch|a-button-toggle", section, re.IGNORECASE))
    if hits <= 1:
        return None
    return min(120, hits)


def extract_amazon_selected_variants(html: str) -> Dict[str, str]:
    selected_color = ""
    selected_size = ""

    color_block = extract_element_by_id(html, "variation_color_name", max_scan=850000)
    if color_block:
        selected_color = normalize_space(strip_tags(color_block))
    size_block = extract_element_by_id(html, "variation_size_name", max_scan=850000)
    if size_block:
        selected_size = normalize_space(strip_tags(size_block))

    if selected_color.lower().startswith("color"):
        key, value = split_key_value_text(selected_color)
        if key and value:
            selected_color = value
    if selected_size.lower().startswith("size"):
        key, value = split_key_value_text(selected_size)
        if key and value:
            selected_size = value
    return {"selected_color": selected_color, "selected_size": selected_size}


def extract_amazon_market_signals(product: Product, html: str, current_url: str, key_specs: Dict[str, str]) -> Dict[str, Any]:
    asin = extract_asin_from_url(current_url or product.url) or product.product_id
    parent_asin = extract_amazon_parent_asin(html)
    rating_value, review_count, rating_text, review_text = extract_amazon_rating_and_reviews(html)
    availability = extract_amazon_availability(html)
    delivery_message = extract_amazon_delivery_message(html)
    merchant = extract_amazon_merchant_and_fulfillment(html)
    badges = extract_amazon_badges(html)
    breadcrumbs = extract_amazon_breadcrumbs(html)
    image_count, video_count = extract_amazon_media_counts(html)
    list_price_value, list_price_currency, list_price_raw = extract_amazon_list_price(html)
    coupon_text = extract_amazon_coupon_text(html)
    discount_percent = extract_amazon_discount_percent(html)
    sales_rank = extract_amazon_sales_rank(key_specs, html)
    question_count = extract_amazon_question_count(html)
    bought_past_month = extract_amazon_bought_past_month(html)
    buying_options_count, buy_box_suppressed = extract_amazon_buying_options(html)
    is_prime = extract_amazon_prime_flag(html, delivery_message, badges)
    byline_brand = extract_amazon_byline_brand(html)
    variation_count = extract_amazon_variation_count(html)
    selected_variants = extract_amazon_selected_variants(html)
    item_model_number = get_spec_value_by_keywords(key_specs, ["item model number", "model number"])
    date_first_available = get_spec_value_by_keywords(key_specs, ["date first available", "first available"])
    product_dimensions = get_spec_value_by_keywords(key_specs, ["product dimensions", "package dimensions"])
    item_weight = get_spec_value_by_keywords(key_specs, ["item weight", "weight"])
    country_of_origin = get_spec_value_by_keywords(key_specs, ["country of origin"])
    manufacturer = get_spec_value_by_keywords(key_specs, ["manufacturer"])
    brand_name = get_spec_value_by_keywords(key_specs, ["brand"])
    if not brand_name:
        brand_name = byline_brand

    return {
        "asin": asin.upper() if isinstance(asin, str) else "",
        "parent_asin": parent_asin,
        "category_path": " > ".join(breadcrumbs),
        "category_nodes": breadcrumbs,
        "rating_value": rating_value,
        "rating_text": rating_text,
        "review_count": review_count,
        "review_text": review_text,
        "availability": availability,
        "delivery_message": delivery_message,
        "ships_from": merchant.get("ships_from", ""),
        "sold_by": merchant.get("sold_by", ""),
        "seller_type": merchant.get("seller_type", ""),
        "fulfillment_type": merchant.get("fulfillment_type", ""),
        "merchant_id": merchant.get("merchant_id", ""),
        "merchant_info_text": merchant.get("merchant_info_text", ""),
        "badges": badges,
        "list_price_value": list_price_value,
        "list_price_currency": list_price_currency,
        "list_price_raw": list_price_raw,
        "coupon_text": coupon_text,
        "discount_percent": discount_percent,
        "image_count": image_count,
        "video_count": video_count,
        "has_aplus": ("id=\"aplus\"" in html.lower()) or ("id='aplus'" in html.lower()),
        "question_count": question_count,
        "bought_past_month": bought_past_month,
        "buying_options_count": buying_options_count,
        "buy_box_suppressed": buy_box_suppressed,
        "is_prime": is_prime,
        "brand_name": brand_name,
        "byline_brand": byline_brand,
        "variation_count": variation_count,
        "selected_color": selected_variants["selected_color"],
        "selected_size": selected_variants["selected_size"],
        "item_model_number": item_model_number,
        "date_first_available": date_first_available,
        "product_dimensions": product_dimensions,
        "item_weight": item_weight,
        "country_of_origin": country_of_origin,
        "manufacturer": manufacturer,
        "best_sellers_rank_text": sales_rank["best_sellers_rank_text"],
        "bsr_rank": sales_rank["bsr_rank"],
        "bsr_category": sales_rank["bsr_category"],
        "bsr_subrank": sales_rank["bsr_subrank"],
        "bsr_subcategory": sales_rank["bsr_subcategory"],
    }


def normalize_schema_availability(value: str) -> str:
    text = normalize_space(value)
    if not text:
        return ""
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text


def extract_independent_market_signals(
    product: Product,
    html: str,
    current_url: str,
    product_jsons: Sequence[Dict[str, Any]],
    key_specs: Dict[str, str],
) -> Dict[str, Any]:
    breadcrumbs: List[str] = []
    section = extract_element_by_id(html, "breadcrumbs", max_scan=200000)
    if section:
        for li in LIST_ITEM_RE.findall(section):
            text = normalize_space(strip_tags(li))
            if text:
                breadcrumbs.append(text)
    rating_value = parse_first_float(extract_meta_content(html, "og:rating"))
    review_count: Optional[int] = None
    availability = ""
    discount_percent: Optional[float] = None
    brand_name = ""
    sku = ""
    mpn = ""
    gtin = ""
    product_type = ""
    variation_count: Optional[int] = None
    in_stock: Optional[bool] = None
    price_valid_until = ""
    item_condition = ""

    product_json = product_jsons[0] if product_jsons else {}
    if isinstance(product_json, dict):
        aggregate = product_json.get("aggregateRating")
        if isinstance(aggregate, dict):
            rating_value = parse_first_float(str(aggregate.get("ratingValue", "") or "")) or rating_value
            review_count = parse_first_int(str(aggregate.get("reviewCount", "") or "")) or review_count
        brand = product_json.get("brand")
        if isinstance(brand, dict):
            brand_name = normalize_space(str(brand.get("name", "") or ""))
        elif isinstance(brand, str):
            brand_name = normalize_space(brand)
        sku = normalize_space(str(product_json.get("sku", "") or ""))
        mpn = normalize_space(str(product_json.get("mpn", "") or ""))
        gtin = normalize_space(
            str(
                product_json.get("gtin13")
                or product_json.get("gtin12")
                or product_json.get("gtin14")
                or product_json.get("gtin8")
                or ""
            )
        )
        product_type = normalize_space(str(product_json.get("category", "") or product_json.get("@type", "") or ""))

        offers = product_json.get("offers")
        offer = offers[0] if isinstance(offers, list) and offers else offers
        if isinstance(offers, list):
            variation_count = len(offers)
        if isinstance(offer, dict):
            availability = normalize_schema_availability(str(offer.get("availability", "") or ""))
            price_valid_until = normalize_space(str(offer.get("priceValidUntil", "") or ""))
            item_condition = normalize_schema_availability(str(offer.get("itemCondition", "") or ""))
            compare_at = parse_first_float(str(offer.get("highPrice", "") or offer.get("price", "") or ""))
            base = parse_first_float(str(offer.get("lowPrice", "") or offer.get("price", "") or ""))
            if compare_at is not None and base is not None and compare_at > base > 0:
                discount_percent = round(((compare_at - base) / compare_at) * 100.0, 2)
        if not sku and isinstance(offer, dict):
            sku = normalize_space(str(offer.get("sku", "") or ""))
    if availability:
        in_stock = "instock" in availability.lower()

    if not brand_name:
        brand_name = get_spec_value_by_keywords(key_specs, ["brand", "manufacturer"])

    return {
        "asin": "",
        "parent_asin": "",
        "category_path": " > ".join(dedupe_keep_order(breadcrumbs, limit=8)),
        "category_nodes": dedupe_keep_order(breadcrumbs, limit=8),
        "rating_value": rating_value,
        "rating_text": "",
        "review_count": review_count,
        "review_text": "",
        "availability": availability,
        "delivery_message": "",
        "ships_from": "",
        "sold_by": "",
        "seller_type": "",
        "fulfillment_type": "",
        "merchant_id": "",
        "merchant_info_text": "",
        "badges": [],
        "list_price_value": None,
        "list_price_currency": "",
        "list_price_raw": "",
        "coupon_text": "",
        "discount_percent": discount_percent,
        "image_count": None,
        "video_count": None,
        "has_aplus": False,
        "question_count": None,
        "bought_past_month": None,
        "buying_options_count": None,
        "buy_box_suppressed": False,
        "is_prime": False,
        "brand_name": brand_name,
        "byline_brand": "",
        "variation_count": variation_count,
        "selected_color": "",
        "selected_size": "",
        "item_model_number": get_spec_value_by_keywords(key_specs, ["model", "model number"]),
        "date_first_available": "",
        "product_dimensions": get_spec_value_by_keywords(key_specs, ["dimensions", "size"]),
        "item_weight": get_spec_value_by_keywords(key_specs, ["weight"]),
        "country_of_origin": get_spec_value_by_keywords(key_specs, ["country of origin"]),
        "manufacturer": get_spec_value_by_keywords(key_specs, ["manufacturer"]),
        "best_sellers_rank_text": "",
        "bsr_rank": None,
        "bsr_category": "",
        "bsr_subrank": None,
        "bsr_subcategory": "",
        "sku": sku,
        "mpn": mpn,
        "gtin": gtin,
        "product_type": product_type,
        "in_stock": in_stock,
        "price_valid_until": price_valid_until,
        "item_condition": item_condition,
    }


def extract_market_signals(
    product: Product,
    html: str,
    site_type: str,
    current_url: str,
    product_jsons: Sequence[Dict[str, Any]],
    key_specs: Dict[str, str],
) -> Dict[str, Any]:
    if site_type == "amazon":
        return extract_amazon_market_signals(product, html, current_url, key_specs)
    return extract_independent_market_signals(product, html, current_url, product_jsons, key_specs)


def is_probable_amazon_captcha_page(html: str) -> bool:
    source = (html or "")[:250000].lower()
    markers = [
        "enter the characters you see below",
        "type the characters you see in this image",
        "to discuss automated access to amazon data",
        "sorry, we just need to make sure you're not a robot",
        "automated access",
        "/errors/validatecaptcha",
    ]
    hits = sum(1 for marker in markers if marker in source)
    if hits >= 2:
        return True
    if "captcha" in source and "robot" in source:
        return True
    return False


def has_amazon_product_markers(html: str) -> bool:
    source = (html or "")[:300000].lower()
    markers = [
        'id="producttitle"',
        "id='producttitle'",
        'id="acrcustomerreviewtext"',
        "id='acrcustomerreviewtext'",
        'id="feature-bullets"',
        "id='feature-bullets'",
        'id="averagecustomerreviews"',
        "id='averagecustomerreviews'",
    ]
    hits = sum(1 for marker in markers if marker in source)
    return hits >= 2


def compute_gtm_signal_score(site_type: str, row: Dict[str, Any]) -> Dict[str, Any]:
    checks: List[Tuple[str, bool]] = [
        ("title", bool(normalize_space(str(row.get("title", ""))))),
        ("price", row.get("price_value") is not None),
        ("features", len(row.get("key_features", [])) > 0),
        ("specs", len(row.get("key_specs", {})) > 0),
    ]
    if site_type == "amazon":
        checks.extend(
            [
                ("asin", bool(normalize_space(str(row.get("asin", ""))))),
                ("category", bool(normalize_space(str(row.get("category_path", ""))))),
                ("brand", bool(normalize_space(str(row.get("brand_name", ""))))),
                ("rating", row.get("rating_value") is not None),
                ("reviews", row.get("review_count") is not None),
                ("availability", bool(normalize_space(str(row.get("availability", ""))))),
                ("seller", bool(normalize_space(str(row.get("sold_by", ""))))),
                ("delivery", bool(normalize_space(str(row.get("delivery_message", ""))))),
                ("bsr", row.get("bsr_rank") is not None),
                ("model", bool(normalize_space(str(row.get("item_model_number", ""))))),
            ]
        )
    else:
        checks.extend(
            [
                ("brand", bool(normalize_space(str(row.get("brand_name", ""))))),
                ("availability", bool(normalize_space(str(row.get("availability", ""))))),
                ("sku", bool(normalize_space(str(row.get("sku", ""))))),
            ]
        )

    total = len(checks)
    hit = sum(1 for _, ok in checks if ok)
    missing = [name for name, ok in checks if not ok]
    pct = round((hit / total) * 100.0, 1) if total > 0 else 0.0
    return {"score": f"{hit}/{total}", "score_pct": pct, "missing_fields": missing}


def parse_snapshot(
    product: Product,
    final_url: str,
    status_code: int,
    html: str,
    max_feature_items: int = 12,
) -> Snapshot:
    site_type = infer_site_type(final_url or product.url, product.site_type)
    page_hash = hashlib.sha1((html or "").encode("utf-8", errors="ignore")).hexdigest()
    json_source = html
    if site_type == "amazon":
        json_source = html[:900000]
    product_jsons = reorder_product_jsons_by_relevance(extract_json_ld_products(json_source), final_url or product.url)

    title = extract_first_title(html, site_type, product_jsons)
    price_value, currency, price_raw = extract_price(html, site_type, product_jsons)
    key_features = extract_feature_bullets(html, site_type, product_jsons, max_items=max_feature_items)
    key_specs = extract_specs(html, product_jsons, site_type=site_type)
    market_signals = extract_market_signals(
        product,
        html,
        site_type,
        final_url or product.url,
        product_jsons,
        key_specs,
    )

    return Snapshot(
        product_id=product.product_id,
        captured_at=now_utc_iso(),
        url=final_url or product.url,
        site_type=site_type,
        success=True,
        http_status=status_code,
        error_message="",
        title=title,
        price_value=price_value,
        currency=currency,
        price_raw=price_raw,
        key_features=key_features,
        key_specs=key_specs,
        market_signals=market_signals,
        page_hash=page_hash,
    )


def failed_snapshot(product: Product, message: str, status_code: int = 0) -> Snapshot:
    return Snapshot(
        product_id=product.product_id,
        captured_at=now_utc_iso(),
        url=product.url,
        site_type=product.site_type,
        success=False,
        http_status=status_code,
        error_message=normalize_space(message),
        title="",
        price_value=None,
        currency="",
        price_raw="",
        key_features=[],
        key_specs={},
        market_signals={},
        page_hash="",
    )


def get_latest_success_snapshot(conn: sqlite3.Connection, product_id: str) -> Optional[sqlite3.Row]:
    row = conn.execute(
        """
        SELECT *
        FROM snapshots
        WHERE product_id = ? AND success = 1
        ORDER BY id DESC
        LIMIT 1
        """,
        (product_id,),
    ).fetchone()
    return row


def insert_snapshot(conn: sqlite3.Connection, snapshot: Snapshot) -> int:
    cursor = conn.execute(
        """
        INSERT INTO snapshots (
            product_id, captured_at, url, site_type, success, http_status, error_message,
            title, price_value, currency, price_raw, key_features_json, key_specs_json, market_signals_json, page_hash
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot.product_id,
            snapshot.captured_at,
            snapshot.url,
            snapshot.site_type,
            1 if snapshot.success else 0,
            snapshot.http_status,
            snapshot.error_message,
            snapshot.title,
            snapshot.price_value,
            snapshot.currency,
            snapshot.price_raw,
            json.dumps(snapshot.key_features, ensure_ascii=False),
            json.dumps(snapshot.key_specs, ensure_ascii=False),
            json.dumps(snapshot.market_signals, ensure_ascii=False),
            snapshot.page_hash,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def row_price_value(row: sqlite3.Row) -> Optional[float]:
    value = row["price_value"]
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_title_for_compare(title: str) -> str:
    lowered = normalize_space(title).lower()
    lowered = re.sub(r"[^a-z0-9 ]+", " ", lowered)
    return normalize_space(lowered)


def row_json_list(row: sqlite3.Row, key: str) -> List[str]:
    raw = row[key] if key in row.keys() else "[]"
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [normalize_space(str(v)) for v in value if normalize_space(str(v))]


def row_json_dict(row: sqlite3.Row, key: str) -> Dict[str, str]:
    raw = row[key] if key in row.keys() else "{}"
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in value.items():
        nk = normalize_space(str(k))
        nv = normalize_space(str(v))
        if nk and nv:
            out[nk] = nv
    return out


def row_json_object(row: sqlite3.Row, key: str) -> Dict[str, Any]:
    raw = row[key] if key in row.keys() else "{}"
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    if isinstance(value, dict):
        return value
    return {}


def normalize_scalar_for_compare(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "|".join([normalize_space(str(x)).lower() for x in value if normalize_space(str(x))])
    return normalize_space(str(value)).lower()


def diff_feature_lists(previous: Sequence[str], current: Sequence[str]) -> Dict[str, List[str]]:
    prev_map: Dict[str, str] = {}
    for item in previous:
        prev_map.setdefault(normalize_item_for_compare(item), normalize_space(item))
    curr_map: Dict[str, str] = {}
    for item in current:
        curr_map.setdefault(normalize_item_for_compare(item), normalize_space(item))

    prev_keys = {k for k in prev_map.keys() if k}
    curr_keys = {k for k in curr_map.keys() if k}

    added_keys = sorted(curr_keys - prev_keys)
    removed_keys = sorted(prev_keys - curr_keys)

    return {
        "added": [curr_map[k] for k in added_keys],
        "removed": [prev_map[k] for k in removed_keys],
    }


def diff_specs(previous: Dict[str, str], current: Dict[str, str]) -> Dict[str, Any]:
    prev_keys = set(previous.keys())
    curr_keys = set(current.keys())
    added = sorted(curr_keys - prev_keys)
    removed = sorted(prev_keys - curr_keys)
    changed: List[Dict[str, str]] = []
    for key in sorted(prev_keys & curr_keys):
        if normalize_item_for_compare(previous[key]) != normalize_item_for_compare(current[key]):
            changed.append({"key": key, "previous": previous[key], "current": current[key]})
    return {
        "added": [{"key": k, "value": current[k]} for k in added],
        "removed": [{"key": k, "value": previous[k]} for k in removed],
        "changed": changed,
    }


def detect_changes(
    product: Product,
    previous: sqlite3.Row,
    current: Snapshot,
    price_change_threshold_pct: float,
    feature_change_min_items: int,
    spec_change_min_items: int,
    enabled_alert_types: Sequence[str],
    max_feature_items_in_alert: int,
    max_spec_items_in_alert: int,
    spec_watch_keywords: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    alerts: List[Dict[str, Any]] = []
    enabled = {normalize_space(str(a)).upper() for a in enabled_alert_types}

    prev_price = row_price_value(previous)
    curr_price = current.price_value
    prev_currency = normalize_space(str(previous["currency"] or "")).upper()
    curr_currency = normalize_space(current.currency).upper()
    currency_changed = bool(prev_currency and curr_currency and prev_currency != curr_currency)

    if "CURRENCY_CHANGE" in enabled and currency_changed:
        alerts.append(
            {
                "alert_type": "CURRENCY_CHANGE",
                "previous_value": prev_currency,
                "current_value": curr_currency,
                "details": {},
            }
        )

    if (
        "PRICE_CHANGE" in enabled
        and not currency_changed
        and prev_price is not None
        and curr_price is not None
        and prev_price > 0
    ):
        delta = curr_price - prev_price
        delta_pct = (delta / prev_price) * 100.0
        if abs(delta_pct) >= price_change_threshold_pct:
            alerts.append(
                {
                    "alert_type": "PRICE_CHANGE",
                    "previous_value": f"{prev_price:.2f}",
                    "current_value": f"{curr_price:.2f}",
                    "details": {"delta": round(delta, 4), "delta_pct": round(delta_pct, 2)},
                }
            )

    prev_title = normalize_title_for_compare(previous["title"] or "")
    curr_title = normalize_title_for_compare(current.title)
    if "TITLE_CHANGE" in enabled and prev_title and curr_title and prev_title != curr_title:
        alerts.append(
            {
                "alert_type": "TITLE_CHANGE",
                "previous_value": previous["title"] or "",
                "current_value": current.title,
                "details": {},
            }
        )

    if "TITLE_KEYWORD_CHANGE" in enabled and product.watch_keywords and curr_title:
        keyword_changes = compare_keyword_hits(product.watch_keywords, previous["title"] or "", current.title)
        if keyword_changes["added"] or keyword_changes["removed"]:
            alerts.append(
                {
                    "alert_type": "TITLE_KEYWORD_CHANGE",
                    "previous_value": "|".join(keyword_changes["prev_hit"]),
                    "current_value": "|".join(keyword_changes["curr_hit"]),
                    "details": keyword_changes,
                }
            )

    if "FEATURE_CHANGE" in enabled:
        prev_features = row_json_list(previous, "key_features_json")
        curr_features = list(current.key_features)
        feature_diff = diff_feature_lists(prev_features, curr_features)
        diff_count = len(feature_diff["added"]) + len(feature_diff["removed"])
        if diff_count >= max(1, int(feature_change_min_items)):
            alerts.append(
                {
                    "alert_type": "FEATURE_CHANGE",
                    "previous_value": f"{len(prev_features)} items",
                    "current_value": f"{len(curr_features)} items",
                    "details": {
                        "added_count": len(feature_diff["added"]),
                        "removed_count": len(feature_diff["removed"]),
                        "added": feature_diff["added"][: max(1, int(max_feature_items_in_alert))],
                        "removed": feature_diff["removed"][: max(1, int(max_feature_items_in_alert))],
                    },
                }
            )

    if "SPEC_CHANGE" in enabled:
        prev_specs = row_json_dict(previous, "key_specs_json")
        curr_specs = dict(current.key_specs)
        watch_keywords = [normalize_space(str(x)).lower() for x in (spec_watch_keywords or []) if normalize_space(str(x))]
        if watch_keywords:
            prev_specs = {
                k: v
                for k, v in prev_specs.items()
                if any(token in normalize_space(k).lower() for token in watch_keywords)
            }
            curr_specs = {
                k: v
                for k, v in curr_specs.items()
                if any(token in normalize_space(k).lower() for token in watch_keywords)
            }
        spec_diff = diff_specs(prev_specs, curr_specs)
        diff_count = len(spec_diff["added"]) + len(spec_diff["removed"]) + len(spec_diff["changed"])
        if diff_count >= max(1, int(spec_change_min_items)):
            alerts.append(
                {
                    "alert_type": "SPEC_CHANGE",
                    "previous_value": f"{len(prev_specs)} items",
                    "current_value": f"{len(curr_specs)} items",
                    "details": {
                        "added": spec_diff["added"][: max(1, int(max_spec_items_in_alert))],
                        "removed": spec_diff["removed"][: max(1, int(max_spec_items_in_alert))],
                        "changed": spec_diff["changed"][: max(1, int(max_spec_items_in_alert))],
                    },
                }
            )

    if "MARKET_SIGNAL_CHANGE" in enabled:
        prev_market = row_json_object(previous, "market_signals_json")
        curr_market = dict(current.market_signals or {})
        keys_to_watch = [
            "availability",
            "delivery_message",
            "seller_type",
            "fulfillment_type",
            "sold_by",
            "ships_from",
            "coupon_text",
            "discount_percent",
            "rating_value",
            "review_count",
            "badges",
            "has_aplus",
            "brand_name",
            "item_model_number",
            "bsr_rank",
            "bsr_category",
            "question_count",
            "bought_past_month",
            "buying_options_count",
            "buy_box_suppressed",
            "is_prime",
        ]
        changed_items: List[Dict[str, str]] = []
        for key in keys_to_watch:
            prev_val = normalize_scalar_for_compare(prev_market.get(key))
            curr_val = normalize_scalar_for_compare(curr_market.get(key))
            if prev_val == curr_val:
                continue
            changed_items.append(
                {
                    "key": key,
                    "previous": normalize_space(str(prev_market.get(key, ""))),
                    "current": normalize_space(str(curr_market.get(key, ""))),
                }
            )
        if changed_items:
            alerts.append(
                {
                    "alert_type": "MARKET_SIGNAL_CHANGE",
                    "previous_value": f"{len(changed_items)} field(s)",
                    "current_value": f"{len(changed_items)} field(s)",
                    "details": {"changes": changed_items[:12]},
                }
            )

    return alerts


def compare_keyword_hits(keywords: Sequence[str], previous_title: str, current_title: str) -> Dict[str, List[str]]:
    prev_norm = normalize_title_for_compare(previous_title)
    curr_norm = normalize_title_for_compare(current_title)
    prev_hit: List[str] = []
    curr_hit: List[str] = []
    for kw in keywords:
        k = normalize_space(kw).lower()
        if not k:
            continue
        if k in prev_norm:
            prev_hit.append(k)
        if k in curr_norm:
            curr_hit.append(k)
    prev_set = set(prev_hit)
    curr_set = set(curr_hit)
    return {
        "prev_hit": sorted(prev_set),
        "curr_hit": sorted(curr_set),
        "added": sorted(curr_set - prev_set),
        "removed": sorted(prev_set - curr_set),
    }


def persist_alerts(
    conn: sqlite3.Connection,
    snapshot_id: int,
    product_id: str,
    alerts: Sequence[Dict[str, Any]],
    created_at: str,
) -> None:
    for item in alerts:
        conn.execute(
            """
            INSERT INTO alerts (
                snapshot_id, product_id, alert_type, previous_value, current_value, details_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                product_id,
                item.get("alert_type", ""),
                str(item.get("previous_value", "")),
                str(item.get("current_value", "")),
                json.dumps(item.get("details", {}), ensure_ascii=False),
                created_at,
            ),
        )
    conn.commit()


def classify_quota_mode(weekly_quota_pct: float) -> str:
    if weekly_quota_pct < 30:
        return "PAUSE"
    if weekly_quota_pct < 50:
        return "CORE_ONLY"
    return "NORMAL"


def build_alert_digest(run_id: str, product_map: Dict[str, Product], alerts: Sequence[Dict[str, Any]]) -> str:
    lines: List[str] = []
    lines.append(f"# Competitor Monitor Alerts ({run_id})")
    lines.append("")
    lines.append(f"- Total alerts: {len(alerts)}")
    lines.append(f"- Generated at (UTC): {now_utc_iso()}")
    lines.append("")

    for item in alerts:
        product = product_map[item["product_id"]]
        lines.append(f"## {product.brand} | {product.product_line} | {product.product_name or product.product_id}")
        lines.append(f"- Product ID: {product.product_id}")
        lines.append(f"- URL: {product.url}")
        lines.append(f"- Alert Type: {item['alert_type']}")
        if item.get("previous_value"):
            lines.append(f"- Previous: {item['previous_value']}")
        if item.get("current_value"):
            lines.append(f"- Current: {item['current_value']}")
        details = item.get("details", {})
        if details:
            lines.append(f"- Details: {json.dumps(details, ensure_ascii=False)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_run_snapshot_markdown(run_id: str, summary: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"# Competitor Monitor Snapshot ({run_id})")
    lines.append("")
    lines.append(f"- Status: {summary.get('status', '')}")
    lines.append(f"- Quota Mode: {summary.get('quota_mode', '')}")
    lines.append(f"- Products Processed: {summary.get('products_processed', 0)}")
    lines.append(f"- Success: {summary.get('success_count', 0)}")
    lines.append(f"- Errors: {summary.get('error_count', 0)}")
    lines.append(f"- Alerts: {summary.get('alerts_count', 0)}")
    lines.append("")

    results = summary.get("product_results", [])
    for row in results:
        lines.append(f"## {row.get('brand', '')} | {row.get('product_name', row.get('product_id', ''))}")
        lines.append(f"- Product ID: {row.get('product_id', '')}")
        lines.append(f"- Site Type: {row.get('site_type', '')}")
        lines.append(f"- URL: {row.get('url', '')}")
        lines.append(f"- Success: {row.get('success', False)}")
        lines.append(f"- HTTP Status: {row.get('http_status', 0)}")
        if row.get("data_freshness"):
            lines.append(f"- Data Freshness: {row.get('data_freshness')}")
        if row.get("fallback_from_previous"):
            lines.append(f"- Fallback Captured At: {row.get('fallback_captured_at', '')}")
        if row.get("error_message"):
            lines.append(f"- Error: {row['error_message']}")
            # Keep rendering fallback values when available.
            if not row.get("fallback_from_previous"):
                lines.append("")
                continue
        lines.append(f"- Title: {row.get('title', '')}")
        lines.append(f"- Price: {row.get('price_value', '')} {row.get('currency', '')}".strip())
        if row.get("asin"):
            lines.append(f"- ASIN: {row.get('asin', '')}")
        if row.get("parent_asin"):
            lines.append(f"- Parent ASIN: {row.get('parent_asin', '')}")
        if row.get("brand_name"):
            lines.append(f"- Brand: {row.get('brand_name', '')}")
        if row.get("category_path"):
            lines.append(f"- Category Path: {row.get('category_path', '')}")
        if row.get("best_sellers_rank_text"):
            lines.append(f"- Best Sellers Rank: {row.get('best_sellers_rank_text')}")
        if row.get("bsr_rank") is not None:
            lines.append(f"- BSR Main: #{row.get('bsr_rank')} in {row.get('bsr_category', '')}")
        if row.get("bsr_subrank") is not None:
            lines.append(f"- BSR Sub: #{row.get('bsr_subrank')} in {row.get('bsr_subcategory', '')}")
        if row.get("rating_value") is not None:
            lines.append(f"- Rating: {row.get('rating_value')}")
        if row.get("review_count") is not None:
            lines.append(f"- Review Count: {row.get('review_count')}")
        if row.get("question_count") is not None:
            lines.append(f"- Questions: {row.get('question_count')}")
        if row.get("availability"):
            lines.append(f"- Availability: {row.get('availability')}")
        if row.get("in_stock") is not None:
            lines.append(f"- In Stock: {row.get('in_stock')}")
        if row.get("delivery_message"):
            lines.append(f"- Delivery: {row.get('delivery_message')}")
        if row.get("is_prime") is not None:
            lines.append(f"- Prime Eligible: {row.get('is_prime')}")
        if row.get("bought_past_month") is not None:
            lines.append(f"- Bought Past Month: {row.get('bought_past_month')}")
        if row.get("buying_options_count") is not None:
            lines.append(f"- Buying Options: {row.get('buying_options_count')}")
        if row.get("buy_box_suppressed") is not None:
            lines.append(f"- Buy Box Suppressed: {row.get('buy_box_suppressed')}")
        if row.get("sold_by") or row.get("ships_from"):
            lines.append(f"- Sold/Ships: {row.get('sold_by', '')} / {row.get('ships_from', '')}")
        if row.get("seller_type") or row.get("fulfillment_type"):
            lines.append(f"- Seller/Fulfillment: {row.get('seller_type', '')} / {row.get('fulfillment_type', '')}")
        if row.get("coupon_text"):
            lines.append(f"- Coupon: {row.get('coupon_text')}")
        if row.get("list_price_value") is not None:
            lines.append(f"- List Price: {row.get('list_price_value')} {row.get('list_price_currency', '')}".strip())
        if row.get("discount_percent") is not None:
            lines.append(f"- Discount: {row.get('discount_percent')}%")
        if row.get("item_model_number"):
            lines.append(f"- Model: {row.get('item_model_number')}")
        if row.get("product_dimensions"):
            lines.append(f"- Dimensions: {row.get('product_dimensions')}")
        if row.get("item_weight"):
            lines.append(f"- Weight: {row.get('item_weight')}")
        if row.get("date_first_available"):
            lines.append(f"- First Available: {row.get('date_first_available')}")
        if row.get("variation_count") is not None:
            lines.append(f"- Variation Count: {row.get('variation_count')}")
        if row.get("selected_color") or row.get("selected_size"):
            lines.append(f"- Selected Variant: color={row.get('selected_color','')} size={row.get('selected_size','')}")
        if row.get("sku"):
            lines.append(f"- SKU: {row.get('sku')}")
        if row.get("gtin"):
            lines.append(f"- GTIN: {row.get('gtin')}")
        if row.get("badges"):
            lines.append(f"- Badges: {' | '.join(row.get('badges', []))}")
        if row.get("image_count") is not None or row.get("video_count") is not None:
            lines.append(f"- Media: images={row.get('image_count')} videos={row.get('video_count')}")
        lines.append(f"- A+ Content: {row.get('has_aplus', False)}")
        if row.get("gtm_signal_score"):
            lines.append(
                f"- GTM Signal Score: {row.get('gtm_signal_score')} ({row.get('gtm_signal_score_pct')}%)"
            )
        if row.get("gtm_missing_fields"):
            lines.append(f"- GTM Missing: {' | '.join(row.get('gtm_missing_fields', []))}")
        features = row.get("key_features", [])
        specs = row.get("key_specs", {})
        lines.append(f"- Features Count: {len(features)}")
        if features:
            lines.append("- Top Features:")
            for item in features[:6]:
                lines.append(f"  - {item}")
        lines.append(f"- Specs Count: {len(specs)}")
        if specs:
            lines.append("- Top Specs:")
            for idx, (k, v) in enumerate(specs.items()):
                if idx >= 8:
                    break
                lines.append(f"  - {k}: {v}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_text(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def save_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_product_result_record(product: Product, snapshot: Snapshot) -> Dict[str, Any]:
    market = dict(snapshot.market_signals or {})
    row = {
        "title": snapshot.title,
        "price_value": snapshot.price_value,
        "currency": snapshot.currency,
        "price_raw": snapshot.price_raw,
        "key_features": list(snapshot.key_features),
        "key_specs": dict(snapshot.key_specs),
        "market_signals": market,
    }
    record = {
        "product_id": product.product_id,
        "brand": product.brand,
        "relationship": product.relationship,
        "category": product.category,
        "channel": product.channel,
        "country_market": product.country_market,
        "url_type": product.url_type,
        "priority": product.priority,
        "product_line": product.product_line,
        "product_name": product.product_name,
        "url": snapshot.url or product.url,
        "site_type": snapshot.site_type or product.site_type,
        "success": bool(snapshot.success),
        "http_status": int(snapshot.http_status),
        "error_message": snapshot.error_message,
        "data_freshness": "fresh" if snapshot.success else "empty",
        "fallback_from_previous": False,
        "fallback_captured_at": "",
        "title": row["title"],
        "price_value": row["price_value"],
        "currency": row["currency"],
        "price_raw": row["price_raw"],
        "key_features": row["key_features"],
        "key_specs": row["key_specs"],
        "market_signals": row["market_signals"],
        "asin": market.get("asin", ""),
        "parent_asin": market.get("parent_asin", ""),
        "category_path": market.get("category_path", ""),
        "rating_value": market.get("rating_value"),
        "review_count": market.get("review_count"),
        "availability": market.get("availability", ""),
        "delivery_message": market.get("delivery_message", ""),
        "sold_by": market.get("sold_by", ""),
        "ships_from": market.get("ships_from", ""),
        "seller_type": market.get("seller_type", ""),
        "fulfillment_type": market.get("fulfillment_type", ""),
        "coupon_text": market.get("coupon_text", ""),
        "discount_percent": market.get("discount_percent"),
        "list_price_value": market.get("list_price_value"),
        "list_price_currency": market.get("list_price_currency", ""),
        "badges": market.get("badges", []),
        "image_count": market.get("image_count"),
        "video_count": market.get("video_count"),
        "has_aplus": market.get("has_aplus", False),
        "brand_name": market.get("brand_name", ""),
        "item_model_number": market.get("item_model_number", ""),
        "date_first_available": market.get("date_first_available", ""),
        "product_dimensions": market.get("product_dimensions", ""),
        "item_weight": market.get("item_weight", ""),
        "country_of_origin": market.get("country_of_origin", ""),
        "manufacturer": market.get("manufacturer", ""),
        "best_sellers_rank_text": market.get("best_sellers_rank_text", ""),
        "bsr_rank": market.get("bsr_rank"),
        "bsr_category": market.get("bsr_category", ""),
        "bsr_subrank": market.get("bsr_subrank"),
        "bsr_subcategory": market.get("bsr_subcategory", ""),
        "question_count": market.get("question_count"),
        "bought_past_month": market.get("bought_past_month"),
        "buying_options_count": market.get("buying_options_count"),
        "buy_box_suppressed": market.get("buy_box_suppressed"),
        "is_prime": market.get("is_prime"),
        "variation_count": market.get("variation_count"),
        "selected_color": market.get("selected_color", ""),
        "selected_size": market.get("selected_size", ""),
        "sku": market.get("sku", ""),
        "mpn": market.get("mpn", ""),
        "gtin": market.get("gtin", ""),
        "product_type": market.get("product_type", ""),
        "in_stock": market.get("in_stock"),
        "price_valid_until": market.get("price_valid_until", ""),
        "item_condition": market.get("item_condition", ""),
        "gtm_signal_score": "",
        "gtm_signal_score_pct": 0.0,
        "gtm_missing_fields": [],
    }
    score = compute_gtm_signal_score(snapshot.site_type or product.site_type, record)
    record["gtm_signal_score"] = score["score"]
    record["gtm_signal_score_pct"] = score["score_pct"]
    record["gtm_missing_fields"] = score["missing_fields"]
    return record


def build_product_result_record_with_fallback(
    product: Product,
    snapshot: Snapshot,
    fallback_previous: Optional[sqlite3.Row],
) -> Dict[str, Any]:
    if snapshot.success or fallback_previous is None:
        return build_product_result_record(product, snapshot)

    fallback_features = row_json_list(fallback_previous, "key_features_json")
    fallback_specs = row_json_dict(fallback_previous, "key_specs_json")
    fallback_market = row_json_object(fallback_previous, "market_signals_json")
    base = {
        "title": normalize_space(str(fallback_previous["title"] or "")),
        "price_value": row_price_value(fallback_previous),
        "currency": normalize_space(str(fallback_previous["currency"] or "")),
        "price_raw": normalize_space(str(fallback_previous["price_raw"] or "")),
        "key_features": fallback_features,
        "key_specs": fallback_specs,
        "market_signals": fallback_market,
    }
    row = {
        "product_id": product.product_id,
        "brand": product.brand,
        "relationship": product.relationship,
        "category": product.category,
        "channel": product.channel,
        "country_market": product.country_market,
        "url_type": product.url_type,
        "priority": product.priority,
        "product_line": product.product_line,
        "product_name": product.product_name,
        "url": snapshot.url or product.url,
        "site_type": snapshot.site_type or product.site_type,
        "success": bool(snapshot.success),
        "http_status": int(snapshot.http_status),
        "error_message": snapshot.error_message,
        "data_freshness": "stale_from_last_success",
        "fallback_from_previous": True,
        "fallback_captured_at": normalize_space(str(fallback_previous["captured_at"] or "")),
        "title": base["title"],
        "price_value": base["price_value"],
        "currency": base["currency"],
        "price_raw": base["price_raw"],
        "key_features": base["key_features"],
        "key_specs": base["key_specs"],
        "market_signals": base["market_signals"],
        "asin": fallback_market.get("asin", ""),
        "parent_asin": fallback_market.get("parent_asin", ""),
        "category_path": fallback_market.get("category_path", ""),
        "rating_value": fallback_market.get("rating_value"),
        "review_count": fallback_market.get("review_count"),
        "availability": fallback_market.get("availability", ""),
        "delivery_message": fallback_market.get("delivery_message", ""),
        "sold_by": fallback_market.get("sold_by", ""),
        "ships_from": fallback_market.get("ships_from", ""),
        "seller_type": fallback_market.get("seller_type", ""),
        "fulfillment_type": fallback_market.get("fulfillment_type", ""),
        "coupon_text": fallback_market.get("coupon_text", ""),
        "discount_percent": fallback_market.get("discount_percent"),
        "list_price_value": fallback_market.get("list_price_value"),
        "list_price_currency": fallback_market.get("list_price_currency", ""),
        "badges": fallback_market.get("badges", []),
        "image_count": fallback_market.get("image_count"),
        "video_count": fallback_market.get("video_count"),
        "has_aplus": fallback_market.get("has_aplus", False),
        "brand_name": fallback_market.get("brand_name", ""),
        "item_model_number": fallback_market.get("item_model_number", ""),
        "date_first_available": fallback_market.get("date_first_available", ""),
        "product_dimensions": fallback_market.get("product_dimensions", ""),
        "item_weight": fallback_market.get("item_weight", ""),
        "country_of_origin": fallback_market.get("country_of_origin", ""),
        "manufacturer": fallback_market.get("manufacturer", ""),
        "best_sellers_rank_text": fallback_market.get("best_sellers_rank_text", ""),
        "bsr_rank": fallback_market.get("bsr_rank"),
        "bsr_category": fallback_market.get("bsr_category", ""),
        "bsr_subrank": fallback_market.get("bsr_subrank"),
        "bsr_subcategory": fallback_market.get("bsr_subcategory", ""),
        "question_count": fallback_market.get("question_count"),
        "bought_past_month": fallback_market.get("bought_past_month"),
        "buying_options_count": fallback_market.get("buying_options_count"),
        "buy_box_suppressed": fallback_market.get("buy_box_suppressed"),
        "is_prime": fallback_market.get("is_prime"),
        "variation_count": fallback_market.get("variation_count"),
        "selected_color": fallback_market.get("selected_color", ""),
        "selected_size": fallback_market.get("selected_size", ""),
        "sku": fallback_market.get("sku", ""),
        "mpn": fallback_market.get("mpn", ""),
        "gtin": fallback_market.get("gtin", ""),
        "product_type": fallback_market.get("product_type", ""),
        "in_stock": fallback_market.get("in_stock"),
        "price_valid_until": fallback_market.get("price_valid_until", ""),
        "item_condition": fallback_market.get("item_condition", ""),
        "gtm_signal_score": "",
        "gtm_signal_score_pct": 0.0,
        "gtm_missing_fields": [],
    }
    score = compute_gtm_signal_score(snapshot.site_type or product.site_type, row)
    row["gtm_signal_score"] = score["score"]
    row["gtm_signal_score_pct"] = score["score_pct"]
    row["gtm_missing_fields"] = score["missing_fields"]
    return row


def save_product_results_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    fieldnames = [
        "product_id",
        "brand",
        "relationship",
        "category",
        "channel",
        "country_market",
        "url_type",
        "priority",
        "product_line",
        "product_name",
        "url",
        "site_type",
        "success",
        "http_status",
        "error_message",
        "data_freshness",
        "fallback_from_previous",
        "fallback_captured_at",
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
        "sold_by",
        "ships_from",
        "seller_type",
        "fulfillment_type",
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
        "key_features",
        "key_specs",
        "market_signals",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            out = dict(row)
            out["badges"] = " | ".join(out.get("badges", []))
            out["gtm_missing_fields"] = " | ".join(out.get("gtm_missing_fields", []))
            out["key_features"] = " | ".join(out.get("key_features", []))
            out["key_specs"] = json.dumps(out.get("key_specs", {}), ensure_ascii=False)
            out["market_signals"] = json.dumps(out.get("market_signals", {}), ensure_ascii=False)
            writer.writerow(out)


def run_monitor(
    products_csv: Path = DEFAULT_PRODUCTS_CSV,
    db_path: Path = DEFAULT_DB_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    weekly_quota_pct: float = 100.0,
    price_change_threshold_pct: Optional[float] = None,
    timeout_seconds: int = 25,
    config_path: Optional[Path] = DEFAULT_CONFIG_PATH,
    spec_watch_keywords: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    quota_mode = classify_quota_mode(weekly_quota_pct)
    monitor_cfg = load_monitor_config(config_path)
    cfg_threshold = _coerce_float(monitor_cfg.get("price_change_threshold_pct"), 0.1)
    cli_threshold = _coerce_float(price_change_threshold_pct, cfg_threshold) if price_change_threshold_pct is not None else None
    effective_price_threshold = cli_threshold if cli_threshold is not None else cfg_threshold

    ensure_output_dir(output_dir)
    all_products = read_products_csv(products_csv)
    products = [p for p in all_products if p.enabled]
    product_map = {p.product_id: p for p in products}

    summary: Dict[str, Any] = {
        "run_id": run_id,
        "started_at_utc": now_utc_iso(),
        "weekly_quota_pct": weekly_quota_pct,
        "quota_mode": quota_mode,
        "monitor_config_path": str(config_path) if config_path else "",
        "monitor_config": monitor_cfg,
        "effective_price_threshold_pct": effective_price_threshold,
        "products_total": len(products),
        "products_processed": 0,
        "success_count": 0,
        "error_count": 0,
        "alerts_count": 0,
        "alerts": [],
        "failures": [],
        "product_results": [],
    }

    if quota_mode == "PAUSE":
        summary["status"] = "SKIPPED_QUOTA_GUARD"
        summary["finished_at_utc"] = now_utc_iso()
        summary_path = output_dir / f"run_summary_{run_id}.json"
        save_json(summary_path, summary)
        summary["summary_path"] = str(summary_path)
        return summary

    conn = db_connect(db_path)
    init_db(conn)
    upsert_products(conn, all_products)

    all_alerts: List[Dict[str, Any]] = []
    product_results: List[Dict[str, Any]] = []
    enabled_alert_types = [str(x).strip().upper() for x in monitor_cfg.get("enabled_alert_types", [])]
    for product in products:
        summary["products_processed"] += 1
        previous = get_latest_success_snapshot(conn, product.product_id)

        try:
            final_url, status_code, html = fetch_html(
                product.url,
                site_type=product.site_type,
                timeout_seconds=timeout_seconds,
                retry_count=_coerce_int(monitor_cfg.get("http_retry_count"), 1),
                retry_backoff_seconds=_coerce_float(monitor_cfg.get("http_retry_backoff_seconds"), 1.0),
                max_html_bytes=_coerce_int(monitor_cfg.get("http_max_html_bytes"), 1500000),
            )
            if product.site_type == "amazon":
                is_blocked = is_probable_amazon_captcha_page(html) and not has_amazon_product_markers(html)
                blocked_retry_count = _coerce_int(monitor_cfg.get("amazon_blocked_retry_count"), 1)
                if is_blocked and blocked_retry_count > 0:
                    for retry_idx in range(blocked_retry_count):
                        time.sleep(max(0.0, _coerce_float(monitor_cfg.get("http_retry_backoff_seconds"), 1.0)) * (retry_idx + 1))
                        retry_url, retry_status, retry_html = fetch_html(
                            product.url,
                            site_type=product.site_type,
                            timeout_seconds=timeout_seconds,
                            retry_count=1,
                            retry_backoff_seconds=0.0,
                            max_html_bytes=_coerce_int(monitor_cfg.get("http_max_html_bytes"), 1500000),
                        )
                        final_url, status_code, html = retry_url, retry_status, retry_html
                        is_blocked = is_probable_amazon_captcha_page(html) and not has_amazon_product_markers(html)
                        if not is_blocked:
                            break

            if status_code >= 400:
                snapshot = failed_snapshot(product, f"HTTP_{status_code}", status_code=status_code)
            elif is_probable_amazon_captcha_page(html) and product.site_type == "amazon":
                if has_amazon_product_markers(html):
                    snapshot = parse_snapshot(
                        product,
                        final_url,
                        status_code,
                        html,
                        max_feature_items=_coerce_int(monitor_cfg.get("max_feature_items"), 12),
                    )
                else:
                    snapshot = failed_snapshot(product, "AMAZON_CAPTCHA_OR_BLOCKED", status_code=status_code)
            else:
                snapshot = parse_snapshot(
                    product,
                    final_url,
                    status_code,
                    html,
                    max_feature_items=_coerce_int(monitor_cfg.get("max_feature_items"), 12),
                )
        except requests.RequestException as exc:
            snapshot = failed_snapshot(product, f"REQUEST_ERROR:{exc.__class__.__name__}:{exc}")
        except Exception as exc:  # Defensive guard for parser edge cases.
            snapshot = failed_snapshot(product, f"PARSER_ERROR:{exc.__class__.__name__}:{exc}")

        snapshot_id = insert_snapshot(conn, snapshot)
        product_results.append(build_product_result_record_with_fallback(product, snapshot, previous))

        if not snapshot.success:
            summary["error_count"] += 1
            summary["failures"].append(
                {
                    "product_id": product.product_id,
                    "url": product.url,
                    "error_message": snapshot.error_message,
                    "http_status": snapshot.http_status,
                }
            )
            if (
                previous is not None
                and "BLOCKED_PAGE" in enabled_alert_types
                and ("BLOCKED" in snapshot.error_message or "CAPTCHA" in snapshot.error_message)
            ):
                blocked_alert = {
                    "alert_type": "BLOCKED_PAGE",
                    "previous_value": normalize_space(str(previous["captured_at"] or "")),
                    "current_value": normalize_space(snapshot.error_message),
                    "details": {
                        "http_status": snapshot.http_status,
                        "url": product.url,
                        "last_success_title": normalize_space(str(previous["title"] or "")),
                    },
                }
                now_iso = now_utc_iso()
                persist_alerts(conn, snapshot_id, product.product_id, [blocked_alert], now_iso)
                all_alerts.append(
                    {
                        "product_id": product.product_id,
                        "alert_type": blocked_alert["alert_type"],
                        "previous_value": blocked_alert["previous_value"],
                        "current_value": blocked_alert["current_value"],
                        "details": blocked_alert["details"],
                    }
                )
            continue

        summary["success_count"] += 1
        if previous is None:
            continue

        alerts = detect_changes(
            product=product,
            previous=previous,
            current=snapshot,
            price_change_threshold_pct=effective_price_threshold,
            feature_change_min_items=_coerce_int(monitor_cfg.get("feature_change_min_items"), 1),
            spec_change_min_items=_coerce_int(monitor_cfg.get("spec_change_min_items"), 1),
            enabled_alert_types=enabled_alert_types,
            max_feature_items_in_alert=_coerce_int(monitor_cfg.get("max_feature_items_in_alert"), 6),
            max_spec_items_in_alert=_coerce_int(monitor_cfg.get("max_spec_items_in_alert"), 8),
            spec_watch_keywords=spec_watch_keywords,
        )
        if not alerts:
            continue

        now_iso = now_utc_iso()
        persist_alerts(conn, snapshot_id, product.product_id, alerts, now_iso)
        for a in alerts:
            record = {
                "product_id": product.product_id,
                "alert_type": a["alert_type"],
                "previous_value": a.get("previous_value", ""),
                "current_value": a.get("current_value", ""),
                "details": a.get("details", {}),
            }
            all_alerts.append(record)

    conn.close()

    summary["alerts"] = all_alerts
    summary["alerts_count"] = len(all_alerts)
    summary["product_results"] = product_results
    alerts_by_type: Dict[str, int] = {}
    for item in all_alerts:
        alert_type = normalize_space(str(item.get("alert_type", ""))).upper() or "UNKNOWN"
        alerts_by_type[alert_type] = alerts_by_type.get(alert_type, 0) + 1
    summary["alerts_by_type"] = alerts_by_type
    summary["status"] = "OK"
    summary["finished_at_utc"] = now_utc_iso()

    details_json_path = output_dir / f"run_details_{run_id}.json"
    details_csv_path = output_dir / f"run_details_{run_id}.csv"
    save_json(details_json_path, {"run_id": run_id, "product_results": product_results})
    save_product_results_csv(details_csv_path, product_results)
    summary["details_json_path"] = str(details_json_path)
    summary["details_csv_path"] = str(details_csv_path)

    run_snapshot_md_path = output_dir / f"run_snapshot_{run_id}.md"
    save_text(run_snapshot_md_path, build_run_snapshot_markdown(run_id, summary))
    summary["run_snapshot_md_path"] = str(run_snapshot_md_path)

    alert_digest = build_alert_digest(run_id, product_map, all_alerts) if all_alerts else ""
    if alert_digest:
        alert_path = output_dir / f"alerts_{run_id}.md"
        save_text(alert_path, alert_digest)
        summary["alert_digest_path"] = str(alert_path)

    summary_path = output_dir / f"run_summary_{run_id}.json"
    save_json(summary_path, summary)
    summary["summary_path"] = str(summary_path)
    return summary
