import json
from db.redis import redis_client

CACHE_TTL = 3600  # 1 hour

def get_customer_from_cache(customer_id: str):
    key = f"customer:{customer_id}"
    data = redis_client.get(key)
    if not data:
        return None
    try:
        return json.loads(data)
    except Exception:
        return None

def set_customer_to_cache(customer_id: str, payload: dict, ttl: int = CACHE_TTL):
    key = f"customer:{customer_id}"
    redis_client.set(key, json.dumps(payload, default=str), ex=ttl)

def invalidate_customer_cache(customer_id: str):
    key = f"customer:{customer_id}"
    redis_client.delete(key)
