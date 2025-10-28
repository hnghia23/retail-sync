DROP DATABASE IF EXISTS store;
CREATE DATABASE store;
USE store;

-- 1. Customer
CREATE TABLE customer (
    id INT PRIMARY KEY,
    points INT DEFAULT 0,
    discount_percentage DECIMAL(5,2) DEFAULT 0
);

-- 2. Employee
CREATE TABLE employee (
    id INT PRIMARY KEY, 
    employee_name VARCHAR(255) NOT NULL,
    position VARCHAR(20),
    start_date DATE NOT NULL, 
    retire_date DATE,
    salary DECIMAL(5, 2) NOT NULL
);

-- 3. Store
CREATE TABLE store (
    id INT PRIMARY KEY, 
    address VARCHAR(255),
    city VARCHAR(50),
    manager_id INT,
    open_date DATE,
    FOREIGN KEY (manager_id) REFERENCES employee(id)
);

-- 4. Products
CREATE TABLE products (
    id INT PRIMARY KEY, 
    product_name VARCHAR(255) NOT NULL, 
    price DECIMAL(10, 2) NOT NULL,
    UNIQUE(product_name)
);

-- 5. Transactions
CREATE TABLE transactions (
    transaction_id INT PRIMARY KEY AUTO_INCREMENT,
    employee_id INT NOT NULL, 
    customer_id INT, 
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    payment_method ENUM('cash','card','mobile'),
--     total_amount DECIMAL(10,2), 
    discount DECIMAL(5,2), 	
    final_amount DECIMAL(10,2),
    INDEX (employee_id),
    INDEX (customer_id),
    FOREIGN KEY (employee_id) REFERENCES employee(id),
    FOREIGN KEY (customer_id) REFERENCES customer(id)
);

-- 6. Transaction Item (chi tiết đơn hàng)
CREATE TABLE transaction_item (
    transaction_id INT,
    product_id INT,
    quantity INT, 
    unit_price DOUBLE,
    PRIMARY KEY(transaction_id, product_id),
    FOREIGN KEY (transaction_id) REFERENCES transactions(transaction_id),
    FOREIGN KEY (product_id) REFERENCES products(id)
);

INSERT INTO employee (id, employee_name, position, start_date, salary)
VALUES (5, 'Test Employee', 'Cashier', '2025-10-27 10:30:00', 100);

INSERT INTO customer (id, points, discount_percentage) VALUES (2001, 0, 0);


INSERT INTO products (id, product_name, price) VALUES (1, 'Product A', 100), (2, 'Product B', 50);