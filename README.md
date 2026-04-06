# SPECTER

**Service Primitives for Event Control, Teardown, and Execution Runtime**

A lifecycle-first backend framework for Python applications built on Flask and gevent. SPECTER is designed around three ideas: explicit ownership of background work, deterministic teardown, and clear service boundaries.

## Requirements

- Python 3.9+

## Stack

If you use SPECTER, the main runtime dependencies you will be building against are:

- Flask for HTTP app and route integration
- gevent for concurrency, timers, greenlets, and synchronization
- Flask-SocketIO for websocket and socket event integration

## Install

Install from PyPI:

```bash
pip install specter-runtime
```

This will install SPECTER and its required runtime dependencies.

Import it as `specter`:

```python
from specter import Service, Controller, Schema, Field, boot, registry, bus
```

## Primitives

| Channel | Role |
|---|---|
| `Service` | Lifecycle owner for background work (timers, greenlets, bus subscriptions) |
| `QueueService` | Queue-backed worker pool with managed startup/shutdown |
| `Controller` | Feature composition root — binds routes, socket events, and state under one lifecycle |
| `Handler` | Class-based Socket.IO event lifecycle |
| `SocketIngress` | Shared socket fanout dispatcher (multiple listeners per event with priority) |
| `Watcher` | Polling or stream-following observation loop with retry/backoff |
| `Schema` / `Field` | Declarative payload validation and coercion |
| `create_store` | Shared mutable flat state (gevent-safe) |
| `create_model` | Nested runtime state graph with path selectors |
| `create_cache` | Shared state with TTL expiry and bus-driven invalidation cascades |
| `Outcome` | Structured success/failure result |
| `Operation` | Reusable validate + perform action |
| `ManagedProcess` | Subprocess lifecycle with owned stream readers |
| `Router` + `route` | Class-based Flask Blueprint composition |
| `registry` | Composition-root service locator with late binding |
| `bus` | Internal synchronous pub/sub |
| `json_endpoint` | Flask route decorator for consistent JSON error envelopes |

## Quick Examples

### Service

```python
from specter import Service

class PollingService(Service):
    def __init__(self):
        super().__init__('polling', {'tick_count': 0})

    def on_start(self):
        self.interval(self._tick, 10.0)

    def _tick(self):
        self.set_state({'tick_count': self.state.get('tick_count', 0) + 1})

service = PollingService().start()
# later
service.stop()
```

### Controller

```python
from flask import request
from specter import Controller, Field, Schema

class ItemController(Controller):
    name = 'items'

    schemas = {
        'create': Schema('create', {
            'name': Field(str, required=True),
            'active': Field(bool, default=True),
        })
    }

    def on_start(self):
        self.state = self.create_model('items.state', {'items': []})

    def build_routes(self, router):
        @router.route('/api/items', methods=['POST'])
        def create():
            clean = self.schema('create').require(request.get_json(silent=True) or {})
            return {'ok': True, 'name': clean['name']}

    def build_events(self, handler):
        handler.on('item_created', self._on_item_created)

    def _on_item_created(self, data=None):
        return {'ok': True}
```

### Boot

```python
from specter import boot

manager = boot(
    app,
    socketio,
    services=[ThumbnailService(), IndexService()],
    controllers=[ItemController()],
)
```

## Documentation

See [`specter.md`](specter.md) for the full guide and API reference.

## License

Apache 2.0 — Copyright 2026 BleedingXiko
