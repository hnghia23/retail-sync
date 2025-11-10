-- 1. Xóa và tạo lại database với UTF-8 đầy đủ
CREATE DATABASE IF NOT EXISTS store
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE store;

-- 2. Employee
CREATE TABLE employee (
    id INT PRIMARY KEY, 
    employee_name VARCHAR(255) NOT NULL,
    position VARCHAR(50),
    start_date DATE NOT NULL,   -- dùng DATETIME thay vì DATE
    retire_date DATE,
    salary INT NOT NULL
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- 3. Store
CREATE TABLE store (
    id VARCHAR(20) PRIMARY KEY, 
    address VARCHAR(255),
    city VARCHAR(50),
    manager_id INT,
    open_date DATE,
    FOREIGN KEY (manager_id) REFERENCES employee(id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- 4. Products
CREATE TABLE products (
    id VARCHAR(20) PRIMARY KEY, 
    product_name VARCHAR(255) NOT NULL, 
    price INT UNSIGNED NOT NULL,
    UNIQUE(product_name)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- 5. Transactions
CREATE TABLE transactions (
    transaction_id INT PRIMARY KEY AUTO_INCREMENT,
    employee_id INT NOT NULL, 
    customer_id VARCHAR(15), 
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    payment_method ENUM('cash','card','e-wallet'),
    discount DECIMAL(5,2), 	
    final_amount INT UNSIGNED NOT NULL,
    point_changed INT,
    INDEX (employee_id),
    FOREIGN KEY (employee_id) REFERENCES employee(id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- 6. Transaction Item (chi tiết đơn hàng)
CREATE TABLE transaction_item (
    transaction_id INT,
    product_id VARCHAR(20),
    quantity INT, 
    PRIMARY KEY(transaction_id, product_id),
    FOREIGN KEY (transaction_id) REFERENCES transactions(transaction_id),
    FOREIGN KEY (product_id) REFERENCES products(id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- === Insert dữ liệu mẫu ===
INSERT INTO employee (id, employee_name, position, start_date, salary)
VALUES (5, 'Test Employee', 'Cashier', '2025-10-27 10:30:00', 100000);

-- === Dữ liệu sản phẩm ===
INSERT INTO products (id, product_name, price) VALUES
('MNLS-ho', 'Combo 6 Muỗng cafe inox 11.6cm LUCKY_STAR, vỉ 2 cái MNLS', 210000),
('KZ118', 'Combo 12 Mút rửa ly tách POLY-TECH KZ118', 260000),
('SBCA3BD', '10 quyển Sổ A3 bìa cứng 6 tập bằng đầu 240 trang', 460000),
('OEMNGD1673', '4 can nước giặt D-nee Thái Lan 3L', 676000),
('OEMXD193-ho', '2 chai Xì dầu Càng Cua đặc biệt', 66000),
('THE001', '150 cái Bao đựng thẻ nhân viên 108', 169000),
('OEMST788', '60 tập truyện tranh Tam Quốc Diễn Nghĩa', 990000),
('OEMST661', 'Đừng phê bình tôi', 23000),
('MSRVH009', '2 chai Rượu Vang Hibiscus Roselle', 256000),
('MSRBN010', 'Rượu bán ngọt Hibiscus Roselle', 275000),
('HK25', 'Tấm sưởi điện HK25', 1100000),
('OEMQS842', 'Quạt sưởi Fairlady NSB-90 công nghệ Đức', 599000),
('ERKLT080-10', 'Combo 8 chai Nước giặt chống phai màu Kolortex tặng 2 chai xả vải + 1 chai lau sàn + 1 chai lau bếp', 549000),
('ECKLT079-10', 'Combo 4 chai Nước giặt chống lem màu Kolortex', 259000),
('OEMST716', 'Trò chuyện với cõi vô hình', 219000),
('OEMKS224', 'Kệ sắt để chậu hoa trang trí', 340000),
('OEMTM421', '2 lọ Tẩy mốc đa năng của Hàn Quốc', 119000),
('BS101', 'Hộp 12 cái Bút sơn Toyo SA101', 127000),
('NNLS-Vi', 'Combo 3 vỉ (6 cái)- Nĩa trái cây inox 11.6cm LUCKY_STAR', 158000),
('SC_MS1947', 'Bộ vợt & bẫy muỗi 2 trong 1', 299000),
('OEMMD945', 'Mũ Fedora Floppy dạ nỉ, vành nhỏ', 143000),
('OEMRT069', 'Bộ lấy ráy tai khô có đèn và kìm gắp', 116000),
('ST_403-ho', 'Giá úp ly cốc inox (25cm x 16cm x 21cm) ST_403', 180000),
('SC_F2022', 'Chảo chiên SUPER CHEF hợp kim tráng men đáy từ 22cm', 283000),
('SC_F2024', 'Chảo chiên SUPER CHEF hợp kim tráng men đáy từ 24cm', 293000),
('SC_F2026', 'Chảo chiên SUPER CHEF hợp kim tráng men đáy từ 26cm', 320000),
('SC_FM2016', 'Quánh nắp kính SUPER CHEF hợp kim phủ vân đá đáy từ 16cm', 283000),
('SC_S2020', 'Nồi nắp kính SUPER CHEF hợp kim tráng men đáy từ 20cm', 406000),
('SC_M2018', 'Quánh nắp kính SUPER CHEF hợp kim tráng men đáy từ 18cm', 348000),
('SC_M2014', 'Quánh nắp kính SUPER CHEF hợp kim tráng men đáy từ 14cm', 302000);

-- Giao dịch mẫu
INSERT INTO transactions (employee_id, customer_id, created_at, payment_method, discount, final_amount, point_changed)
VALUES
(5, '0901234567', '2025-10-28 09:30:00', 'cash', 0.00, 560000, 56),
(5, '0902345678', '2025-10-29 10:15:00', 'card', 5.00, 980000, 98),
(5, '0903456789', '2025-10-30 14:20:00', 'e-wallet', 0.00, 450000, 45),
(5, '0904567890', '2025-10-31 18:05:00', 'cash', 10.00, 1500000, 150),
(5, '0905678901', '2025-11-01 11:10:00', 'card', 0.00, 320000, 32);

-- Chi tiết giao dịch
INSERT INTO transaction_item (transaction_id, product_id, quantity)
VALUES
-- Giao dịch 1
(1, 'OEMNGD1673', 1),
(1, 'THE001', 1),

-- Giao dịch 2
(2, 'SC_F2024', 2),
(2, 'OEMST661', 1),

-- Giao dịch 3
(3, 'OEMTM421', 3),
(3, 'OEMMD945', 1),

-- Giao dịch 4
(4, 'ERKLT080-10', 1),
(4, 'OEMST716', 2),
(4, 'OEMKS224', 1),

-- Giao dịch 5
(5, 'BS101', 2),
(5, 'SC_MS1947', 1);
