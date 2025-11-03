from typing import List, Dict
import mysql.connector

class ProductRepository:
    def __init__(self, conn: mysql.connector.MySQLConnection):
        self.conn = conn

    def get_products_by_ids(self, product_ids: List[int]) -> List[Dict]:
        if not product_ids:
            return []
        cursor = self.conn.cursor(dictionary=True)
        format_strings = ','.join(['%s'] * len(product_ids))
        query = f"SELECT id, product_name, price FROM products WHERE id IN ({format_strings})"
        cursor.execute(query, tuple(product_ids))
        rows = cursor.fetchall()
        cursor.close()
        return rows
