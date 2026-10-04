"""HTTP API: framework-free handlers plus a WSGI adapter.

    POST /orders              {"customer": str, "sku": str, "quantity": int}
    GET  /orders?customer=X   orders for one customer
    GET  /orders/<id>         order summary
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from orders import db
from orders.service import (
    DEFAULT_STOCK,
    Inventory,
    InvalidQuantity,
    OrderService,
    OutOfStock,
    UnknownSku,
    WarehouseClient,
)

ORDER_PATH = re.compile(r"/orders/(\d+)")
MAX_BODY_BYTES = 64 * 1024


@dataclass
class Response:
    status: int
    body: Any


def _error(status: int, message: str) -> Response:
    return Response(status, {"error": message})


class OrderAPI:
    def __init__(self, service: OrderService) -> None:
        self.service = service

    def create_order(self, payload: Any) -> Response:
        if not isinstance(payload, dict):
            return _error(400, "body must be a JSON object")
        customer, sku, quantity = payload.get("customer"), payload.get("sku"), payload.get("quantity")
        if not isinstance(customer, str) or not customer.strip():
            return _error(400, "customer is required")
        if not isinstance(sku, str):
            return _error(400, "sku is required")
        if not isinstance(quantity, int) or isinstance(quantity, bool):
            return _error(400, "quantity must be an integer")
        try:
            order = self.service.place_order(customer.strip(), sku, quantity)
        except (UnknownSku, InvalidQuantity) as exc:
            return _error(400, str(exc))
        except OutOfStock as exc:
            return _error(409, str(exc))
        return Response(201, asdict(order))

    def get_order(self, order_id: int) -> Response:
        summary = self.service.summarize(order_id)
        return Response(200, summary) if summary else _error(404, "order not found")

    def list_orders(self, customer: str) -> Response:
        if not customer:
            return _error(400, "customer query parameter is required")
        return Response(200, [asdict(o) for o in self.service.orders_for(customer)])


def make_wsgi_app(api: OrderAPI):
    def app(environ, start_response):
        method = environ["REQUEST_METHOD"]
        path = environ.get("PATH_INFO", "")
        match = ORDER_PATH.fullmatch(path)
        if method == "POST" and path == "/orders":
            response = _with_json_body(environ, api.create_order)
        elif method == "GET" and path == "/orders":
            query = parse_qs(environ.get("QUERY_STRING", ""))
            response = api.list_orders(query.get("customer", [""])[0])
        elif method == "GET" and match:
            response = api.get_order(int(match.group(1)))
        else:
            response = _error(404, "not found")

        body = json.dumps(response.body).encode()
        status = HTTPStatus(response.status)
        start_response(
            f"{status.value} {status.phrase}",
            [("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
        )
        return [body]

    return app


def _with_json_body(environ, handler) -> Response:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return _error(400, "invalid Content-Length")
    if length > MAX_BODY_BYTES:
        return _error(413, "body too large")
    try:
        payload = json.loads(environ["wsgi.input"].read(length) or b"null")
    except ValueError:
        return _error(400, "body must be valid JSON")
    return handler(payload)


def build_api(db_path: str = "orders.db") -> OrderAPI:
    service = OrderService(db.connect(db_path), Inventory(DEFAULT_STOCK, WarehouseClient()))
    return OrderAPI(service)


if __name__ == "__main__":
    with make_server("127.0.0.1", 8000, make_wsgi_app(build_api())) as server:
        print("Serving on http://127.0.0.1:8000")
        server.serve_forever()
