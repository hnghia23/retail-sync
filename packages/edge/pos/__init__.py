"""Module POS — bán hàng, trả hàng, sản phẩm, khách hàng (phía cửa hàng).

Ranh giới (ADR-004):
  - Gọi loyalty CHỈ qua `edge.loyalty.api` (implement `LoyaltyPort`).
  - KHÔNG `SELECT` bảng của loyalty.
  - Mọi lời gọi loyalty trong luồng bán hàng PHẢI có fallback: bug ở loyalty không được
    làm dừng việc bán hàng.

Tầng: `domain` (thuần) < `application` (use case) < `adapters` (DB/HTTP).
"""
