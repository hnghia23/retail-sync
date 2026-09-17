"""Nhận sự kiện từ sync worker của các cửa hàng.

Chốt chặn idempotency của TOÀN hệ thống nằm ở đây (docs/12 §5). Không có `event_type`
nào được miễn bước này — kể cả loại "chắc chắn không lặp".
"""
