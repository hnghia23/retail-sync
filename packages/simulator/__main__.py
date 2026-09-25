"""CLI bộ giả lập — docs/18 §7.

    SEED_EMPLOYEE_PASSWORD=... uv run python -m simulator run --profile t0 \\
        --store store-001=http://localhost:8001 --days 1 --rate 60 --seed 42

    # Chế độ virtual: 20 cửa hàng ảo đẩy thẳng lên trung tâm, 3 ngày vắt qua cuối tháng
    uv run python -m simulator run --mode virtual --profile t1 --keys runs/virtual-keys.env \\
        --central http://localhost:8000 --days 3 --start-date 2026-08-30 --rate 1800 \\
        --quirks offline,resend,concurrent_customer

    # Chế độ bulk: T2 24 tháng → Parquet bronze trên lake (bronze/bulk-t2), đo khối lượng (LD-3)
    uv run python -m simulator bulk --profile t2 --months 24 --workers 6

    uv run python -m simulator audit --manifest runs/<run_id>/manifest.json \\
        --edge-dsn store-001=postgresql://edge_app:...@localhost:5433/edge_store_001 \\
        --central-dsn postgresql://central_app:...@localhost:5434/central --wait 120

Mã thoát của `audit`: 0 = CONVERGED, 1 = còn lại. Test ngâm/hỗn loạn dùng đúng mã này.
`--watch 300` đối soát mỗi 5 phút tới Ctrl+C; `--otlp` (hoặc OTEL_EXPORTER_OTLP_ENDPOINT) đẩy
kết quả lên dashboard "sức khỏe luồng" (`simulator.telemetry`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

__all__: list[str] = []

_REPO = Path(__file__).resolve().parents[2]
_REAL_CRAWL = _REPO / "crawl_data"
#: `crawl_data/` thật không nằm trong git; clone sạch dùng bộ tổng hợp cùng định dạng — đúng thứ
#: `infra/bootstrap.py` đã seed vào stack khi thiếu dữ liệu thật.
_DEFAULT_CRAWL = (
    _REAL_CRAWL
    if (_REAL_CRAWL / "products" / "product_details.csv").exists()
    else _REPO / "tests" / "fixtures" / "crawl_data"
)
_ENV_FILE = _REPO / "infra" / ".env"


def _env_value(name: str) -> str:
    """Biến môi trường, không có thì đọc `infra/.env` — nơi `infra/bootstrap.py` giữ secret dev
    (mật khẩu nhân viên dựng sẵn), để không phải truyền tay qua shell mỗi lần chạy."""
    if value := os.environ.get(name, ""):
        return value
    if _ENV_FILE.exists():
        for raw in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            key, sep, value = raw.strip().partition("=")
            if sep and key.strip() == name:
                return value.strip()
    return ""


def _pairs(values: list[str], what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in values:
        key, sep, val = v.partition("=")
        if not sep:
            raise SystemExit(f"--{what} phải có dạng store_id=giá_trị, nhận được: {v!r}")
        out[key] = val
    return out


def _catalog(crawl_data: Path) -> dict[str, int]:
    """product_id → giá niêm yết, cùng nguồn với `edge.ops.seed` nên mã nào cũng có ở cửa hàng."""
    from shared.seed_data import parse_products

    products = parse_products(crawl_data / "products" / "product_details.csv")
    return {p.product_id: p.unit_price for p in products}


async def _run_virtual(args: argparse.Namespace) -> int:
    from shared.config import SyncSettings
    from simulator.generator import plan_store
    from simulator.manifest import Manifest
    from simulator.profile import load_profile
    from simulator.quirks import Quirks, mark_concurrent_customers, offline_stores
    from simulator.sinks.virtual import read_keys, run_virtual

    if args.keys is None:
        raise SystemExit("--mode virtual cần --keys (file từ central.ops.provision_store)")
    targets = read_keys(args.keys.read_text(encoding="utf-8"))
    if args.stores:
        targets = targets[: args.stores]
    if not targets:
        raise SystemExit(f"{args.keys} không có khóa nào")
    profile = load_profile(args.profile)
    quirks = Quirks.parse(args.quirks, args.quirk_arg)
    catalog = _catalog(args.crawl_data)
    nonce = args.nonce if args.nonce is not None else secrets.randbelow(10**8)
    started = datetime.now(UTC)
    local_today = (started + timedelta(minutes=args.utc_offset_minutes)).date()
    # Mặc định kết thúc HÔM QUA: occurred_at luôn trước recorded_at, như cửa hàng đồng bộ trễ.
    start_date = args.start_date or (local_today - timedelta(days=args.days))
    run_id = args.run_id or f"{started:%Y%m%dT%H%M%SZ}-virtual-{profile.name}-s{args.seed}"

    store_ids = [t.store_id for t in targets]
    plans = mark_concurrent_customers(
        {
            sid: plan_store(
                profile,
                store_id=sid,
                catalog=list(catalog),
                prices=catalog,
                seed=args.seed,
                nonce=nonce,
                days=args.days,
                rate=args.rate,
                start_date=start_date,
            )
            for sid in store_ids
        },
        quirks,
        seed=args.seed,
    )
    offline = offline_stores(store_ids, quirks, seed=args.seed)
    defaults = SyncSettings()
    settings = SyncSettings(
        batch_size=args.batch_size or defaults.batch_size,
        poll_interval_seconds=args.poll or defaults.poll_interval_seconds,
        backoff_max_seconds=args.backoff_max or defaults.backoff_max_seconds,
        max_attempts_before_dead_letter=defaults.max_attempts_before_dead_letter,
        request_timeout_seconds=args.request_timeout,
    )
    n_sales = sum(1 for p in plans.values() for it in p if type(it).__name__ == "Sale")
    real_seconds = profile.day_hours * 3600 * args.days / args.rate
    print(
        f"run_id={run_id} mode=virtual profile={profile.name} stores={len(targets)}"
        f" ngày={start_date}..{start_date + timedelta(days=args.days - 1)} rate=x{args.rate:g}"
        f" → ~{real_seconds / 60:.1f} phút, {n_sales} đơn; tật={quirks.names() or '-'}"
        f" offline={offline or '-'}"
    )
    manifest = Manifest(
        run_id=run_id,
        mode="virtual",
        profile=profile.name,
        seed=args.seed,
        nonce=nonce,
        days=args.days,
        rate=args.rate,
        started_at=started.isoformat(),
        options={
            "start_date": start_date.isoformat(),
            "quirks": quirks.names(),
            "quirk_args": list(args.quirk_arg),
            "offline_stores": offline,
            "batch_size": settings.batch_size,
            "backoff_max_seconds": settings.backoff_max_seconds,
        },
    )
    try:
        await run_virtual(
            targets,
            plans,
            central_url=args.central,
            manifest=manifest,
            profile=profile,
            start_date=start_date,
            rate=args.rate,
            prices=catalog,
            quirks=quirks,
            offline_store_ids=offline,
            settings=settings,
            utc_offset_minutes=args.utc_offset_minutes,
            pii_key=os.environ.get("PII_HASH_KEY", "simulator-virtual"),
            drain_timeout=args.drain_timeout,
            seed=args.seed,
        )
    finally:
        path = manifest.write(args.out / run_id)
    totals = {
        k: sum(r.outbox.get(k, 0) for r in manifest.stores.values())
        for k in ("sent", "pending", "dead", "transport_errors", "resent_batches",
                  "resend_mismatch", "foreign_customer_sales", "foreign_fallback")
    }  # fmt: skip
    print(json.dumps({"sync": totals, "stats": manifest.stats()}, ensure_ascii=False, indent=2))
    print(f"manifest: {path}")
    return 0 if totals["pending"] == 0 and totals["dead"] == 0 else 1


async def _run(args: argparse.Namespace) -> int:
    import httpx

    from simulator.generator import plan_store
    from simulator.manifest import Manifest
    from simulator.profile import load_profile
    from simulator.sinks.edge_http import StoreTarget, run_edge

    password = _env_value("SEED_EMPLOYEE_PASSWORD")
    if not password:
        raise SystemExit(
            "Thiếu SEED_EMPLOYEE_PASSWORD (mật khẩu nhân viên dựng sẵn): đặt biến môi trường,"
            " hoặc chạy infra/bootstrap.py để nó sinh vào infra/.env"
        )
    stores = _pairs(args.store, "store")
    profile = load_profile(args.profile)
    catalog = _catalog(args.crawl_data)
    nonce = args.nonce if args.nonce is not None else secrets.randbelow(10**8)
    started = datetime.now(UTC)
    run_id = args.run_id or f"{started:%Y%m%dT%H%M%SZ}-{profile.name}-s{args.seed}"
    local_today = (started + timedelta(minutes=args.utc_offset_minutes)).date()

    plans = {
        sid: plan_store(
            profile,
            store_id=sid,
            catalog=list(catalog),
            prices=catalog,
            seed=args.seed,
            nonce=nonce,
            days=args.days,
            rate=args.rate,
            start_date=local_today,
        )
        for sid in stores
    }
    n_sales = sum(1 for p in plans.values() for it in p if type(it).__name__ == "Sale")
    real_seconds = profile.day_hours * 3600 * args.days / args.rate
    print(
        f"run_id={run_id} profile={profile.name} stores={len(stores)} days={args.days}"
        f" rate=x{args.rate:g} → ~{real_seconds / 60:.1f} phút, {n_sales} đơn đã lên lịch"
    )

    manifest = Manifest(
        run_id=run_id,
        mode="edge",
        profile=profile.name,
        seed=args.seed,
        nonce=nonce,
        days=args.days,
        rate=args.rate,
        started_at=started.isoformat(),
    )
    timeout = httpx.Timeout(args.request_timeout)
    limits = httpx.Limits(max_connections=args.max_connections)
    clients = {
        sid: httpx.AsyncClient(base_url=url, timeout=timeout, limits=limits)
        for sid, url in stores.items()
    }
    out_dir = args.out / run_id
    try:
        await run_edge(
            [
                StoreTarget(sid, client, utc_offset_minutes=args.utc_offset_minutes)
                for sid, client in clients.items()
            ],
            plans,
            password=password,
            manifest=manifest,
            opening_cash=profile.opening_cash,
        )
    finally:
        for c in clients.values():
            await c.aclose()
        path = manifest.write(out_dir)  # ghi cả khi dừng giữa chừng — đáp án dở vẫn dùng được

    print(
        json.dumps(
            {"summary": manifest.summary(), "stats": manifest.stats()}, ensure_ascii=False, indent=2
        )
    )
    print(f"manifest: {path}")
    return 0


def _lake_fs() -> Any:
    """MinIO của compose, secret từ biến môi trường hoặc `infra/.env` (như mật khẩu nhân viên)."""
    import pyarrow.fs as pafs

    return pafs.S3FileSystem(
        access_key=_env_value("MINIO_ROOT_USER") or "minioadmin",
        secret_key=_env_value("MINIO_ROOT_PASSWORD"),
        endpoint_override=os.environ.get("LAKE_ENDPOINT", "localhost:9000"),
        scheme="http",
        region="us-east-1",
    )


def _latest_snapshot(fs: Any, base: str, table: str) -> str | None:
    import pyarrow.fs as pafs

    infos = fs.get_file_info(pafs.FileSelector(f"{base}/{table}", allow_not_found=True))
    parts = sorted(i.path for i in infos if i.type == pafs.FileType.Directory)
    return f"{parts[-1]}/snapshot.parquet" if parts else None


def _bulk_window(args: argparse.Namespace) -> tuple[date, date]:
    """Mặc định: `--months` tháng trọn vẹn, kết thúc ở ngày cuối tháng trước."""
    today = datetime.now(UTC).date()
    end: date = args.end_date or (today.replace(day=1) - timedelta(days=1))
    if args.start_date:
        return args.start_date, end
    months = max(1, args.months)
    y, m = end.year, end.month - months + 1
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, 1), end


def _bulk(args: argparse.Namespace) -> int:
    """Chế độ `bulk` (docs/18 §3): lịch sử nhiều tháng → Parquet bronze trên lake, để đo khối
    lượng (LD-3, seam 50 triệu dòng ledger). Chạy lại cùng lệnh là an toàn: mảnh đã sinh và file
    đã có trên lake đều được giữ nguyên."""
    import time
    from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
    from dataclasses import asdict

    import pyarrow.parquet as pq

    from simulator.profile import load_profile
    from simulator.sinks.bulk_parquet import (
        SNAPSHOT_TABLES,
        generate_store,
        month_window,
        months_between,
        publish_month,
    )

    profile = load_profile(args.profile)
    n_stores = args.stores or profile.stores
    start, end = _bulk_window(args)
    days = (end - start).days + 1
    run_id = args.run_id or f"bulk-{profile.name}-{start:%Y%m}-{end:%Y%m}-s{args.seed}"
    work: Path = args.work or (args.out / run_id / "work")
    fs = None if args.no_publish else _lake_fs()
    source = f"{args.bucket}/bronze/central"
    prefix = args.lake_prefix or f"bronze/bulk-{profile.name}"
    if prefix.strip("/") == "bronze/central":
        raise SystemExit(
            "bulk KHÔNG ghi vào bronze/central: dữ liệu đo khối lượng không lẫn vào lake thật"
        )
    target = f"{args.bucket}/{prefix.strip('/')}"

    # Cửa hàng: N cửa hàng THẬT đầu tiên của master data (snapshot `store` trên lake thật), để
    # dim_store của mart có tên, vùng... Chỉ sinh cục bộ (không lake) thì đặt tên tổng hợp.
    store_ids = [f"bulk-{i:04d}" for i in range(1, n_stores + 1)]
    if fs is not None and (snap := _latest_snapshot(fs, source, "store")):
        with fs.open_input_file(snap) as f:
            ids = sorted(pq.read_table(f, columns=["store_id"]).column("store_id").to_pylist())
        if len(ids) < n_stores:
            raise SystemExit(f"master data chỉ có {len(ids)} cửa hàng, cần {n_stores}")
        store_ids = ids[:n_stores]

    catalog = _catalog(args.crawl_data)
    nonce = args.nonce if args.nonce is not None else args.seed
    print(
        f"run_id={run_id} bulk profile={profile.name} stores={n_stores} {start}..{end}"
        f" ({days} ngày) workers={args.workers} → {target if fs else work}",
        flush=True,
    )
    t0 = time.perf_counter()
    stats: list[dict[str, Any]] = []
    done_marker = work / "_generated.json"
    if done_marker.exists():
        stats = json.loads(done_marker.read_text(encoding="utf-8"))
        print(f"bước 1: đã có ({len(stats)} cửa hàng) — bỏ qua", flush=True)
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = [
                pool.submit(
                    generate_store,
                    profile,
                    store_id=sid,
                    prices=catalog,
                    seed=args.seed,
                    nonce=nonce,
                    start_date=start,
                    days=days,
                    work=work,
                )
                for sid in store_ids
            ]
            for i, fut in enumerate(futures, start=1):
                stats.append(asdict(fut.result()))
                if i % 10 == 0 or i == len(futures):
                    elapsed = time.perf_counter() - t0
                    print(f"  bước 1: {i}/{len(futures)} cửa hàng, {elapsed:.0f}s", flush=True)
        done_marker.write_text(json.dumps(stats, ensure_ascii=False), encoding="utf-8")
    t1 = time.perf_counter()

    published: dict[str, dict[str, int]] = {}
    if fs is not None:
        months = months_between(start, days)

        def publish(month: str) -> dict[str, int]:
            return publish_month(fs, work=work, month=month, base=target)

        with ThreadPoolExecutor(max_workers=args.publish_workers) as threads:
            for month, rows in zip(months, threads.map(publish, months), strict=True):
                published[month] = rows
                print(f"  bước 2: {month} {rows}", flush=True)
        # Master data: bản chụp mới nhất của lake THẬT, đặt ở ngày đầu của cửa sổ đầu tiên.
        first_dt = month_window(months[0])[0].date().isoformat()
        for table in SNAPSHOT_TABLES:
            snap = _latest_snapshot(fs, source, table)
            if snap is None:
                raise SystemExit(f"lake thật chưa có snapshot `{table}` — chạy pipeline một lần")
            key = f"{target}/{table}/dt={first_dt}/snapshot.parquet"
            fs.create_dir(key.rsplit("/", 1)[0], recursive=True)
            fs.copy_file(snap, key)

    totals = {
        k: sum(int(s[k]) for s in stats)
        for k in ("sales", "lines", "ledger", "customers", "shifts")
    }
    totals["total_vnd"] = sum(int(s["total"]) for s in stats)
    report = {
        "run_id": run_id,
        "mode": "bulk",
        "profile": profile.name,
        "seed": args.seed,
        "stores": n_stores,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "lake": target if fs else None,
        "work": str(work),
        "totals": totals,
        "published": published,
        "seconds": {"generate": round(t1 - t0, 1), "publish": round(time.perf_counter() - t1, 1)},
    }
    out = args.out / run_id / "bulk.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"totals": totals, "seconds": report["seconds"]}, ensure_ascii=False))
    print(f"báo cáo: {out}")
    return 0


async def _audit(args: argparse.Namespace) -> int:
    from simulator.audit import ClickHouseTarget, audit, audit_until_settled
    from simulator.manifest import Manifest
    from simulator.telemetry import AuditPublisher

    manifest = Manifest.read(args.manifest)
    edge_dsns = _pairs(args.edge_dsn, "edge-dsn")
    clickhouse = ClickHouseTarget.parse(args.clickhouse) if args.clickhouse else None
    publisher = AuditPublisher(args.otlp) if args.otlp else None
    out = args.manifest.parent / "audit.json"
    status = "CONVERGING"
    try:
        if not args.watch:
            report = await audit_until_settled(
                manifest,
                edge_dsns=edge_dsns,
                central_dsn=args.central_dsn,
                wait_seconds=args.wait,
                clickhouse=clickhouse,
                marts=args.marts,
            )
            if publisher is not None:
                publisher.publish(report)
            _print_audit(report, out)
            return 0 if report.status == "CONVERGED" else 1

        # Test ngâm / hỗn loạn (docs/08 §6.2): đối soát MỘT lượt mỗi chu kỳ, không chờ hội tụ —
        # khi luồng còn đang chảy thì `CONVERGING` là bình thường, chỉ `DIVERGED` là sự cố.
        while True:
            report = await audit(
                manifest,
                edge_dsns=edge_dsns,
                central_dsn=args.central_dsn,
                clickhouse=clickhouse,
                marts=args.marts,
            )
            status = report.status
            out.write_text(
                json.dumps(report.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if publisher is not None:
                publisher.publish(report)
            behind = max(
                (
                    f["behind_seconds"]
                    for layers in report.freshness.values()
                    for f in layers.values()
                ),
                default=0.0,
            )
            print(
                f"{datetime.now(UTC):%H:%M:%S} status={status} findings={len(report.findings)}"
                f" behind_max={behind:.0f}s",
                flush=True,
            )
            await asyncio.sleep(args.watch)
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0 if status == "CONVERGED" else 1
    finally:
        if publisher is not None:
            publisher.close()


def _print_audit(report: Any, out: Path) -> None:
    out.write_text(json.dumps(report.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"summary": report.summary, "outbox": report.outbox, "freshness": report.freshness},
            ensure_ascii=False,
            indent=2,
        )
    )
    for f in report.findings[:30]:
        print(f"  [{f.level}] {f.store_id} {f.layer} {f.what} {f.ref}")
    if len(report.findings) > 30:
        print(f"  … và {len(report.findings) - 30} phát hiện khác (xem {out})")
    print(f"status={report.status}  ({out})")


def main(argv: list[str] | None = None) -> int:
    from shared.console import utf8_stdio

    utf8_stdio()
    parser = argparse.ArgumentParser(prog="simulator", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="phát lịch ý định vào Edge API (chế độ edge)")
    run.add_argument("--profile", default="t0")
    run.add_argument("--store", action="append", default=[], metavar="STORE_ID=URL")
    run.add_argument("--days", type=int, default=1)
    run.add_argument("--rate", type=float, default=60.0, help="nén thời gian: 60 = 1 giờ/phút")
    run.add_argument("--seed", type=int, default=42)
    run.add_argument("--nonce", type=int, default=None, help="mặc định ngẫu nhiên (SĐT mới)")
    run.add_argument("--run-id", default=None)
    run.add_argument("--out", type=Path, default=_REPO / "runs")
    run.add_argument("--crawl-data", type=Path, default=_DEFAULT_CRAWL)
    run.add_argument("--utc-offset-minutes", type=int, default=420)
    run.add_argument("--request-timeout", type=float, default=10.0)
    run.add_argument("--max-connections", type=int, default=50)
    run.add_argument("--mode", choices=["edge", "virtual"], default="edge")
    virtual = run.add_argument_group("chế độ virtual (docs/18 §3)")
    virtual.add_argument("--central", default="http://localhost:8000", help="URL Central API")
    virtual.add_argument("--keys", type=Path, default=None, help="file store_id=khóa")
    virtual.add_argument("--stores", type=int, default=0, help="chỉ N cửa hàng đầu của file khóa")
    virtual.add_argument(
        "--start-date",
        type=lambda v: datetime.strptime(v, "%Y-%m-%d").date(),  # noqa: DTZ007 — chỉ lấy ngày
        default=None,
        help="ngày giả lập đầu tiên (mặc định: sao cho ngày cuối là hôm qua)",
    )
    virtual.add_argument("--quirks", default="", help="offline,resend,concurrent_customer")
    virtual.add_argument(
        "--quirk-arg", action="append", default=[], metavar="KEY=VALUE", help="xem quirks.py"
    )
    virtual.add_argument("--batch-size", type=int, default=0, help="mặc định SYNC_BATCH_SIZE")
    virtual.add_argument("--poll", type=float, default=0.0, help="giây giữa hai lần xả")
    virtual.add_argument("--backoff-max", type=float, default=0.0, help="trần backoff (giây)")
    virtual.add_argument(
        "--drain-timeout", type=float, default=600.0, help="giây chờ xả hết outbox sau khi bán xong"
    )

    def day(value: str) -> date:
        return datetime.strptime(value, "%Y-%m-%d").date()  # noqa: DTZ007 — chỉ lấy ngày

    bulk = sub.add_parser("bulk", help="lịch sử nhiều tháng → Parquet bronze (docs/18 §3)")
    bulk.add_argument("--profile", default="t2")
    bulk.add_argument("--months", type=int, default=1, help="số tháng, kết thúc ở --end-date")
    bulk.add_argument("--start-date", type=day, default=None, help="đè --months")
    bulk.add_argument("--end-date", type=day, default=None, help="mặc định: hết tháng trước")
    bulk.add_argument("--stores", type=int, default=0, help="mặc định: số cửa hàng của profile")
    bulk.add_argument("--seed", type=int, default=42)
    bulk.add_argument("--nonce", type=int, default=None)
    bulk.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    bulk.add_argument("--publish-workers", type=int, default=2)
    bulk.add_argument("--bucket", default="lake")
    bulk.add_argument("--lake-prefix", default="", help="mặc định bronze/bulk-<profile>")
    bulk.add_argument("--no-publish", action="store_true", help="chỉ sinh mảnh cục bộ")
    bulk.add_argument("--run-id", default=None)
    bulk.add_argument("--out", type=Path, default=_REPO / "runs")
    bulk.add_argument("--work", type=Path, default=None)
    bulk.add_argument("--crawl-data", type=Path, default=_DEFAULT_CRAWL)

    aud = sub.add_parser("audit", help="đối soát manifest với các tầng")
    aud.add_argument("--manifest", type=Path, required=True)
    aud.add_argument("--edge-dsn", action="append", default=[], metavar="STORE_ID=DSN")
    aud.add_argument("--central-dsn", required=True)
    aud.add_argument("--wait", type=float, default=0.0, help="giây chờ hội tụ trước khi kết luận")
    aud.add_argument(
        "--clickhouse",
        default=None,
        metavar="URL",
        help="bronze cho đối soát L3: http://user:pass@host:8123/dw (trống = chỉ L0 tới L2)",
    )
    aud.add_argument(
        "--marts",
        action="store_true",
        help="thêm L4: mart của dbt (fact_sale_line, fact_payment, fact_point_event)",
    )
    aud.add_argument(
        "--watch",
        type=float,
        default=0.0,
        metavar="GIÂY",
        help="đối soát lặp lại mỗi N giây tới Ctrl+C (test ngâm) — không chờ hội tụ",
    )
    aud.add_argument(
        "--otlp",
        default=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        metavar="URL",
        help="đẩy kết quả lên metric (http://localhost:4318); mặc định OTEL_EXPORTER_OTLP_ENDPOINT",
    )

    args = parser.parse_args(argv)
    if args.command == "bulk":
        return _bulk(args)
    if args.command == "run":
        if args.mode == "virtual":
            return asyncio.run(_run_virtual(args))
        if not args.store:
            args.store = ["store-001=http://localhost:8001"]
        return asyncio.run(_run(args))
    return asyncio.run(_audit(args))


if __name__ == "__main__":
    sys.exit(main())
