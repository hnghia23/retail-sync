"""Cấu hình Jinja2 dùng chung cho mọi route UI."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _money(value: int) -> str:
    """`150000` → `"150.000đ"`. Định dạng Việt Nam: `.` ngăn cách nghìn."""
    return f"{value:,}".replace(",", ".") + "đ"


templates.env.filters["money"] = _money
