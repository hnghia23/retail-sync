from fastapi import APIRouter
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
    def create_order(order_body: dict):
        # Extract basic fields
        transaction_id = str(order_body.get("transaction_id"))
        customer_id = str(order_body.get("customer_id"))
        employee_id = str(order_body.get("employee_id"))
        created_at = order_body.get("created_at")
        discount = float(order_body.get("discount", 0.0))
        payment_method = order_body.get("payment_method", "CASH")

        # Parse product items
        items = []
        idx = 1
        while True:
            pid_key = f"product_id_{idx}"
            qty_key = f"qty_{idx}"
            price_key = f"price_{idx}"

            if pid_key not in order_body:
                break

            product_id = str(order_body.get(pid_key))
            quantity = int(order_body.get(qty_key))
            price = float(order_body.get(price_key))

            # Create OrderItem domain object
            items.append(OrderItem(product_id=product_id,
                                quantity=quantity,
                                unit_price=price))
            idx += 1

        if not items:
            return {"error": "No valid order items found"}

        order = Order(
            transaction_id=transaction_id,
            customer_id=customer_id,
            employee_id=employee_id,
            created_at=created_at,
            items=items,
            discount=discount,
            payment_method=payment_method
        )

        return place_order_uc.execute(order)

    return router