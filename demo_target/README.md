# demo_target

A small order-processing service used as a target repo for CodeLoop runs.
Standard library only; pytest for tests.

- `orders/api.py`: HTTP handlers and a WSGI adapter (`python -m orders.api` serves on :8000)
- `orders/service.py`: pricing, inventory reservation, order placement
- `orders/db.py`: SQLite storage

```
pip install pytest
python -m pytest
```
