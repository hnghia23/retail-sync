"""Image của compose phải GHIM được, và test tích hợp dùng ĐÚNG image đó (ADR-005, nguồn cung).

Hai lần nguồn MinIO đổi chính sách (Docker Hub xóa repo 2026-09-24, quay.io bắt đăng nhập 2026-09):
máy dev vẫn chạy nhờ image nằm trong cache, chỉ máy SẠCH (CI) mới lộ ra. Test này không kéo được
image (không có Docker), nhưng chặn hai cách hỏng rẻ nhất: dùng `latest` (đổi âm thầm), và compose
với test lệch nhau (test xanh trên một image mà compose không dùng).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "infra" / "compose.yaml"
CONFTEST = ROOT / "tests" / "integration" / "conftest.py"

_IMAGE = re.compile(r"^\s*image:\s*(\S+)", re.MULTILINE)


def _compose_images() -> list[str]:
    return _IMAGE.findall(COMPOSE.read_text(encoding="utf-8"))


def test_every_compose_image_is_pinned() -> None:
    for image in _compose_images():
        if image.startswith("retail-sync-"):
            continue  # image tự build từ repo
        assert "@sha256:" in image or (":" in image.rsplit("/", 1)[-1]), image
        assert not image.endswith(":latest"), f"{image}: `latest` đổi âm thầm dưới chân"


def test_integration_tests_use_the_compose_minio_image() -> None:
    minio = next(i for i in _compose_images() if "minio" in i)
    [fixture] = re.findall(r'^MINIO_IMAGE = "([^"]+)"', CONFTEST.read_text("utf-8"), re.MULTILINE)
    assert fixture == minio
    # Nguồn hiện tại (Chainguard, bản miễn phí) chỉ có tag `latest` → chỉ digest mới ghim được.
    assert "@sha256:" in minio
