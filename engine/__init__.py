"""engine — orchestration, feed ingestion, runtime lifecycle.

Owns the asyncio loop, WebSocket reconnect logic, and per-tick dispatch into
signals/orderflow/analytics modules. The only layer allowed to import from
every other package.
"""
