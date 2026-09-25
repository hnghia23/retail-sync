"""Sinh `grafana/dashboards/flow-health.json` — dashboard "Sức khỏe luồng dữ liệu" (docs/17 §6).

    uv run python infra/observability/build_dashboard.py

Sửa dashboard ở ĐÂY rồi chạy lại, không sửa tay file JSON (mọi panel cùng một khuôn).
`tests/unit/test_flow_dashboard.py` kiểm mọi metric trong panel đều có tiến trình phát.
"""

import itertools
import json
from pathlib import Path

# Bảng dữ liệu: một mục mỗi rule, cùng thứ tự với docs/08 §5.

DS = {"type": "prometheus", "uid": "prometheus"}
ids = itertools.count(1)
panels: list[dict] = []
y = 0
STORE = 'store_id=~"$store"'
EDGE_JOB = 'job=~"edge-api-${store:regex}"'


def row(title):
    global y
    panels.append(
        {
            "type": "row",
            "id": next(ids),
            "title": title,
            "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "panels": [],
        }
    )
    y += 1


def thresholds(steps):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in steps]}


def stat(title, expr, x, w, *, unit="none", steps=None, mappings=None, desc="", nodata="—"):
    panels.append(
        {
            "type": "stat",
            "id": next(ids),
            "title": title,
            "description": desc,
            "datasource": DS,
            "gridPos": {"h": 4, "w": w, "x": x, "y": y},
            "targets": [{"refId": "A", "datasource": DS, "expr": expr, "instant": True}],
            "options": {
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "colorMode": "background",
                "graphMode": "none",
                "textMode": "auto",
                "justifyMode": "center",
                "orientation": "auto",
            },
            "fieldConfig": {
                "defaults": {
                    "unit": unit,
                    "noValue": nodata,
                    "mappings": mappings or [],
                    "thresholds": thresholds(steps or [(None, "green")]),
                    "color": {"mode": "thresholds"},
                },
                "overrides": [],
            },
        }
    )


def ts(title, targets, x, w, *, unit="s", threshold=None, desc="", h=8):
    fc = {
        "unit": unit,
        "min": 0,
        "color": {"mode": "palette-classic-by-name"},
        "custom": {
            "lineWidth": 2,
            "fillOpacity": 0,
            "showPoints": "never",
            "spanNulls": False,
            "drawStyle": "line",
            "stacking": {"mode": "none", "group": "A"},
            "thresholdsStyle": {"mode": "dashed" if threshold is not None else "off"},
        },
    }
    if threshold is not None:
        fc["thresholds"] = thresholds([(None, "transparent"), (threshold, "red")])
    panels.append(
        {
            "type": "timeseries",
            "id": next(ids),
            "title": title,
            "description": desc,
            "datasource": DS,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "targets": [
                {"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": lf}
                for i, (e, lf) in enumerate(targets)
            ],
            "fieldConfig": {"defaults": fc, "overrides": []},
            "options": {
                "legend": {
                    "displayMode": "table",
                    "placement": "bottom",
                    "calcs": ["lastNotNull", "max"],
                },
                "tooltip": {"mode": "multi", "sort": "desc"},
            },
        }
    )


AUDIT_MAP = [
    {
        "type": "value",
        "options": {
            "0": {"text": "CONVERGED", "color": "green", "index": 0},
            "1": {"text": "CONVERGING", "color": "yellow", "index": 1},
            "2": {"text": "DIVERGED", "color": "red", "index": 2},
        },
    }
]
UP_MAP = [
    {
        "type": "value",
        "options": {
            "0": {"text": "MẤT NGUỒN", "color": "red", "index": 0},
            "1": {"text": "ĐỦ 3 NGUỒN", "color": "green", "index": 1},
        },
    }
]

