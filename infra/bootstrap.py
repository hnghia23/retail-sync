"""Dựng stack compose từ con số 0 tới lúc chạy được bộ giả lập và `tests/scenarios/`.

Một lệnh cho cả ba nơi cần nó: máy dev sau `git clone` (điều kiện "Vận hành" của cổng B, docs/06),
job CI `scenarios` (docs/16 §6), và máy dev muốn dựng lại sau khi xóa volume.

    uv run python infra/bootstrap.py                     # 3 cửa hàng + trung tâm + nền tảng dữ liệu
    uv run python infra/bootstrap.py --observability     # thêm otel-lgtm + flow-monitor
    uv run python infra/bootstrap.py --project retail-sync-ci --env-file runs/ci.env

Các bước, mỗi bước CHẠY LẠI ĐƯỢC:
  1. Database + MinIO + ClickHouse lên trước (chờ healthcheck).
  2. Migration Alembic cho Postgres trung tâm và từng cửa hàng.
  3. Seed master data (vùng, cửa hàng, sản phẩm, hạng) — upsert.
  4. Khóa sync: GIỮ khóa đang có nếu trung tâm còn nhận nó (hỏi thẳng `POST /events []`), chỉ cấp
     mới khi thiếu hoặc sai. Cấp lại là thu hồi khóa cũ (`provision_store`), nên cấp bừa ở mỗi lần
     chạy sẽ làm sync worker đang chạy nhận `401`. Khóa ghi vào file env, KHÔNG in ra.
  5. Nhân viên dựng sẵn (trung tâm + từng cửa hàng) với `SEED_EMPLOYEE_PASSWORD`. Chưa có thì sinh
     ngẫu nhiên và ghi vào file env — một nơi giữ mật khẩu, thay vì "truyền tay qua biến môi trường"
     rồi không ai nhớ.
  6. Bucket lake + bảng bronze (`pipeline init`).
  7. Cả stack lên (worker nhận khóa mới), chờ Edge/Central API, Airflow và DAG `retail_pipeline`.

Dữ liệu master: `crawl_data/` nếu có (dữ liệu crawl thật, KHÔNG nằm trong git), không thì bộ
tổng hợp `tests/fixtures/crawl_data/` cùng định dạng — đủ cho bộ giả lập và `tests/scenarios/`.

Script này là người vận hành: chỉ gọi các lệnh ops có sẵn (`central.ops.*`, `edge.ops.seed`,
Alembic, `pipeline`) qua tiến trình con, không import code ứng dụng.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "infra" / "compose.yaml"
REAL_CRAWL = ROOT / "crawl_data"
FIXTURE_CRAWL = ROOT / "tests" / "fixtures" / "crawl_data"


@dataclass(frozen=True)
class EdgeStore:
    store_id: str
    db_port: int
    api_port: int
    #: Biến chứa khóa sync của cửa hàng trong file env (compose đọc đúng tên này).
    key_var: str

    @property
    def db_name(self) -> str:
        return "edge_" + self.store_id.replace("-", "_")


#: Khớp `infra/compose.yaml` (cổng host, tên DB, tên biến khóa).
STORES = (
    EdgeStore("store-001", 5433, 8001, "CENTRAL_API_KEY"),
    EdgeStore("store-002", 5435, 8002, "CENTRAL_API_KEY_STORE_002"),
    EdgeStore("store-003", 5436, 8003, "CENTRAL_API_KEY_STORE_003"),
)
CENTRAL_DB_PORT, CENTRAL_API = 5434, "http://localhost:8000"


# ─────────────────────────────── file env ───────────────────────────────


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def set_env(path: Path, key: str, value: str) -> None:
    """Đặt `KEY=value` trong file env: thay dòng có sẵn, không thì thêm cuối file."""
    text = path.read_text(encoding="utf-8")
    line = f"{key}={value}"
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    text = (
        pattern.sub(line, text, count=1) if pattern.search(text) else text.rstrip() + f"\n{line}\n"
    )
    path.write_text(text, encoding="utf-8")


# ─────────────────────────────── tiến trình con ───────────────────────────────


class Bootstrap:
    def __init__(self, args: argparse.Namespace) -> None:
        self.project: str = args.project
        self.env_file: Path = args.env_file.resolve()
        self.crawl: Path = args.crawl_data.resolve()
        self.stores = STORES[: args.stores]
        self.virtual: int = args.virtual
        self.virtual_keys: Path = args.virtual_keys.resolve()
        self.profiles = ["edge", "central", "data"]
        if args.stores > 1:
            self.profiles.append("edge-multi")
        if args.observability:
            self.profiles.append("observability")

    # ── lệnh ──

    def compose(self, *args: str, capture: bool = False) -> str:
        cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", self.project]
        cmd += ["--env-file", str(self.env_file)]
        for p in self.profiles:
            cmd += ["--profile", p]
        return self._run([*cmd, *args], capture=capture)

    def ops(self, *module_args: str, env: dict[str, str], capture: bool = False) -> str:
        """`python -m <module> ...` bằng CHÍNH interpreter đang chạy script (venv của repo)."""
        return self._run([sys.executable, *module_args], env=env, capture=capture)

    def _run(self, cmd: list[str], *, env: dict[str, str] | None = None, capture: bool) -> str:
        full_env = os.environ | {"PYTHONUTF8": "1"} | (env or {})
        result = subprocess.run(
            cmd, cwd=ROOT, env=full_env, capture_output=capture, text=True, encoding="utf-8"
        )
        if result.returncode != 0:
            # Không in `env`: có mật khẩu. stderr của lệnh ops không chứa secret.
            detail = (result.stderr or "")[-2000:] if capture else ""
            raise SystemExit(f"lỗi (mã {result.returncode}): {' '.join(cmd[:6])} …\n{detail}")
        return result.stdout or ""

    # ── môi trường cho lệnh ops trên máy host ──

    @property
    def env(self) -> dict[str, str]:
        return read_env(self.env_file)

    def central_url(self, driver: str = "postgresql+asyncpg") -> str:
        e = self.env
        user = e.get("CENTRAL_DB_USER", "central_app")
        return f"{driver}://{user}:{e['CENTRAL_DB_PASSWORD']}@localhost:{CENTRAL_DB_PORT}/central"

    def edge_url(self, store: EdgeStore) -> str:
        e = self.env
        user = e.get("EDGE_DB_USER", "edge_app")
        return (
            f"postgresql+asyncpg://{user}:{e['EDGE_DB_PASSWORD']}"
            f"@localhost:{store.db_port}/{store.db_name}"
        )

    def pipeline_env(self) -> dict[str, str]:
        e = self.env
        return {
            "PIPELINE_PG_DSN": self.central_url("postgresql"),
            "LAKE_ENDPOINT": "localhost:9000",
            "LAKE_ACCESS_KEY": e.get("MINIO_ROOT_USER", "minioadmin"),
            "LAKE_SECRET_KEY": e["MINIO_ROOT_PASSWORD"],
            "LAKE_URL_FOR_CLICKHOUSE": "http://minio:9000",
            "CLICKHOUSE_URL": "http://localhost:8123",
            "CLICKHOUSE_USER": e.get("CLICKHOUSE_USER", "dw"),
            "CLICKHOUSE_PASSWORD": e["CLICKHOUSE_PASSWORD"],
        }

    # ── các bước ──

    def step(self, title: str) -> None:
        print(f"\n▶ {title}", flush=True)

    def run(self) -> None:
        self.step(f"dữ liệu master: {self.crawl}")
        self.ensure_env_file()

        self.step("database, MinIO, ClickHouse")
        dbs = ["central-db", *(f"edge-db-{s.store_id}" for s in self.stores), "minio", "clickhouse"]
        self.compose("up", "-d", "--wait", *dbs)
        wait_http("http://localhost:8123/ping", what="ClickHouse")

        self.step("migration Alembic")
        alembic = ["-m", "alembic", "-c"]
        self.ops(
            *alembic,
            "packages/central/alembic.ini",
            "upgrade",
            "head",
            env={"DATABASE_URL": self.central_url()},
        )
        for s in self.stores:
            self.ops(
                *alembic,
                "packages/edge/alembic.ini",
                "upgrade",
                "head",
                env={"DATABASE_URL": self.edge_url(s)},
            )

        self.step("seed master data ở trung tâm")
        central_env = {"DATABASE_URL": self.central_url()}
        print(
            self.ops(
                "-m",
                "central.ops.seed",
                "--crawl-data",
                str(self.crawl),
                env=central_env,
                capture=True,
            ).strip()
        )

        self.step("Central API + khóa sync")
        self.compose("up", "-d", "--build", "central-api")
        wait_http(f"{CENTRAL_API}/health", what="Central API")
        self.ensure_store_keys(central_env)
        self.ensure_virtual_keys(central_env)

        self.step("nhân viên dựng sẵn (trung tâm + cửa hàng) và sản phẩm cửa hàng")
        password = {"SEED_EMPLOYEE_PASSWORD": self.env["SEED_EMPLOYEE_PASSWORD"]}
        ids = ",".join(s.store_id for s in self.stores)
        self.ops(
            "-m",
            "central.ops.seed",
            "--crawl-data",
            str(self.crawl),
            "--employees-for",
            ids,
            env=central_env | password,
            capture=True,
        )
        for s in self.stores:
            out = self.ops(
                "-m",
                "edge.ops.seed",
                "--crawl-data",
                str(self.crawl),
                "--employees",
                env={"DATABASE_URL": self.edge_url(s), "STORE_ID": s.store_id} | password,
                capture=True,
            )
            print(out.strip())

        self.step("lake + bảng bronze")
        print(self.ops("-m", "pipeline", "init", env=self.pipeline_env(), capture=True).strip())

        self.step("cả stack")
        self.compose("up", "-d", "--build", "--wait", "airflow-api-server")
        self.compose("up", "-d", "--build")
        for s in self.stores:
            wait_http(f"http://localhost:{s.api_port}/health", what=f"Edge API {s.store_id}")
        self.wait_dag()
        print(
            f"\n✅ stack '{self.project}' sẵn sàng: {len(self.stores)} cửa hàng, trung tâm, lake,"
            f" ClickHouse, Airflow (DAG retail_pipeline). Secret ở {self.env_file}."
        )

    def ensure_env_file(self) -> None:
        if not self.env_file.exists():
            self.env_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / "infra" / ".env.example", self.env_file)
            print(
                f"  tạo {self.env_file} từ infra/.env.example"
                " (giá trị dev — đổi trước khi dùng thật)"
            )
        if not self.env.get("SEED_EMPLOYEE_PASSWORD"):
            set_env(self.env_file, "SEED_EMPLOYEE_PASSWORD", secrets.token_urlsafe(18))
            print(f"  sinh SEED_EMPLOYEE_PASSWORD → {self.env_file.name} (không in ra)")

    def ensure_store_keys(self, central_env: dict[str, str]) -> None:
        for s in self.stores:
            if key_accepted(self.env.get(s.key_var, "")):
                print(f"  {s.store_id}: giữ khóa đang có")
                continue
            out = self.ops(
                "-m",
                "central.ops.provision_store",
                "--store-id",
                s.store_id,
                env=central_env,
                capture=True,
            )
            match = re.search(r"^CENTRAL_API_KEY=(\S+)$", out, re.MULTILINE)
            if match is None:
                raise SystemExit(f"provision_store không trả khóa cho {s.store_id}")
            set_env(self.env_file, s.key_var, match.group(1))
            print(f"  {s.store_id}: cấp khóa mới → {s.key_var} trong {self.env_file.name}")

    def ensure_virtual_keys(self, central_env: dict[str, str]) -> None:
        if not self.virtual:
            return
        if self.virtual_keys.exists():
            keys = [v for k, v in read_env(self.virtual_keys).items() if k.strip()]
            if len(keys) >= self.virtual and all(key_accepted(k) for k in keys[:2]):
                print(f"  {len(keys)} cửa hàng ảo: giữ khóa đang có ({self.virtual_keys.name})")
                return
        print(
            self.ops(
                "-m",
                "central.ops.provision_store",
                "--from-master",
                str(self.virtual),
                "--keys-out",
                str(self.virtual_keys),
                env=central_env,
                capture=True,
            ).strip()
        )

    def wait_dag(self, timeout: float = 300) -> None:
        scheduler = f"{self.project}-airflow-scheduler-1"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["docker", "exec", scheduler, "airflow", "dags", "list", "-o", "json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            try:
                if any(d.get("dag_id") == "retail_pipeline" for d in json.loads(result.stdout)):
                    print("  DAG retail_pipeline đã được đọc")
                    return
            except json.JSONDecodeError:
                pass
            time.sleep(5)
        raise SystemExit(f"quá {timeout:.0f}s mà Airflow chưa thấy DAG retail_pipeline")


def key_accepted(key: str) -> bool:
    """Trung tâm còn nhận khóa này không? Lô rỗng: xác thực chạy, không ghi gì (docs/13 §2)."""
    if not key:
        return False
    req = urllib.request.Request(  # noqa: S310 — URL cố định của stack cục bộ
        f"{CENTRAL_API}/api/v1/events",
        data=b"[]",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return bool(resp.status == 200)
    except urllib.error.HTTPError:
        return False


def wait_http(url: str, *, what: str, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 — URL cố định
                if resp.status == 200:
                    print(f"  {what}: sẵn sàng")
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(2)
    raise SystemExit(f"quá {timeout:.0f}s mà {what} ({url}) chưa trả lời")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--project", default="retail-sync", help="tên project compose (-p)")
    parser.add_argument("--env-file", type=Path, default=ROOT / "infra" / ".env")
    parser.add_argument(
        "--crawl-data",
        type=Path,
        default=REAL_CRAWL
        if (REAL_CRAWL / "products" / "product_details.csv").exists()
        else FIXTURE_CRAWL,
        help="mặc định crawl_data/ nếu có, không thì tests/fixtures/crawl_data/",
    )
    parser.add_argument("--stores", type=int, choices=[1, 2, 3], default=3)
    parser.add_argument("--virtual", type=int, default=20, help="số cửa hàng ảo cấp khóa (0 = bỏ)")
    parser.add_argument("--virtual-keys", type=Path, default=ROOT / "runs" / "virtual-keys.env")
    parser.add_argument(
        "--observability", action="store_true", help="thêm otel-lgtm + flow-monitor"
    )
    Bootstrap(parser.parse_args(argv)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
