# Competitor Product Monitor (Extended)

This monitor tracks competitor product pages (Amazon US + independent stores), stores snapshots, detects multi-field changes, and sends alert emails.

中文文档请看: [README.zh-CN.md](README.zh-CN.md)

## What It Does (Extended Loop)

1. Read product links from `data/products.csv`.
2. Fetch each product page and extract:
   - title
   - price
   - key features (selling points)
   - key specs (if available)
   - GTM market signals (ASIN/category/brand/rating/reviews/BSR/availability/seller/fulfillment/coupon/media/variant etc.)
3. Save each run as a snapshot in SQLite.
4. Compare with the previous successful snapshot.
5. Trigger alerts when:
   - price changes beyond threshold
   - watched keywords in title are added/removed
   - feature bullets change
   - key specs change
   - market signals change (availability/seller/coupon/rating/reviews/badges etc.)
   - blocked page (Amazon captcha/blocked)
6. Generate alert digest and optionally send by SMTP email.
7. Generate run snapshot report (`.md`) and latest board export (`.csv`).

## Quota Brake Line

Use `--weekly-quota-pct` when running:

- `>= 50`: run core monitoring as usual.
- `30-49`: run core monitoring only (MVP mode).
- `< 30`: stop run early to avoid extra usage.

## Folder Layout

- `data/products.csv`: monitored product list
- `data/monitor_config.json`: rules and thresholds
- `data/monitor.db`: local snapshot database (auto-created)
- `scripts/run_monitor.py`: one-command monitor runner
- `scripts/monitor_core.py`: crawler/parser/storage/change logic
- `scripts/email_gateway.py`: SMTP loader + sender
- `scripts/export_latest_board.py`: latest state board exporter
- `outputs/`: run summaries and alert previews

## Quick Start

```bash
cd "/Users/WIll/Desktop/CODEX/tool/GTM search"
python3 scripts/run_monitor.py --weekly-quota-pct 81 --mail-mode prepare
```

This writes run artifacts to `outputs/` and does not send real email.

Each run now outputs:

- `outputs/run_summary_<run_id>.json`: run status + counts + inline product results
- `outputs/run_details_<run_id>.json`: full per-product detail
- `outputs/run_details_<run_id>.csv`: easy-to-read table (title/price/features/specs)
- `outputs/run_snapshot_<run_id>.md`: human-readable run report
  - includes `gtm_signal_score` (field completeness)
  - includes `data_freshness` (`fresh` / `stale_from_last_success` / `empty`)

Export latest board:

```bash
cd "/Users/WIll/Desktop/CODEX/tool/GTM search"
python3 scripts/export_latest_board.py
```

Outputs:

- `outputs/latest_board.csv`

## Expanded Dashboard (Link + Query)

Beyond MVP, you can run an extensible dashboard service:

- Link mode:
  - paste one or many product URLs
  - add to watchlist (`data/products.csv`) directly from UI
  - store true brand separately from relationship (`owned` / `competitor`)
  - validate link type; homepage/collection links are saved as low-priority disabled records until replaced by PDP links
  - run quick link-to-link comparison without changing watchlist
  - when the link box is empty, selected brands default to a massage-category comparison from the watchlist
- Query mode:
  - filter by brand / relationship / product line / category / channel / site type / url type / priority / status
  - keyword search across product metadata + latest snapshot
  - click a row to inspect snapshot history + recent alerts

Start dashboard server:

```bash
cd "/Users/WIll/Desktop/CODEX/tool/GTM search"
python3 scripts/dashboard_server.py --host 127.0.0.1 --port 8787
```

Open:

- `http://127.0.0.1:8787`

Share on the same WiFi:

```bash
cd "/Users/WIll/Desktop/CODEX/tool/GTM search"
./scripts/start_lan_dashboard.sh
```

The terminal prints:

- `LOCAL_URL`: open on this Mac.
- `LAN_URL`: share this link with trusted teammates on the same WiFi.

Notes:

