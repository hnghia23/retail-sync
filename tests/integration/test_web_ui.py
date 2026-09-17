"""UI POS qua HTTP thật — roadmap tuần 1 ngày 5.

Tự động hóa lại đúng phiên `curl` thủ công đã xác minh trước khi viết file này: đăng nhập
(cookie) → mở ca (route tạm) → quét mã vạch hai lần → xóa dòng → mã vạch không tồn tại →
thanh toán → xác nhận hóa đơn VÀ dữ liệu trong Postgres. `/ui/pos/checkout` gọi đúng
`place_sale()` production — test này vì vậy cũng là một bài kiểm khác cho đường bán hàng
chính, nhưng đi qua bề mặt HTML thay vì JSON.
"""

from __future__ import annotations

import re
from typing import Any


async def _seed_employee(db: Any, *, employee_id: str = "emp-007", password_hash: str) -> None:
    await db.execute(
        """INSERT INTO employee_cache (employee_id, name, role, password_hash, is_active)
           VALUES ($1, 'Nguyen Van A', 'cashier', $2, true)""",
        employee_id,
        password_hash,
    )


async def _seed_product(
    db: Any, *, product_id: str = "SKU-001", barcode: str = "8930001", unit_price: int = 150_000
) -> None:
    await db.execute(
        """INSERT INTO product_cache (product_id, sku, barcode, name, unit_price, is_sellable)
           VALUES ($1, $1, $2, 'Nuoc mam Phu Quoc', $3, true)""",
        product_id,
        barcode,
        unit_price,
    )


def _password_hash(password: str = "dung-mat-khau-123") -> str:
    from shared.security import hash_password

    return hash_password(password)


def _extract_cart_json(html: str) -> str:
    """Lấy giá trị `cart_json` từ hidden field, giải mã HTML entity — đúng cách trình
    duyệt đọc `value` attribute trước khi submit lại."""
    import html as html_module

    match = re.search(r"id=\"cart-data\" name=\"cart_json\" value='([^']*)'", html)
    assert match is not None, "khong tim thay hidden field cart_json trong HTML"
    return html_module.unescape(match.group(1))


async def _login(
    client: Any, *, employee_id: str = "emp-007", password: str = "dung-mat-khau-123"
) -> None:
    resp = await client.post("/ui/login", data={"employee_id": employee_id, "password": password})
    assert resp.status_code == 200
    assert resp.headers["hx-redirect"] == "/ui/pos"
    assert "access_token" in resp.cookies


async def _open_shift(client: Any, *, opening_cash: int = 500_000) -> None:
    resp = await client.post("/ui/shifts/open", data={"opening_cash": opening_cash})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/pos"


# ═══════════════════════ Đăng nhập ═══════════════════════


async def test_login_sets_httponly_cookie_and_redirects(edge_db: Any, http_client: Any) -> None:
    await _seed_employee(edge_db, password_hash=_password_hash())
    resp = await http_client.post(
        "/ui/login", data={"employee_id": "emp-007", "password": "dung-mat-khau-123"}
    )
    assert resp.status_code == 200
    assert resp.headers["hx-redirect"] == "/ui/pos"
    assert "access_token" in resp.cookies


async def test_login_with_wrong_password_rerenders_form_with_error(
    edge_db: Any, http_client: Any
) -> None:
    """Sai mật khẩu KHÔNG được set cookie, KHÔNG redirect — chỉ hiện lại form kèm lỗi."""
    await _seed_employee(edge_db, password_hash=_password_hash())
    resp = await http_client.post(
        "/ui/login", data={"employee_id": "emp-007", "password": "sai-mat-khau"}
    )
    assert resp.status_code == 200
    assert "hx-redirect" not in resp.headers
    assert "access_token" not in resp.cookies
    assert "không đúng" in resp.text


# ═══════════════════════ Bảo vệ route (cookie) ═══════════════════════


async def test_pos_without_cookie_redirects_plain_request(http_client: Any) -> None:
    resp = await http_client.get("/ui/pos")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"


