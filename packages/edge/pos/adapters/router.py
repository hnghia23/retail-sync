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
from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
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
    quote_sale,
)
from edge.pos.application.shifts import (
    CloseShiftCommand,
    OpenShiftCommand,
    ShiftRejectedError,
    close_shift,
    open_shift,
)
from edge.settings import get_jwt_settings, get_pii_settings, get_pricing_rules, get_settings
from shared.db import transaction
from shared.outbox import OutboxPublisher
from shared.pii import InvalidPhoneError
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

#: `ShiftRejectedError.code` → HTTP.
_SHIFT_HTTP_STATUS: dict[str, int] = {
    "SHIFT_ALREADY_OPEN": status.HTTP_409_CONFLICT,
    "SHIFT_CLOSED": status.HTTP_409_CONFLICT,
    "INVALID_BUSINESS_DATE": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "INVALID_OPENING_CASH": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "INVALID_COUNTED_CASH": status.HTTP_422_UNPROCESSABLE_CONTENT,
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


class QuoteRequest(BaseModel):
    """Cùng giỏ và khách như `POST /sales`, không có thanh toán và ca."""

    lines: list[SaleLineRequest] = Field(min_length=1)
    customer_id: uuid.UUID | None = None
    promo_discount_pct: int = Field(default=0, ge=0, le=100)


class QuoteResponse(BaseModel):
    subtotal: Money
    discount_tier: Money
    discount_promo: Money
    total: Money
    tier_discount_pct: int


class ProductResponse(BaseModel):
    product_id: str
    sku: str
    barcode: str | None
    name: str
    unit_price: Money
    #: Tuổi của bản sao master data. UI cảnh báo khi > 24h (B02 E03).
    synced_hours_ago: float


class RegisterCustomerRequest(BaseModel):
    """FR-L02. CHỈ SĐT — không nhận tên ở giai đoạn A: chưa có khóa mã hóa PII, và không thu
    thập thứ chưa bảo vệ được (ràng buộc #10). Tên đi kèm mã hóa ở giai đoạn C."""

    phone: str = Field(min_length=8, max_length=20)


class RegisterCustomerResponse(BaseModel):
    customer_id: uuid.UUID
    #: `false` = SĐT đã có ở cửa hàng này, trả về khách cũ (HTTP 200 thay vì 201).
    created: bool


class OpenShiftRequest(BaseModel):
    #: G5 — khai tường minh; phải là hôm nay hoặc hôm qua theo giờ cửa hàng.
    business_date: date
    opening_cash: Money = Field(ge=0)


class OpenShiftResponse(BaseModel):
    shift_id: uuid.UUID
    business_date: date


class CloseShiftRequest(BaseModel):
    counted_cash: Money = Field(ge=0)
    variance_note: str | None = Field(default=None, max_length=500)


class CloseShiftResponse(BaseModel):
    shift_id: uuid.UUID
    business_date: date
    opening_cash: Money
    cash_collected: Money
    expected_cash: Money
    counted_cash: Money
    variance: Money
    closed_at: datetime


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
    "/sales/quote",
    response_model=QuoteResponse,
    summary="FR-P05 — tạm tính giỏ hàng (không ghi gì)",
    dependencies=[Depends(require_role("cashier"))],
)
async def quote(request: Request, body: QuoteRequest) -> QuoteResponse:
    """Cùng hàm tính tiền với `POST /sales` — số `total` trả về là số phải thanh toán.

    Không mở transaction ghi: chỉ đọc `product_cache`, bản sao khách và quy tắc hạng.
    """
    settings = get_settings()
    async with request.app.state.session_factory() as session:
        loyalty = await build_loyalty_service(session, store_id=settings.store_id, at=utcnow())
        customer = (
            await loyalty.snapshot_for(body.customer_id)
            if body.customer_id
            else CustomerSnapshot.anonymous()
        )
        try:
            result = await quote_sale(
                lines=[RequestedLine(line.product_id, line.quantity) for line in body.lines],
                customer=customer,
                promo_discount_pct=body.promo_discount_pct,
                catalog=PostgresProductCatalog(session),
                loyalty=loyalty,
                rules=get_pricing_rules(),
            )
        except SaleRejectedError as exc:
            raise HTTPException(
                _HTTP_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
    return QuoteResponse(**asdict(result))


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


# ═════════════ Khách hàng & ca — nguồn dữ liệu của luồng (ADR-010 quy tắc 2) ═════════════


def _shift_error(exc: ShiftRejectedError) -> HTTPException:
    detail: dict[str, object] = {"code": exc.code, "message": str(exc)}
    if exc.shift_id is not None:
        detail["shift_id"] = str(exc.shift_id)
    return HTTPException(_SHIFT_HTTP_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST), detail)


