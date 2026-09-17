"""Ứng dụng tại cửa hàng — modular monolith (ADR-004).

Một tiến trình FastAPI, ba module: `pos` · `loyalty` · `reporting`.
Sync worker (`edge.sync`) là tiến trình RIÊNG — vòng đời khác hẳn (chạy nền, không phục
vụ request).

Ranh giới giữa các module được cưỡng chế bằng `import-linter` (xem `.importlinter`),
không bằng kỷ luật.
"""