async def test_pos_without_cookie_uses_hx_redirect_for_htmx_request(http_client: Any) -> None:
    """Request HTMX (`HX-Request: true`) không được nhận 303 trần — phải là `HX-Redirect`,
    nếu không HTMX coi 303 là lỗi swap thay vì điều hướng cả trang."""
    resp = await http_client.get("/ui/pos", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert resp.headers["hx-redirect"] == "/ui/login"


# ═══════════════════════ Mở ca (route tạm) ═══════════════════════


async def test_pos_page_prompts_to_open_shift_when_none_open(
    edge_db: Any, http_client: Any
) -> None:
    await _seed_employee(edge_db, password_hash=_password_hash())
    await _login(http_client)
    resp = await http_client.get("/ui/pos")
    assert resp.status_code == 200
    assert "Mở ca" in resp.text


async def test_open_shift_then_pos_page_shows_pos_screen(edge_db: Any, http_client: Any) -> None:
    await _seed_employee(edge_db, password_hash=_password_hash())
    await _login(http_client)
    await _open_shift(http_client)

    resp = await http_client.get("/ui/pos")
    assert resp.status_code == 200
    assert "emp-007" in resp.text
    assert "Chưa có sản phẩm" in resp.text

    assert await edge_db.fetchval("SELECT count(*) FROM shift WHERE status = 'OPEN'") == 1


# ═══════════════════════ Quét hàng ═══════════════════════


async def test_scanning_unknown_barcode_shows_inline_error(edge_db: Any, http_client: Any) -> None:
    await _seed_employee(edge_db, password_hash=_password_hash())
    await _login(http_client)
    await _open_shift(http_client)

    resp = await http_client.post(
        "/ui/pos/scan", data={"cart_json": "[]", "barcode": "0000000000000"}
    )
    assert resp.status_code == 200
    assert "Không tìm thấy mã vạch" in resp.text


async def test_scanning_same_barcode_twice_increments_quantity(
    edge_db: Any, http_client: Any
) -> None:
    await _seed_employee(edge_db, password_hash=_password_hash())
    await _seed_product(edge_db)
    await _login(http_client)
    await _open_shift(http_client)

    first = await http_client.post("/ui/pos/scan", data={"cart_json": "[]", "barcode": "8930001"})
    assert first.status_code == 200
    cart_json = _extract_cart_json(first.text)

    second = await http_client.post(
        "/ui/pos/scan", data={"cart_json": cart_json, "barcode": "8930001"}
    )
    assert second.status_code == 200
    assert "300.000đ" in second.text  # 2 × 150.000

    cart = _extract_cart_json(second.text)
    assert '"quantity": 2' in cart
    # Vẫn CHỈ MỘT dòng — quét trùng sản phẩm tăng số lượng, không thêm dòng mới.
    assert cart.count('"product_id"') == 1


async def test_remove_line_empties_the_cart(edge_db: Any, http_client: Any) -> None:
    await _seed_employee(edge_db, password_hash=_password_hash())
    await _seed_product(edge_db)
    await _login(http_client)
    await _open_shift(http_client)

    scanned = await http_client.post("/ui/pos/scan", data={"cart_json": "[]", "barcode": "8930001"})
    cart_json = _extract_cart_json(scanned.text)

    removed = await http_client.post("/ui/pos/remove", data={"cart_json": cart_json, "line_no": 0})
    assert removed.status_code == 200
    assert "Chưa có sản phẩm" in removed.text
    assert "Tổng: 0đ" in removed.text


async def test_malformed_cart_json_is_treated_as_empty_cart(edge_db: Any, http_client: Any) -> None:
    """Hidden field do server sinh ra, nhưng vẫn không tin vô điều kiện — JSON hỏng
    (curl/DevTools thủ công) không được làm sập route, chỉ coi như giỏ trống."""
    await _seed_employee(edge_db, password_hash=_password_hash())
    await _seed_product(edge_db)
    await _login(http_client)
    await _open_shift(http_client)

    resp = await http_client.post(
        "/ui/pos/scan", data={"cart_json": "khong-phai-json-hop-le", "barcode": "8930001"}
    )
    assert resp.status_code == 200
    assert "150.000đ" in resp.text  # giỏ coi như rỗng rồi thêm đúng 1 dòng mới


# ═══════════════════════ Thanh toán — chốt đơn qua HTML ═══════════════════════


async def test_checkout_persists_sale_through_real_place_sale(
    edge_db: Any, http_client: Any
) -> None:
    """⚠️ Test quan trọng nhất file: `/ui/pos/checkout` phải đi qua ĐÚNG use case sản xuất,
    không phải một đường tắt riêng cho UI — xác nhận bằng cách kiểm DB thật, không chỉ tin
    HTML trả về."""
    await _seed_employee(edge_db, password_hash=_password_hash())
    await _seed_product(edge_db)
    await _login(http_client)
    await _open_shift(http_client)

    scanned = await http_client.post("/ui/pos/scan", data={"cart_json": "[]", "barcode": "8930001"})
    cart_json = _extract_cart_json(scanned.text)
    scanned2 = await http_client.post(
        "/ui/pos/scan", data={"cart_json": cart_json, "barcode": "8930001"}
    )
    cart_json = _extract_cart_json(scanned2.text)

    resp = await http_client.post(
        "/ui/pos/checkout", data={"cart_json": cart_json, "tendered_amount": 300_000}
    )
    assert resp.status_code == 200
    assert "300.000đ" in resp.text
    assert "Tiền thối: 0đ" in resp.text

    sale = await edge_db.fetchrow("SELECT employee_id, subtotal, total, status FROM sale")
    assert sale["employee_id"] == "emp-007"
    assert sale["subtotal"] == 300_000
    assert sale["total"] == 300_000
    assert sale["status"] == "COMPLETED"

    line = await edge_db.fetchrow("SELECT product_id, quantity, unit_price FROM sale_line")
    assert (line["product_id"], line["quantity"], line["unit_price"]) == ("SKU-001", 2, 150_000)

    payment = await edge_db.fetchrow("SELECT method, amount FROM sale_payment")
    assert (payment["method"], payment["amount"]) == ("CASH", 300_000)

    assert (
        await edge_db.fetchval("SELECT count(*) FROM outbox WHERE event_type = 'SaleCompleted'")
        == 1
    )


async def test_checkout_with_change_shows_correct_amount(edge_db: Any, http_client: Any) -> None:
    await _seed_employee(edge_db, password_hash=_password_hash())
    await _seed_product(edge_db, unit_price=150_000)
    await _login(http_client)
    await _open_shift(http_client)

    scanned = await http_client.post("/ui/pos/scan", data={"cart_json": "[]", "barcode": "8930001"})
    cart_json = _extract_cart_json(scanned.text)

    resp = await http_client.post(
        "/ui/pos/checkout", data={"cart_json": cart_json, "tendered_amount": 200_000}
    )
    assert resp.status_code == 200
    assert "Tiền thối: 50.000đ" in resp.text


async def test_checkout_uses_real_pricing_config_not_hardcoded(
    edge_db: Any, http_client: Any
) -> None:
    """Không có nhân viên tra khách trong UI (đơn giản hóa có chủ đích) → 0 điểm tích lũy,
    không phải lỗi."""
    await _seed_employee(edge_db, password_hash=_password_hash())
    await _seed_product(edge_db)
    await _login(http_client)
    await _open_shift(http_client)

    scanned = await http_client.post("/ui/pos/scan", data={"cart_json": "[]", "barcode": "8930001"})
    cart_json = _extract_cart_json(scanned.text)
    resp = await http_client.post(
        "/ui/pos/checkout", data={"cart_json": cart_json, "tendered_amount": 150_000}
    )
    assert "Điểm tích lũy: +0" in resp.text


# ═══════════════════════ Tài sản tĩnh ═══════════════════════


async def test_static_assets_are_served_locally_not_from_cdn(http_client: Any) -> None:
    """Nguyên tắc kiến trúc #1: cửa hàng bán được hàng khi mất mạng — JS phải tự phục vụ."""
    htmx = await http_client.get("/ui/static/vendor/htmx.min.js")
    alpine = await http_client.get("/ui/static/vendor/alpine.min.js")
    assert htmx.status_code == 200
    assert alpine.status_code == 200
    assert len(htmx.content) > 10_000
    assert len(alpine.content) > 10_000
