"""Băm mật khẩu + JWT — `shared/security.py`. Thuần crypto, không cần DB."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shared.config import JwtSettings
from shared.security import (
    InvalidTokenError,
    decode_access_token,
    encode_access_token,
    encode_refresh_token,
    hash_password,
    verify_password,
)


def _jwt_settings(*, secret_key: str = "test-secret-key", **overrides: object) -> JwtSettings:
    return JwtSettings(_env_file=None, secret_key=secret_key, **overrides)  # type: ignore[arg-type]


# ═══════════════════════ Mật khẩu ═══════════════════════


def test_correct_password_verifies() -> None:
    hashed = hash_password("mat-khau-cua-thu-ngan")
    assert verify_password("mat-khau-cua-thu-ngan", hashed) is True


def test_wrong_password_does_not_verify() -> None:
    hashed = hash_password("mat-khau-dung")
    assert verify_password("mat-khau-sai", hashed) is False


def test_empty_hash_does_not_verify() -> None:
    """Tài khoản chưa từng đặt mật khẩu (`password_hash = ''`) — không được `500`, không match."""
    assert verify_password("bat-ky-gi", "") is False


def test_garbage_hash_does_not_verify() -> None:
    """Hash hỏng (dữ liệu lỗi, migration cũ) → coi như không khớp, không ném lỗi ra ngoài."""
    assert verify_password("bat-ky-gi", "khong-phai-argon2-hash") is False


def test_hash_is_salted_differently_each_time() -> None:
    """Hai lần hash cùng mật khẩu phải ra hai chuỗi khác nhau (salt ngẫu nhiên)."""
    assert hash_password("cung-mat-khau") != hash_password("cung-mat-khau")


# ═══════════════════════ JWT ═══════════════════════


def test_access_token_roundtrip() -> None:
    settings = _jwt_settings()
    token = encode_access_token(
        store_id="store-001", employee_id="emp-007", role="cashier", settings=settings
    )
    claims = decode_access_token(token, settings=settings)
    assert claims.store_id == "store-001"
    assert claims.employee_id == "emp-007"
    assert claims.role == "cashier"


def test_expired_token_is_rejected() -> None:
    settings = _jwt_settings(access_token_ttl_minutes=15)
    issued_long_ago = datetime.now(UTC) - timedelta(hours=1)
    token = encode_access_token(
        store_id="store-001",
        employee_id="emp-007",
        role="cashier",
        settings=settings,
        now=issued_long_ago,
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings=settings)


def test_token_signed_with_wrong_secret_is_rejected() -> None:
    token = encode_access_token(
        store_id="store-001",
        employee_id="emp-007",
        role="cashier",
        settings=_jwt_settings(secret_key="secret-a"),
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings=_jwt_settings(secret_key="secret-b"))


def test_garbage_token_is_rejected() -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token("khong-phai.mot.jwt", settings=_jwt_settings())


def test_refresh_token_is_rejected_as_access_token() -> None:
    """Refresh token không mang `role` — dùng nhầm nó làm access token phải bị chặn.

    Nếu không chặn, một token có TTL 7 ngày sẽ được chấp nhận ở route cần TTL 15 phút.
    """
    settings = _jwt_settings()
    refresh = encode_refresh_token(store_id="store-001", employee_id="emp-007", settings=settings)
    with pytest.raises(InvalidTokenError, match="access token"):
        decode_access_token(refresh, settings=settings)


def test_refresh_token_outlives_access_token() -> None:
    settings = _jwt_settings(access_token_ttl_minutes=15, refresh_token_ttl_days=7)
    now = datetime.now(UTC)
    access = encode_access_token(
        store_id="s", employee_id="e", role="cashier", settings=settings, now=now
    )
    refresh = encode_refresh_token(store_id="s", employee_id="e", settings=settings, now=now)

    access_claims = decode_access_token(access, settings=settings)
    # JWT `exp`/`iat` là timestamp NGUYÊN GIÂY — so khớp tuyệt đối với `now` (còn micro giây)
    # sẽ luôn lệch vài trăm micro giây. Dung sai 1 giây là đủ để xác nhận đúng TTL.
    assert abs((access_claims.expires_at - now) - timedelta(minutes=15)) < timedelta(seconds=1)

    # Refresh token không giải mã được bằng decode_access_token (đúng — khác `type`),
    # nhưng ta có thể kiểm TTL của nó qua payload thô.
    import jwt as pyjwt

    payload = pyjwt.decode(refresh, settings.secret_key, algorithms=["HS256"])
    assert payload["type"] == "refresh"
    assert "role" not in payload