@router.post(
    "/customers",
    response_model=RegisterCustomerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="FR-L02 — đăng ký khách tại quầy (chạy được khi offline)",
    dependencies=[Depends(require_role("cashier"))],
)
async def register_customer(
    request: Request, body: RegisterCustomerRequest, response: Response
) -> RegisterCustomerResponse:
    """Tạo khách cục bộ + `CustomerCreated` vào outbox, một transaction. Không gọi trung tâm.

    SĐT đã có ở cửa hàng → `200` với khách cũ. Thu ngân gõ lại SĐT khách quen là chuyện hằng
    ngày; trả lỗi ở đây chỉ bắt thu ngân làm thêm một bước tra cứu.
    """
    settings = get_settings()
    occurred_at = utcnow()
    try:
        async with transaction(request.app.state.session_factory) as session:
            loyalty = await build_loyalty_service(
                session,
                store_id=settings.store_id,
                at=occurred_at,
                phone_hash_key=get_pii_settings().hash_key,
            )
            result = await loyalty.register_customer(phone=body.phone, occurred_at=occurred_at)
    except InvalidPhoneError as exc:
        # Thông điệp của InvalidPhoneError không chứa số khách gõ — an toàn để trả về.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "INVALID_PHONE", "message": str(exc)},
        ) from exc

    if not result.created:
        response.status_code = status.HTTP_200_OK
    return RegisterCustomerResponse(customer_id=result.customer_id, created=result.created)


@router.post(
    "/shifts/open",
    response_model=OpenShiftResponse,
    status_code=status.HTTP_201_CREATED,
    summary="FR-P11 — mở ca",
)
async def open_shift_route(
    request: Request,
    body: OpenShiftRequest,
    claims: Annotated[AccessTokenClaims, Depends(require_role("cashier"))],
) -> OpenShiftResponse:
    """Cửa hàng đã có ca mở → `409 SHIFT_ALREADY_OPEN`, kèm `shift_id` của ca đó."""
    settings = get_settings()
    try:
        async with transaction(request.app.state.session_factory) as session:
            shift = await open_shift(
                OpenShiftCommand(
                    store_id=settings.store_id,
                    employee_id=claims.employee_id,
                    business_date=body.business_date,
                    opening_cash=body.opening_cash,
                    opened_at=utcnow(),
                    store_utc_offset=timedelta(minutes=settings.store_utc_offset_minutes),
                ),
                shifts=PostgresShifts(session),
            )
    except ShiftRejectedError as exc:
        raise _shift_error(exc) from exc
    return OpenShiftResponse(shift_id=shift.shift_id, business_date=shift.business_date)


@router.post(
    "/shifts/{shift_id}/close",
    response_model=CloseShiftResponse,
    summary="FR-P11 — đóng ca (quản lý)",
)
async def close_shift_route(
    request: Request,
    shift_id: uuid.UUID,
    body: CloseShiftRequest,
    claims: Annotated[AccessTokenClaims, Depends(require_role("manager"))],
) -> CloseShiftResponse:
    """Ca đã đóng / không thuộc cửa hàng này → `409 SHIFT_CLOSED`."""
    settings = get_settings()
    try:
        async with transaction(request.app.state.session_factory) as session:
            report = await close_shift(
                CloseShiftCommand(
                    shift_id=shift_id,
                    store_id=settings.store_id,
                    employee_id=claims.employee_id,
                    counted_cash=body.counted_cash,
                    variance_note=body.variance_note,
                    closed_at=utcnow(),
                ),
                shifts=PostgresShifts(session),
                events=OutboxPublisher(session, store_id=settings.store_id),
            )
    except ShiftRejectedError as exc:
        raise _shift_error(exc) from exc
    return CloseShiftResponse(**asdict(report))
