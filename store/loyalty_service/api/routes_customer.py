from fastapi import APIRouter
from services.cache_service import get_customer_from_cache
from repositories.customer_repo import get_customer_by_id

router = APIRouter()

@router.get("/customers/{customer_id}")
def get_customer(customer_id: str):
    cache_data = get_customer_from_cache(customer_id)
    if cache_data:
        return {"source": "cache", "data": cache_data}

    db_data = get_customer_by_id(customer_id)
    # lưu cache
    return {"source": "db", "data": db_data}
