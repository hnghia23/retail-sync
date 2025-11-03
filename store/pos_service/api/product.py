from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List
from use_cases.get_product_price import GetProductPrices

class ProductRequest(BaseModel):
    product_ids: List[int]

def setup_product_routes(use_case: GetProductPrices):
    router = APIRouter(prefix="/products", tags=["products"])

    @router.post("/prices")
    def get_product_prices(request: ProductRequest):
        if not request.product_ids:
            raise HTTPException(status_code=400, detail="product_ids cannot be empty")

        products = use_case.execute(request.product_ids)
        return products

    return router
