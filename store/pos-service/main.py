from fastapi import FastAPI
import mysql.connector
from adapters.mysql_repo import MySQLOrderRepository
from use_cases.place_order import PlaceOrder
from api.order_router import setup_routes

app = FastAPI()

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


conn = mysql.connector.connect(
    host="localhost",
    port=3307,
    user="app_user",
    password="app_pass",
    database="store"
)

order_repo = MySQLOrderRepository(conn)
place_order_uc = PlaceOrder(order_repo)

app.include_router(setup_routes(place_order_uc))
