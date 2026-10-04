from __future__ import annotations

import pytest

from orders import db
from orders.api import OrderAPI
from orders.service import Inventory, OrderService, WarehouseClient


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def inventory():
    return Inventory({"WIDGET": 100, "GADGET": 10, "GIZMO": 2}, WarehouseClient(latency_s=0.002))


@pytest.fixture
def service(conn, inventory):
    return OrderService(conn, inventory)


@pytest.fixture
def api(service):
    return OrderAPI(service)
