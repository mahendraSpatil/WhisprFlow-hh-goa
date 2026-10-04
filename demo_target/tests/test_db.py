from __future__ import annotations

from orders import db


def test_insert_and_get_round_trip(conn):
    created = db.insert_order(conn, "alice", "WIDGET", 3, 750)
    assert db.get_order(conn, created.id) == created


def test_get_missing_order_returns_none(conn):
    assert db.get_order(conn, 999) is None


def test_find_orders_by_customer_returns_only_theirs(conn):
    first = db.insert_order(conn, "alice", "WIDGET", 1, 250)
    db.insert_order(conn, "bob", "GADGET", 1, 1200)
    second = db.insert_order(conn, "alice", "GIZMO", 1, 4999)

    assert db.find_orders_by_customer(conn, "alice") == [first, second]


def test_find_orders_handles_apostrophes(conn):
    order = db.insert_order(conn, "O'Brien", "WIDGET", 2, 500)
    assert db.find_orders_by_customer(conn, "O'Brien") == [order]


def test_find_orders_does_not_leak_other_customers(conn):
    db.insert_order(conn, "alice", "WIDGET", 1, 250)
    db.insert_order(conn, "bob", "GADGET", 1, 1200)

    assert db.find_orders_by_customer(conn, "nobody' OR '1'='1") == []