# ── Tổng quan ──
row("Tổng quan — những con số phải về 0 (docs/17 §5, docs/08 §5)")
stat(
    "Đối soát (audit)",
    "max(audit_status)",
    0,
    3,
    mappings=AUDIT_MAP,
    steps=[(None, "green"), (1, "yellow"), (2, "red")],
    nodata="chưa chạy audit",
    desc="Kết quả `simulator audit` gần nhất (--otlp / --watch). DIVERGED = sự cố nghiêm trọng nhất.",
)
stat(
    "Lệch INV-4 đang mở",
    "max(reconcile_drift_count)",
    3,
    3,
    steps=[(None, "green"), (1, "red")],
    desc="reconciliation_drift chưa xử lý: số dư ≠ Σ sổ cái. > 0 = sai tiền của khách.",
)
stat(
    "Bảo trì trung tâm chạy cách đây",
    "max(reconcile_last_run_age_seconds)",
    6,
    3,
    unit="s",
    steps=[(None, "green"), (10800, "red")],
    nodata="chưa chạy lần nào",
    desc="central-maintenance (mỗi giờ): partition point_ledger + đối soát INV-4. Lệch = 0 chỉ có nghĩa khi job còn chạy. > 3 giờ = lịch đã chết.",
)
stat(
    "Dead-letter (trung tâm + cửa hàng)",
    "(sum(dead_letter_events) or vector(0)) + (sum(outbox_dead_lettered) or vector(0))",
    9,
    3,
    steps=[(None, "green"), (1, "red")],
    desc="Sự kiện không bao giờ được áp nữa. > 0 cảnh báo (docs/17 §6, S3).",
)
stat(
    "Outbox chờ gửi",
    f"sum(outbox_depth{{{STORE}}})",
    12,
    3,
    steps=[(None, "green"), (5000, "red")],
    desc="Tổng sự kiện cửa hàng chưa gửi được. > 5000 = đồng bộ đang tắc.",
)
stat(
    "Sự kiện chờ cũ nhất",
    f"max(outbox_oldest_unsent_age_seconds{{{STORE}}})",
    15,
    3,
    unit="s",
    steps=[(None, "green"), (3600, "red")],
    desc="Chỉ số sức khỏe quan trọng nhất (docs/08 §5): dữ liệu cửa hàng đang trễ bao lâu.",
)
stat(
    "Mart tụt sau bronze",
    "max(pipeline_transform_lag_seconds)",
    18,
    3,
    unit="s",
    steps=[(None, "green"), (4200, "red")],
    desc="Tuổi đơn đã nạp mà dbt chưa dựng. 0 = fact đã bắt kịp. Ngưỡng: 1 chu kỳ DAG (mặc định 1 giờ) + 10 phút.",
)
stat(
    "Bộ giám sát",
    "min(flow_monitor_source_up)",
    21,
    3,
    mappings=UP_MAP,
    steps=[(None, "red"), (1, "green")],
    nodata="flow-monitor không chạy",
    desc="flow-monitor đọc được Postgres trung tâm, lake và ClickHouse.",
)
y += 4
stat(
    "Partition point_ledger còn",
    "min(point_ledger_partition_months_ahead)",
    0,
    6,
    unit="none",
    steps=[(None, "red"), (2, "green")],
    nodata="flow-monitor không chạy",
    desc="Số tháng partition tạo sẵn (ràng buộc #2). < 2 = lịch central-maintenance đã ngừng; về 0 là mọi sự kiện có điểm bị từ chối.",
)
stat(
    "Cửa hàng im lặng > 1 giờ",
    'count(max by (store_id) (store_last_seen_age_seconds{watched="1"}) > 3600) or vector(0)',
    6,
    6,
    steps=[(None, "green"), (1, "red")],
    desc="stores_not_seen_recently (docs/08 §5). Worker rảnh gửi heartbeat mỗi 5 phút, nên im lặng 1 giờ = worker, mạng hoặc máy cửa hàng đã chết — kể cả ngoài giờ bán. Chỉ cửa hàng `watched` (FLOW_MONITOR_WATCH_STORES; cửa hàng ảo im lặng là bình thường).",
)
stat(
    "Lô bị từ chối: quá tải / rate limit (1 giờ)",
    "sum(increase(ingest_batches_refused_total[1h])) or vector(0)",
    12,
    6,
    steps=[(None, "green"), (1, "yellow")],
    desc="503 (semaphore) và 429 (một cửa hàng gửi quá nhanh) của POST /events. Cửa hàng lùi đúng Retry-After rồi gửi lại (không mất gì); kéo dài = trung tâm thiếu sức chứa.",
)
stat(
    "Tỉ lệ sự kiện gửi trùng (1 giờ)",
    'sum(increase(ingest_events_total{outcome="duplicate"}[1h])) / clamp_min(sum(increase(ingest_events_total[1h])), 1)',
    18,
    6,
    unit="percentunit",
    steps=[(None, "green")],
    desc="event_duplicate_rate (docs/08 §5): idempotency đang làm việc. Tăng vọt = cửa hàng gửi lại nhiều (mạng chập chờn, timeout).",
)
y += 4

