from sqlalchemy import Column, String, DateTime
from db.base_model import Base, BaseModel

class CustomerInfo(Base, BaseModel):
    __tablename__ = "customer_info"
    id = Column(String(20), primary_key=True)
    name = Column(String(100))
    join_date = Column(DateTime)
