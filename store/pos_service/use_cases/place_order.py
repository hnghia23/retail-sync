class PlaceOrder:
    def __init__(self, order_repo):
        self.order_repo = order_repo
        # self.loyalty_client = loyalty_client 
        # self.kafka_producer = kafka_producer 

    def execute(self, order):
        # customer = self.loyalty_client.get_customer(order.customer_id)

        # if not customer:
        #     customer = 'khach le'

        
        self.order_repo.save(order)