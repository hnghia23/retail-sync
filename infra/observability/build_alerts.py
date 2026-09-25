"""Sinh `grafana/provisioning/alerting/retail-sync.yaml` — ngưỡng cảnh báo docs/08 §5 + docs/17 §6.

    uv run python infra/observability/build_alerts.py

Sửa ngưỡng ở ĐÂY (và ở docs/08 §5), không sửa tay file sinh ra. Cùng file: contact point
webhook → service `alert-sink` (compose, profile observability) — mọi thông báo được ghi lại
để test hỗn loạn kiểm "cảnh báo đã THẬT SỰ tới", không chỉ "rule đổi trạng thái".
`tests/unit/test_flow_dashboard.py` kiểm mọi rule dùng metric có tiến trình phát.
"""

import json
from pathlib import Path

# Bảng dữ liệu: một mục mỗi rule, cùng thứ tự với docs/08 §5.

RULES = [
    # uid, tiêu đề, PromQL (một số), toán tử, ngưỡng, for, severity, noData, mô tả
    (
        "rs-drift",
        "Lệch INV-4: số dư điểm ≠ Σ sổ cái",
        "max(reconcile_drift_count)",
        "gt",
        0,
        "0s",
        "critical",
        "OK",
        "Sai số điểm là sai tiền của khách (docs/08 §5 reconcile_drift_count). Xem bảng reconciliation_drift.",
    ),
    (
        "rs-audit-diverged",
        "Đối soát xuyên tầng DIVERGED",
        "max(audit_status)",
        "gt",
        1,
        "0s",
        "critical",
        "OK",
        "simulator audit báo tầng thừa/sai/mất (docs/17 §5). Xem runs/<id>/audit.json.",
    ),
    (
        "rs-partition",
        "Partition point_ledger sắp cạn",
        "min(point_ledger_partition_months_ahead)",
        "lt",
        2,
        "0s",
        "critical",
        "OK",
        "Ràng buộc #2: về 0 là mọi sự kiện có điểm bị từ chối. Kiểm service central-maintenance.",
    ),
    (
        "rs-dead-letter",
        "Có sự kiện vào dead-letter",
        "(sum(dead_letter_events) or vector(0)) + (sum(outbox_dead_lettered) or vector(0))",
        "gt",
        0,
        "0s",
        "warning",
        "OK",
        "Sự kiện không bao giờ được áp nữa (docs/17 §6 S3). Cần người xem dead_letter_event / outbox.",
    ),
    (
        "rs-outbox-oldest",
        "Dữ liệu cửa hàng trễ hơn 1 giờ",
        "max(outbox_oldest_unsent_age_seconds)",
        "gt",
        3600,
        "5m",
        "warning",
        "OK",
        "Chỉ số sức khỏe quan trọng nhất (docs/08 §5). Mạng cửa hàng hoặc trung tâm có vấn đề.",
    ),
    (
        "rs-outbox-depth",
        "Outbox tồn đọng > 5000",
        "max(outbox_depth)",
        "gt",
        5000,
        "5m",
        "warning",
        "OK",
        "Đồng bộ đang tắc (docs/08 §5 outbox_depth).",
    ),
    (
        "rs-sync-failure-rate",
        "Tỉ lệ gửi lỗi > 10% (5 phút)",
        "sum(rate(sync_push_failures_total[5m])) / clamp_min(sum(rate(sync_push_failures_total[5m])) + sum(rate(sync_batches_total[5m])), 1e-9)",
        "gt",
        0.1,
        "5m",
        "warning",
        "OK",
        "sync_failure_rate (docs/08 §5): lỗi ĐƯỜNG TRUYỀN, không tính lượt thử (AT-02).",
    ),
    (
        "rs-store-lag",
        "Cửa hàng trễ đồng bộ > 15 phút",
        "max(store_sync_lag_seconds)",
        "gt",
        900,
        "10m",
        "warning",
        "OK",
        "store_sync_lag_seconds (FR-C10). Chế độ virtual có occurred_at trong quá khứ: bỏ qua khi đang giả lập virtual.",
    ),
    (
        "rs-ingest-p95",
        "POST /events p95 > 1 s",
        'histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{job="central-api", http_route="/api/v1/events"}[5m])))',
        "gt",
        1,
        "10m",
        "warning",
        "OK",
        "Ngân sách S3 (docs/17 §6).",
    ),
    (
        "rs-sales-p95",
        "POST /sales p95 > 500 ms",
        'histogram_quantile(0.95, sum by (le, job) (rate(http_server_request_duration_seconds_bucket{job=~"edge-api-.*", http_route="/api/v1/sales"}[5m])))',
        "gt",
        0.5,
        "10m",
        "warning",
        "OK",
        "NFR-02 — chốt đơn tại quầy.",
    ),
    (
        "rs-horizon",
        "Trích xuất đứng lại: có transaction mở lâu",
        "max(pipeline_extract_horizon_lag_seconds)",
        "gt",
        300,
        "5m",
        "warning",
        "OK",
        "extract_horizon() bị một transaction mở lâu giữ lại (bẫy 1). An toàn nhưng luồng ngừng tiến. Xem pg_stat_activity.",
    ),
    (
        "rs-loaded-until",
        "Mép 'đã nạp tới' đứng > 3 giờ",
        "max(pipeline_loaded_until_age_seconds)",
        "gt",
        10800,
        "0s",
        "warning",
        "OK",
        "S4/S5 không chạy: > 2 chu kỳ DAG + cửa sổ (docs/17 §6). Xem Airflow.",
    ),
    (
        "rs-transform",
        "Mart tụt sau bronze > 70 phút",
        "max(pipeline_transform_lag_seconds)",
        "gt",
        4200,
        "0s",
        "warning",
        "OK",
        "S6: dbt không dựng kịp (1 chu kỳ DAG + 10 phút).",
    ),
    (
        "rs-maintenance",
        "Bảo trì trung tâm không chạy > 3 giờ",
        "max(reconcile_last_run_age_seconds)",
        "gt",
        10800,
        "0s",
        "warning",
        "OK",
        "central-maintenance chạy mỗi giờ: 3 lượt liền không chạy thì partition + đối soát INV-4 đều đứng.",
    ),
    (
        "rs-monitor",
        "Bộ giám sát mất nguồn hoặc không chạy",
        "min(flow_monitor_source_up)",
        "lt",
        1,
        "5m",
        "warning",
        "Alerting",
        "flow-monitor không đọc được Postgres trung tâm/lake/ClickHouse — hoặc không chạy (không có dữ liệu cũng báo).",
    ),
    # ── docs/08 §5, bổ sung 2026-09-25 (giai đoạn B): đủ mọi ngưỡng của bảng chỉ số ──
    (
        "rs-store-disk",
        "Đĩa cửa hàng > 75%",
        "max(disk_used_ratio)",
        "gt",
        0.75,
        "1m",
        "critical",
        "OK",
        "disk_used_pct (docs/08 §5): đầy đĩa là rủi ro chí tử ở cửa hàng (log, dữ liệu lịch sử). Phải tới TRƯỚC lần ghi đầu tiên lỗi (CH-4).",
    ),
    (
        "rs-db-pool",
        "Pool kết nối DB > 80%",
        "max(db_pool_used_ratio)",
        "gt",
        0.8,
        "5m",
        "warning",
        "OK",
        "db_connections_used (docs/08 §5): request sắp phải chờ kết nối; độ trễ tăng trước khi có lỗi.",
    ),
    (
        "rs-central-pg-conn",
        "Postgres trung tâm: kết nối > 70% max_connections",
        "max(pg_connections_used_ratio)",
        "gt",
        0.7,
        "1m",
        "warning",
        "OK",
        "pg_connections_used (docs/08 §5): chạm trần là mọi kết nối mới bị từ chối — cần PgBouncer (docs/02 §4, LD-4).",
    ),
    (
        "rs-circuit-open",
        "Cửa hàng mất kết nối trung tâm > 10 phút",
        "max(min_over_time(sync_consecutive_failures[10m]))",
        "gt",
        0,
        "0s",
        "warning",
        "OK",
        "circuit_breaker_state (docs/08 §5): mọi lần gửi (cả heartbeat) lỗi đường truyền suốt 10 phút. Cửa hàng vẫn bán (tự chủ); dữ liệu dồn ở outbox.",
    ),
    (
        "rs-reconcile-full",
        "Quét toàn bộ INV-4 không chạy > 35 ngày",
        "max(reconcile_full_last_run_age_seconds)",
        "gt",
        3024000,
        "0s",
        "warning",
        "Alerting",
        "docs/08 §4.2: lượt tăng dần bỏ qua khách không còn giao dịch; chỉ lần quét toàn bộ (mỗi 30 ngày, 01-05 giờ cửa hàng) bắt được snapshot hỏng của họ. Không có dữ liệu = chưa quét lần nào cũng báo.",
    ),
    (
        "rs-store-silent",
        "Cửa hàng im lặng > 1 giờ",
        'max(store_last_seen_age_seconds{watched="1"})',
        "gt",
        3600,
        "0s",
        "warning",
        "OK",
        "stores_not_seen_recently (docs/08 §5): worker gửi heartbeat mỗi 5 phút khi rảnh, nên im lặng 1 giờ = worker/mạng/máy cửa hàng đã chết. Chỉ cửa hàng watched (FLOW_MONITOR_WATCH_STORES).",
    ),
]

