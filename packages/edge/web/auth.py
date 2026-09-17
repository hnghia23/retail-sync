"""Xác thực cho UI — cookie, khác với `edge.pos.adapters.auth` (Bearer header cho JSON API).

Hai bề mặt xác thực CÙNG kiểm một JWT bằng CÙNG `shared.security.decode_access_token` —
không có đường xác thực thứ hai nào được phát minh riêng cho UI. Chỉ khác chỗ lấy token:
JSON API đọc header `Authorization`, UI đọc cookie `access_token` (trình duyệt tự gửi lại
mỗi request, không cần JS quản lý token).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Cookie, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from edge.pos.domain.auth import UnknownRoleError, meets_minimum
from edge.settings import get_jwt_settings, get_settings
from shared.security import AccessTokenClaims, InvalidTokenError, decode_access_token
from shared.types import Role

COOKIE_NAME = "access_token"

#: Không `Secure=True`: spike/dev chạy HTTP thuần trên LAN. Bật khi triển khai có TLS.
_COOKIE_KWARGS: dict[str, object] = {"httponly": True, "samesite": "lax", "path": "/"}


class RequiresLoginError(Exception):
    """Chưa đăng nhập, token hỏng, hoặc không đủ vai trò — điều hướng về `/ui/login`.

    Ném ra thay vì `HTTPException` thường: một GET trang thường cần redirect 303, nhưng
    một POST của HTMX (`hx-post`) cần header `HX-Redirect` để HTMX tự điều hướng thay vì
    coi 303 là lỗi swap. Exception handler ở `edge.web.router` phân biệt hai trường hợp
    bằng header `HX-Request`.
    """


def set_login_cookie(response: Response, token: str) -> None:
    response.set_cookie(COOKIE_NAME, token, **_COOKIE_KWARGS)  # type: ignore[arg-type]


def clear_login_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def require_role_from_cookie(minimum: Role) -> Callable[..., Any]:
    """FastAPI dependency — bản UI của `edge.pos.adapters.auth.require_role`."""

    def dependency(
        request: Request,
        access_token: Annotated[str | None, Cookie()] = None,
    ) -> AccessTokenClaims:
        if not access_token:
            raise RequiresLoginError

        settings = get_settings()
        try:
            claims = decode_access_token(access_token, settings=get_jwt_settings())
        except InvalidTokenError as exc:
            raise RequiresLoginError from exc

        if claims.store_id != settings.store_id:
            raise RequiresLoginError

        try:
            allowed = meets_minimum(claims.role, minimum)
        except UnknownRoleError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        if not allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Cần vai trò tối thiểu '{minimum}'")

        _ = request  # giữ tham số cho chữ ký dependency rõ ràng, không dùng trực tiếp
        return claims

    return dependency


def requires_login_handler(request: Request, exc: Exception) -> Response:
    """Đăng ký ở `edge.main.create_app()`.

    Chữ ký `(Request, Exception)` — không phải `(Request, RequiresLoginError)` — vì
    Starlette khai kiểu `add_exception_handler` tổng quát theo `Exception`, bất kể lớp cụ
    thể đăng ký handler cho lớp nào. `add_exception_handler(RequiresLoginError, ...)` đảm
    bảo hàm này chỉ được gọi với đúng loại lỗi đó lúc chạy.

    HTMX request (`HX-Request: true`) → `200` kèm `HX-Redirect`: HTMX tự điều hướng cả
    trang. Request thường (F5, gõ URL) → `303` kèm `Location`: trình duyệt tự điều hướng.
    """
    _ = exc
    if request.headers.get("HX-Request") == "true":
        response = Response(status_code=status.HTTP_200_OK)
        response.headers["HX-Redirect"] = "/ui/login"
        return response
    return RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
