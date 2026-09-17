"""FastAPI dependency xác thực — docs/13 §1.

"Mọi route (trừ `/auth/login`, `/health`) yêu cầu JWT, token mang `store_id` +
`employee_id` + `role`." `require_role(minimum)` cưỡng chế đúng câu đó tại tầng route,
tập trung một chỗ thay vì lặp lại kiểm tra header ở từng handler.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Header, HTTPException, status

from edge.pos.domain.auth import UnknownRoleError, meets_minimum
from edge.settings import get_jwt_settings, get_settings
from shared.security import AccessTokenClaims, InvalidTokenError, decode_access_token
from shared.types import Role


def _bearer_token(authorization: Annotated[str | None, Header()] = None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Thiếu header Authorization: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return authorization.removeprefix("Bearer ").strip()


def require_role(minimum: Role) -> Callable[..., Any]:
    """Trả về một FastAPI dependency: giải mã token, kiểm `store_id`, kiểm vai trò.

    Trả closure thay vì một dependency cố định vì mỗi route cần một `minimum` khác nhau
    (`cashier` cho `/sales`, `manager` cho `/sales/{id}/void`...).
    """

    async def dependency(
        authorization: Annotated[str | None, Header()] = None,
    ) -> AccessTokenClaims:
        token = _bearer_token(authorization)
        settings = get_settings()
        try:
            claims = decode_access_token(token, settings=get_jwt_settings())
        except InvalidTokenError as exc:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, str(exc), headers={"WWW-Authenticate": "Bearer"}
            ) from exc

        # Token phát bởi một cửa hàng khác — không nên xảy ra trong vận hành bình thường
        # (mỗi cửa hàng ký bằng JWT_SECRET_KEY riêng), nhưng kiểm tường minh phòng khi
        # secret bị chia sẻ nhầm giữa các cửa hàng.
        if claims.store_id != settings.store_id:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token không thuộc cửa hàng này")

        try:
            allowed = meets_minimum(claims.role, minimum)
        except UnknownRoleError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        if not allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Cần vai trò tối thiểu '{minimum}'")
        return claims

    return dependency
