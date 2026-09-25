"""stdout/stderr UTF-8 cho mọi CLI — thông điệp và `--help` của ta là tiếng Việt.

Trên Windows, khi output bị chuyển hướng (`| tee`, `> file`, chạy trong CI), Python mã hóa
stdout bằng code page của máy (cp1252) thay vì UTF-8, và CLI chết bằng `UnicodeEncodeError`
ngay ở chữ tiếng Việt đầu tiên — kể cả `--help`. Gọi hàm này đầu mỗi `__main__`.
"""

from __future__ import annotations

import sys

__all__ = ["utf8_stdio"]


def utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
