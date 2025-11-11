import json
import time
from kafka import KafkaProducer
from config import KAFKA_BOOTSTRAP, KAFKA_TOPIC

# tạo producer (sử dụng value_serializer để gửi JSON)
producer = KafkaProducer(
    bootstrap_servers=[KAFKA_BOOTSTRAP],
    value_serializer=lambda v: json.dumps(v, default=str).encode('utf-8'),
    retries=5,
)

def publish_event(event_type: str, payload: dict):
    """
    event_type: e.g. 'customer.updated', 'customer.created', 'loyalty.point_updated'
    payload: dict chứa dữ liệu cần đồng bộ (customer_id, store_id, points, total_money_used, last_updated, ...)
    """
    event = {
        "type": event_type,
        "timestamp": int(time.time() * 1000),
        "payload": payload
    }
    
    # async send: returns Future; call get() nếu muốn block/kiểm tra
    producer.send(KAFKA_TOPIC, event)
    # flush không quá thường xuyên trong production, nhưng để đảm bảo demo, ta flush
    # producer.flush()
