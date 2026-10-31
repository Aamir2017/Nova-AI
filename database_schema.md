# Database Schema (`ecommerce_db`)

The chatbot requires a MySQL database named `ecommerce_db`. To run the backend successfully, execute the following SQL commands to create the 7 required tables before starting the Rasa Action Server.

### 1. `accounts`
*Note: The `actions.py` script will attempt to auto-generate this table and create a default admin account on startup, but the explicit schema is provided here for reference.*
```sql
CREATE TABLE accounts (
    user_id INT AUTO_INCREMENT PRIMARY KEY,
    user_name VARCHAR(50) NOT NULL UNIQUE,
    email VARCHAR(100) NOT NULL UNIQUE,
    password VARCHAR(255) NOT NULL,
    tier ENUM('Standard', 'Prime', 'Admin') DEFAULT 'Standard',
    wallet_balance DECIMAL(10, 2) DEFAULT 0.00,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;
```

### 2. `products`
```sql
CREATE TABLE products (
    product_id INT AUTO_INCREMENT PRIMARY KEY,
    sku VARCHAR(50) UNIQUE,
    name VARCHAR(255) NOT NULL,
    brand VARCHAR(100),
    category VARCHAR(100),
    price DECIMAL(10, 2),
    discount_percent INT DEFAULT 0,
    stock_quantity INT DEFAULT 0,
    rating DECIMAL(3, 2),
    warranty_months INT,
    is_returnable BOOLEAN DEFAULT TRUE,
    specs_json JSON,
    description TEXT
);
```

### 3. `orders`
```sql
CREATE TABLE orders (
    order_id INT AUTO_INCREMENT PRIMARY KEY,
    user_name VARCHAR(100),
    total_amount DECIMAL(10, 2),
    discount_applied DECIMAL(10, 2) DEFAULT 0.00,
    payment_method VARCHAR(50),
    status ENUM('Processing', 'Shipped', 'Out for Delivery', 'Delivered', 'Cancelled', 'Returned') DEFAULT 'Processing',
    tracking_number VARCHAR(50),
    ordered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    delivered_at DATETIME
);
```

### 4. `order_items`
```sql
CREATE TABLE order_items (
    item_id INT AUTO_INCREMENT PRIMARY KEY,
    order_id INT,
    product_id INT,
    product_name VARCHAR(255),
    quantity INT,
    unit_price DECIMAL(10, 2),
    item_status ENUM('Active', 'Cancelled', 'Return Requested', 'Returned') DEFAULT 'Active',
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);
```

### 5. `coupons`
```sql
CREATE TABLE coupons (
    coupon_code VARCHAR(50) PRIMARY KEY,
    discount_percent INT,
    min_order_value DECIMAL(10, 2) DEFAULT 0.00,
    max_discount DECIMAL(10, 2),
    required_tier ENUM('All', 'Standard', 'Prime', 'Admin') DEFAULT 'All',
    is_active BOOLEAN DEFAULT TRUE
);
```

### 6. `support_tickets`
```sql
CREATE TABLE support_tickets (
    ticket_id INT AUTO_INCREMENT PRIMARY KEY,
    user_name VARCHAR(100),
    order_id INT NULL,
    issue_category ENUM('General', 'Warranty', 'Bot Escalation') DEFAULT 'General',
    priority ENUM('Low', 'Medium', 'High') DEFAULT 'Medium',
    description TEXT,
    status ENUM('Open', 'In Progress', 'Resolved') DEFAULT 'Open',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### 7. `returns`
```sql
CREATE TABLE returns (
    return_id INT AUTO_INCREMENT PRIMARY KEY,
    order_id INT,
    user_name VARCHAR(100),
    reason TEXT,
    resolution_type VARCHAR(100),
    refund_amount DECIMAL(10, 2),
    return_status VARCHAR(50)
);
```