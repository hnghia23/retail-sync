from dataclasses import dataclass 

@dataclass
class Customer:
    customer_id: str 
    points: float 
    discount_percentage: float 