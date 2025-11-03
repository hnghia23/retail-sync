from typing import List, Dict

class GetProductPrices:
    def __init__(self, repository):
        self.repository = repository

    def execute(self, product_ids: List[int]) -> List[Dict]:
        products = self.repository.get_products_by_ids(product_ids)
        return [
            {"id": p["id"], "name": p["product_name"], "price": float(p["price"])}
            for p in products
        ]