# ── S1 ──
row("S1 — Ghi tại cửa hàng (POST /sales, p95 < 500 ms)")
SALES = f'{EDGE_JOB}, http_route="/api/v1/sales"'
ts(
    "POST /sales p95",
    [
        (
            f"histogram_quantile(0.95, sum by (job, le) (rate(http_server_request_duration_seconds_bucket{{{SALES}}}[$__rate_interval])))",
            "{{job}}",
        )
    ],
    0,
    12,
    threshold=0.5,
    desc="Instrumentation FastAPI (semconv HTTP ổn định). Đường đỏ: NFR-02 500 ms.",
)
ts(
    "Request /sales mỗi giây và lỗi 5xx",
    [
        (
            f"sum by (job) (rate(http_server_request_duration_seconds_count{{{SALES}}}[$__rate_interval]))",
            "{{job}}",
        ),
        (
            f'sum by (job) (rate(http_server_request_duration_seconds_count{{{SALES}, http_response_status_code=~"5.."}}[$__rate_interval]))',
            "{{job}} 5xx",
        ),
    ],
    12,
    12,
    unit="reqps",
)
y += 8

# ── S2 ──
row("S2 — Đẩy lên trung tâm (outbox → POST /events)")
ts(
    "Sự kiện chờ cũ nhất theo cửa hàng",
    [(f"max by (store_id) (outbox_oldest_unsent_age_seconds{{{STORE}}})", "{{store_id}}")],
    0,
    8,
    threshold=3600,
    desc="Đo ở Edge API, không ở worker (worker chết đúng là lúc outbox dồn). Không tính dead-letter.",
)
ts(
    "Outbox chờ gửi theo cửa hàng",
    [(f"max by (store_id) (outbox_depth{{{STORE}}})", "{{store_id}}")],
    8,
    8,
    unit="none",
    threshold=5000,
)
ts(
    "Sync worker: gửi được / lỗi đường truyền / bị từ chối",
    [
        (
            f"sum by (store_id) (rate(sync_events_sent_total{{{STORE}}}[$__rate_interval]))",
            "{{store_id}} gửi/s",
        ),
        (
            f"sum by (store_id) (rate(sync_push_failures_total{{{STORE}}}[$__rate_interval]))",
            "{{store_id}} lô lỗi đường truyền/s",
        ),
        (
            f"sum by (store_id, outcome) (rate(sync_events_rejected_total{{{STORE}}}[$__rate_interval]))",
            "{{store_id}} bị từ chối ({{outcome}})",
        ),
    ],
    16,
    8,
    unit="short",
    desc="Lỗi đường truyền (mất mạng/401/503) KHÁC trung tâm từ chối — không tính lượt thử (AT-02).",
)
y += 8

# ── S3 ──
row("S3 — Áp ở trung tâm (ingest, p95 < 1 s)")
EVENTS = 'job="central-api", http_route="/api/v1/events"'
ts(
    "POST /events p95 / p99",
    [
        (
            f"histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{{{EVENTS}}}[$__rate_interval])))",
            "p95",
        ),
        (
            f"histogram_quantile(0.99, sum by (le) (rate(http_server_request_duration_seconds_bucket{{{EVENTS}}}[$__rate_interval])))",
            "p99",
        ),
    ],
    0,
    6,
    threshold=1,
)
ts(
    "Sự kiện nhận theo kết cục",
    [
        ("sum by (outcome) (rate(ingest_events_total[$__rate_interval]))", "{{outcome}}"),
        (
            "sum by (reason) (rate(ingest_batches_refused_total[$__rate_interval]))",
            "lô bị từ chối: {{reason}}",
        ),
    ],
    6,
    6,
    unit="short",
    desc="duplicate = gửi lại (idempotency đang làm việc). rejected_permanent → dead-letter.",
)
ts(
    "Trễ đồng bộ theo cửa hàng",
    [(f"max by (store_id) (store_sync_lag_seconds{{{STORE}}})", "{{store_id}}")],
    12,
    6,
    threshold=900,
    desc="recorded_at - occurred_at của lô gần nhất (FR-C10). > 900 s = LAGGING.",
)
ts(
    "Lần cuối cửa hàng liên lạc",
    [(f"max by (store_id) (store_last_seen_age_seconds{{{STORE}}})", "{{store_id}}")],
    18,
    6,
    threshold=3600,
    desc="Lô sự kiện hoặc heartbeat (5 phút một lần khi rảnh). > 3600 s ở cửa hàng watched = cảnh báo.",
)
y += 8

# ── S4–S6 ──
row("S4 → S6 — Trích xuất, nạp, biến đổi (mép theo recorded_at)")
ts(
    "S4 mép trích xuất",
    [
        ("max by (table) (pipeline_extract_watermark_age_seconds)", "{{table}}"),
        ("max(pipeline_extract_horizon_lag_seconds)", "extract_horizon() trễ"),
    ],
    0,
    8,
    desc="Răng cưa theo chu kỳ DAG là bình thường. extract_horizon trễ ≫ 30 s = transaction treo giữ mép (bẫy 1).",
)
ts(
    "S5 mép nạp ClickHouse",
    [
        ("max by (table) (pipeline_load_watermark_age_seconds)", "{{table}}"),
        ("max(pipeline_loaded_until_age_seconds)", "đã nạp tới (min 7 bảng)"),
    ],
    8,
    8,
    desc="Trùng S4 sau mỗi lượt. Cách xa S4 = nạp chết giữa chừng.",
)
ts(
    "S6 đơn chờ dbt dựng",
    [("max(pipeline_transform_pending_sales)", "đơn chờ")],
    16,
    8,
    unit="none",
    desc="Đơn đã nạp dưới mép mà fact chưa có (bẫy 5). Về 0 sau mỗi lượt dbt.",
)
y += 8

