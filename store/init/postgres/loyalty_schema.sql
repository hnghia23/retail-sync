CREATE TABLE loyalty_tier (
    tier VARCHAR(20) PRIMARY KEY,
    min_point INT NOT NULL,
    discount_percentage NUMERIC(5,2) NOT NULL,
    description TEXT
);

CREATE TABLE customer_info (
    id VARCHAR(30) PRIMARY KEY,
    name VARCHAR(100),
    join_date TIMESTAMP DEFAULT NOW()
);

CREATE TABLE loyalty_customer (
    customer_id VARCHAR(30) PRIMARY KEY,
    tier VARCHAR(20),
    point INT DEFAULT 0,
    total_money_used NUMERIC(18,2) DEFAULT 0,
    FOREIGN KEY (tier) REFERENCES loyalty_tier(tier),
    FOREIGN KEY (customer_id) REFERENCES customer_info(id)
);

CREATE INDEX idx_loyalty_customer_customerid ON loyalty_customer(customer_id);
CREATE INDEX idx_customerinf_customerid ON customer_info(id);
