# 竞品商品监控工具（扩展版）

此工具用于监控竞品商品页面（美国亚马逊 + 独立站），保存快照，检测多字段变化，并发送预警邮件。

English README: [README.md](README.md)

## 功能概览（扩展闭环）

1. 从 `data/products.csv` 读取监控链接。
2. 抓取每个商品页并提取：
   - 标题
   - 价格
   - 核心卖点（features）
   - 关键参数（specs，如可获取）
   - GTM 市场信号（ASIN/类目/品牌/评分/评论/BSR/库存/卖家/履约/优惠券/媒体/变体等）
3. 将每次运行结果写入 SQLite 快照。
4. 与上一次成功快照对比。
5. 命中以下条件时触发预警：
   - 价格变化超过阈值
   - 标题关键词新增/移除
   - 卖点变更
   - 参数变更
   - 市场信号变更（库存/卖家/优惠/评分/评论/徽章等）
   - 页面被拦截（Amazon captcha/blocked）
6. 生成预警摘要，并可通过 SMTP 发邮件。
7. 输出运行快照报告（`.md`）和最新看板导出（`.csv`）。

## 配额刹车线

运行时使用 `--weekly-quota-pct`：

- `>= 50`：正常运行核心监控。
- `30-49`：仅运行核心闭环（MVP 模式）。
- `< 30`：提前停止，避免额外消耗。

## 目录结构

- `data/products.csv`：监控产品清单
- `data/monitor_config.json`：规则与阈值配置
- `data/monitor.db`：本地快照数据库（自动创建）
- `scripts/run_monitor.py`：一键运行入口
- `scripts/monitor_core.py`：抓取/解析/存储/变更检测逻辑
- `scripts/email_gateway.py`：SMTP 加载与发信
- `scripts/export_latest_board.py`：最新状态看板导出
- `feishu_approval_patrol/`：飞书审批巡检自动化用例说明
- `outputs/`：运行摘要和预警预览

## 快速开始

```bash
cd /Users/WIll/Desktop/CODEX/competitor_product_monitor_mvp
python3 scripts/run_monitor.py --weekly-quota-pct 81 --mail-mode prepare
```

上述命令会输出运行产物到 `outputs/`，但不会发送真实邮件。

每次运行会输出：

- `outputs/run_summary_<run_id>.json`：运行状态 + 统计 + 产品结果摘要
- `outputs/run_details_<run_id>.json`：完整逐品详情
- `outputs/run_details_<run_id>.csv`：可读表格（标题/价格/卖点/参数）
- `outputs/run_snapshot_<run_id>.md`：人类可读运行报告
  - 包含 `gtm_signal_score`（字段完整度）
  - 包含 `data_freshness`（`fresh` / `stale_from_last_success` / `empty`）

导出最新看板：

```bash
cd /Users/WIll/Desktop/CODEX/competitor_product_monitor_mvp
python3 scripts/export_latest_board.py
```

输出：

- `outputs/latest_board.csv`

## 扩展看板（链接 + 查询）

超出 MVP 后，可启用可扩展看板服务：

- 链接模式：
  - 支持粘贴单条或多条商品链接
  - 可直接在 UI 中加入监控清单（`data/products.csv`）
  - 品牌（true brand）与关系（`owned` / `competitor`）分开存储
  - 自动校验链接类型；homepage/collection 会先标记为低优先级禁用，待替换为 PDP
  - 支持不改清单时的“临时链接对比”
  - 当链接输入为空时，已选择品牌会默认按按摩类目做对比
- 查询模式：
  - 支持按 brand / relationship / product line / category / channel / site type / url type / priority / status 筛选
  - 支持对产品元信息 + 最新快照做关键词搜索
  - 支持点击行查看快照历史 + 最近告警

启动看板服务：

```bash
cd /Users/WIll/Desktop/CODEX/competitor_product_monitor_mvp
python3 scripts/dashboard_server.py --host 127.0.0.1 --port 8787
```

打开：

- `http://127.0.0.1:8787`

同 WiFi 共享：

```bash
cd /Users/WIll/Desktop/CODEX/competitor_product_monitor_mvp
./scripts/start_lan_dashboard.sh
```

