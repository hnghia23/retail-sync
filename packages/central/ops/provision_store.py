"""Đăng ký một cửa hàng và cấp khóa API cho sync worker của nó — case E06 (khai trương).

Một cửa hàng phải tồn tại trong `store` của trung tâm trước khi gửi được sự kiện: mọi bảng
replica có khóa ngoại tới `store`, và `POST /events` chỉ nhận khóa của cửa hàng đã đăng ký.

Khóa in ra MỘT lần. Trung tâm chỉ giữ SHA-256 của nó — mất thì chạy lại lệnh này để cấp khóa
mới (khóa cũ mất hiệu lực ngay).

Chạy:
    uv run python -m central.ops.provision_store --store-id store-001 --name "Cửa hàng demo"

Rồi đặt khóa vào `infra/.env` của cửa hàng đó: `CENTRAL_API_KEY=<khóa>`.

Cửa hàng ảo của bộ giả lập (docs/18 §3) — cấp khóa cho N cửa hàng THẬT trong master data (sắp
theo mã, bỏ qua cửa hàng đã có khóa để không thu hồi khóa của cửa hàng thật đang chạy) và ghi
ra file thay vì in. Chạy lại với cùng file khóa thì giữ đúng các cửa hàng cũ (cấp khóa mới):

    uv run python -m central.ops.provision_store --from-master 20 --keys-out runs/virtual-keys.env

File khóa là SECRET: để trong `runs/` (gitignore), không commit, không dán vào chat.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from sqlalchemy import text

from central.auth import digest_store_key, new_store_key

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_ENSURE_STORE = text(
    """
    INSERT INTO store (store_id, name) VALUES (:store_id, :name)
    ON CONFLICT (store_id) DO NOTHING
    """
)

# Một khóa hiệu lực cho mỗi cửa hàng. Cấp lại = thay thế, không cộng thêm: hai khóa cùng
# sống cho một cửa hàng là hai chỗ phải thu hồi khi lộ.
_ISSUE_KEY = text(
    """
    INSERT INTO store_credential (store_id, key_sha256) VALUES (:store_id, :digest)
    ON CONFLICT (store_id) DO UPDATE SET
        key_sha256 = EXCLUDED.key_sha256, created_at = now(), revoked_at = NULL
    """
)


async def provision_store(session: AsyncSession, *, store_id: str, name: str | None) -> str:
    """Đảm bảo cửa hàng tồn tại, cấp khóa mới. Trả khóa GỐC — chỉ lần này là thấy được."""
    await session.execute(_ENSURE_STORE, {"store_id": store_id, "name": name or store_id})
    key = new_store_key()
    await session.execute(_ISSUE_KEY, {"store_id": store_id, "digest": digest_store_key(key)})
    return key


_UNCREDENTIALED = text(
    """
    SELECT s.store_id FROM store s
    WHERE NOT EXISTS (SELECT 1 FROM store_credential c WHERE c.store_id = s.store_id)
    ORDER BY s.store_id LIMIT :n
    """
)


async def stores_without_key(session: AsyncSession, *, n: int) -> list[str]:
    """N cửa hàng trong master data CHƯA có khóa — tất định (sắp theo mã)."""
    return list((await session.execute(_UNCREDENTIALED, {"n": n})).scalars())


def _stores_in(path: object) -> list[str]:
    from pathlib import Path

    if not isinstance(path, Path) or not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.split("=", 1)[0].strip() for ln in lines if "=" in ln and not ln.startswith("#")]


async def _main() -> None:  # pragma: no cover — điểm vào CLI
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-id", action="append", default=[], help="lặp lại được")
    parser.add_argument("--name", help="Tên hiển thị, chỉ dùng khi cửa hàng CHƯA có ở trung tâm")
    parser.add_argument(
        "--from-master", type=int, default=0, metavar="N", help="thêm N cửa hàng chưa có khóa"
    )
    parser.add_argument(
        "--keys-out", type=Path, default=None, help="ghi `store_id=khóa` vào file thay vì in"
    )
    args = parser.parse_args()
    if not args.store_id and not args.from_master:
        parser.error("cần --store-id hoặc --from-master")

    from central.settings import get_settings
    from shared.db import make_engine, make_session_factory, transaction

    engine = make_engine(get_settings().database_url)
    keys: dict[str, str] = {}
    try:
        async with transaction(make_session_factory(engine)) as session:
            store_ids = list(args.store_id)
            if args.from_master:
                # Chạy lại với cùng file khóa → CÙNG các cửa hàng (cấp khóa mới), để lần chạy
                # giả lập sau so được với lần trước. Chỉ bổ sung cửa hàng chưa có khóa: không
                # bao giờ thu hồi khóa của một cửa hàng thật đang đồng bộ.
                prior = _stores_in(args.keys_out)[: args.from_master]
                store_ids += prior
                store_ids += await stores_without_key(session, n=args.from_master - len(prior))
            for store_id in store_ids:
                keys[store_id] = await provision_store(session, store_id=store_id, name=args.name)
    finally:
        await engine.dispose()

    if args.keys_out is not None:
        args.keys_out.parent.mkdir(parents=True, exist_ok=True)
        args.keys_out.write_text(
            "# SECRET — khóa cửa hàng (central.ops.provision_store). Không commit.\n"
            + "".join(f"{sid}={key}\n" for sid, key in keys.items()),
            encoding="utf-8",
        )
        print(f"{len(keys)} khóa → {args.keys_out}: {', '.join(keys)}")
        return
    for store_id, key in keys.items():
        print(f"store_id={store_id}")
        print(f"CENTRAL_API_KEY={key}")


if __name__ == "__main__":  # pragma: no cover
    import asyncio

    from shared.console import utf8_stdio

    utf8_stdio()
    asyncio.run(_main())
