import logging
from urllib.parse import urlencode

from .errors import SyncError
from .http import HttpClient
from .models import is_deleted, order_fields

LOG = logging.getLogger(__name__)


class BigCommerceClient:
    def __init__(self, config, http=None):
        self.config = config
        self.http = http or HttpClient("BigCommerce", config.timeout, config.attempts)
        self.headers = {"X-Auth-Token": config.api_token, "Accept": "application/json"}
        self.pages = 0
        self.scanned = 0
        self.deleted_skipped = 0

    def fetch_orders(self):
        orders = {}
        seen_ids = set()
        page = 1
        while True:
            query = urlencode({"min_id": self.config.min_id, "is_deleted": "false",
                               "limit": 250, "page": page, "sort": "id:asc"})
            url = self.config.store_url.rstrip("/") + "/v2/orders/?" + query
            data = self.http.request("GET", url, headers=self.headers)
            # BigCommerce may return 204 at the end of the collection.
            if data is None or data == []:
                LOG.info("BigCommerce: exhausted at page %s", page)
                break
            if not isinstance(data, list):
                raise SyncError(f"BigCommerce page {page}: expected an array")
            self.pages += 1
            for order in data:
                if not isinstance(order, dict):
                    raise SyncError(f"BigCommerce page {page}: invalid order")
                order_id = order.get("id")
                if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id < self.config.min_id:
                    raise SyncError(f"BigCommerce page {page}: invalid/out-of-range order ID")
                if order_id in seen_ids:
                    raise SyncError(f"BigCommerce repeated order ID {order_id}; no SharePoint writes performed")
                seen_ids.add(order_id)
                self.scanned += 1
                if is_deleted(order):
                    self.deleted_skipped += 1
                    continue
                fields = order_fields(order)
                orders[fields["Title"]] = fields
            LOG.info("BigCommerce page %s: %s received, %s active orders collected",
                     page, len(data), len(orders))
            page += 1
        return orders
