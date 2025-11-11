from sqlalchemy import Column, String, Integer
from db.base_model import Base, BaseModel

class LoyaltyTier(Base, BaseModel):
    __tablename__ = "loyalty_tier"

    tier_name = Column(String(20), primary_key=True)
    min_points = Column(Integer, default=0)
    discount_rate = Column(Integer, default=0)  # percentage discount
    description = Column(String(255))