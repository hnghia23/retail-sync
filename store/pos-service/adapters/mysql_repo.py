class MySQLOrderRepository:
    def __init__(self, conn):
        self.conn = conn

    def save(self, order):
        cursor = self.conn.cursor()

        cursor.execute(
            "INSERT INTO transactions (transaction_id, customer_id, employee_id, created_at, discount, final_amount, payment_method)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                order.transaction_id,
                order.customer_id,
                order.employee_id,
                order.created_at,
                order.discount,
                order.final_amount,
                order.payment_method,
            ),
        )
        
        for item in order.items:
            cursor.execute(
                "INSERT INTO transaction_item (transaction_id, product_id, quantity, unit_price)"
                " VALUES (%s, %s, %s, %s)",
                (order.transaction_id, item.product_id, item.quantity, item.unit_price),
            )

        self.conn.commit()