#: Webhook tới `alert-sink` (compose) — nơi ghi lại MỌI thông báo, cả lúc hết cảnh báo.
CONTACT_POINT = "retail-sync-webhook"


def rule(uid, title, expr, op, value, for_, severity, nodata, desc):
    return {
        "uid": uid,
        "title": title,
        "condition": "C",
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 600, "to": 0},
                "datasourceUid": "prometheus",
                "model": {
                    "refId": "A",
                    "expr": expr,
                    "instant": True,
                    "intervalMs": 1000,
                    "maxDataPoints": 43200,
                },
            },
            {
                "refId": "B",
                "datasourceUid": "__expr__",
                "model": {"refId": "B", "type": "reduce", "expression": "A", "reducer": "max"},
            },
            {
                "refId": "C",
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "C",
                    "type": "threshold",
                    "expression": "B",
                    "conditions": [{"evaluator": {"type": op, "params": [value]}}],
                },
            },
        ],
        "noDataState": nodata,
        "execErrState": "Error",
        "for": for_,
        "labels": {"severity": severity, "project": "retail-sync"},
        "annotations": {"summary": title, "description": desc},
    }


doc = {
    "apiVersion": 1,
    "groups": [
        {
            "orgId": 1,
            "name": "luong-du-lieu",
            "folder": "retail-sync",
            "interval": "1m",
            "rules": [rule(*r) for r in RULES],
        }
    ],
    "contactPoints": [
        {
            "orgId": 1,
            "name": CONTACT_POINT,
            "receivers": [
                {
                    "uid": "rs-webhook",
                    "type": "webhook",
                    "settings": {"url": "http://alert-sink:8080/alert", "httpMethod": "POST"},
                    "disableResolveMessage": False,
                }
            ],
        }
    ],
    # Cây định tuyến của CẢ org: mọi cảnh báo → webhook. Nhóm theo rule, gửi sau 10 giây (thấy
    # được trong test hỗn loạn), nhắc lại mỗi 4 giờ nếu vẫn còn.
    "policies": [
        {
            "orgId": 1,
            "receiver": CONTACT_POINT,
            "group_by": ["alertname"],
            "group_wait": "10s",
            "group_interval": "1m",
            "repeat_interval": "4h",
        }
    ],
}
header = (
    "# Ngưỡng cảnh báo docs/08 §5 + docs/17 §6 — provision vào Grafana của otel-lgtm lúc khởi động.\n"
    "# SINH TỪ infra/observability/build_alerts.py — sửa ở đó (và docs/08 §5), không sửa tay file này.\n"
    "# Không có rule cho độ tươi: ngoài giờ bán nó tăng là bình thường — cần lịch giờ mở cửa trước.\n"
    "# Thông báo: webhook → alert-sink (compose) ghi lại mọi lần kêu/hết kêu.\n"
    "# (YAML là tập cha của JSON: file này là JSON để không cần thư viện YAML khi sinh.)\n"
)
out = Path(__file__).parent / "grafana" / "provisioning" / "alerting" / "retail-sync.yaml"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(header + json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(len(RULES), "rules →", out)
