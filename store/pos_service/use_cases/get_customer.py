class GetCustomerUseCase:
    def __init__(self, order_repo):
        self.order_repo = order_repo

    def execute(self, customer_id):
        return self.order_repo.get_customer(customer_id)
