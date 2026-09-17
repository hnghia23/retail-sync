# Image chung cho edge-api, central-api và sync-worker.
#
# Ba service này khác nhau đúng một thứ: câu lệnh chạy — nên compose truyền `command`,
# không phải ba Dockerfile gần giống nhau. Chúng dùng chung một `pyproject.toml`, một
# virtualenv, một tập `packages/`; tách file chỉ nhân ba chỗ phải sửa khi đổi base image.
#
# Build context là gốc repo (xem infra/compose.yaml).

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONPATH=/app/packages

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

# Cài dependency trước, copy source sau — đổi code không phải cài lại dependency.
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-install-project --no-dev

COPY packages/ /app/packages/
ENV PATH="/app/.venv/bin:$PATH"

# Không chạy bằng root.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8000

# Mặc định là Edge API. Sync worker và Central API override bằng `command` trong compose.
# Một uvicorn worker: mỗi cửa hàng một máy, tải đỉnh ~1 đơn/phút/quầy
# (docs/02-scale-capacity.md) — thêm worker chỉ tốn RAM, không tăng thông lượng.
CMD ["uvicorn", "edge.main:app", "--host", "0.0.0.0", "--port", "8000"]
