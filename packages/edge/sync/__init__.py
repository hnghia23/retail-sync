"""Sync worker — tiến trình RIÊNG, đẩy outbox lên trung tâm (ADR-003).

Vì sao tách tiến trình: vòng đời khác hẳn API (chạy nền, không phục vụ request). Worker
chết thì outbox dồn lại, không mất gì (docs/03 §5).

Đây là tầng **vận chuyển thuần** — không import `pos`/`loyalty`, không hiểu nội dung sự
kiện. Cưỡng chế bởi import-linter.
"""
