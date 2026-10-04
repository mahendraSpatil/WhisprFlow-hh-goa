"""Pricing, inventory reservation and order placement."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from orders import db
from orders.db import OrderRow

CATALOG_CENTS = {"WIDGET": 250, "GADGET": 1200, "GIZMO": 4999}
DEFAULT_STOCK = {"WIDGET": 500, "GADGET": 100, "GIZMO": 20}

BULK_THRESHOLD = 10
BULK_DISCOUNT_PERCENT = 10


class OrderError(Exception):
    pass


class UnknownSku(OrderError):
    pass


class InvalidQuantity(OrderError):
    pass


class OutOfStock(OrderError):
    pass


def price_order(sku: str, quantity: int) -> int:
    """Total in cents. Zero-quantity orders are allowed: they are placeholders a customer fills in later."""
    if sku not in CATALOG_CENTS:
        raise UnknownSku(sku)
    if quantity < 0:
        raise InvalidQuantity(f"quantity must not be negative, got {quantity}")
    total = CATALOG_CENTS[sku] * quantity
    if quantity >= BULK_THRESHOLD:
        total = total * (100 - BULK_DISCOUNT_PERCENT) // 100
    return total


class WarehouseClient:
    """Stand-in for the remote warehouse API. Every call costs a network round trip."""

    def __init__(self, latency_s: float = 0.002) -> None:
        self.latency_s = latency_s

    def confirm_pick(self, sku: str, quantity: int) -> None:
        time.sleep(self.latency_s)


class Inventory:
    def __init__(self, stock: dict[str, int], warehouse: WarehouseClient) -> None:
        self._stock = dict(stock)
        self._warehouse = warehouse

    def available(self, sku: str) -> int:
        return self._stock.get(sku, 0)

    def reserve(self, sku: str, quantity: int) -> int:
        """Take ``quantity`` units out of stock and return what is left."""
        current = self._stock.get(sku, 0)
        if quantity > current:
            raise OutOfStock(f"{sku}: requested {quantity}, only {current} left")
        self._warehouse.confirm_pick(sku, quantity)
        self._stock[sku] = current - quantity
        return self._stock[sku]


@dataclass(frozen=True)
class OrderRequest:
    customer: str
    sku: str
    quantity: int


@dataclass
class BatchResult:
    placed: list[OrderRow] = field(default_factory=list)
    rejected: list[OrderRequest] = field(default_factory=list)


class OrderService:
    def __init__(self, conn: sqlite3.Connection, inventory: Inventory) -> None:
        self.conn = conn
        self.inventory = inventory

    def place_order(self, customer: str, sku: str, quantity: int) -> OrderRow:
        total = price_order(sku, quantity)
        self.inventory.reserve(sku, quantity)
        return db.insert_order(self.conn, customer, sku, quantity, total)

    def place_batch(self, requests: Sequence[OrderRequest], workers: int = 2) -> BatchResult:
        """Place many orders. Each line is placed or rejected on its own.

        Reservations are dominated by warehouse round trips, so they run in a
        thread pool; orders are then written on the calling thread, which owns
        the SQLite connection.
        """
        for request in requests:
            price_order(request.sku, request.quantity)  # reject bad input before reserving anything

        def try_reserve(request: OrderRequest) -> bool:
            try:
                self.inventory.reserve(request.sku, request.quantity)
            except OutOfStock:
                return False
            return True

        with ThreadPoolExecutor(max_workers=workers) as pool:
            reserved = list(pool.map(try_reserve, requests))

        result = BatchResult()
        for request, ok in zip(requests, reserved):
            if ok:
                total = price_order(request.sku, request.quantity)
                result.placed.append(db.insert_order(self.conn, request.customer, request.sku, request.quantity, total))
            else:
                result.rejected.append(request)
        return result

    def summarize(self, order_id: int) -> dict[str, object] | None:
        order = db.get_order(self.conn, order_id)
        if order is None:
            return None
        return {
            "id": order.id,
            "customer": order.customer,
            "sku": order.sku,
            "quantity": order.quantity,
            "total_cents": order.total_cents,
            "unit_price_cents": order.total_cents // order.quantity,
            "bulk_discount": order.quantity >= BULK_THRESHOLD,
        }

    def orders_for(self, customer: str) -> list[OrderRow]:
        return db.find_orders_by_customer(self.conn, customer)
