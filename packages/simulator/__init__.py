"""Bộ giả lập dữ liệu — nguồn dữ liệu của giai đoạn A/B thay UI (ADR-010, docs/18).

Là một CLIENT của hệ thống bị thử, không phải một phần của nó: không import `edge` hay
`central` (hợp đồng `import-linter`). Đi vào qua HTTP như một quầy thu ngân thật, và đọc
thẳng Postgres chỉ ở bộ đối soát (`audit`) — quyền của người kiểm tra, không phải người ghi.
"""
