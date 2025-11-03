from fastapi import FastAPI
import mysql.connector
from fastapi.middleware.cors import CORSMiddleware

from pos_service.adapters.mysql_repo import MySQLOrderRepository
from pos_service.use_cases.place_order import PlaceOrder
from pos_service.api.order_router import setup_routes

from pos_service.adapters.product_repository import ProductRepository
from pos_service.use_cases.get_product_price import GetProductPrices
from pos_service.api.product import setup_product_routes

app = FastAPI()

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

# Order Use Case
order_repo = MySQLOrderRepository(conn)
place_order_uc = PlaceOrder(order_repo)

# Product Use Case
product_repo = ProductRepository(conn)
get_product_prices_uc = GetProductPrices(product_repo)

# Routers
app.include_router(setup_routes(place_order_uc))
app.include_router(setup_product_routes(get_product_prices_uc))
