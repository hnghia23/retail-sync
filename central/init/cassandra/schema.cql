-- Central Cassandra schema for loyalty
-- Keyspace and tables optimized for common read patterns

CREATE KEYSPACE IF NOT EXISTS loyalty WITH replication = {
  'class': 'SimpleStrategy',
  'replication_factor': '1'
};

USE loyalty;

-- Customer master record (fast lookup by customer_id)
CREATE TABLE IF NOT EXISTS customers_point (
  customer_id text PRIMARY KEY,
  tier text,
  points int,
  total_money_used int
);


CREATE TABLE IF NOT EXIST customers_info (
  customer_id text PRIMARY KEY, 
  name text, 
  join_date timestamp
)