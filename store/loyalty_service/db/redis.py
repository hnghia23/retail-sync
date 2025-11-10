import redis
from config import REDIS_URL

# decode_responses=True để trả về str thay vì bytes
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
