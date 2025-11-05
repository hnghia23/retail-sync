-- ============================
-- SCHEMA: LOYALTY
-- ============================

CREATE SCHEMA IF NOT EXISTS loyalty;
SET search_path TO loyalty;


-- ============================
-- 1. Bảng hạng thành viên
-- ============================
CREATE TABLE loyalty_tier (
    tier VARCHAR(20) PRIMARY KEY,
    min_point INT NOT NULL CHECK (min_point >= 0),
    discount_percentage DECIMAL(5,2) NOT NULL CHECK (discount_percentage >= 0 AND discount_percentage <= 100),
    description TEXT
);


-- ============================
-- 2. Bảng thông tin khách hàng
-- ============================
CREATE TABLE customer_info (
    id VARCHAR(20) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    join_date TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_customer_info_joindate ON customer_info(join_date);


-- ============================
-- 3. Bảng điểm và hạng của khách hàng
-- ============================
CREATE TABLE loyalty_customer (
    customer_id VARCHAR(20) PRIMARY KEY,
    tier VARCHAR(20) REFERENCES loyalty_tier(tier),
    point INT DEFAULT 0 CHECK (point >= 0),
    total_money_used BIGINT DEFAULT 0 CHECK (total_money_used >= 0),
    last_updated TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (customer_id) REFERENCES customer_info(id)
);

CREATE INDEX idx_loyalty_customer_tier ON loyalty_customer(tier);
CREATE INDEX idx_loyalty_customer_lastupdated ON loyalty_customer(last_updated);

