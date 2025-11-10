from flask import Flask, render_template, request, jsonify
from sqlalchemy.orm import Session
from db.postgres import engine
from models.customer_info import CustomerInfo
from models.loyalty_customer import LoyaltyCustomer 
from services.cache_service import get_customer_from_cache, set_customer_to_cache, invalidate_customer_cache
from services.sync_service import publish_event
from config import STORE_ID

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/customer", methods=["GET"])
def get_customer():
    customer_id = request.args.get("id")
    if not customer_id:
        return jsonify({"error": "Missing customer_id"}), 400

    # 1) thử cache trước
    cached = get_customer_from_cache(customer_id)
    if cached:
        return jsonify({"source": "cache", "data": cached})

    # 2) nếu không có cache thì đọc DB (join customer_info + loyalty_customer nếu có)
    with Session(engine) as session:
        customer = session.query(CustomerInfo).filter_by(id=customer_id).first()
        loyalty = session.query(LoyaltyCustomer).filter_by(customer_id=customer_id).first()

        if not customer:
            return jsonify({"message": f"Customer {customer_id} not found"}), 404

        data = customer.as_dict()
        if loyalty:
            data.update({
                "tier": loyalty.tier,
                "point": loyalty.point,
                "total_money_used": loyalty.total_money_used,
                "last_updated": loyalty.last_updated.isoformat() if loyalty.last_updated else None
            })

        # 3) lưu cache
        set_customer_to_cache(customer_id, data)
        return jsonify({"source": "db", "data": data})


@app.route("/api/customer", methods=["POST"])
def create_or_update_customer():
    """
    Body JSON:
    {
      "id": "C001",
      "name": "Nguyen A",
      "join_date": "2025-11-10T08:00:00Z"  # optional
    }
    """
    payload = request.get_json()
    if not payload or "id" not in payload or "name" not in payload:
        return jsonify({"error": "Missing id or name"}), 400

    cid = payload["id"]
    name = payload["name"]

    with Session(engine) as session:
        customer = session.query(CustomerInfo).filter_by(id=cid).first()
        if not customer:
            customer = CustomerInfo(id=cid, name=name)
            session.add(customer)
            session.commit()
            created = True
        else:
            customer.name = name
            session.commit()
            created = False

        # Lấy data trả về
        data = customer.as_dict()

    # invalidate và set cache mới
    invalidate_customer_cache(cid)
    set_customer_to_cache(cid, data)

    # publish event to kafka
    publish_event("customer.created" if created else "customer.updated", {
        "customer_id": cid,
        "store_id": STORE_ID,
        "name": name,
        "join_date": data.get("join_date")
    })

    return jsonify({"ok": True, "created": created, "data": data}), 201 if created else 200


@app.route("/api/loyalty/add_points", methods=["POST"])
def add_points():
    """
    Body JSON:
    {
      "customer_id": "C001",
      "earned_point": 100,
      "amount": 250000   # money used in this transaction
    }
    """
    body = request.get_json()
    if not body:
        return jsonify({"error": "Missing body"}), 400

    cid = body.get("customer_id")
    earned = int(body.get("earned_point", 0))
    amount = int(body.get("amount", 0))

    with Session(engine) as session:
        loyalty = session.query(LoyaltyCustomer).filter_by(customer_id=cid).first()
        if not loyalty:
            # nếu chưa có record loyalty, tạo mới
            loyalty = LoyaltyCustomer(customer_id=cid, point=earned, total_money_used=amount, tier=None)
            session.add(loyalty)
        else:
            loyalty.point = (loyalty.point or 0) + earned
            loyalty.total_money_used = (loyalty.total_money_used or 0) + amount
        loyalty.last_updated = None  # SQLAlchemy onupdate/calc if set up; for demo set manually
        import datetime
        loyalty.last_updated = datetime.datetime.utcnow()
        session.commit()

        # build payload to cache & event
        payload = {
            "customer_id": cid,
            "point": loyalty.point,
            "total_money_used": loyalty.total_money_used,
            "tier": loyalty.tier,
            "last_updated": loyalty.last_updated.isoformat()
        }

    # update cache (join with customer info ideally)
    # fetch customer info
    with Session(engine) as session:
        customer = session.query(CustomerInfo).filter_by(id=cid).first()
        if customer:
            payload.update(customer.as_dict())

    set_customer_to_cache(cid, payload)

    # publish event
    publish_event("loyalty.point_updated", {
        "customer_id": cid,
        "store_id": STORE_ID,
        "points": loyalty.point,
        "total_money_used": loyalty.total_money_used,
        "last_updated": payload["last_updated"]
    })

    return jsonify({"ok": True, "data": payload})
