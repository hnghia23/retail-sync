"""`infra/store_backup.py` — phần quyết định (chọn bản, luật so), không cần Docker."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_PATH = Path(__file__).resolve().parents[2] / "infra" / "store_backup.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("store_backup", _PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # @dataclass tra module qua sys.modules lúc tạo lớp
    spec.loader.exec_module(mod)
    return mod


sb = _module()


def test_latest_is_the_newest_timestamp_not_the_listing_order() -> None:
    dumps = ["store-001-20260925T030000Z.dump", "store-001-20260926T030000Z.dump",
             "store-001-20260924T030000Z.dump"]  # fmt: skip
    assert sb.latest(dumps) == "store-001-20260926T030000Z.dump"
    with pytest.raises(SystemExit):
        sb.latest([])


def test_restored_copy_may_lag_live_but_never_exceed_it() -> None:
    live = dict.fromkeys(sb.TABLES, 100)
    assert (
        sb.compare(live, dict.fromkeys(sb.TABLES, 90)) == []
    )  # backup chụp ở quá khứ: bình thường
    ahead = dict.fromkeys(sb.TABLES, 90) | {"sale": 101}
    assert sb.compare(live, ahead) == ["sale: bản khôi phục 101 > DB thật 100"]
    # outbox được dọn định kỳ nên được phép lệch cả hai chiều.
    assert sb.compare(live, dict.fromkeys(sb.TABLES, 90) | {"outbox": 500}) == []
    missing = {t: 1 for t in sb.TABLES if t != "point_ledger_local"}
    assert sb.compare(live, missing) == ["thiếu bảng point_ledger_local"]


def test_target_names_match_compose() -> None:
    t = sb.Target("retail-sync", "store-002")
    assert t.sidecar == "retail-sync-edge-backup-store-002-1"
    assert t.database == "edge_store_002"
    assert "retail-sync-edge-sync-worker-store-002-1" in t.containers_to_stop()