终端会输出：

- `LOCAL_URL`：本机访问地址
- `LAN_URL`：同网络同事访问地址

说明：

- 提供服务的 Mac 需保持唤醒状态。
- 如果 macOS 弹出入站连接权限，请允许。
- 持有 `LAN_URL` 的人都可操作看板，请仅在可信网络分享。
- 若公司访客网络禁止设备互访，请切换办公主网或热点。

看板底层调用的 API：

- `/api/add-links`
- `/api/run-monitor`
- `/api/dashboard`
- `/api/email-settings`
- `/api/history`
- `/api/compare-links`

邮件预警行为：

- 在看板中填写并保存“Alert Email”。
- 在同一区域设置价格变动阈值与监控参数字段。
- 使用 “Send Test Email” 验证发信链路（示例 `PRICE_CHANGE` 告警）。
- 当监控到价格、标题、卖点、参数或市场信号变化时，会向自定义收件箱发送摘要。
- 发件人固定为 `media@nekteck.com`。
- SMTP 优先读取本项目 `data/smtp_profile.local.env`，其次读取现有 `competitor_pr_automation/data/smtp_profile.local.env`。

## 真实发信配置

创建 `data/smtp_profile.local.env`：

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

live 模式运行：

```bash
cd /Users/WIll/Desktop/CODEX/competitor_product_monitor_mvp
python3 scripts/run_monitor.py --weekly-quota-pct 81 --mail-mode live --alert-to your_email@example.com
```

使用自定义配置运行：

```bash
cd /Users/WIll/Desktop/CODEX/competitor_product_monitor_mvp
python3 scripts/run_monitor.py --config-path data/monitor_config.json --weekly-quota-pct 81 --mail-mode prepare
```

## 建议调度频率（准实时）

MVP 阶段建议先用每 2-6 小时执行一次，而不是分钟级轮询。

示例 cron（每 3 小时）：

```cron
0 */3 * * * cd /Users/WIll/Desktop/CODEX/competitor_product_monitor_mvp && /usr/bin/python3 scripts/run_monitor.py --weekly-quota-pct 81 --mail-mode live --alert-to your_email@example.com
```

## 产品输入格式

编辑 `data/products.csv`：

- `product_id`：唯一 ID（手工 ID、ASIN 或自定义 slug）
- `brand`：真实品牌名
- `relationship`：`owned` 或 `competitor`
- `category`：GTM 类目，例如 `Foot Massager`
- `channel`：`Amazon`、`DTC`、`Walmart` 等
- `country_market`：市场代码，默认 `US`
- `url_type`：`pdp`、`collection`、`homepage`、`other`
- `priority`：`A`、`B`、`C`
- `product_line`：产品线
- `product_name`：可读产品名
- `url`：产品页链接
- `site_type`：`amazon` 或 `independent`（可留空自动识别）
- `watch_keywords`：用 `|` 分隔关键词，如 `gan|65w|travel`
- `enabled`：`1` 或 `0`

正式运行前请先替换样例链接为真实竞品 PDP 链接。

## 规则配置

编辑 `data/monitor_config.json`：

- `enabled_alert_types`：选择启用的告警类型
- `price_change_threshold_pct`：价格告警阈值（百分比）
- `feature_change_min_items`：触发卖点变更告警的最小差异数
- `spec_change_min_items`：触发参数变更告警的最小差异数
- `http_max_html_bytes`：单请求 HTML 最大字节数（大页面保护）
- `http_retry_count`：请求重试次数
- `http_retry_backoff_seconds`：重试退避秒数
- `amazon_blocked_retry_count`：Amazon 被拦截时附加重试次数

## 说明与限制

- Amazon 反爬策略可能阻断直接 HTML 抓取。
- 被拦截时会保留失败状态，但可回退展示最近成功快照（标记为 stale），保证看板可读性。
- 当前版本优先保证流程可靠性，而非字段 100% 完整覆盖。
- 若要稳定覆盖 Amazon，下一阶段建议接入数据 API 适配层（如 Keepa/Rainforest）。
