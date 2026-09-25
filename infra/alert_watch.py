"""Nhật ký cảnh báo — bằng chứng cho điều kiện cổng B "mọi ngưỡng cảnh báo đã KÍCH HOẠT THỬ được".

Chạy nền suốt đợt test hỗn loạn/tải; mỗi lần một rule đổi trạng thái (inactive → pending →
firing → inactive) ghi một dòng JSONL. `--report` đối chiếu nhật ký với danh sách rule trong
file provisioning VÀ với thông báo mà `alert-sink` đã thật sự nhận (contact point webhook).

    uv run python infra/alert_watch.py --out runs/alerts/timeline.jsonl          # chạy nền
    uv run python infra/alert_watch.py --report runs/alerts/timeline.jsonl       # tổng kết

Một rule chỉ tính là "đã kích hoạt thử" khi CẢ HAI cùng có: Grafana đưa nó sang `firing` và
`alert-sink` nhận thông báo `firing` của nó. Chỉ đổi màu trong Grafana thì chưa ai biết.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
RULES_FILE = ROOT / "infra/observability/grafana/provisioning/alerting/retail-sync.yaml"
GRAFANA = "http://localhost:3001"
SINK = "http://localhost:8089"


def provisioned() -> dict[str, str]:
    """Tiêu đề rule → uid, đọc từ file provisioning (JSON sau các dòng chú thích `#`)."""
    body = "\n".join(
        line
        for line in RULES_FILE.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    )
    rules = json.loads(body)["groups"][0]["rules"]
    return {r["title"]: r["uid"] for r in rules}


def states(client: httpx.Client) -> dict[str, str]:
    r = client.get(f"{GRAFANA}/api/prometheus/grafana/api/v1/rules", auth=("admin", "admin"))
    r.raise_for_status()
    out: dict[str, str] = {}
    for group in r.json()["data"]["groups"]:
        for rule in group["rules"]:
            # `health` = error/nodata là rule HỎNG (truy vấn lỗi) — ghi lại để khỏi nhầm với "ổn".
            health = rule.get("health", "ok")
            out[rule["name"]] = rule["state"] if health == "ok" else f"{rule['state']}/{health}"
    return out


def watch(out: Path, interval: float) -> int:
    titles = provisioned()
    out.parent.mkdir(parents=True, exist_ok=True)
    last: dict[str, str] = {}
    with httpx.Client(timeout=10) as client:
        while True:
            try:
                now = states(client)
            except httpx.HTTPError as exc:
                print(f"{datetime.now(UTC):%H:%M:%S} Grafana lỗi: {exc}", flush=True)
                time.sleep(interval)
                continue
            for name, state in now.items():
                if last.get(name) != state:
                    at = datetime.now(UTC).isoformat()
                    row = {
                        "at": at,
                        "uid": titles.get(name, "?"),
                        "rule": name,
                        "state": state,
                        "was": last.get(name),
                    }
                    with out.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    print(f"{at[11:19]} {row['uid']:<22} {row['was']} → {state}", flush=True)
            last = now
            time.sleep(interval)


def report(timeline: Path) -> int:
    titles = provisioned()
    fired: dict[str, str] = {}
    for line in timeline.read_text(encoding="utf-8").splitlines():
        change: dict[str, Any] = json.loads(line)
        if change["state"] == "firing":
            fired.setdefault(change["uid"], change["at"])
    notified: dict[str, str] = {}
    try:
        text = httpx.get(f"{SINK}/alerts", timeout=10).text
    except httpx.HTTPError:
        text = ""
    uid_of = dict(titles)
    for line in text.splitlines():
        row: dict[str, Any] = json.loads(line)
        if row.get("status") == "firing":
            uid = row.get("rule_uid") or uid_of.get(row.get("alertname") or "", "?")
            notified.setdefault(uid, row["received_at"])
    missing = []
    print(f"{'rule':<22} {'firing (Grafana)':<27} {'thông báo (alert-sink)':<27}")
    for title, uid in titles.items():
        a, b = fired.get(uid, "—"), notified.get(uid, "—")
        print(f"{uid:<22} {a[:19]:<27} {b[:19]:<27} {title}")
        if uid not in fired or uid not in notified:
            missing.append(uid)
    print(f"\n{len(titles) - len(missing)}/{len(titles)} rule đã kích hoạt thử (cả hai phía)")
    if missing:
        print("chưa:", ", ".join(missing))
    return 1 if missing else 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "runs" / "alerts" / "timeline.jsonl")
    parser.add_argument("--interval", type=float, default=20.0)
    parser.add_argument("--report", type=Path, default=None, metavar="TIMELINE")
    args = parser.parse_args(argv)
    if args.report:
        return report(args.report)
    return watch(args.out, args.interval)


if __name__ == "__main__":
    sys.exit(main())