- The Mac running the server must stay awake while teammates use it.
- If macOS asks for incoming network permission, allow it.
- Anyone with the `LAN_URL` can use the dashboard actions, so only share it inside a trusted network.
- Some office/guest WiFi networks block device-to-device access; use the main office WiFi or a hotspot if teammates cannot open the link.

The dashboard calls existing monitor engine APIs under the hood:

- `/api/add-links`
- `/api/run-monitor`
- `/api/dashboard`
- `/api/email-settings`
- `/api/history`
- `/api/compare-links`

Alert email behavior:

- Fill the dashboard "Alert Email" field and save it.
- Configure price alert threshold and watched spec fields in the same email panel.
- Use "Send Test Email" to verify the same SMTP sender path with a sample `PRICE_CHANGE` alert.
- When a monitor run detects price, title, feature, spec, or market-signal changes, the dashboard sends an alert summary to that custom recipient.
- Sender is fixed to `media@nekteck.com`.
- SMTP credentials are loaded from this project's `data/smtp_profile.local.env` first, then from the existing `competitor_pr_automation/data/smtp_profile.local.env` profile.

## Live Email Setup

Create `data/smtp_profile.local.env`:

```env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=your_username
SMTP_PASSWORD=your_password
MAIL_FROM_ADDRESS=alerts@yourbrand.com
MAIL_FROM_NAME=Competitor Monitor
MAIL_REPLY_TO=ops@yourbrand.com
SMTP_USE_SSL=false
SMTP_USE_STARTTLS=true
SMTP_TIMEOUT_SECONDS=30
```

Run in live mode:

```bash
cd "/Users/WIll/Desktop/CODEX/tool/GTM search"
python3 scripts/run_monitor.py --weekly-quota-pct 81 --mail-mode live --alert-to your_email@example.com
```

Use custom config file:

```bash
cd "/Users/WIll/Desktop/CODEX/tool/GTM search"
python3 scripts/run_monitor.py --config-path data/monitor_config.json --weekly-quota-pct 81 --mail-mode prepare
```

## Suggested Schedule (Near Real-Time)

For MVP, start with every 2-6 hours instead of minute-level polling.

Example cron (every 3 hours):

```cron
0 */3 * * * cd "/Users/WIll/Desktop/CODEX/tool/GTM search" && /usr/bin/python3 scripts/run_monitor.py --weekly-quota-pct 81 --mail-mode live --alert-to your_email@example.com
```

## Product Input Format

Edit `data/products.csv`:

- `product_id`: unique id (manual id, ASIN, or custom slug)
- `brand`: true brand name
- `relationship`: `owned` or `competitor`
- `category`: GTM category, for example `Foot Massager`
- `channel`: `Amazon`, `DTC`, `Walmart`, etc.
- `country_market`: market code, default `US`
- `url_type`: `pdp`, `collection`, `homepage`, or `other`
- `priority`: `A`, `B`, or `C`
- `product_line`: product line
- `product_name`: readable label
- `url`: product page url
- `site_type`: `amazon` or `independent` (optional, auto-detected when empty)
- `watch_keywords`: pipe-separated keywords like `gan|65w|travel`
- `enabled`: `1` or `0`

Replace the sample links with real competitor product URLs before production runs.

## Rule Config

Edit `data/monitor_config.json`:

- `enabled_alert_types`: choose alert types
- `price_change_threshold_pct`: percent threshold for price alerts
- `feature_change_min_items`: minimum feature diff count to trigger
- `spec_change_min_items`: minimum spec diff count to trigger
- `http_max_html_bytes`: html byte cap per request (for large Amazon pages)
- `http_retry_count`: request retries
- `http_retry_backoff_seconds`: retry backoff seconds
- `amazon_blocked_retry_count`: additional retries when Amazon returns captcha/blocked page

## Notes / Limitations

- Amazon anti-bot protections can block direct HTML crawling.
- When blocked, the run keeps the failure status but can surface the latest successful snapshot as stale fallback data for board readability.
- This MVP prioritizes reliability of the workflow over perfect field completeness.
- For stable Amazon coverage, plan to add a data API adapter (for example Keepa/Rainforest) in next phase.
