from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List
from datetime import datetime
from domain.order import Order
from domain.order_item import OrderItem

router = APIRouter()


class OrderItemDTO(BaseModel):
    product_id: str
    quantity: int
    unit_price: float


class OrderDTO(BaseModel):
    transaction_id: str
    customer_id: str
    employee_id: str
    created_at: datetime
    items: List[OrderItemDTO]
    discount: float = 0.0
    payment_method: str = "CASH"


def setup_routes(place_order_uc):

    @router.post("/orders")
    def create_order(order_body: OrderDTO):
        # Validate items
        if not order_body.items or len(order_body.items) == 0:
            raise HTTPException(status_code=400, detail="No valid order items found")

        items = [
            OrderItem(product_id=item.product_id, quantity=item.quantity, unit_price=item.unit_price)
            for item in order_body.items
        ]

        order = Order(
            transaction_id=order_body.transaction_id,
            customer_id=order_body.customer_id,
            employee_id=order_body.employee_id,
            created_at=order_body.created_at,
            items=items,
            discount=order_body.discount,
            payment_method=order_body.payment_method,
        )

        result = place_order_uc.execute(order)
        if isinstance(result, dict) and result.get("status") == "failed":
            raise HTTPException(status_code=500, detail=result.get("error", "order_failed"))

        return result

    return router