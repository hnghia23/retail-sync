from sqlalchemy import Column, String, Integer, BigInteger, DateTime, ForeignKey
from db.base_model import Base, BaseModel

class LoyaltyCustomer(Base, BaseModel):
    __tablename__ = "loyalty_customer"

    customer_id = Column(String(20), primary_key=True)
    tier = Column(String(20))
    point = Column(Integer, default=0)
    total_money_used = Column(BigInteger, default=0)
    last_updated = Column(DateTime)
