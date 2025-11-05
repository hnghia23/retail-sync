from dataclasses import dataclass

@dataclass
class OrderItem:
    product_id: str
    quantity: int
    unit_price: int
    
    @property
    def total_price(self) -> int:
        return self.quantity * self.unit_price