# ── Tài nguyên ──
row("Tài nguyên — đĩa, kết nối, đường truyền (docs/08 §5)")
ts(
    "Đĩa cửa hàng đã dùng",
    [(f"max by (store_id) (disk_used_ratio{{{STORE}}})", "{{store_id}}")],
    0,
    6,
    unit="percentunit",
    threshold=0.75,
    desc="disk_used_pct (docs/08 §5): > 75% = nguy cơ chí tử. Cảnh báo phải tới TRƯỚC lần ghi đầu tiên lỗi (CH-4).",
)
ts(
    "Pool kết nối DB đang dùng",
    [
        (f"max by (store_id) (db_pool_used_ratio{{{STORE}}})", "{{store_id}}"),
        ('max by (service) (db_pool_used_ratio{service!=""})', "{{service}}"),
    ],
    6,
    6,
    unit="percentunit",
    threshold=0.8,
    desc="db_connections_used (docs/08 §5): > 80% pool = request sắp phải chờ kết nối.",
)
ts(
    "Postgres trung tâm: kết nối / max_connections",
    [("max(pg_connections_used_ratio)", "central-db")],
    12,
    6,
    unit="percentunit",
    threshold=0.7,
    desc="pg_connections_used (docs/08 §5): > 70% = cần PgBouncer (docs/02 §4, LD-4).",
)
ts(
    "Sync worker: lỗi đường truyền liên tiếp",
    [(f"max by (store_id) (sync_consecutive_failures{{{STORE}}})", "{{store_id}}")],
    18,
    6,
    unit="none",
    desc="circuit_breaker_state (docs/08 §5): > 0 suốt 10 phút = mất kết nối trung tâm kéo dài. Heartbeat giữ số này đúng cả khi cửa hàng không bán gì.",
)
y += 8

# ── Toàn luồng ──
row("Toàn luồng — độ tươi L2/L4 và đối soát xuyên tầng")
ts(
    "Độ tươi theo tầng và cửa hàng",
    [
        (f'max by (store_id) (flow_freshness_seconds{{layer="L2", {STORE}}})', "L2 {{store_id}}"),
        (f'max by (store_id) (flow_freshness_seconds{{layer="L4", {STORE}}})', "L4 {{store_id}}"),
    ],
    0,
    12,
    desc="now - occurred_at mới nhất. Giờ bán: L2 < 5 phút, L4 < 1 chu kỳ DAG + 10 phút (docs/17 §6).",
)
ts(
    "Audit: tầng tụt sau tầng tươi nhất",
    [(f"max by (layer) (audit_behind_seconds{{{STORE}}})", "{{layer}}")],
    12,
    6,
    desc="Từ simulator audit (--watch). 0 ở mọi tầng = đã bắt kịp. Đúng cả ở chế độ virtual.",
)
ts(
    "Audit: phát hiện theo mức",
    [("max by (level) (audit_findings)", "{{level}}")],
    18,
    6,
    unit="none",
    desc="CONVERGING = đang chảy (bình thường). DIVERGED > 0 = sự cố.",
)
y += 8

dash = {
    "uid": "retail-flow-health",
    "title": "Sức khỏe luồng dữ liệu",
    "tags": ["retail-sync", "flow"],
    "description": "docs/17 §6 — chỉ số theo chặng S1 → S6, độ tươi L2/L4, chênh đối soát.",
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "editable": True,
    "refresh": "30s",
    "time": {"from": "now-3h", "to": "now"},
    "templating": {
        "list": [
            {
                "name": "store",
                "label": "Cửa hàng",
                "type": "query",
                "datasource": DS,
                "query": {"query": "label_values(store_id)", "refId": "store"},
                "definition": "label_values(store_id)",
                "multi": True,
                "includeAll": True,
                "allValue": ".*",
                "current": {"text": "All", "value": "$__all"},
                "refresh": 2,
                "sort": 1,
            }
        ]
    },
    "annotations": {"list": []},
    "panels": panels,
}
out = Path(__file__).parent / "grafana" / "dashboards" / "flow-health.json"
out.write_text(json.dumps(dash, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(len(panels), "panels →", out)
