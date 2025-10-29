from dataclasses import dataclass

@dataclass
class OrderItem:
    product_id: str
    quantity: int
    unit_price: float
    
    @property
    def total_price(self) -> float:
        return self.quantity * self.unit_price
