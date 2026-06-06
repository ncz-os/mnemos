from mnemos.services.routing_engine.base import BaseRoutingRule
import uuid

class RuleLiftABTest:
    def __init__(self):
        self.variant = 'control' if hash(uuid.uuid4()) % 2 else 'experiment'
        self.metrics = {
            'variant': self.variant,
            'cost': 0,
            'latency': 0,
            'success': 0
        }

    def route_candidate(self, request):
        if self.variant == 'control':
            return SubFirstRouting().route(request)
        else:
            return APIFirstRouting().route(request)

    def update_metrics(self, cost, latency, success):
        self.metrics.update({
            'cost': cost,
            'latency': latency,
            'success': success
        })
        return self.metrics