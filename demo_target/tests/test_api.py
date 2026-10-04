from __future__ import annotations

import io
import json
from wsgiref.util import setup_testing_defaults

from orders.api import make_wsgi_app


def test_create_order_returns_201(api):
    response = api.create_order({"customer": "alice", "sku": "WIDGET", "quantity": 2})

    assert response.status == 201
    assert response.body["total_cents"] == 500


def test_create_order_validates_payload(api):
    assert api.create_order({"customer": "alice", "sku": "WIDGET", "quantity": "2"}).status == 400
    assert api.create_order({"customer": "", "sku": "WIDGET", "quantity": 1}).status == 400
    assert api.create_order({"customer": "alice", "sku": "NOPE", "quantity": 1}).status == 400
    assert api.create_order(["not", "an", "object"]).status == 400


def test_create_order_out_of_stock_returns_409(api):
    assert api.create_order({"customer": "alice", "sku": "GIZMO", "quantity": 5}).status == 409


def test_get_order_summary(api):
    created = api.create_order({"customer": "alice", "sku": "GADGET", "quantity": 2}).body
    response = api.get_order(created["id"])

    assert response.status == 200
    assert response.body["unit_price_cents"] == 1200


def test_get_unknown_order_returns_404(api):
    assert api.get_order(12345).status == 404


def test_get_zero_quantity_order(api):
    created = api.create_order({"customer": "alice", "sku": "WIDGET", "quantity": 0}).body
    response = api.get_order(created["id"])

    assert response.status == 200
    assert response.body["unit_price_cents"] == 0


def test_list_orders_for_customer_with_apostrophe(api):
    api.create_order({"customer": "D'Angelo", "sku": "WIDGET", "quantity": 1})
    response = api.list_orders("D'Angelo")

    assert response.status == 200
    assert [o["customer"] for o in response.body] == ["D'Angelo"]


def call_wsgi(app, method, path, body=None, query=""):
    raw = json.dumps(body).encode() if body is not None else b""
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(raw)),
        "wsgi.input": io.BytesIO(raw),
    }
    setup_testing_defaults(environ)
    captured = {}

    def start_response(status, headers):
        captured["status"] = int(status.split()[0])

    payload = b"".join(app(environ, start_response))
    return captured["status"], json.loads(payload)


def test_wsgi_round_trip(api):
    app = make_wsgi_app(api)

    status, created = call_wsgi(app, "POST", "/orders", {"customer": "bob", "sku": "WIDGET", "quantity": 1})
    assert status == 201

    status, summary = call_wsgi(app, "GET", f"/orders/{created['id']}")
    assert status == 200 and summary["customer"] == "bob"

    status, listed = call_wsgi(app, "GET", "/orders", query="customer=bob")
    assert status == 200 and [o["id"] for o in listed] == [created["id"]]


def test_wsgi_rejects_invalid_json(api):
    app = make_wsgi_app(api)
    environ = {"REQUEST_METHOD": "POST", "PATH_INFO": "/orders", "CONTENT_LENGTH": "5", "wsgi.input": io.BytesIO(b"{oops")}
    setup_testing_defaults(environ)
    statuses = []
    app(environ, lambda status, headers: statuses.append(status))
    assert statuses == ["400 Bad Request"]
