"""Route HTTP của module POS — docs/13 §1, §3.

Tầng này làm đúng năm việc và không làm gì hơn:
  1. Dịch JSON ↔ kiểu của use case.
  2. **Mở transaction** và dựng adapter trên cùng một session.
  3. Dịch `SaleRejectedError.code` sang mã HTTP.
  4. Phân giải khách hàng *trước* khi vào use case.
  5. Xác thực JWT qua `require_role()` — docs/13 §1: mọi route trừ `/auth/login`, `/health`.

Việc (2) là quan trọng nhất: `async with transaction(...)` bao trọn lời gọi use case, nên
`sale` + `sale_line` + `sale_payment` + `point_ledger_local` + `outbox` cùng commit hoặc
cùng rollback (docs/03 §4.1). Không adapter nào tự commit.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from edge.loyalty.api import CustomerSnapshot, build_loyalty_service
from edge.pos.adapters.auth import require_role
from edge.pos.adapters.postgres import (
    PostgresEmployees,
    PostgresProductCatalog,
    PostgresSaleRepository,
    PostgresShifts,
)
from edge.pos.application.login import LoginCommand, LoginRejectedError, login
from edge.pos.application.place_sale import (
    PlaceSaleCommand,
    RequestedLine,
    RequestedPayment,
    SaleRejectedError,
    place_sale,
)
from edge.settings import get_jwt_settings, get_pricing_rules, get_settings
from shared.db import transaction
from shared.outbox import OutboxPublisher
from shared.security import AccessTokenClaims
from shared.types import Money, PaymentMethod, Role, utcnow

router = APIRouter(tags=["pos"])

#: `SaleRejectedError.code` → HTTP. Bảng này là bản dịch trực tiếp của docs/13 §3.
_HTTP_STATUS: dict[str, int] = {
    "EMPTY_CART": status.HTTP_400_BAD_REQUEST,
    "PAYMENT_MISMATCH": status.HTTP_400_BAD_REQUEST,
    "INVALID_PRICING": status.HTTP_400_BAD_REQUEST,
    "PRODUCT_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "SHIFT_CLOSED": status.HTTP_409_CONFLICT,
}


# ═══════════════════════ Hợp đồng HTTP ═══════════════════════


class SaleLineRequest(BaseModel):
    product_id: str
    quantity: int = Field(gt=0, description="Đơn bán luôn dương; trả hàng đi qua POST /returns")


class SalePaymentRequest(BaseModel):
    method: PaymentMethod
    amount: Money
    reference: str | None = None


class PlaceSaleRequest(BaseModel):
    """Body `POST /sales` — docs/13 §3."""

    employee_id: str
    shift_id: uuid.UUID
    lines: list[SaleLineRequest] = Field(min_length=1)
    payments: list[SalePaymentRequest] = Field(min_length=1)
    customer_id: uuid.UUID | None = None
    tendered_amount: Money | None = None
    promotion_id: str | None = None
    promo_discount_pct: int = Field(default=0, ge=0, le=100)


class PlaceSaleResponse(BaseModel):
    sale_id: uuid.UUID
    status: str
    subtotal: Money
    discount_tier: Money
    discount_promo: Money
    total: Money
    change_amount: Money | None
    tier_discount_pct: int
    #: `null` = module loyalty suy giảm, đơn vẫn hoàn tất (ADR-004).
    #: Khác hẳn `0` = khách vãng lai hoặc đơn quá nhỏ để ra điểm.
    points_earned: int | None


class ProductResponse(BaseModel):
    product_id: str
    sku: str
    barcode: str | None
    name: str
    unit_price: Money
    #: Tuổi của bản sao master data. UI cảnh báo khi > 24h (B02 E03).
    synced_hours_ago: float


class LoginRequest(BaseModel):
    employee_id: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 — RFC 6750, không phải mật khẩu
    role: Role
    expires_in_minutes: int


# ═══════════════════════ Route ═══════════════════════


@router.post("/auth/login", response_model=LoginResponse, summary="FR-P01 — đăng nhập")
async def login_route(request: Request, body: LoginRequest) -> LoginResponse:
    """Route DUY NHẤT của module `pos` không yêu cầu JWT (docs/13 §1).

    Chỉ đọc `employee_cache`, không ghi gì — không cần mở transaction.
    """
    settings = get_settings()
    async with request.app.state.session_factory() as session:
        try:
            result = await login(
                LoginCommand(body.employee_id, body.password),
                employees=PostgresEmployees(session),
                store_id=settings.store_id,
                jwt_settings=get_jwt_settings(),
            )
        except LoginRejectedError as exc:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, str(exc), headers={"WWW-Authenticate": "Bearer"}
            ) from exc

    return LoginResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        role=result.role,
        expires_in_minutes=result.expires_in_minutes,
    )


@router.get(
    "/products",
    response_model=ProductResponse,
    summary="FR-P03 — quét mã vạch",
    dependencies=[Depends(require_role("cashier"))],
)
async def get_product_by_barcode(
    request: Request,
    barcode: Annotated[str, Query(min_length=1)],
) -> ProductResponse:
    """Đọc `product_cache` — không bao giờ gọi trung tâm (ngân sách p95 < 100ms).

    Chỉ đọc, nên không mở transaction ghi.
    """
    async with request.app.state.session_factory() as session:
        product = await PostgresProductCatalog(session).by_barcode(barcode)

    if product is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Không tìm thấy mã vạch {barcode} trong product_cache"
        )
    return ProductResponse(**product)


@router.post(
    "/sales",
    response_model=PlaceSaleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="FR-P04..P07 — chốt đơn (docs/13 §3)",
)
async def create_sale(
    request: Request,
    body: PlaceSaleRequest,
    claims: Annotated[AccessTokenClaims, Depends(require_role("cashier"))],
) -> PlaceSaleResponse:
    """Chốt một đơn hàng.

    Không có mã lỗi nào cho "trung tâm không tới được": luồng này không gọi trung tâm.
    """
    if body.employee_id != claims.employee_id:
        # Token là nguồn sự thật cho danh tính; `employee_id` trong body chỉ là xác nhận
        # tường minh, khớp docs/13 §3. Lệch nhau nghĩa là ai đó đang cố ghi nhận đơn dưới
        # tên một nhân viên khác — không phải lỗi dữ liệu, mà là cố ý giả mạo.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "employee_id trong body phải khớp với người đăng nhập trên token",
        )

    settings = get_settings()
    occurred_at = utcnow()

    async with transaction(request.app.state.session_factory) as session:
        outbox = OutboxPublisher(session, store_id=settings.store_id)
        loyalty = await build_loyalty_service(session, store_id=settings.store_id, at=occurred_at)

        # Phân giải khách TRƯỚC khi vào use case: bước này có thể chạm mạng (thác đổ ở
        # docs/13 §4), và use case thì không được phép chạm mạng.
        customer = (
            await loyalty.snapshot_for(body.customer_id)
            if body.customer_id
            else CustomerSnapshot.anonymous()
        )

        command = PlaceSaleCommand(
            employee_id=body.employee_id,
            shift_id=body.shift_id,
            lines=[RequestedLine(line.product_id, line.quantity) for line in body.lines],
            payments=[RequestedPayment(p.method, p.amount, p.reference) for p in body.payments],
            customer=customer,
            tendered_amount=body.tendered_amount,
            promotion_id=body.promotion_id,
            promo_discount_pct=body.promo_discount_pct,
            occurred_at=occurred_at,
        )

        try:
            receipt = await place_sale(
                command,
                catalog=PostgresProductCatalog(session),
                shifts=PostgresShifts(session),
                sales=PostgresSaleRepository(session),
                events=outbox,
                loyalty=loyalty,
                rules=get_pricing_rules(),
            )
        except SaleRejectedError as exc:
            # Thoát khỏi `async with` bằng exception → transaction rollback. Mọi mã 4xx
            # đều xảy ra trước lần ghi đầu tiên, nên ở đây không có gì để hoàn tác.
            raise HTTPException(
                _HTTP_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    return PlaceSaleResponse(**asdict(receipt))
