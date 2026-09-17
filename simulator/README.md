# Simulator

Sinh tải và mô phỏng nhiều cửa hàng — phục vụ kịch bản LD-1..LD-4 và CH-6
([docs/16-test-plan.md](../docs/16-test-plan.md)).

Mục tiêu tải đỉnh cần tái hiện: **~67 writes/giây ở 2000 cửa hàng**
([docs/02-scale-capacity.md](../docs/02-scale-capacity.md)). Con số này là lý do ADR-001 và
ADR-003 loại Cassandra/Kafka — simulator tồn tại để *chứng minh bằng số đo*, không phải để
ghi lại trong doc.
