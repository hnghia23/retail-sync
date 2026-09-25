# Postgres 18 với libfaketime — cho test "chỉnh đồng hồ tới tháng sau" (docs/08 §4.1,
# tests/integration/test_partition_clock.py). CHỈ để test: đồng hồ của cả server bị dời.
#
# - Chỉ tiến trình `postgres` nạp libfaketime (wrapper ở /usr/local/bin, đứng trước bin thật trong
#   PATH). Nạp cho cả script khởi tạo của image (bash) thì nó treo ngay từ đầu (thử 2026-09-25).
# - FAKETIME_TIMESTAMP_FILE + FAKETIME_NO_CACHE: đọc độ lệch từ file ở MỖI lần hỏi giờ, nên test
#   dời đồng hồ bằng cách ghi file (docker exec), không khởi động lại Postgres. Độ lệch dạng
#   "+<giây>" để đồng hồ vẫn CHẠY (đồng hồ đứng yên làm timeout, autovacuum... hỏng kiểu khác).
FROM postgres:18
RUN apt-get update \
    && apt-get install -y --no-install-recommends libfaketime \
    && rm -rf /var/lib/apt/lists/* \
    && echo "+0" > /etc/faketimerc && chmod 666 /etc/faketimerc \
    && printf '#!/bin/sh\nexport LD_PRELOAD=/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1\nexec /usr/lib/postgresql/18/bin/postgres "$@"\n' > /usr/local/bin/postgres \
    && chmod 755 /usr/local/bin/postgres
ENV FAKETIME_TIMESTAMP_FILE=/etc/faketimerc \
    FAKETIME_NO_CACHE=1
