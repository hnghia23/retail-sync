from dataclasses import dataclass, field
from datetime import datetime
from typing import List
from .order_item import OrderItem

@dataclass
class Order:
    transaction_id: int
    employee_id: int
    customer_id: str
    created_at: datetime
    items: List[OrderItem] = field(default_factory=list)
    
    total_amount: int = 0.0
    discount: float = 0.0
    final_amount: float = 0.0
    payment_method: str = "CASH"

    def calculate_totals(self):
        self.total_amount = sum(i.total_price for i in self.items)
        self.final_amount = self.total_amount - self.discount
