#!/bin/sh
# Backup Postgres CỬA HÀNG theo lịch — sidecar `edge-backup-<store>` (infra/compose.yaml).
#
# RPO cửa hàng ≤ 24h (docs/01 NFR). Outbox chưa gửi là dữ liệu CHƯA TỒN TẠI ở đâu khác (B02 I06):
# máy hỏng mà không có backup cục bộ là mất đúng phần đó. Ở production đây là cron trên máy cửa
# hàng, ghi ra đĩa thứ hai/USB; trong POC là một container ngủ giữa các lần dump.
#
# pg_dump -Fc: nén, khôi phục chọn lọc được, và `pg_restore --list` kiểm được file không hỏng.
# Dump là snapshot nhất quán (một transaction REPEATABLE READ) — không cần dừng bán hàng.
#
# Biến: PGHOST PGUSER PGPASSWORD PGDATABASE STORE_ID BACKUP_INTERVAL_SECONDS BACKUP_KEEP
set -eu

dir="/backups/${STORE_ID}"
mkdir -p "$dir"

while true; do
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    tmp="$dir/.${STORE_ID}-${stamp}.dump.part"
    out="$dir/${STORE_ID}-${stamp}.dump"
    # Ghi ra file tạm rồi đổi tên: một dump dở (container chết giữa chừng) không bao giờ trông
    # giống một backup hợp lệ.
    if pg_dump -Fc -f "$tmp" && pg_restore --list "$tmp" > /dev/null; then
        mv "$tmp" "$out"
        echo "backup OK $out $(du -h "$out" | cut -f1)"
    else
        rm -f "$tmp"
        echo "backup LỖI ${STORE_ID} ${stamp}" >&2
    fi
    # Giữ BACKUP_KEEP bản mới nhất (tên chứa mốc UTC nên sắp theo tên = sắp theo thời gian).
    ls -1 "$dir"/"${STORE_ID}"-*.dump 2>/dev/null | sort | head -n "-${BACKUP_KEEP}" | xargs -r rm -f
    sleep "${BACKUP_INTERVAL_SECONDS}"
done
