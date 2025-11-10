from db.postgres import SessionLocal
from models.customer_info import CustomerInfo

def get_customer_by_id(customer_id):
    with SessionLocal() as db:
        return db.query(CustomerInfo).filter(CustomerInfo.id == customer_id).first()
