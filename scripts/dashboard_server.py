#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import socket
import sqlite3
import threading
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import parse_qs, urlparse

from email_gateway import build_email_message, load_smtp_config_from_env, send_via_smtp
from monitor_core import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PRODUCTS_CSV,
    Product,
    compute_gtm_signal_score,
    db_connect,
    fallback_product_id,
    fetch_html,
    get_spec_value_by_keywords,
    infer_site_type,
    init_db,
    normalize_space,
    parse_snapshot,
    read_products_csv,
    run_monitor,
    upsert_products,
)
from run_monitor import build_email_body, build_email_subject, parse_alert_targets


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_HTML_PATH = PROJECT_ROOT / "dashboard" / "index.html"
DEFAULT_DASHBOARD_SETTINGS_PATH = PROJECT_ROOT / "data" / "dashboard_settings.json"
DEFAULT_ALERT_FROM_EMAIL = "media@nekteck.com"
DEFAULT_ALERT_FROM_NAME = "Nekteck Media"
DEFAULT_PRICE_ALERT_THRESHOLD_PCT = 3.0
DEFAULT_SPEC_WATCH_FIELDS = "dimensions|weight|wattage|power|voltage|temperature|heat|timer|intensity|mode|foot size|material"
COMPARE_AT_RE = re.compile(
    r"(?:compare_at_price|compareAtPrice)\s*[:=]\s*\"?([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
KNOWN_BRANDS = ["Nekteck", "Renpho", "Snailax", "Comfier", "Resteck", "Fitking", "Bob & Brad"]
DEFAULT_MASSAGE_COMPARE_TOKENS = ["massage", "massager", "shiatsu", "foot", "eye", "heat", "compression", "按摩"]
HOST_BRAND_ALIASES = {
    "nekteck": "Nekteck",
    "renpho": "Renpho",
    "snailax": "Snailax",
    "comfier": "Comfier",
    "resteck": "Resteck",
    "fitking": "Fitking",
    "bobandbrad": "Bob & Brad",
    "bob-brad": "Bob & Brad",
    "bobbrad": "Bob & Brad",
    "therabody": "Therabody",
    "hyperice": "Hyperice",
}


def detect_lan_ip() -> str:
    sock: Optional[socket.socket] = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = normalize_space(str(sock.getsockname()[0]))
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    finally:
        if sock is not None:
            sock.close()

    try:
        ip = normalize_space(socket.gethostbyname(socket.gethostname()))
        return "" if ip.startswith("127.") else ip
    except OSError:
        return ""


def parse_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    lowered = normalize_space(str(value)).lower()
    if not lowered:
        return default
    return lowered not in {"0", "false", "no", "off"}


def json_loads_dict(raw: str) -> Dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def json_loads_list(raw: str) -> List[Any]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def default_dashboard_settings() -> Dict[str, Any]:
    return {
        "alert_to": "",
        "send_on_no_alerts": False,
        "price_change_threshold_pct": DEFAULT_PRICE_ALERT_THRESHOLD_PCT,
        "spec_watch_fields": DEFAULT_SPEC_WATCH_FIELDS,
        "from_email": DEFAULT_ALERT_FROM_EMAIL,
        "from_name": DEFAULT_ALERT_FROM_NAME,
    }


def load_dashboard_settings(path: Path = DEFAULT_DASHBOARD_SETTINGS_PATH) -> Dict[str, Any]:
    settings = default_dashboard_settings()
    if not path.exists():
        return settings
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return settings
    if not isinstance(loaded, dict):
        return settings
    settings.update({k: loaded.get(k, v) for k, v in settings.items()})
    settings["alert_to"] = normalize_space(str(settings.get("alert_to", "")))
    settings["send_on_no_alerts"] = bool(settings.get("send_on_no_alerts", False))
    settings["price_change_threshold_pct"] = float_or_none(settings.get("price_change_threshold_pct")) or DEFAULT_PRICE_ALERT_THRESHOLD_PCT
    settings["spec_watch_fields"] = normalize_space(str(settings.get("spec_watch_fields", ""))) or DEFAULT_SPEC_WATCH_FIELDS
    settings["from_email"] = DEFAULT_ALERT_FROM_EMAIL
    settings["from_name"] = DEFAULT_ALERT_FROM_NAME
    return settings


def save_dashboard_settings(settings: Dict[str, Any], path: Path = DEFAULT_DASHBOARD_SETTINGS_PATH) -> Dict[str, Any]:
    normalized = default_dashboard_settings()
    normalized.update({k: settings.get(k, v) for k, v in normalized.items()})
    normalized["alert_to"] = normalize_space(str(normalized.get("alert_to", "")))
    normalized["send_on_no_alerts"] = bool(normalized.get("send_on_no_alerts", False))
    normalized["price_change_threshold_pct"] = float_or_none(normalized.get("price_change_threshold_pct")) or DEFAULT_PRICE_ALERT_THRESHOLD_PCT
    normalized["spec_watch_fields"] = normalize_space(str(normalized.get("spec_watch_fields", ""))) or DEFAULT_SPEC_WATCH_FIELDS
    normalized["from_email"] = DEFAULT_ALERT_FROM_EMAIL
    normalized["from_name"] = DEFAULT_ALERT_FROM_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return normalized


def invalid_email_targets(raw: str) -> List[str]:
    targets = parse_alert_targets(raw)
    return [target for target in targets if not EMAIL_RE.match(target)]


def infer_brand_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    first = host.split(".")[0]
    return first.capitalize() if first else ""


def infer_known_brand_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().replace("&", "and")
    compact = re.sub(r"[^a-z0-9]+", "", host)
    dashed = re.sub(r"[^a-z0-9]+", "-", host).strip("-")
    for token, brand in HOST_BRAND_ALIASES.items():
        token_compact = re.sub(r"[^a-z0-9]+", "", token.lower())
        if token.lower() in dashed or token_compact in compact:
            return brand
    return ""


def infer_channel_from_url(url: str, site_type: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "amazon." in host:
        return "Amazon"
    if "walmart." in host:
        return "Walmart"
    if "target." in host:
        return "Target"
    if site_type == "independent":
        return "DTC"
    return site_type.capitalize() if site_type else ""


def infer_url_type(url: str) -> str:
    parsed = urlparse(url)
    path = normalize_space(parsed.path or "").lower().strip("/")
    if not path:
        return "homepage"
    if re.search(r"/(?:dp|gp/product|aw/d)/[a-z0-9]{10}", "/" + path, re.IGNORECASE):
        return "pdp"
    if "/products/" in "/" + path or path.startswith("products/"):
        return "pdp"
    if any(token in "/" + path for token in ["/collections/", "/category/", "/categories/", "/search"]):
        return "collection"
    return "other"


def normalize_relationship(relationship: str, brand: str, url: str) -> str:
    raw = normalize_space(relationship).lower()
    if raw in {"owned", "competitor"}:
        return raw
    host = (urlparse(url).hostname or "").lower()
    brand_l = normalize_space(brand).lower()
    if "nekteck" in host or brand_l == "nekteck":
        return "owned"
    return "competitor"


def normalize_priority(priority: str) -> str:
    raw = normalize_space(priority).upper()
    return raw if raw in {"A", "B", "C"} else "B"


def split_brand_candidates(raw: Any) -> List[str]:
    if isinstance(raw, list):
        values = raw
    else:
        values = re.split(r"[|,\n]+", str(raw or ""))
    brands: List[str] = []
    for value in values:
        brand = normalize_space(str(value))
        if brand and not any(x.lower() == brand.lower() for x in brands):
            brands.append(brand)
    return brands


def choose_brand_for_entry(entry: Dict[str, Any], url: str) -> str:
    candidates = split_brand_candidates(entry.get("brands", [])) or split_brand_candidates(entry.get("brand", ""))
    host_brand = infer_known_brand_from_url(url)
    if host_brand and (not candidates or any(x.lower() == host_brand.lower() for x in candidates)):
        return host_brand
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        for brand in candidates:
            if brand.lower() != "nekteck":
                return brand
        return candidates[0]
    return host_brand or infer_brand_from_url(url)


def url_validation_warning(url: str, url_type: str) -> str:
    if url_type == "pdp":
        return ""
    label = {"homepage": "首页", "collection": "集合/分类页", "other": "非标准产品页"}.get(url_type, url_type)
    return f"{label}不会进入自动抓取，请替换为具体产品详情页：{url}"


def parse_pipe_list(raw: str) -> List[str]:
    return [normalize_space(x) for x in re.split(r"[|,\n]+", str(raw or "")) if normalize_space(x)]


def extract_urls_from_text(value: Any) -> List[str]:
    text = str(value or "")
    if not text:
        return []
    urls: List[str] = []
    for part in re.split(r"(?=https?://)", text, flags=re.IGNORECASE):
        for chunk in re.split(r"[\s,，]+", part):
            url = normalize_space(chunk).rstrip("。；;，,)")
            if url.startswith("http") and not any(existing.lower() == url.lower() for existing in urls):
                urls.append(url)
    return urls


def dashboard_field_config() -> Dict[str, Any]:
    return {
        "main_param_rows": MAIN_PARAM_ROWS,
        "gtm_required_rows": GTM_REQUIRED_ROWS,
        "feature_signal_rows": FEATURE_SIGNAL_ROWS,
    }


def row_matches_category_tokens(row: Dict[str, Any], tokens: Sequence[str]) -> bool:
    lowered_tokens = [normalize_space(str(token)).lower() for token in tokens if normalize_space(str(token))]
    if not lowered_tokens:
        return True
    blob = " ".join(
        [
            normalize_space(str(row.get("category", ""))),
            normalize_space(str(row.get("product_line", ""))),
            normalize_space(str(row.get("product_name", ""))),
            normalize_space(str(row.get("title", ""))),
            " ".join([normalize_space(str(x)) for x in row.get("key_features", [])[:6]]),
        ]
    ).lower()
    return any(token in blob for token in lowered_tokens)


def csv_headers() -> List[str]:
    return [
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
        "watch_keywords",
        "enabled",
    ]


MAIN_PARAM_ROWS: List[Dict[str, str]] = [
    {"key": "price", "label": "价格"},
    {"key": "list_price", "label": "原价"},
    {"key": "discount_percent", "label": "折扣(%)"},
    {"key": "rating_value", "label": "评分"},
    {"key": "review_count", "label": "评论数"},
    {"key": "availability", "label": "库存状态"},
    {"key": "sku", "label": "SKU"},
    {"key": "barcode_or_mpn", "label": "条码/型号"},
    {"key": "item_model_number", "label": "型号"},
    {"key": "product_dimensions", "label": "尺寸"},
    {"key": "item_weight", "label": "重量"},
    {"key": "power_spec", "label": "功率"},
    {"key": "voltage_spec", "label": "电压"},
    {"key": "heat_spec", "label": "温度/热敷"},
    {"key": "timer_spec", "label": "定时"},
    {"key": "intensity_spec", "label": "模式/力度"},
    {"key": "foot_size_spec", "label": "适配脚码"},
    {"key": "material_spec", "label": "材质"},
    {"key": "image_count", "label": "主图数"},
    {"key": "variation_count", "label": "变体数"},
    {"key": "features_count", "label": "卖点数"},
    {"key": "specs_count", "label": "结构化参数数"},
]

GTM_REQUIRED_ROWS: List[Dict[str, str]] = [
    {"key": "title", "label": "标题"},
    {"key": "price", "label": "价格"},
    {"key": "compare_at_price", "label": "原价"},
    {"key": "discount_pct", "label": "折扣"},
    {"key": "brand_name", "label": "品牌"},
    {"key": "availability", "label": "库存"},
    {"key": "sku", "label": "SKU"},
    {"key": "barcode_or_mpn", "label": "条码/型号"},
    {"key": "rating", "label": "评分"},
    {"key": "feature_bullets", "label": "卖点"},
    {"key": "spec_structured", "label": "结构化参数"},
    {"key": "image_count", "label": "主图数"},
    {"key": "variant_count", "label": "变体数"},
]

FEATURE_SIGNAL_ROWS: List[Dict[str, str]] = [
    {"key": "heat", "label": "heat"},
    {"key": "shiatsu", "label": "shiatsu"},
    {"key": "air_compression", "label": "air_compression"},
    {"key": "timer", "label": "timer"},
    {"key": "intensity_levels", "label": "intensity_levels"},
    {"key": "circulation_claim", "label": "circulation_claim"},
    {"key": "sleep_claim", "label": "sleep_claim"},
    {"key": "portable_claim", "label": "portable_claim"},
    {"key": "foot_size_claim", "label": "foot_size_claim"},
]


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = normalize_space(str(value or ""))
        if text:
            return text
    return ""


def float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def pick_spec(specs: Dict[str, str], keywords: Sequence[str]) -> str:
    return get_spec_value_by_keywords(specs, keywords)


def first_feature_hit(features: Sequence[str], keywords: Sequence[str]) -> str:
    if not features:
        return ""
    normalized_pairs: List[Any] = []
    for item in features:
        text = normalize_space(str(item))
        if not text:
            continue
        normalized_pairs.append((text, text.lower()))
    for kw in keywords:
        kw_l = normalize_space(kw).lower()
        if not kw_l:
            continue
        for original, lowered in normalized_pairs:
            if kw_l in lowered and len(original) <= 90:
                return original
    return ""


def build_feature_signal_map(title: str, features: Sequence[str], specs: Dict[str, str]) -> Dict[str, bool]:
    texts: List[str] = [normalize_space(str(title or "")).lower()]
    texts.extend([normalize_space(str(v)).lower() for v in features if normalize_space(str(v))])
    texts.extend(
        [
            normalize_space(str(k)).lower() + " " + normalize_space(str(v)).lower()
            for k, v in specs.items()
            if normalize_space(str(k)) or normalize_space(str(v))
        ]
    )
    blob = " | ".join(texts)

    def has_any(tokens: Sequence[str]) -> bool:
        return any(token in blob for token in tokens)

    return {
        "heat": has_any(["heat", "heating", "warm"]),
        "shiatsu": has_any(["shiatsu"]),
        "air_compression": has_any(["air compression", "compression", "airbag", "air pressure"]),
        "timer": has_any(["timer", "timing", "auto off", "auto-off", "shut off"]),
        "intensity_levels": has_any(["intensity", "levels", "level setting", "pressure level"]),
        "circulation_claim": has_any(["circulation", "blood flow"]),
        "sleep_claim": has_any(["sleep", "deep relax", "relaxation"]),
        "portable_claim": has_any(["portable", "compact", "travel", "carry"]),
        "foot_size_claim": has_any(["foot size", "shoe size", "fits up to", "fit for", "fits feet"]),
    }


def build_major_params(
    title: str,
    price_value: Optional[float],
    currency: str,
    market: Dict[str, Any],
    specs: Dict[str, str],
    features: Sequence[str],
    site_type: str,
) -> Dict[str, Any]:
    power_spec = first_non_empty(
        pick_spec(specs, ["wattage", "rated power", "power"]),
        first_feature_hit(features, ["watt", "power"]),
    )
    voltage_spec = first_non_empty(
        pick_spec(specs, ["voltage", "input voltage", "rated voltage", "input"]),
        first_feature_hit(features, ["voltage", "110v", "120v"]),
    )
    heat_spec = first_non_empty(
        pick_spec(specs, ["temperature", "heat", "heating"]),
        first_feature_hit(features, ["heat", "heating", "warm"]),
    )
    timer_spec = first_non_empty(
        pick_spec(specs, ["timer", "timing", "auto-off", "auto off"]),
        first_feature_hit(features, ["timer", "auto-off", "auto off"]),
    )
    intensity_spec = first_non_empty(
        pick_spec(specs, ["intensity", "mode", "levels", "compression"]),
        first_feature_hit(features, ["intensity", "levels", "mode", "compression"]),
    )
    foot_size_spec = first_non_empty(
        pick_spec(specs, ["foot size", "shoe size", "fit for", "size range"]),
        first_feature_hit(features, ["foot size", "shoe size", "fits up to", "fit for"]),
    )
    material_spec = first_non_empty(
        pick_spec(specs, ["material", "fabric", "cover"]),
        first_feature_hit(features, ["material", "leather", "fabric"]),
    )
    barcode_or_mpn = first_non_empty(
        market.get("gtin"),
        market.get("mpn"),
        market.get("asin") if site_type == "amazon" else "",
        market.get("item_model_number"),
    )

    return {
        "price": price_value,
        "currency": normalize_space(str(currency or "")),
        "list_price": float_or_none(market.get("list_price_value")),
        "discount_percent": float_or_none(market.get("discount_percent")),
        "rating_value": float_or_none(market.get("rating_value")),
        "review_count": int_or_none(market.get("review_count")),
        "availability": normalize_space(str(market.get("availability", ""))),
        "sku": normalize_space(str(market.get("sku", ""))),
        "barcode_or_mpn": barcode_or_mpn,
        "item_model_number": first_non_empty(market.get("item_model_number")),
        "product_dimensions": first_non_empty(
            market.get("product_dimensions"),
            pick_spec(specs, ["product dimensions", "package dimensions", "dimensions", "size"]),
        ),
        "item_weight": first_non_empty(
            market.get("item_weight"),
            pick_spec(specs, ["item weight", "weight"]),
        ),
        "power_spec": power_spec,
        "voltage_spec": voltage_spec,
        "heat_spec": heat_spec,
        "timer_spec": timer_spec,
        "intensity_spec": intensity_spec,
        "foot_size_spec": foot_size_spec,
        "material_spec": material_spec,
        "image_count": int_or_none(market.get("image_count")),
        "variation_count": int_or_none(market.get("variation_count")),
        "features_count": len(features),
        "specs_count": len(specs),
        "title": normalize_space(str(title or "")),
    }


def build_gtm_required_coverage(
    title: str,
    price_value: Optional[float],
    market: Dict[str, Any],
    features: Sequence[str],
    specs: Dict[str, str],
    site_type: str,
) -> Dict[str, bool]:
    barcode_or_mpn = first_non_empty(
        market.get("gtin"),
        market.get("mpn"),
        market.get("asin") if site_type == "amazon" else "",
        market.get("item_model_number"),
    )
    return {
        "title": bool(normalize_space(str(title or ""))),
        "price": price_value is not None,
        "compare_at_price": float_or_none(market.get("list_price_value")) is not None,
        "discount_pct": float_or_none(market.get("discount_percent")) is not None,
        "brand_name": bool(first_non_empty(market.get("brand_name"))),
        "availability": bool(normalize_space(str(market.get("availability", "")))),
        "sku": bool(normalize_space(str(market.get("sku", "")))),
        "barcode_or_mpn": bool(barcode_or_mpn),
        "rating": float_or_none(market.get("rating_value")) is not None,
        "feature_bullets": len(features) > 0,
        "spec_structured": len(specs) > 0,
        "image_count": int_or_none(market.get("image_count")) is not None,
        "variant_count": int_or_none(market.get("variation_count")) is not None,
    }


def build_gtm_suggestions(
    relationship: str,
    url_type: str,
    gtm_required_missing_fields: Sequence[str],
    feature_signals: Dict[str, bool],
    major_params: Dict[str, Any],
) -> List[Dict[str, str]]:
    missing = {normalize_space(str(x)) for x in gtm_required_missing_fields}
    suggestions: List[Dict[str, str]] = []
    scope = "自有页面优化" if relationship == "owned" else "竞品观察"

    if url_type != "pdp":
        suggestions.append(
            {
                "level": "high",
                "topic": "链接类型",
                "message": "当前不是产品详情页，先换成具体PDP链接，否则价格、参数和卖点监控都会不稳定。",
                "action": "替换为产品详情页后再加入监控。",
            }
        )
    if {"price", "compare_at_price", "discount_pct"} & missing:
        suggestions.append(
            {
                "level": "high",
                "topic": "价格策略",
                "message": f"{scope}缺少完整价格信号，GTM判断会缺少促销力度和价格锚点。",
                "action": "补齐现价、原价、折扣，后续用3%默认阈值触发价格波动提醒。",
            }
        )
    if {"rating", "image_count", "variant_count"} & missing:
        suggestions.append(
            {
                "level": "medium",
                "topic": "信任与转化",
                "message": "评论、图片或变体信息不完整，影响对转化资产强弱的判断。",
                "action": "优先补评论评分、主图数量、变体数量，用来对比同价位竞品转化资产。",
            }
        )
    if {"sku", "barcode_or_mpn", "spec_structured"} & missing:
        suggestions.append(
            {
                "level": "medium",
                "topic": "参数资产",
                "message": "SKU、型号或结构化参数不足，不利于运营做卖点拆解和渠道上架校验。",
                "action": "补型号/SKU/尺寸/重量/功率/电压/材质等结构化字段。",
            }
        )
    if not feature_signals.get("heat") or not feature_signals.get("timer") or not feature_signals.get("intensity_levels"):
        suggestions.append(
            {
                "level": "medium",
                "topic": "核心卖点",
                "message": "热敷、定时、模式/力度等高频卖点信号不完整。",
                "action": "检查标题、五点、详情页模块是否清晰露出这些购买决策点。",
            }
        )
    if not major_params.get("product_dimensions") or not major_params.get("item_weight"):
        suggestions.append(
            {
                "level": "low",
                "topic": "物流与适配",
                "message": "尺寸或重量缺失，影响运费、包装和适配场景判断。",
                "action": "补尺寸、重量、适配脚码/适配人群，便于渠道和客服复用。",
            }
        )
    if not suggestions:
        suggestions.append(
            {
                "level": "low",
                "topic": "持续监控",
                "message": "当前GTM核心字段覆盖较好。",
                "action": "保持价格、标题关键词和重点参数监控，观察竞品后续调整。",
            }
        )
    return suggestions[:5]


def infer_compare_at_price_from_html(html: str, current_price: Optional[float]) -> Optional[float]:
    source = html or ""
    values: List[float] = []
    for m in COMPARE_AT_RE.finditer(source):
        raw = normalize_space(m.group(1))
        if not raw:
            continue
        try:
            amount = float(raw)
        except ValueError:
            continue
        if amount <= 0:
            continue
        if "." not in raw and amount >= 1000:
            as_dollars = amount / 100.0
            if current_price is None or (as_dollars >= max(1.0, current_price * 0.7) and as_dollars <= current_price * 4.0):
                amount = as_dollars
        values.append(amount)
    if not values:
        return None
    values.sort()
    # Prefer the largest compare-at price from the page.
    return values[-1]


@dataclass
class DashboardConfig:
    products_csv: Path
    db_path: Path
    output_dir: Path
    config_path: Path
    default_weekly_quota_pct: float


class DashboardService:
    def __init__(self, cfg: DashboardConfig):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._ensure_products_csv()
        self.sync_products_to_db()

    def _ensure_products_csv(self) -> None:
        if self.cfg.products_csv.exists():
            return
        self.cfg.products_csv.parent.mkdir(parents=True, exist_ok=True)
        with self.cfg.products_csv.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=csv_headers())
            writer.writeheader()

    def _connect(self) -> sqlite3.Connection:
        conn = db_connect(self.cfg.db_path)
        init_db(conn)
        return conn

    def sync_products_to_db(self) -> None:
        products = read_products_csv(self.cfg.products_csv)
        conn = self._connect()
        try:
            upsert_products(conn, products)
        finally:
            conn.close()

    def read_watchlist_rows(self) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        with self.cfg.products_csv.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append({h: normalize_space(row.get(h, "")) for h in csv_headers()})
        return rows

    def write_watchlist_rows(self, rows: Sequence[Dict[str, str]]) -> None:
        with self.cfg.products_csv.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=csv_headers())
            writer.writeheader()
            for row in rows:
                out = {h: normalize_space(row.get(h, "")) for h in csv_headers()}
                if not out["enabled"]:
                    out["enabled"] = "1"
                writer.writerow(out)

    def upsert_links(self, entries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        with self._lock:
            rows = self.read_watchlist_rows()
            idx_by_url = {normalize_space(r.get("url", "")).lower(): i for i, r in enumerate(rows)}

            added = 0
            updated = 0
            touched: List[str] = []
            validation_warnings: List[str] = []
            for entry in entries:
                url = normalize_space(str(entry.get("url", "") or ""))
                if not url.startswith("http"):
                    if url:
                        validation_warnings.append(f"已跳过非HTTP链接：{url}")
                    continue

                brand = choose_brand_for_entry(entry, url)
                product_line = normalize_space(str(entry.get("product_line", "") or ""))
                product_name = normalize_space(str(entry.get("product_name", "") or ""))
                site_type = infer_site_type(url, normalize_space(str(entry.get("site_type", "") or "")))
                relationship = normalize_relationship(str(entry.get("relationship", "") or ""), brand, url)
                category = normalize_space(str(entry.get("category", "") or "")) or product_line
                channel = normalize_space(str(entry.get("channel", "") or "")) or infer_channel_from_url(url, site_type)
                country_market = normalize_space(str(entry.get("country_market", "") or "")) or "US"
                url_type = normalize_space(str(entry.get("url_type", "") or "")) or infer_url_type(url)
                priority = normalize_priority(str(entry.get("priority", "") or ""))
                watch_keywords = normalize_space(str(entry.get("watch_keywords", "") or ""))
                enabled = "1" if parse_bool(entry.get("enabled"), default=True) else "0"
                warning = url_validation_warning(url, url_type)
                if warning:
                    validation_warnings.append(warning)
                    enabled = "0"
                    priority = "C"
                product_id = normalize_space(str(entry.get("product_id", "") or "")) or fallback_product_id(url)

                key = url.lower()
                if key in idx_by_url:
                    row = rows[idx_by_url[key]]
                    row["product_id"] = row["product_id"] or product_id
                    if brand:
                        row["brand"] = brand
                    if product_line:
                        row["product_line"] = product_line
                    if product_name:
                        row["product_name"] = product_name
                    row["site_type"] = site_type
                    row["relationship"] = relationship
                    row["category"] = category or row.get("category", "")
                    row["channel"] = channel
                    row["country_market"] = country_market
                    row["url_type"] = url_type
                    row["priority"] = priority
                    if watch_keywords:
                        row["watch_keywords"] = watch_keywords
                    row["enabled"] = enabled
                    updated += 1
                    touched.append(row["product_id"])
                else:
                    rows.append(
                        {
                            "product_id": product_id,
                            "brand": brand,
                            "relationship": relationship,
                            "category": category,
                            "channel": channel,
                            "country_market": country_market,
                            "url_type": url_type,
                            "priority": priority,
                            "product_line": product_line,
                            "product_name": product_name,
                            "url": url,
                            "site_type": site_type,
                            "watch_keywords": watch_keywords,
                            "enabled": enabled,
                        }
                    )
                    idx_by_url[key] = len(rows) - 1
                    added += 1
                    touched.append(product_id)

            self.write_watchlist_rows(rows)
            self.sync_products_to_db()
            return {
                "added": added,
                "updated": updated,
                "touched_product_ids": touched,
                "validation_warnings": validation_warnings,
            }

    def latest_rows(
        self,
        search: str = "",
        brand: str = "",
        relationship: str = "",
        product_line: str = "",
        site_type: str = "",
        category: str = "",
        channel: str = "",
        url_type: str = "",
        priority: str = "",
        status: str = "all",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            base_rows = conn.execute(
                """
                SELECT
                    p.product_id,
                    p.brand,
                    p.relationship,
                    p.category,
                    p.channel,
                    p.country_market,
                    p.url_type,
                    p.priority,
                    p.product_line,
                    p.product_name,
                    p.url AS product_url,
                    p.site_type AS product_site_type,
                    p.enabled,
                    s.id AS snapshot_id,
                    s.captured_at,
                    s.url AS snapshot_url,
                    s.site_type AS snapshot_site_type,
                    s.success,
                    s.http_status,
                    s.error_message,
                    s.title,
                    s.price_value,
                    s.currency,
                    s.price_raw,
                    s.key_features_json,
                    s.key_specs_json,
                    s.market_signals_json
                FROM products p
                LEFT JOIN (
                    SELECT s1.*
                    FROM snapshots s1
                    INNER JOIN (
                        SELECT product_id, MAX(id) AS max_id
                        FROM snapshots
                        GROUP BY product_id
                    ) latest
                    ON s1.id = latest.max_id
                ) s
                ON s.product_id = p.product_id
                ORDER BY p.updated_at DESC, p.product_id
                """
            ).fetchall()

            rows: List[Dict[str, Any]] = []
            search_l = normalize_space(search).lower()
            brand_l = normalize_space(brand).lower()
            relationship_l = normalize_space(relationship).lower()
            line_l = normalize_space(product_line).lower()
            site_l = normalize_space(site_type).lower()
            category_l = normalize_space(category).lower()
            channel_l = normalize_space(channel).lower()
            url_type_l = normalize_space(url_type).lower()
            priority_l = normalize_space(priority).lower()
            status_l = normalize_space(status).lower()

            for row in base_rows:
                latest_success = bool(row["success"]) if row["snapshot_id"] is not None else False
                if status_l == "success" and not latest_success:
                    continue
                if status_l == "error" and latest_success:
                    continue

                source = row
                data_freshness = "fresh"
                fallback_captured_at = ""
                if row["snapshot_id"] is None:
                    data_freshness = "empty"
                elif not latest_success:
                    prev = conn.execute(
                        """
                        SELECT *
                        FROM snapshots
                        WHERE product_id = ? AND success = 1
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (row["product_id"],),
                    ).fetchone()
                    if prev is not None:
                        source = prev
                        data_freshness = "stale_from_last_success"
                        fallback_captured_at = normalize_space(str(prev["captured_at"] or ""))
                    else:
                        data_freshness = "empty"

                market = json_loads_dict(source["market_signals_json"] if "market_signals_json" in source.keys() else "{}")
                features = json_loads_list(source["key_features_json"] if "key_features_json" in source.keys() else "[]")
                specs = json_loads_dict(source["key_specs_json"] if "key_specs_json" in source.keys() else "{}")
                gtm = compute_gtm_signal_score(
                    normalize_space(str(row["product_site_type"] or source["site_type"] or "")),
                    {
                        "title": normalize_space(str(source["title"] or "")),
                        "price_value": source["price_value"],
                        "key_features": features,
                        "key_specs": specs,
                        "category_path": market.get("category_path"),
                        "brand_name": market.get("brand_name"),
                        "availability": market.get("availability"),
                        "sold_by": market.get("sold_by"),
                        "delivery_message": market.get("delivery_message"),
                        "asin": market.get("asin"),
                        "rating_value": market.get("rating_value"),
                        "review_count": market.get("review_count"),
                        "bsr_rank": market.get("bsr_rank"),
                        "item_model_number": market.get("item_model_number"),
                        "sku": market.get("sku"),
                    },
                )
                product_site_type = normalize_space(str(row["product_site_type"] or source["site_type"] or ""))
                major_params = build_major_params(
                    title=normalize_space(str(source["title"] or "")),
                    price_value=source["price_value"],
                    currency=normalize_space(str(source["currency"] or "")),
                    market=market,
                    specs=specs,
                    features=features,
                    site_type=product_site_type,
                )
                gtm_required_coverage = build_gtm_required_coverage(
                    title=normalize_space(str(source["title"] or "")),
                    price_value=source["price_value"],
                    market=market,
                    features=features,
                    specs=specs,
                    site_type=product_site_type,
                )
                feature_signals = build_feature_signal_map(normalize_space(str(source["title"] or "")), features, specs)
                gtm_required_missing_fields = [
                    item["key"] for item in GTM_REQUIRED_ROWS if not gtm_required_coverage.get(item["key"], False)
                ]
                row_relationship = normalize_relationship(
                    normalize_space(str(row["relationship"] or "")),
                    normalize_space(str(row["brand"] or "")),
                    normalize_space(str(row["product_url"] or source["url"] or "")),
                )
                row_url_type = normalize_space(str(row["url_type"] or "")) or infer_url_type(
                    normalize_space(str(row["product_url"] or source["url"] or ""))
                )

                view_row = {
                    "product_id": row["product_id"],
                    "brand": normalize_space(str(row["brand"] or "")),
                    "relationship": row_relationship,
                    "category": normalize_space(str(row["category"] or "")),
                    "channel": normalize_space(str(row["channel"] or "")),
                    "country_market": normalize_space(str(row["country_market"] or "")),
                    "url_type": row_url_type,
                    "priority": normalize_priority(str(row["priority"] or "")),
                    "product_line": normalize_space(str(row["product_line"] or "")),
                    "product_name": normalize_space(str(row["product_name"] or "")),
                    "url": normalize_space(str(row["product_url"] or source["url"] or "")),
                    "site_type": product_site_type,
                    "enabled": bool(row["enabled"]),
                    "latest_snapshot_id": row["snapshot_id"],
                    "latest_captured_at": normalize_space(str(row["captured_at"] or "")),
                    "success": latest_success,
                    "http_status": int(row["http_status"] or 0),
                    "error_message": normalize_space(str(row["error_message"] or "")),
                    "data_freshness": data_freshness,
                    "fallback_captured_at": fallback_captured_at,
                    "title": normalize_space(str(source["title"] or "")),
                    "price_value": source["price_value"],
                    "currency": normalize_space(str(source["currency"] or "")),
                    "availability": normalize_space(str(market.get("availability", ""))),
                    "rating_value": market.get("rating_value"),
                    "review_count": market.get("review_count"),
                    "sku": normalize_space(str(market.get("sku", ""))),
                    "gtm_score": gtm["score"],
                    "gtm_score_pct": gtm["score_pct"],
                    "gtm_missing_fields": gtm["missing_fields"],
                    "features_count": len(features),
                    "specs_count": len(specs),
                    "discount_percent": market.get("discount_percent"),
                    "compare_at_price": market.get("list_price_value"),
                    "market_signals": market,
                    "key_features": features,
                    "key_specs": specs,
                    "main_params": major_params,
                    "gtm_required_coverage": gtm_required_coverage,
                    "gtm_required_missing_fields": gtm_required_missing_fields,
                    "feature_signals": feature_signals,
                    "gtm_suggestions": build_gtm_suggestions(
                        row_relationship,
                        row_url_type,
                        gtm_required_missing_fields,
                        feature_signals,
                        major_params,
                    ),
                }

                if brand_l and brand_l not in view_row["brand"].lower():
                    continue
                if relationship_l and relationship_l != view_row["relationship"].lower():
                    continue
                if line_l and line_l not in view_row["product_line"].lower():
                    continue
                if site_l and site_l not in view_row["site_type"].lower():
                    continue
                if category_l and category_l not in view_row["category"].lower():
                    continue
                if channel_l and channel_l not in view_row["channel"].lower():
                    continue
                if url_type_l and url_type_l != view_row["url_type"].lower():
                    continue
                if priority_l and priority_l != view_row["priority"].lower():
                    continue
                if search_l:
                    search_blob = " ".join(
                        [
                            view_row["product_id"],
                            view_row["brand"],
                            view_row["relationship"],
                            view_row["category"],
                            view_row["channel"],
                            view_row["url_type"],
                            view_row["priority"],
                            view_row["product_line"],
                            view_row["product_name"],
                            view_row["title"],
                            view_row["url"],
                            " ".join(view_row["key_features"][:4]),
                        ]
                    ).lower()
                    if search_l not in search_blob:
                        continue
                rows.append(view_row)

            return rows[: max(1, int(limit))]
        finally:
            conn.close()

    def filter_values(self) -> Dict[str, List[str]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT brand, relationship, category, channel, product_line, site_type, url_type, priority
                FROM products
                ORDER BY brand, product_line
                """
            ).fetchall()
            brands = sorted({normalize_space(str(r["brand"] or "")) for r in rows if normalize_space(str(r["brand"] or ""))})
            relationships = sorted(
                {normalize_space(str(r["relationship"] or "")) for r in rows if normalize_space(str(r["relationship"] or ""))}
            )
            categories = sorted(
                {normalize_space(str(r["category"] or "")) for r in rows if normalize_space(str(r["category"] or ""))}
            )
            channels = sorted({normalize_space(str(r["channel"] or "")) for r in rows if normalize_space(str(r["channel"] or ""))})
            lines = sorted(
                {normalize_space(str(r["product_line"] or "")) for r in rows if normalize_space(str(r["product_line"] or ""))}
            )
            sites = sorted({normalize_space(str(r["site_type"] or "")) for r in rows if normalize_space(str(r["site_type"] or ""))})
            url_types = sorted(
                {normalize_space(str(r["url_type"] or "")) for r in rows if normalize_space(str(r["url_type"] or ""))}
            )
            priorities = sorted({normalize_priority(str(r["priority"] or "")) for r in rows if normalize_space(str(r["priority"] or ""))})
            return {
                "brands": brands,
                "relationships": relationships,
                "categories": categories,
                "channels": channels,
                "product_lines": lines,
                "site_types": sites,
                "url_types": url_types,
                "priorities": priorities,
            }
        finally:
            conn.close()

    def email_settings_status(self) -> Dict[str, Any]:
        settings = load_dashboard_settings()
        config, missing = load_smtp_config_from_env(default_from_name=DEFAULT_ALERT_FROM_NAME)
        return {
            "settings": {
                "alert_to": settings.get("alert_to", ""),
                "send_on_no_alerts": bool(settings.get("send_on_no_alerts", False)),
                "price_change_threshold_pct": settings.get("price_change_threshold_pct", DEFAULT_PRICE_ALERT_THRESHOLD_PCT),
                "spec_watch_fields": settings.get("spec_watch_fields", DEFAULT_SPEC_WATCH_FIELDS),
                "from_email": DEFAULT_ALERT_FROM_EMAIL,
                "from_name": DEFAULT_ALERT_FROM_NAME,
            },
            "smtp_configured": config is not None,
            "smtp_missing": missing,
        }

    def save_email_settings(
        self,
        alert_to: str,
        send_on_no_alerts: bool = False,
        price_change_threshold_pct: Optional[float] = None,
        spec_watch_fields: str = "",
    ) -> Dict[str, Any]:
        invalid = invalid_email_targets(alert_to)
        if invalid:
            raise ValueError(f"Invalid email target(s): {', '.join(invalid)}")
        settings = save_dashboard_settings(
            {
                "alert_to": alert_to,
                "send_on_no_alerts": send_on_no_alerts,
                "price_change_threshold_pct": price_change_threshold_pct or DEFAULT_PRICE_ALERT_THRESHOLD_PCT,
                "spec_watch_fields": spec_watch_fields or DEFAULT_SPEC_WATCH_FIELDS,
            }
        )
        return self.email_settings_status() | {"saved": settings}

    def product_history(self, product_id: str, limit: int = 30) -> Dict[str, Any]:
        pid = normalize_space(product_id)
        if not pid:
            return {"history": [], "alerts": []}
        conn = self._connect()
        try:
            hist_rows = conn.execute(
                """
                SELECT *
                FROM snapshots
                WHERE product_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (pid, max(1, int(limit))),
            ).fetchall()
            alert_rows = conn.execute(
                """
                SELECT *
                FROM alerts
                WHERE product_id = ?
                ORDER BY id DESC
                LIMIT 50
                """,
                (pid,),
            ).fetchall()

            history = []
            for r in hist_rows:
                market = json_loads_dict(r["market_signals_json"])
                history.append(
                    {
                        "id": int(r["id"]),
                        "captured_at": normalize_space(str(r["captured_at"] or "")),
                        "success": bool(r["success"]),
                        "http_status": int(r["http_status"] or 0),
                        "error_message": normalize_space(str(r["error_message"] or "")),
                        "title": normalize_space(str(r["title"] or "")),
                        "price_value": r["price_value"],
                        "currency": normalize_space(str(r["currency"] or "")),
                        "availability": normalize_space(str(market.get("availability", ""))),
                        "rating_value": market.get("rating_value"),
                        "review_count": market.get("review_count"),
                    }
                )

            alerts = []
            for r in alert_rows:
                alerts.append(
                    {
                        "id": int(r["id"]),
                        "alert_type": normalize_space(str(r["alert_type"] or "")),
                        "previous_value": normalize_space(str(r["previous_value"] or "")),
                        "current_value": normalize_space(str(r["current_value"] or "")),
                        "created_at": normalize_space(str(r["created_at"] or "")),
                        "details": json_loads_dict(r["details_json"] or "{}"),
                    }
                )
            return {"history": history, "alerts": alerts}
        finally:
            conn.close()

    def run_monitor_now(self, weekly_quota_pct: Optional[float] = None) -> Dict[str, Any]:
        with self._lock:
            quota = self.cfg.default_weekly_quota_pct if weekly_quota_pct is None else float(weekly_quota_pct)
            settings = load_dashboard_settings()
            summary = run_monitor(
                products_csv=self.cfg.products_csv,
                db_path=self.cfg.db_path,
                output_dir=self.cfg.output_dir,
                weekly_quota_pct=quota,
                config_path=self.cfg.config_path,
                price_change_threshold_pct=float_or_none(settings.get("price_change_threshold_pct")),
                spec_watch_keywords=parse_pipe_list(str(settings.get("spec_watch_fields", ""))),
            )
            summary["mail_result"] = self.send_summary_alert_email(summary)
            return summary

    def send_summary_alert_email(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        settings = load_dashboard_settings()
        targets = parse_alert_targets(str(settings.get("alert_to", "")))
        if not targets:
            return {"status": "SKIPPED", "reason": "NO_ALERT_TO"}
        if int(summary.get("alerts_count", 0) or 0) <= 0 and not bool(settings.get("send_on_no_alerts", False)):
            return {"status": "SKIPPED", "reason": "NO_ALERTS", "targets": targets}

        config, missing = load_smtp_config_from_env(default_from_name=DEFAULT_ALERT_FROM_NAME)
        if config is None:
            return {"status": "ERROR", "reason": "SMTP_CONFIG_MISSING", "missing": missing, "targets": targets}

        config.from_address = DEFAULT_ALERT_FROM_EMAIL
        config.from_name = DEFAULT_ALERT_FROM_NAME
        config.reply_to = DEFAULT_ALERT_FROM_EMAIL

        subject = build_email_subject(summary)
        body = build_email_body(summary)
        results = []
        has_error = False
        for target in targets:
            msg = build_email_message(config, target, subject, body)
            result = send_via_smtp(config, msg)
            results.append({"to": target, **result})
            if result.get("status") != "SENT":
                has_error = True
        return {
            "status": "ERROR" if has_error else "SENT",
            "from_email": DEFAULT_ALERT_FROM_EMAIL,
            "targets": targets,
            "results": results,
        }

    def send_test_alert_email(self, alert_to: str = "") -> Dict[str, Any]:
        targets = parse_alert_targets(alert_to) or parse_alert_targets(str(load_dashboard_settings().get("alert_to", "")))
        if not targets:
            raise ValueError("Please enter at least one alert recipient email.")
        invalid = [target for target in targets if not EMAIL_RE.match(target)]
        if invalid:
            raise ValueError(f"Invalid email target(s): {', '.join(invalid)}")

        summary = {
            "run_id": "TEST_EMAIL_PRICE_CHANGE",
            "status": "TEST_EMAIL",
            "quota_mode": "TEST",
            "weekly_quota_pct": self.cfg.default_weekly_quota_pct,
            "products_processed": 1,
            "success_count": 1,
            "error_count": 0,
            "alerts_count": 1,
            "alerts_by_type": {"PRICE_CHANGE": 1},
            "alerts": [
                {
                    "product_id": "test-price-alert",
                    "alert_type": "PRICE_CHANGE",
                    "previous_value": "119.99",
                    "current_value": "89.99",
                    "details": {
                        "note": "Dashboard test email. Real runs send this when monitored product price/spec/title signals change.",
                    },
                }
            ],
            "failures": [],
        }

        config, missing = load_smtp_config_from_env(default_from_name=DEFAULT_ALERT_FROM_NAME)
        if config is None:
            return {"status": "ERROR", "reason": "SMTP_CONFIG_MISSING", "missing": missing, "targets": targets}

        config.from_address = DEFAULT_ALERT_FROM_EMAIL
        config.from_name = DEFAULT_ALERT_FROM_NAME
        config.reply_to = DEFAULT_ALERT_FROM_EMAIL

        subject = "[Test] " + build_email_subject(summary)
        body = build_email_body(summary)
        results = []
        has_error = False
        for target in targets:
            msg = build_email_message(config, target, subject, body)
            result = send_via_smtp(config, msg)
            results.append({"to": target, **result})
            if result.get("status") != "SENT":
                has_error = True
        return {
            "status": "ERROR" if has_error else "SENT",
            "from_email": DEFAULT_ALERT_FROM_EMAIL,
            "targets": targets,
            "results": results,
        }

    def compare_watchlist_category(
        self,
        brands: Sequence[str],
        category_tokens: Optional[Sequence[str]] = None,
        limit: int = 8,
    ) -> Dict[str, Any]:
        selected_brands = [normalize_space(str(brand)) for brand in brands if normalize_space(str(brand))]
        selected_lower = {brand.lower() for brand in selected_brands}
        rows = self.latest_rows(limit=500)
        records: List[Dict[str, Any]] = []
        tokens = list(category_tokens or DEFAULT_MASSAGE_COMPARE_TOKENS)

        for row in rows:
            brand = normalize_space(str(row.get("brand", "")))
            if selected_lower and brand.lower() not in selected_lower:
                continue
            if not row_matches_category_tokens(row, tokens):
                continue
            record = dict(row)
            record["status_code"] = int(row.get("http_status") or 0)
            record["top_features"] = list(row.get("key_features") or [])[:6]
            record["top_specs"] = dict(list((row.get("key_specs") or {}).items())[:8])
            record["source"] = "watchlist_default_massage"
            records.append(record)

        priority_rank = {"A": 0, "B": 1, "C": 2}
        brand_rank = {brand.lower(): idx for idx, brand in enumerate(selected_brands)}
        records.sort(
            key=lambda item: (
                0 if item.get("enabled") else 1,
                0 if item.get("url_type") == "pdp" else 1,
                priority_rank.get(normalize_priority(str(item.get("priority", ""))), 9),
                0 if item.get("success") else 1,
                brand_rank.get(normalize_space(str(item.get("brand", ""))).lower(), 99),
                normalize_space(str(item.get("product_name", ""))).lower(),
            )
        )
        records = records[: max(1, int(limit))]

        return {
            "records": records,
            "tables": dashboard_field_config(),
            "field_config": dashboard_field_config(),
            "source": "watchlist_default_massage",
            "selected_brands": selected_brands,
            "category_tokens": tokens,
        }

    def compare_links(self, links: Sequence[str]) -> Dict[str, Any]:
        cleaned: List[str] = []
        for item in links:
            for url in extract_urls_from_text(item):
                if not any(existing.lower() == url.lower() for existing in cleaned):
                    cleaned.append(url)
        records: List[Dict[str, Any]] = []
        for url in cleaned[:6]:
            site_type = infer_site_type(url, "")
            brand = infer_known_brand_from_url(url) or infer_brand_from_url(url)
            url_type = infer_url_type(url)
            relationship = normalize_relationship("", brand, url)
            product = Product(
                product_id=fallback_product_id(url),
                brand=brand,
                product_line="",
                product_name="",
                url=url,
                site_type=site_type,
                watch_keywords=[],
                enabled=True,
                relationship=relationship,
                category="",
                channel=infer_channel_from_url(url, site_type),
                country_market="US",
                url_type=url_type,
                priority="B" if url_type == "pdp" else "C",
            )
            final_url, status_code, html = fetch_html(
                url,
                site_type=site_type,
                timeout_seconds=25,
                retry_count=2,
                retry_backoff_seconds=1.0,
                max_html_bytes=2200000,
            )
            snap = parse_snapshot(product, final_url, status_code, html, max_feature_items=12)
            market = dict(snap.market_signals or {})
            if market.get("list_price_value") is None:
                inferred_compare_at = infer_compare_at_price_from_html(html, snap.price_value)
                if inferred_compare_at is not None:
                    market["list_price_value"] = inferred_compare_at
                    if not normalize_space(str(market.get("list_price_currency", ""))):
                        market["list_price_currency"] = snap.currency
            if market.get("discount_percent") is None:
                list_price = float_or_none(market.get("list_price_value"))
                curr_price = float_or_none(snap.price_value)
                if list_price is not None and curr_price is not None and list_price >= curr_price > 0:
                    market["discount_percent"] = round(((list_price - curr_price) / list_price) * 100.0, 2)
            specs = dict(snap.key_specs or {})
            features = list(snap.key_features or [])
            gtm = compute_gtm_signal_score(
                snap.site_type,
                {
                    "title": snap.title,
                    "price_value": snap.price_value,
                    "key_features": features,
                    "key_specs": specs,
                    "category_path": market.get("category_path"),
                    "brand_name": market.get("brand_name"),
                    "availability": market.get("availability"),
                    "sold_by": market.get("sold_by"),
                    "delivery_message": market.get("delivery_message"),
                    "asin": market.get("asin"),
                    "rating_value": market.get("rating_value"),
                    "review_count": market.get("review_count"),
                    "bsr_rank": market.get("bsr_rank"),
                    "item_model_number": market.get("item_model_number"),
                    "sku": market.get("sku"),
                },
            )
            major_params = build_major_params(
                title=snap.title,
                price_value=snap.price_value,
                currency=snap.currency,
                market=market,
                specs=specs,
                features=features,
                site_type=snap.site_type,
            )
            gtm_required_coverage = build_gtm_required_coverage(
                title=snap.title,
                price_value=snap.price_value,
                market=market,
                features=features,
                specs=specs,
                site_type=snap.site_type,
            )
            feature_signals = build_feature_signal_map(snap.title, features, specs)
            gtm_required_missing_fields = [
                row["key"] for row in GTM_REQUIRED_ROWS if not gtm_required_coverage.get(row["key"], False)
            ]
            gtm_suggestions = build_gtm_suggestions(
                relationship,
                url_type,
                gtm_required_missing_fields,
                feature_signals,
                major_params,
            )
            records.append(
                {
                    "url": final_url,
                    "input_url": url,
                    "brand": brand,
                    "relationship": relationship,
                    "category": "",
                    "channel": product.channel,
                    "country_market": product.country_market,
                    "url_type": url_type,
                    "priority": product.priority,
                    "site_type": snap.site_type,
                    "status_code": status_code,
                    "title": snap.title,
                    "price_value": snap.price_value,
                    "currency": snap.currency,
                    "availability": market.get("availability", ""),
                    "rating_value": market.get("rating_value"),
                    "review_count": market.get("review_count"),
                    "sku": market.get("sku", ""),
                    "features_count": len(features),
                    "specs_count": len(specs),
                    "gtm_score": gtm["score"],
                    "gtm_score_pct": gtm["score_pct"],
                    "gtm_missing_fields": gtm["missing_fields"],
                    "top_features": features[:6],
                    "top_specs": dict(list(specs.items())[:8]),
                    "main_params": major_params,
                    "gtm_required_coverage": gtm_required_coverage,
                    "gtm_required_missing_fields": gtm_required_missing_fields,
                    "feature_signals": feature_signals,
                    "gtm_suggestions": gtm_suggestions,
                }
            )
        return {
            "records": records,
            "tables": dashboard_field_config(),
            "field_config": dashboard_field_config(),
        }


def parse_request_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    content_length = int(handler.headers.get("Content-Length", "0") or "0")
    if content_length <= 0:
        return {}
    raw = handler.rfile.read(content_length)
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def make_handler(service: DashboardService):
    class DashboardHandler(BaseHTTPRequestHandler):
        def _send_json(self, payload: Dict[str, Any], status_code: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, path: Path) -> None:
            if not path.exists():
                self._send_json({"ok": False, "error": f"Missing file: {path}"}, status_code=404)
                return
            raw = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _error(self, message: str, status_code: int = 400) -> None:
            self._send_json({"ok": False, "error": message}, status_code=status_code)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            route = parsed.path
            params = parse_qs(parsed.query or "")

            try:
                if route in {"/", "/index.html"}:
                    return self._send_html(DASHBOARD_HTML_PATH)
                if route == "/api/health":
                    return self._send_json({"ok": True})
                if route == "/api/field-config":
                    return self._send_json({"ok": True, "field_config": dashboard_field_config()})
                if route == "/api/filters":
                    return self._send_json({"ok": True, "filters": service.filter_values()})
                if route == "/api/email-settings":
                    return self._send_json({"ok": True, **service.email_settings_status()})
                if route == "/api/dashboard":
                    rows = service.latest_rows(
                        search=(params.get("search", [""])[0]),
                        brand=(params.get("brand", [""])[0]),
                        relationship=(params.get("relationship", [""])[0]),
                        product_line=(params.get("product_line", [""])[0]),
                        site_type=(params.get("site_type", [""])[0]),
                        category=(params.get("category", [""])[0]),
                        channel=(params.get("channel", [""])[0]),
                        url_type=(params.get("url_type", [""])[0]),
                        priority=(params.get("priority", [""])[0]),
                        status=(params.get("status", ["all"])[0]),
                        limit=int(params.get("limit", ["200"])[0]),
                    )
                    return self._send_json(
                        {"ok": True, "rows": rows, "count": len(rows), "field_config": dashboard_field_config()}
                    )
                if route == "/api/history":
                    pid = params.get("product_id", [""])[0]
                    limit = int(params.get("limit", ["30"])[0])
                    data = service.product_history(pid, limit=limit)
                    return self._send_json({"ok": True, **data})
            except Exception as exc:
                return self._send_json(
                    {"ok": False, "error": f"{exc.__class__.__name__}: {exc}", "traceback": traceback.format_exc()},
                    status_code=500,
                )
            self._error("Not found", status_code=404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            route = parsed.path
            body = parse_request_body(self)

            try:
                if route == "/api/add-links":
                    links = body.get("links", [])
                    if not isinstance(links, list):
                        return self._error("links must be a list")
                    entries = []
                    for item in links:
                        if isinstance(item, str):
                            entries.extend({"url": url} for url in extract_urls_from_text(item))
                        elif isinstance(item, dict):
                            urls = extract_urls_from_text(item.get("url", ""))
                            if len(urls) <= 1:
                                entries.append(item)
                            else:
                                for url in urls:
                                    cloned = dict(item)
                                    cloned["url"] = url
                                    entries.append(cloned)
                    result = service.upsert_links(entries)
                    return self._send_json({"ok": True, **result})

                if route == "/api/run-monitor":
                    quota = body.get("weekly_quota_pct")
                    summary = service.run_monitor_now(weekly_quota_pct=quota)
                    return self._send_json({"ok": True, "summary": summary})

                if route == "/api/email-settings":
                    alert_to = normalize_space(str(body.get("alert_to", "") or ""))
                    send_on_no_alerts = parse_bool(body.get("send_on_no_alerts"), default=False)
                    result = service.save_email_settings(
                        alert_to,
                        send_on_no_alerts=send_on_no_alerts,
                        price_change_threshold_pct=float_or_none(body.get("price_change_threshold_pct")),
                        spec_watch_fields=normalize_space(str(body.get("spec_watch_fields", "") or "")),
                    )
                    return self._send_json({"ok": True, **result})

                if route == "/api/test-email":
                    alert_to = normalize_space(str(body.get("alert_to", "") or ""))
                    result = service.send_test_alert_email(alert_to=alert_to)
                    return self._send_json({"ok": True, "mail_result": result})

                if route == "/api/compare-links":
                    links = body.get("links", [])
                    if not isinstance(links, list):
                        return self._error("links must be a list")
                    cleaned_links: List[str] = []
                    for item in links:
                        for url in extract_urls_from_text(item):
                            if not any(existing.lower() == url.lower() for existing in cleaned_links):
                                cleaned_links.append(url)
                    if cleaned_links:
                        result = service.compare_links(cleaned_links)
                    else:
                        brands = body.get("brands", [])
                        if not isinstance(brands, list):
                            brands = split_brand_candidates(brands)
                        raw_tokens = body.get("category_tokens", DEFAULT_MASSAGE_COMPARE_TOKENS)
                        category_tokens = raw_tokens if isinstance(raw_tokens, list) else parse_pipe_list(str(raw_tokens))
                        result = service.compare_watchlist_category(
                            brands=brands,
                            category_tokens=category_tokens or DEFAULT_MASSAGE_COMPARE_TOKENS,
                            limit=int(body.get("limit", 8) or 8),
                        )
                    return self._send_json({"ok": True, **result})
            except Exception as exc:
                return self._send_json(
                    {"ok": False, "error": f"{exc.__class__.__name__}: {exc}", "traceback": traceback.format_exc()},
                    status_code=500,
                )
            self._error("Not found", status_code=404)

    return DashboardHandler


def main() -> int:
    parser = argparse.ArgumentParser(description="Extensible competitor monitor dashboard server.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--port", type=int, default=8787, help="Port to bind.")
    parser.add_argument(
        "--share-lan",
        action="store_true",
        help="Bind to all network interfaces and print a same-WiFi share URL.",
    )
    parser.add_argument("--products-csv", default=str(DEFAULT_PRODUCTS_CSV), help="Path to products csv.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Path to sqlite db.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Outputs directory.")
    parser.add_argument("--config-path", default=str(DEFAULT_CONFIG_PATH), help="Monitor config path.")
    parser.add_argument(
        "--default-weekly-quota-pct",
        type=float,
        default=81.0,
        help="Default weekly quota percent used by dashboard run action.",
    )
    args = parser.parse_args()
    if args.share_lan:
        args.host = "0.0.0.0"

    cfg = DashboardConfig(
        products_csv=Path(args.products_csv),
        db_path=Path(args.db_path),
        output_dir=Path(args.output_dir),
        config_path=Path(args.config_path),
        default_weekly_quota_pct=args.default_weekly_quota_pct,
    )
    service = DashboardService(cfg)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(service))
    local_url = f"http://127.0.0.1:{args.port}"
    lan_ip = detect_lan_ip()
    lan_url = f"http://{lan_ip}:{args.port}" if lan_ip else ""
    print("DASHBOARD_SERVER_RUNNING")
    print(f"LOCAL_URL {local_url}")
    if args.host in {"0.0.0.0", "::"}:
        print(f"LAN_URL {lan_url or 'UNKNOWN_LAN_IP'}")
        print("LAN_NOTE Share the LAN_URL with trusted teammates on the same WiFi.")
    else:
        print(f"BIND_URL http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
