"""Ứng dụng trung tâm — hợp nhất sự kiện từ mọi cửa hàng.

Ba module: `ingest` (nhận sự kiện) · `lookup` (tra khách + master data) · `reporting`.

Giữ ranh giới module tương tự edge, vì trung tâm có hồ sơ tải khác hẳn và CÓ THỂ cần
tách sớm hơn (ADR-004 "trường hợp đặc biệt"): `ingest` ghi nhiều và chịu đợt dội,
`lookup` đọc nhiều và cần độ trễ thấp. Nếu hai cái cạnh tranh tài nguyên ở T2+, tách
chúng là bước hiển nhiên.
"""
