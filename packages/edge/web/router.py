"""Route UI POS — roadmap tuần 1 ngày 5.

Tầng trình bày mỏng: dịch form HTML ↔ use case THẬT của `edge.pos`/`edge.loyalty`. Đường
chốt đơn (`/ui/pos/checkout`) gọi ĐÚNG `place_sale()` mà `POST /api/v1/sales` gọi — không
có phiên bản "dành cho UI" riêng của logic nghiệp vụ.

Xem `edge/web/__init__.py` để biết ba đơn giản hóa có chủ đích (không tra khách, mở ca
qua route tạm, không CSRF).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text

from edge.loyalty.api import CustomerSnapshot, build_loyalty_service
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
from edge.web.auth import require_role_from_cookie, set_login_cookie
from edge.web.templates import templates
from shared.db import transaction
from shared.outbox import OutboxPublisher
from shared.security import AccessTokenClaims
from shared.types import utcnow

router = APIRouter(prefix="/ui", tags=["ui"])

_CASHIER = require_role_from_cookie("cashier")


@dataclass(frozen=True, slots=True)
class CartLine:
    """Một dòng giỏ hàng đi qua vòng đời request↔request qua hidden field JSON."""

    product_id: str
    name: str
    unit_price: int
    quantity: int


def _cart_to_json(cart: list[CartLine]) -> str:
    return json.dumps([asdict(line) for line in cart])


def _cart_from_json(raw: str) -> list[CartLine]:
    """Giỏ hàng rỗng/hỏng → coi như giỏ trống, KHÔNG 500.

    Hidden field do chính server sinh ra ở lần render trước, nhưng vẫn không tin nó vô
    điều kiện: một request thủ công (curl, DevTools) gửi JSON hỏng không được làm sập UI.
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return [CartLine(**line) for line in data]
    except (json.JSONDecodeError, TypeError, KeyError):
        return []


def _subtotal(cart: list[CartLine]) -> int:
    return sum(line.unit_price * line.quantity for line in cart)


# ═══════════════════════ Đăng nhập ═══════════════════════


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    settings = get_settings()
    return templates.TemplateResponse(
        request, "login.html", {"store_id": settings.store_id, "error": None}
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    employee_id: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> HTMLResponse:
    settings = get_settings()
    async with request.app.state.session_factory() as session:
        try:
            result = await login(
                LoginCommand(employee_id, password),
                employees=PostgresEmployees(session),
                store_id=settings.store_id,
                jwt_settings=get_jwt_settings(),
            )
        except LoginRejectedError:
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "store_id": settings.store_id,
                    "error": "Mã nhân viên hoặc mật khẩu không đúng",
                    "employee_id": employee_id,
                },
            )

    # HTMX swap `outerHTML` trên chính form đăng nhập — muốn điều hướng cả trang thì phải
    # dùng HX-Redirect thay vì trả HTML của trang kế tiếp (nếu không HTMX sẽ nhét HTML của
    # /ui/pos vào đúng chỗ cái form, hỏng bố cục).
    response = HTMLResponse(status_code=200, content="", headers={"HX-Redirect": "/ui/pos"})
    set_login_cookie(response, result.access_token)
    return response


# ═══════════════════════ Bán hàng ═══════════════════════


@router.get("/pos", response_class=HTMLResponse)
async def pos_page(
    request: Request, claims: Annotated[AccessTokenClaims, Depends(_CASHIER)]
) -> HTMLResponse:
    settings = get_settings()
    async with request.app.state.session_factory() as session:
        shift = await PostgresShifts(session).find_open_shift(settings.store_id)

    if shift is None:
        return templates.TemplateResponse(
            request, "open_shift.html", {"store_id": settings.store_id, "error": None}
        )

    return templates.TemplateResponse(
        request,
        "pos.html",
        {
            "store_id": settings.store_id,
            "employee_id": claims.employee_id,
            "cart": [],
            "cart_json": _cart_to_json([]),
            "subtotal": 0,
            "error": None,
            "scan_url": "/ui/pos/scan",
            "remove_url": "/ui/pos/remove",
            "checkout_url": "/ui/pos/checkout",
        },
    )


@router.post("/shifts/open")
async def open_shift_submit(
    request: Request,
    claims: Annotated[AccessTokenClaims, Depends(_CASHIER)],
    opening_cash: Annotated[int, Form(ge=0)],
) -> RedirectResponse:
    """⚠️ STOPGAP — thay bởi `POST /shifts/open` thật (FR-P11) ở tuần 2 ngày 10.

    Ghi trực tiếp bằng SQL, không qua use case (chưa có use case mở ca). `business_date`
    lấy tạm theo ngày UTC hiện tại — G5 (docs/05 §3.2) đòi người mở ca xác định tường
    minh, use case thật sẽ sửa đúng chỗ này.
    """
    settings = get_settings()
    async with transaction(request.app.state.session_factory) as session:
        await session.execute(
            text(
                """
                INSERT INTO shift (shift_id, store_id, business_date,
                                  opened_by_employee_id, opening_cash)
                VALUES (:shift_id, :store_id, :business_date, :employee_id, :opening_cash)
                """
            ),
            {
                "shift_id": uuid.uuid4(),
                "store_id": settings.store_id,
                "business_date": datetime.now(UTC).date(),
                "employee_id": claims.employee_id,
                "opening_cash": opening_cash,
            },
        )

    return RedirectResponse("/ui/pos", status_code=303)


