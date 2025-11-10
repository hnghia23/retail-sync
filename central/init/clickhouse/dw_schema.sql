-- ===================================
-- TẠO DATABASE
-- ===================================
CREATE DATABASE IF NOT EXISTS dw;
USE dw;

-- ===================================
-- DIMENSION TABLES
-- ===================================

-- 🏬 DIM STORE
CREATE TABLE dim_store
(
    store_key UInt32,                    -- surrogate key
    store_id String,                     -- business key (từ OLTP)
    store_name String,
    address String,
    city String,
    manager_id UInt8,
    open_date Date,
    close_date Nullable(Date),
    is_active UInt8 DEFAULT 1,
    PRIMARY KEY store_key
)
ENGINE = MergeTree()
ORDER BY store_key;

-- 👔 DIM EMPLOYEE
CREATE TABLE dim_employee
(
    employee_key UInt32,
    employee_id UInt32,
    employee_name String,
    position String,
    start_date Date,
    retire_date Nullable(Date),
    salary UInt32,
    store_key UInt32,
    is_current UInt8 DEFAULT 1
)
ENGINE = MergeTree()
ORDER BY (store_key, employee_key);

-- 📦 DIM PRODUCT
CREATE TABLE dim_product
(
    product_key UInt32,
    product_id String,
    product_name String,
    price UInt64
)
ENGINE = MergeTree()
ORDER BY product_key;

-- 👤 DIM CUSTOMER
CREATE TABLE dim_customer
(
    customer_key UInt32,
    customer_id String,
    name String,
    join_date Date,
    tier String
)
ENGINE = MergeTree()
ORDER BY customer_key;

-- 📅 DIM DATE
CREATE TABLE dim_date
(
    date_key UInt32,          -- e.g. 20251103
    full_date Date,
    day UInt8,
    month UInt8,
    quarter UInt8,
    year UInt16,
    week UInt8,
    weekday_name String,
    is_holiday UInt8 DEFAULT 0
)
ENGINE = MergeTree()
ORDER BY date_key;

-- ===================================
-- FACT TABLES
-- ===================================

-- 💰 FACT SALES
CREATE TABLE fact_sales
(
    sales_key UUID DEFAULT generateUUIDv4(),
    date_key UInt32,
    store_key UInt32,
    employee_key UInt32,
    customer_key UInt32,
    product_key String,
    quantity UInt32,
    discount Decimal(5,2),
    final_amount UInt64,
    point_changed Int32 DEFAULT 0,
    payment_method Enum8('cash' = 1, 'card' = 2, 'e-wallet' = 3),
    created_at DateTime DEFAULT now()
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_at)
ORDER BY (store_key, date_key, created_at)
SETTINGS index_granularity = 8192;

-- ⭐ FACT POINT TRANSACTION
CREATE TABLE fact_point_transaction
(
    transaction_key UUID DEFAULT generateUUIDv4(),
    customer_key UInt32,
    store_key UInt32,
    date_key UInt32,
    points_changed Int32,
    transaction_time DateTime DEFAULT now()
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(transaction_time)
ORDER BY (customer_key, date_key, transaction_time)
SETTINGS index_granularity = 8192;
