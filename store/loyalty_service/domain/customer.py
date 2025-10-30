from dataclasses import dataclass 
from datetime import datetime
from typing import Optional

@dataclass
class Customer:
    customer_id: str  # phone used as id
    name: str
    join_date: datetime
    tier: str
    point: int
    total_money_used: float