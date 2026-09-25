"""Backoff của đường đồng bộ — tách khỏi `worker.py` để là module THUẦN.

Bộ giả lập chế độ `virtual` (docs/18 §3) dùng lại đúng hàm này cho cửa hàng ảo, để thứ được đo
là hành vi lùi của worker thật chứ không phải một bản chép. `worker.py` kéo theo SQLAlchemy và
DB cửa hàng; module này thì không.
"""

from __future__ import annotations

import random

__all__ = ["next_backoff"]


def next_backoff(attempts: int, *, max_seconds: float, base: float = 1.0) -> float:
    """Backoff lũy thừa + jitter toàn phần.

    Jitter *toàn phần* (random trong [0, delay]) chứ không phải ±10%: khi N cửa hàng mất
    mạng cùng lúc rồi nối lại cùng lúc, jitter nhỏ vẫn để chúng đồng pha (CH-6).
    """
    delay = min(base * (2**attempts), max_seconds)
    return random.uniform(0, delay)  # noqa: S311 — jitter, không phải mục đích mật mã
