class PlaceOrder:
    def __init__(self, order_repo, loyalty_client=None):
        self.order_repo = order_repo
        self.loyalty_client = loyalty_client
        # self.kafka_producer = kafka_producer

    def execute(self, order):
        try:
            # Persist order (may raise)
            self.order_repo.save(order)

            # TODO: call loyalty client asynchronously or publish event to message bus
            # if self.loyalty_client and order.customer_id:
            #     self.loyalty_client.accrue_points(order.customer_id, calculate_points(order))

            return {"transaction_id": order.transaction_id, "status": "success"}
        except Exception as e:
            # surface failure to API layer
            return {"transaction_id": getattr(order, 'transaction_id', None), "status": "failed", "error": str(e)}