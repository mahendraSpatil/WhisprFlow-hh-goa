from __future__ import annotations

import threading

import pytest

from orders.service import (
    InvalidQuantity,
    OrderRequest,
    OutOfStock,
    UnknownSku,
    price_order,
)


def test_price_without_bulk_discount():
    assert price_order("WIDGET", 4) == 1000


def test_price_with_bulk_discount():
    assert price_order("WIDGET", 10) == 2250


def test_price_rejects_unknown_sku():
    with pytest.raises(UnknownSku):
        price_order("NOPE", 1)


def test_price_rejects_negative_quantity():
    with pytest.raises(InvalidQuantity):
        price_order("WIDGET", -1)


def test_place_order_reserves_stock(service, inventory):
    order = service.place_order("alice", "GADGET", 3)

    assert order.total_cents == 3600
    assert inventory.available("GADGET") == 7


def test_place_order_out_of_stock_leaves_stock_untouched(service, inventory):
    with pytest.raises(OutOfStock):
        service.place_order("alice", "GIZMO", 3)
    assert inventory.available("GIZMO") == 2


def test_summary_reports_discounted_unit_price(service):
    order = service.place_order("alice", "WIDGET", 10)
    summary = service.summarize(order.id)

    assert summary["unit_price_cents"] == 225
    assert summary["bulk_discount"] is True


def test_summary_of_zero_quantity_order(service):
    order = service.place_order("alice", "WIDGET", 0)
    summary = service.summarize(order.id)

    assert summary["total_cents"] == 0
    assert summary["unit_price_cents"] == 0


def test_batch_places_every_line_when_stock_suffices(service, inventory):
    requests = [OrderRequest(f"customer-{i}", "WIDGET", 1) for i in range(40)]
    result = service.place_batch(requests, workers=2)

    assert len(result.placed) == 40 and result.rejected == []
    assert inventory.available("WIDGET") == 60


def test_two_threads_reserving_concurrently_keep_count_exact(inventory):
    def reserve_many():
        for _ in range(20):
            inventory.reserve("WIDGET", 1)

    threads = [threading.Thread(target=reserve_many) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert inventory.available("WIDGET") == 60
