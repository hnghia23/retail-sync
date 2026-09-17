"""Xác thực ở tầng HTTP thật — docs/13 §1.

Mọi test khác (`test_place_sale.py`) gọi `place_sale()` trực tiếp, bỏ qua router. File
này là nơi DUY NHẤT đi qua FastAPI thật: parse header, chạy dependency `require_role`,
route `/auth/login`. Đây chính xác là chỗ một lỗi wiring (quên mount router, sai tên
header, dependency áp nhầm route) sẽ không bị bất kỳ test nào khác bắt được.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

# `http_client` (fixture) là tự động — pytest tiêm theo tên, không cần import từ conftest.
# `STORE_ID` KHÔNG phải fixture, phải khai lại ở đây — trùng giá trị với conftest.py vì
# `http_client` gắn cứng "store-001" khi dựng app test.
STORE_ID = "store-001"


async def _seed_employee(
    db: Any,
    *,
    employee_id: str = "emp-007",
    password_hash: str,
    role: str = "cashier",
    is_active: bool = True,
    revoked_at: datetime | None = None,
) -> None:
    await db.execute(
        """
        INSERT INTO employee_cache (employee_id, name, role, password_hash, is_active, revoked_at)
        VALUES ($1, 'Nguyen Van A', $2, $3, $4, $5)
        """,
        employee_id,
        role,
        password_hash,
        is_active,
        revoked_at,
    )


async def _seed_shift_and_product(db: Any) -> uuid.UUID:
    shift_id = uuid.uuid4()
    await db.execute(
        """INSERT INTO shift (shift_id, store_id, business_date,
                              opened_by_employee_id, opening_cash)
           VALUES ($1, $2, DATE '2026-09-17', 'emp-007', 500000)""",
        shift_id,
        STORE_ID,
    )
    await db.execute(
        """INSERT INTO product_cache (product_id, sku, barcode, name, unit_price, is_sellable)
           VALUES ('SKU-001', 'S1', '8930001', 'Nuoc mam', 150000, true)"""
    )
    return shift_id


def _password_hash(password: str = "dung-mat-khau") -> str:
    from shared.security import hash_password

    return hash_password(password)


# ═══════════════════════ /auth/login ═══════════════════════


async def test_login_with_correct_password_returns_tokens(edge_db: Any, http_client: Any) -> None:
    await _seed_employee(edge_db, password_hash=_password_hash("dung-mat-khau"))
    resp = await http_client.post(
        "/api/v1/auth/login", json={"employee_id": "emp-007", "password": "dung-mat-khau"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "cashier"
    assert body["access_token"]
    assert body["refresh_token"]


async def test_login_with_wrong_password_is_401(edge_db: Any, http_client: Any) -> None:
    await _seed_employee(edge_db, password_hash=_password_hash("dung-mat-khau"))
    resp = await http_client.post(
        "/api/v1/auth/login", json={"employee_id": "emp-007", "password": "sai"}
    )
    assert resp.status_code == 401


async def test_login_with_unknown_employee_is_401(edge_db: Any, http_client: Any) -> None:
    resp = await http_client.post(
        "/api/v1/auth/login", json={"employee_id": "emp-999", "password": "bat-ky"}
    )
    assert resp.status_code == 401


async def test_login_with_revoked_employee_is_401(edge_db: Any, http_client: Any) -> None:
    """S01: token thu hồi có hiệu lực dù cửa hàng offline."""
    await _seed_employee(
        edge_db,
        password_hash=_password_hash("dung"),
        revoked_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    resp = await http_client.post(
        "/api/v1/auth/login", json={"employee_id": "emp-007", "password": "dung"}
    )
    assert resp.status_code == 401


# ═══════════════════════ Route được bảo vệ ═══════════════════════


async def test_protected_route_without_token_is_401(http_client: Any) -> None:
    resp = await http_client.get("/api/v1/products", params={"barcode": "8930001"})
    assert resp.status_code == 401


async def test_protected_route_with_garbage_token_is_401(http_client: Any) -> None:
    resp = await http_client.get(
        "/api/v1/products",
        params={"barcode": "8930001"},
        headers={"Authorization": "Bearer khong-hop-le"},
    )
    assert resp.status_code == 401


async def test_valid_token_reaches_the_route(edge_db: Any, http_client: Any) -> None:
    await _seed_employee(edge_db, password_hash=_password_hash("dung"))
    await _seed_shift_and_product(edge_db)

    login_resp = await http_client.post(
        "/api/v1/auth/login", json={"employee_id": "emp-007", "password": "dung"}
    )
    token = login_resp.json()["access_token"]

    resp = await http_client.get(
        "/api/v1/products",
        params={"barcode": "8930001"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["product_id"] == "SKU-001"


# ═══════════════════════ POST /sales qua HTTP thật ═══════════════════════


async def test_place_sale_over_http_end_to_end(edge_db: Any, http_client: Any) -> None:
    """Đường đi đầy đủ: login → token → chốt đơn. Không mock gì ở tầng HTTP hay DB."""
    await _seed_employee(edge_db, password_hash=_password_hash("dung"))
    shift_id = await _seed_shift_and_product(edge_db)

    login_resp = await http_client.post(
        "/api/v1/auth/login", json={"employee_id": "emp-007", "password": "dung"}
    )
    token = login_resp.json()["access_token"]

    resp = await http_client.post(
        "/api/v1/sales",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "employee_id": "emp-007",
            "shift_id": str(shift_id),
            "lines": [{"product_id": "SKU-001", "quantity": 2}],
            "payments": [{"method": "CASH", "amount": 300_000}],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["total"] == 300_000
    assert await edge_db.fetchval("SELECT count(*) FROM sale") == 1


async def test_place_sale_rejects_employee_id_spoofing(edge_db: Any, http_client: Any) -> None:
    """⚠️ Test then chốt của bản vá bảo mật: token của emp-007 không được ghi đơn dưới
    tên emp-999, dù client tự khai `employee_id` khác trong body."""
    await _seed_employee(edge_db, password_hash=_password_hash("dung"))
    shift_id = await _seed_shift_and_product(edge_db)

    login_resp = await http_client.post(
        "/api/v1/auth/login", json={"employee_id": "emp-007", "password": "dung"}
    )
    token = login_resp.json()["access_token"]

    resp = await http_client.post(
        "/api/v1/sales",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "employee_id": "emp-999",  # ← giả mạo
            "shift_id": str(shift_id),
            "lines": [{"product_id": "SKU-001", "quantity": 1}],
            "payments": [{"method": "CASH", "amount": 150_000}],
        },
    )
    assert resp.status_code == 403
    assert await edge_db.fetchval("SELECT count(*) FROM sale") == 0


async def test_expired_token_cannot_place_sale(edge_db: Any, http_client: Any) -> None:
    from edge.settings import get_jwt_settings
    from shared.security import encode_access_token

    expired = encode_access_token(
        store_id=STORE_ID,
        employee_id="emp-007",
        role="cashier",
        settings=get_jwt_settings(),
        now=datetime(2020, 1, 1, tzinfo=UTC),
    )
    resp = await http_client.post(
        "/api/v1/sales",
        headers={"Authorization": f"Bearer {expired}"},
        json={
            "employee_id": "emp-007",
            "shift_id": str(uuid.uuid4()),
            "lines": [{"product_id": "SKU-001", "quantity": 1}],
            "payments": [{"method": "CASH", "amount": 150_000}],
        },
    )
    assert resp.status_code == 401
