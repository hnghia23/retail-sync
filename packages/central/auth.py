"""Xác thực cửa hàng — docs/13 §2: "API key theo cửa hàng, service-to-service".

Không dùng JWT người dùng ở tầng này: người gọi là một TIẾN TRÌNH (sync worker), không
phải nhân viên, và nó chạy nền nhiều ngày liền — không có ai để đăng nhập lại khi token
hết hạn.

Khóa lưu dạng SHA-256, vì sao không phải Argon2: xem docstring migration `0003_central_ingest`.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text

_bearer = HTTPBearer(auto_error=False)

_LOOKUP = text(
    """
    SELECT store_id FROM store_credential
    WHERE key_sha256 = :digest AND revoked_at IS NULL
    """
)


def new_store_key() -> str:
    """32 byte ngẫu nhiên, mã hóa URL-safe (~43 ký tự)."""
    return secrets.token_urlsafe(32)


def digest_store_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuthenticatedStore:
    store_id: str


async def require_store(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthenticatedStore:
    """Dependency FastAPI: trả cửa hàng sở hữu khóa, hoặc `401`.

    Một thông báo lỗi cho mọi trường hợp (thiếu khóa, khóa sai, khóa đã thu hồi) — phân biệt
    chúng chỉ giúp người dò khóa biết mình đoán gần đúng tới đâu.
    """
    if credentials is None or not credentials.credentials:
        raise _unauthorized()

    async with request.app.state.session_factory() as session:
        store_id = (
            await session.execute(_LOOKUP, {"digest": digest_store_key(credentials.credentials)})
        ).scalar_one_or_none()

    if store_id is None:
        raise _unauthorized()
    return AuthenticatedStore(store_id=store_id)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Khóa cửa hàng không hợp lệ",
        headers={"WWW-Authenticate": "Bearer"},
    )
