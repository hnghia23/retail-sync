"""Kiểm và khôi phục backup Postgres cửa hàng — docs/06 ngày 20, B02 I06, RPO ≤ 24h (docs/01).

Backup do sidecar `edge-backup-<store>` tạo (infra/backup/pg_backup.sh: ngay khi khởi động rồi
mỗi ngày, giữ 7 bản). Script này làm ba việc với chúng:

    uv run python infra/store_backup.py list    --store store-001
    uv run python infra/store_backup.py verify  --store store-001            # KHÔNG đụng DB thật
    uv run python infra/store_backup.py restore --store store-001 --file <tên> --yes

`verify` là cách duy nhất biết một backup DÙNG ĐƯỢC: khôi phục bản mới nhất vào một database tạm
cạnh database thật, so số dòng từng bảng, rồi xóa database tạm. Backup chưa từng khôi phục thử thì
chỉ là một file.

`restore` thay database thật bằng bản backup: dừng Edge API + sync worker của cửa hàng (không để
đơn nào ghi vào giữa lúc khôi phục), `pg_restore --clean`, bật lại. Hệ quả phải biết trước:
  - Mọi đơn SAU thời điểm backup mà chưa đồng bộ là MẤT (đó là RPO). Đơn đã đồng bộ vẫn còn ở
    trung tâm.
  - Sự kiện đã gửi sau thời điểm backup sẽ nằm lại trong outbox như "chưa gửi" và được gửi lại:
    vô hại, trung tâm trả `accepted` nhờ idempotency (AT-03).

Mọi lệnh Postgres chạy TRONG container sidecar (có sẵn biến PG* và pg_restore cùng bản server),
nên máy host không cần mật khẩu DB.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass

#: Bảng so khi `verify` — những gì cửa hàng không được mất.
TABLES = (
    "sale",
    "sale_line",
    "sale_payment",
    "point_ledger_local",
    "customer_local",
    "shift",
    "outbox",
)


@dataclass(frozen=True)
class Target:
    project: str
    store: str

    @property
    def sidecar(self) -> str:
        return f"{self.project}-edge-backup-{self.store}-1"

    @property
    def database(self) -> str:
        return "edge_" + self.store.replace("-", "_")

    def containers_to_stop(self) -> list[str]:
        return [
            f"{self.project}-edge-api-{self.store}-1",
            f"{self.project}-edge-sync-worker-{self.store}-1",
        ]


def sh(target: Target, script: str, *, check: bool = True) -> str:
    """Chạy `sh -c` trong sidecar backup của cửa hàng."""
    r = subprocess.run(
        ["docker", "exec", target.sidecar, "sh", "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if check and r.returncode != 0:
        raise SystemExit(f"lỗi trong {target.sidecar}: {r.stderr.strip()[-1500:]}")
    return r.stdout


def list_dumps(target: Target) -> list[str]:
    out = sh(target, f"ls -1 /backups/{target.store}/ 2>/dev/null || true")
    return sorted(line for line in out.split() if line.endswith(".dump"))


def latest(dumps: list[str]) -> str:
    """Tên chứa mốc UTC (`store-001-20260925T030000Z.dump`): bản mới nhất là bản cuối."""
    if not dumps:
        raise SystemExit("chưa có bản backup nào (sidecar edge-backup-* đã chạy chưa?)")
    return sorted(dumps)[-1]


def counts(target: Target, database: str) -> dict[str, int]:
    query = " UNION ALL ".join(f"SELECT '{t}', count(*) FROM {t}" for t in TABLES)
    out = sh(target, f'psql -d {database} -At -F "|" -c "{query}"')
    return {name: int(n) for name, n in (line.split("|") for line in out.splitlines() if line)}


def compare(live: dict[str, int], restored: dict[str, int]) -> list[str]:
    """Bản khôi phục chụp ở QUÁ KHỨ: được ít hơn DB thật (đơn sau thời điểm backup), không được
    nhiều hơn, và không được thiếu bảng. `outbox` được phép lệch cả hai chiều (có dọn định kỳ)."""
    problems = [f"thiếu bảng {t}" for t in TABLES if t not in restored]
    problems += [
        f"{t}: bản khôi phục {restored[t]} > DB thật {live[t]}"
        for t in TABLES
        if t != "outbox" and t in restored and t in live and restored[t] > live[t]
    ]
    return problems


def cmd_list(target: Target) -> int:
    for name in list_dumps(target):
        print(name)
    return 0


def cmd_verify(target: Target, file: str | None) -> int:
    name = file or latest(list_dumps(target))
    scratch = f"{target.database}_verify"
    path = f"/backups/{target.store}/{name}"
    sh(target, f"dropdb --if-exists {scratch} && createdb {scratch}")
    try:
        sh(target, f"pg_restore --no-owner --exit-on-error -d {scratch} {path}")
        live, restored = counts(target, target.database), counts(target, scratch)
    finally:
        sh(target, f"dropdb --if-exists {scratch}", check=False)
    print(f"{name}: khôi phục thử vào {scratch} → OK")
    for t in TABLES:
        print(f"  {t:<20} backup={restored.get(t, '—'):>8}  hiện tại={live.get(t, '—'):>8}")
    problems = compare(live, restored)
    for p in problems:
        print(f"  ✗ {p}")
    return 1 if problems else 0


def cmd_restore(target: Target, file: str, *, yes: bool) -> int:
    if not yes:
        raise SystemExit("restore THAY database thật của cửa hàng — thêm --yes nếu chắc chắn")
    path = f"/backups/{target.store}/{file}"
    if file not in list_dumps(target):
        raise SystemExit(f"không có {path}")
    stop = target.containers_to_stop()
    subprocess.run(["docker", "stop", *stop], check=True, capture_output=True)
    try:
        sh(
            target,
            "pg_restore --clean --if-exists --no-owner --exit-on-error"
            f" -d {target.database} {path}",
        )
    finally:
        subprocess.run(["docker", "start", *stop], check=False, capture_output=True)
    print(f"đã khôi phục {target.database} từ {file}; bật lại {', '.join(stop)}")
    for t, n in counts(target, target.database).items():
        print(f"  {t:<20} {n:>8}")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("command", choices=["list", "verify", "restore"])
    parser.add_argument("--store", default="store-001")
    parser.add_argument("--project", default="retail-sync")
    parser.add_argument(
        "--file", default=None, help="tên file trong /backups/<store>/ (mặc định: mới nhất)"
    )
    parser.add_argument("--yes", action="store_true", help="restore: xác nhận thay database thật")
    args = parser.parse_args(argv)
    target = Target(args.project, args.store)
    if args.command == "list":
        return cmd_list(target)
    if args.command == "verify":
        return cmd_verify(target, args.file)
    if not args.file:
        raise SystemExit("restore cần --file (xem `list`) — không khôi phục ngầm bản 'mới nhất'")
    return cmd_restore(target, args.file, yes=args.yes)


if __name__ == "__main__":
    sys.exit(main())
