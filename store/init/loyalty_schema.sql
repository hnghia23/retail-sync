CREATE TABLE loyalty_tier (
    tier VARCHAR(20) PRIMARY KEY,
    min_point INT NOT NULL,
    discount_percentage NUMERIC(5,2) NOT NULL,
    description TEXT
);

CREATE TABLE loyalty_customer (
    id SERIAL PRIMARY KEY,
    customer_id VARCHAR(30) UNIQUE NOT NULL,
    name VARCHAR(100),
    join_date TIMESTAMP DEFAULT NOW(),
    tier VARCHAR(20),
    point INT DEFAULT 0,
    total_money_used NUMERIC(18,2) DEFAULT 0,
    FOREIGN KEY (tier) REFERENCES loyalty_tier(tier)
);

CREATE TABLE loyalty_point_transactions (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(100) UNIQUE,   -- idempotency token
    customer_id VARCHAR(30) NOT NULL,
    change_point INT NOT NULL,
    money_used NUMERIC(18,2),
    type VARCHAR(20) NOT NULL,
    store_id VARCHAR(50),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_loyalty_customer_customerid ON loyalty_customer(customer_id);
CREATE INDEX idx_tx_customer ON loyalty_point_transactions(customer_id);