"""Nơi nhận thông báo cảnh báo của Grafana (contact point webhook) — service `alert-sink`.

Cảnh báo chỉ đổi màu trong Grafana thì chưa phải cảnh báo: phải có chỗ NHẬN. Ở production đó là
chat/email/điện thoại trực; ở POC là tiến trình này — ghi mọi thông báo (cả lúc hết kêu) ra
stdout (`docker logs`) và một file JSONL, để test hỗn loạn kiểm được "cảnh báo đã THẬT SỰ tới".

    POST /alert     ← Grafana (payload webhook chuẩn: status, alerts[].labels.alertname...)
    GET  /alerts    → mọi thông báo đã nhận, mỗi dòng một JSON (cho test)

Chỉ thư viện chuẩn: chạy được trong bất kỳ image Python nào, không phụ thuộc gói của repo.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

LOG = Path(os.environ.get("ALERT_SINK_FILE", "/data/alerts.jsonl"))
_lock = Lock()


_UID = re.compile(r"/alerting/grafana/([^/]+)/view")


def _rule_uid(url: str) -> str | None:
    match = _UID.search(url)
    return match.group(1) if match else None


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # tên do http.server quy định
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self.send_response(400)
            self.end_headers()
            return
        received = datetime.now(UTC).isoformat()
        rows = [
            {
                "received_at": received,
                "status": alert.get("status"),
                "alertname": alert.get("labels", {}).get("alertname"),
                # Webhook của Grafana không mang uid của rule trong nhãn; nó nằm trong
                # `generatorURL` (…/alerting/grafana/<uid>/view).
                "rule_uid": _rule_uid(alert.get("generatorURL") or ""),
                "severity": alert.get("labels", {}).get("severity"),
                "starts_at": alert.get("startsAt"),
                "ends_at": alert.get("endsAt"),
                "values": alert.get("values"),
            }
            for alert in body.get("alerts", [])
        ]
        with _lock:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            with LOG.open("a", encoding="utf-8") as out:
                for row in rows:
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")
        for row in rows:
            print(json.dumps(row, ensure_ascii=False), flush=True)
        self.send_response(200)
        self.end_headers()

    def do_GET(self) -> None:
        if self.path.rstrip("/") != "/alerts":
            self.send_response(404)
            self.end_headers()
            return
        with _lock:
            data = LOG.read_bytes() if LOG.exists() else b""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass  # mỗi thông báo đã in một dòng JSON ở trên; access log chỉ là nhiễu


def main() -> int:
    port = int(os.environ.get("ALERT_SINK_PORT", "8080"))
    print(f"alert-sink: lắng nghe :{port}, ghi {LOG}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()  # noqa: S104
    return 0


if __name__ == "__main__":
    sys.exit(main())
