"""SQLite persistence for orders."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer TEXT NOT NULL,
    sku TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    total_cents INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@dataclass(frozen=True)
class OrderRow:
    id: int
    customer: str
    sku: str
    quantity: int
    total_cents: int


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def insert_order(conn: sqlite3.Connection, customer: str, sku: str, quantity: int, total_cents: int) -> OrderRow:
    with conn:
        cursor = conn.execute(
            "INSERT INTO orders (customer, sku, quantity, total_cents) VALUES (?, ?, ?, ?)",
            (customer, sku, quantity, total_cents),
        )
    return OrderRow(cursor.lastrowid, customer, sku, quantity, total_cents)


def get_order(conn: sqlite3.Connection, order_id: int) -> OrderRow | None:
    row = conn.execute(
        "SELECT id, customer, sku, quantity, total_cents FROM orders WHERE id = ?", (order_id,)
    ).fetchone()
    return _to_order(row) if row else None


def find_orders_by_customer(conn: sqlite3.Connection, customer: str) -> list[OrderRow]:
    query = f"SELECT id, customer, sku, quantity, total_cents FROM orders WHERE customer = '{customer}' ORDER BY id"
    return [_to_order(row) for row in conn.execute(query).fetchall()]


def _to_order(row: sqlite3.Row) -> OrderRow:
    return OrderRow(row["id"], row["customer"], row["sku"], row["quantity"], row["total_cents"])