@router.post("/pos/scan", response_class=HTMLResponse)
async def scan_barcode(
    request: Request,
    claims: Annotated[AccessTokenClaims, Depends(_CASHIER)],
    cart_json: Annotated[str, Form()],
    barcode: Annotated[str, Form()],
) -> HTMLResponse:
    cart = _cart_from_json(cart_json)
    settings = get_settings()

    barcode = barcode.strip()
    error: str | None = None
    if barcode:
        async with request.app.state.session_factory() as session:
            product = await PostgresProductCatalog(session).by_barcode(barcode)
        if product is None:
            error = f"Không tìm thấy mã vạch {barcode!r}"
        else:
            for i, line in enumerate(cart):
                if line.product_id == product["product_id"]:
                    cart[i] = CartLine(
                        line.product_id, line.name, line.unit_price, line.quantity + 1
                    )
                    break
            else:
                cart.append(
                    CartLine(product["product_id"], product["name"], product["unit_price"], 1)
                )

    return templates.TemplateResponse(
        request,
        "_pos_form.html",
        {
            "store_id": settings.store_id,
            "employee_id": claims.employee_id,
            "cart": cart,
            "cart_json": _cart_to_json(cart),
            "subtotal": _subtotal(cart),
            "error": error,
            "scan_url": "/ui/pos/scan",
            "remove_url": "/ui/pos/remove",
            "checkout_url": "/ui/pos/checkout",
        },
    )


@router.post("/pos/remove", response_class=HTMLResponse)
async def remove_line(
    request: Request,
    claims: Annotated[AccessTokenClaims, Depends(_CASHIER)],
    cart_json: Annotated[str, Form()],
    line_no: Annotated[int, Form()],
) -> HTMLResponse:
    cart = _cart_from_json(cart_json)
    if 0 <= line_no < len(cart):
        del cart[line_no]

    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "_pos_form.html",
        {
            "store_id": settings.store_id,
            "employee_id": claims.employee_id,
            "cart": cart,
            "cart_json": _cart_to_json(cart),
            "subtotal": _subtotal(cart),
            "error": None,
            "scan_url": "/ui/pos/scan",
            "remove_url": "/ui/pos/remove",
            "checkout_url": "/ui/pos/checkout",
        },
    )


@router.post("/pos/checkout", response_class=HTMLResponse)
async def checkout(
    request: Request,
    claims: Annotated[AccessTokenClaims, Depends(_CASHIER)],
    cart_json: Annotated[str, Form()],
    tendered_amount: Annotated[int, Form(ge=0)],
) -> HTMLResponse:
    """Chốt đơn — gọi `place_sale()` THẬT, cùng use case với `POST /api/v1/sales`.

    Không tra khách (xem `edge/web/__init__.py`): mọi đơn qua UI là khách vãng lai. Chỉ
    một hình thức thanh toán: tiền mặt, đúng số tiền khách đưa.
    """
    cart = _cart_from_json(cart_json)
    settings = get_settings()
    occurred_at = utcnow()

    # ⚠️ `sale_payment.amount` phải bằng TỔNG ĐƠN (INV-2), không phải tiền khách đưa —
    # đây từng là một bug thật, bắt được bởi `test_checkout_with_change_shows_correct_amount`.
    # Khách vãng lai qua UI luôn 0% chiết khấu (`CustomerSnapshot.anonymous()`, không có ô
    # nhập khuyến mãi) nên `total == subtotal` chắc chắn đúng ở đây; nếu sai giả định này
    # sau này, `place_sale()` tự tính lại total và INV-2 sẽ chặn ở use case (docs/13 §3),
    # không để lọt xuống DB.
    sale_total = _subtotal(cart)

    async with transaction(request.app.state.session_factory) as session:
        shift = await PostgresShifts(session).find_open_shift(settings.store_id)
        if shift is None:
            return templates.TemplateResponse(
                request, "open_shift.html", {"store_id": settings.store_id, "error": None}
            )

        outbox = OutboxPublisher(session, store_id=settings.store_id)
        loyalty = await build_loyalty_service(session, store_id=settings.store_id, at=occurred_at)

        command = PlaceSaleCommand(
            employee_id=claims.employee_id,
            shift_id=shift.shift_id,
            lines=[RequestedLine(line.product_id, line.quantity) for line in cart],
            payments=[RequestedPayment("CASH", sale_total)],
            customer=CustomerSnapshot.anonymous(),
            tendered_amount=tendered_amount,
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
            return templates.TemplateResponse(
                request,
                "_pos_form.html",
                {
                    "store_id": settings.store_id,
                    "employee_id": claims.employee_id,
                    "cart": cart,
                    "cart_json": _cart_to_json(cart),
                    "subtotal": _subtotal(cart),
                    "error": str(exc),
                    "scan_url": "/ui/pos/scan",
                    "remove_url": "/ui/pos/remove",
                    "checkout_url": "/ui/pos/checkout",
                },
            )

    return templates.TemplateResponse(
        request,
        "receipt.html",
        {"store_id": settings.store_id, "receipt": receipt, "cart": cart},
    )
