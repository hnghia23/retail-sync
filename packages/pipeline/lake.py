"""Lake — MinIO/S3 qua `pyarrow.fs.S3FileSystem`. Bronze BẤT BIẾN (docs/03 §6, ADR-005).

Một đối tượng đã ghi thì không bao giờ ghi lại: bộ trích xuất kiểm tồn tại trước khi ghi.
Đó là thứ làm "chạy lại" an toàn mà không cần bảng trạng thái nào — chính lake là trạng thái.
PUT của S3 là nguyên tử: đối tượng xuất hiện trọn vẹn hoặc không xuất hiện, không có file dở.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pyarrow.fs as pafs

if TYPE_CHECKING:
    from pipeline.config import LakeConfig

__all__ = ["Lake"]


class Lake:
    def __init__(self, cfg: LakeConfig) -> None:
        self.cfg = cfg
        self.fs = self._fs(allow_bucket_creation=False)

    def _fs(self, *, allow_bucket_creation: bool) -> pafs.S3FileSystem:
        return pafs.S3FileSystem(
            access_key=self.cfg.access_key,
            secret_key=self.cfg.secret_key,
            endpoint_override=self.cfg.endpoint,
            scheme=self.cfg.scheme,
            region="us-east-1",
            allow_bucket_creation=allow_bucket_creation,
        )

    def ensure_bucket(self) -> None:
        """CHỈ nơi này được tạo bucket (lệnh `init`). Đường ghi dữ liệu dùng filesystem cấm tạo:
        gõ sai tên bucket thì phải lỗi, không được lặng lẽ tạo ra một lake thứ hai."""
        admin = self._fs(allow_bucket_creation=True)
        if admin.get_file_info(self.cfg.bucket).type == pafs.FileType.NotFound:
            admin.create_dir(self.cfg.bucket)

    def key(self, table: str, dt: str, name: str) -> str:
        """`<bucket>/bronze/central/<bảng>/dt=<ngày nạp>/<tên>` — docs/17 §2."""
        return f"{self.cfg.bucket}/{self.cfg.prefix}/{table}/dt={dt}/{name}"

    def url_for_clickhouse(self, key: str) -> str:
        return f"{self.cfg.url_for_clickhouse.rstrip('/')}/{key}"

    def exists(self, key: str) -> bool:
        return bool(self.fs.get_file_info(key).type == pafs.FileType.File)

    def put(self, key: str, data: bytes) -> None:
        with self.fs.open_output_stream(key) as out:
            out.write(data)

    def get(self, key: str) -> bytes:
        with self.fs.open_input_stream(key) as src:
            return bytes(src.read())

    def sha256(self, key: str) -> str:
        return hashlib.sha256(self.get(key)).hexdigest()

    def latest_files(self, table: str) -> list[str]:
        """Các file trong phân vùng `dt=` MỚI NHẤT của bảng, sắp theo tên.

        Cho bộ giám sát (`pipeline.monitor`), đo mỗi 30 giây: `list_table` liệt kê mọi file
        từ ngày đầu (một file/giờ/bảng → ~9 nghìn file/năm/bảng), còn đây chỉ liệt kê thư mục
        `dt=` rồi một thư mục. Đúng vì tên phân vùng là ngày của MÉP ĐẦU cửa sổ và cửa sổ nối
        tiếp nhau: cửa sổ có mép cuối lớn nhất cũng có mép đầu lớn nhất.
        """
        base = f"{self.cfg.bucket}/{self.cfg.prefix}/{table}"
        if self.fs.get_file_info(base).type == pafs.FileType.NotFound:
            return []
        parts = [
            i.path
            for i in self.fs.get_file_info(pafs.FileSelector(base))
            if i.type == pafs.FileType.Directory and i.base_name.startswith("dt=")
        ]
        if not parts:
            return []
        infos = self.fs.get_file_info(pafs.FileSelector(max(parts)))
        return sorted(i.path for i in infos if i.type == pafs.FileType.File)

    def list_table(self, table: str) -> list[str]:
        """Mọi file của một bảng, sắp theo tên (= theo thời gian, vì tên bắt đầu bằng mốc)."""
        base = f"{self.cfg.bucket}/{self.cfg.prefix}/{table}"
        if self.fs.get_file_info(base).type == pafs.FileType.NotFound:
            return []
        infos = self.fs.get_file_info(pafs.FileSelector(base, recursive=True))
        return sorted(i.path for i in infos if i.type == pafs.FileType.File)
