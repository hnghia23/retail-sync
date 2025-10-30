from dataclasses import dataclass
from datetime import datetime
from typing import Optional

@dataclass
class PointTransaction:
    event_id: str
    customer_id: str
    change_point: int
    money_used: float
    type: str  # "earn" | "redeem" | "adjust"
    store_id: Optional[str]
    created_at: datetime