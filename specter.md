# SPECTER Framework Guide and API Reference

SPECTER (Service Primitives for Event Control, Teardown, and Execution Runtime) is a lifecycle-first backend framework.
It is designed for explicit ownership, deterministic cleanup, and clear service boundaries for Python, Flask, gevent, and SQLite workloads.

This document is split into two parts:
1. Guide: how to design and build backend features in SPECTER.
2. API Reference: method-level behavior for daily implementation work.

---

## Part I: Guide

## 1. Start Here

### What SPECTER optimizes for

- Explicit ownership of background work and side effects.
- Predictable startup and shutdown with reverse-order teardown.
- Gevent-native concurrency (`BoundedSemaphore`, greenlets, cooperative waits).
- Clear boundaries between lifecycle, ingress, contracts, state, and provisioning.

### The channels

| Channel | Role | Default Use |
|---|---|---|
| `Service` | lifecycle owner for background work | timers, greenlets, bus subscriptions, child ownership |
| `QueueService` | queue-backed worker lifecycle | bounded async work queues with worker pools |
| `Controller` | feature composition root | bind routes + socket events + state + schemas under one lifecycle |
| `Handler` | socket event lifecycle owner | class-based Socket.IO event binding |
| `SocketIngress` | shared socket fanout dispatcher | multiple subscribers per socket event with priority order |
| `Watcher` | observation primitive | polling or stream-following loops with retry/backoff |
| `Schema` / `Field` | payload contract | validate/coerce HTTP and socket payloads |
| `create_store` / `create_model` / `create_cache` | state primitives | shared mutable state, nested state graphs, TTL caches |
| `registry` | service locator | composition-root provisioning and late binding |
| `bus` | internal pub/sub | server-side one-to-many notifications |
| `Router` + `route` | class-based HTTP composition | Blueprint-friendly route ownership |
| HTTP helpers | request/response contracts | JSON envelopes and request validation |

### Non-negotiable rules

1. If there is a clear owner, use ownership APIs first (`adopt`, `own`, `add_cleanup`).
2. Do not use `registry` as a substitute for parent-child lifecycle wiring.
3. Use `bus` only for broadcast semantics, not direct control flow.
4. Use lifecycle-managed APIs (`interval`, `timeout`, `spawn`, `Watcher`) instead of ad-hoc greenlets.
5. Use `Schema` for untrusted payloads (HTTP and socket) instead of manual coercion chains.
6. Use gevent locks (`BoundedSemaphore`), never `threading.Lock`, in shared backend state.

---

## 2. Quick Start

### Import surface

Always import from the public entry point:

```python
from specter import (
    Service,
    QueueService,
    Controller,
    Handler,
    Watcher,
    Schema,
    Field,
    create_store,
    create_model,
    create_cache,
    Outcome,
    Operation,
    OperationError,
    ManagedProcess,
    SocketIngress,
    ServiceManager,
    Router,
    route,
    registry,
    bus,
    HTTPError,
    json_endpoint,
    expect_json,
    require_fields,
    boot,
)
```

### First Service

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

### First QueueService

```python
from specter import QueueService


class ThumbnailQueue(QueueService):
    def __init__(self):
        super().__init__('thumbnails', worker_count=2, maxsize=200)

    def handle_item(self, item):
        generate_thumbnail(item['path'])


svc = ThumbnailQueue().start()
svc.enqueue({'path': '/media/a.jpg'})
# later
svc.stop()
```

### First Controller

```python
from flask import jsonify, request

from specter import Controller, Field, Schema


class MediaCastController(Controller):
    name = 'media_cast'

    schemas = {
        'cast_media': Schema('cast_media', {
            'media_path': Field(str, required=True),
            'media_type': Field(str, default='video'),
            'loop': Field(bool, default=True),
        })
    }

    def on_start(self):
        self.state = self.create_model('cast.state', {
            'casting': False,
            'current_media': None,
        })

    def build_routes(self, router):
        @router.route('/api/cast/status')
        def status():
            return jsonify(self.state.get())

        @router.route('/api/cast/start', methods=['POST'])
        def cast():
            clean = self.schema('cast_media').require(request.get_json(silent=True) or {})
            self._cast(clean)
            return {'ok': True}

    def build_events(self, handler):
        handler.on('cast_media', self._handle_cast)

    def _handle_cast(self, data=None):
        clean = self.schema('cast_media').require(data or {})
        self._cast(clean)
        return {'ok': True}

    def _cast(self, clean):
        self.state.patch({'casting': True, 'current_media': clean})
```

### First ServiceManager boot

```python
from specter import ServiceManager

manager = ServiceManager(app, socketio)
manager.register_controller(MediaCastController(), url_prefix='/cast')
manager.boot()
```

---

## 3. Mental Model

Think in terms of lanes.

### Lane A: ownership

- Parent services own child services and resources.
- Use `adopt(child_service)` for service children.
- Use `own(resource)` for lifecycle-aware non-service objects.
- Teardown always runs reverse-order where applicable.

### Lane B: provisioning

- Composition root provides shared dependencies with `registry.provide(...)`.
- Consumers read with `registry.resolve(...)` / `registry.require(...)`.
- This lane is app wiring, not local parent-child state flow.

### Lane C: broadcast

- Use `bus.emit(event, payload)` for one-to-many fanout.
- Subscribers run synchronously in the emitter greenlet.
- Long work in a bus callback should spawn its own greenlet.

### Lane D: ingress

- Socket ingress goes through `Handler` or `SocketIngress`.
- HTTP ingress goes through Flask routes / `Router` and optional `json_endpoint`.
- Validate ingress payloads with `Schema` before business logic.

### Lane E: contracts

- Return `Outcome` from operation-style logic where structured status is needed.
- Use `Operation` for reusable validate + perform flows.

---

## 4. Decision Matrix (Use This First)

| Situation | Use | Do Not Default To |
|---|---|---|
| Background lifecycle with timers/listeners | `Service` | module-level greenlets |
| Queue-backed worker pipeline | `QueueService` | manual queue + ad-hoc worker loops |
| Feature spans routes + sockets + state | `Controller` | split logic across unrelated modules |
| Class-based socket event ownership | `Handler` | free-function handler registrars |
| One socket event with multiple backend listeners | `SocketIngress` | registering competing handlers directly on Socket.IO |
| Polling external state | `Watcher(poll=...)` | hand-rolled `while True` loops |
| Stream-following external source | `Watcher(stream=...)` or `ManagedProcess.watch_stream(...)` | unmanaged reader greenlets |
| Shared mutable flat state | `create_store` | global dict + manual locks |
| Nested runtime state | `create_model` | deep dict mutation scattered in services |
| Expensive data with expiry | `create_cache` | perpetual stale globals |
| Broadcast internal notifications | `bus` | import chains for fanout |
| Provision shared services | `registry.provide` | ad-hoc globals |
| Dependency may arrive later | `registry.wait_for` | busy waiting |
| HTTP class routes | `Router` + `route` | copy/paste Blueprint functions |
| Consistent JSON route envelope | `json_endpoint` | per-route try/except duplication |
| Validate JSON body + required fields | `expect_json` / `require_fields` | repeated manual guard clauses |

---

## 5. Core Lifecycle Model

### Service lifecycle

```text
Service(name, initial_state)
  start() -> on_start()
  runtime -> set_state / timers / listeners / own/adopt
  stop()  -> on_stop()
         -> cancel intervals/timeouts
         -> cancel owned greenlets
         -> unsubscribe bus listeners
         -> stop adopted children (reverse)
         -> run cleanup callbacks (reverse)
         -> clear subscribers
```

### Handler lifecycle

```text
Handler()
  setup(socketio)
    -> register events from class `events`
    -> on_setup()
  teardown()
    -> on_teardown()
    -> run cleanup callbacks
```

Note:
- When handlers are attached through `SocketIngress`, teardown unsubscribes cleanly.
- When handlers are attached directly to Flask-SocketIO, there is no first-class unregister API.

### Controller lifecycle

```text
Controller(name) extends Service
  on_start() -> initialize feature state/watchers/services
  build_blueprint() -> create Router, call build_routes(), return Blueprint
  build_handler(socketio?) -> create handler bridge, call build_events(), optional setup
  stop() -> teardown handler bridge, then Service.stop()
```

### ServiceManager lifecycle

```text
ServiceManager(app, socketio)
  boot()
    -> start manager
    -> provide manager + socket_ingress in registry
    -> start registered services (order)
    -> setup registered handlers
    -> register atexit shutdown
  shutdown()
    -> teardown handlers (reverse)
    -> stop services (reverse)
    -> clear socket_ingress + registry
    -> stop manager
```

### Watcher lifecycle

```text
Watcher(name, poll=... | stream=...)
  start() -> spawn background loop
  subscribe(fn)
  stop()  -> signal stop, join (timeout), then kill if needed
```

Idempotency:
- `Service.start/stop`, `Handler.setup/teardown`, `Watcher.start/stop`, and `ServiceManager.boot/shutdown` are idempotent.

---

## 6. Building a Feature the SPECTER Way

### Step 1: Put orchestration in a Service or Controller

- Use `Service` for one-domain background orchestration.
- Use `Controller` when routes, socket events, and runtime state should live together.

### Step 2: Own external resources explicitly

- Use `own(...)` for `Watcher`, `ManagedProcess`, cache/store/model, and custom resources.
- Use `adopt(...)` for child services.

### Step 3: Validate ingress with Schema

- Route and socket payloads should pass through `schema.validate(...)` or `schema.require(...)`.

### Step 4: Keep provisioning in composition root

- `ServiceManager` boot sequence should register/provide shared dependencies.
- Business modules should resolve dependencies, not instantiate global singletons.

### Step 5: Use the right state primitive

- `Service` local state: lightweight service health/metrics.
- `Store`: shared mutable key-value state.
- `Model`: nested runtime graph with selectors.
- `Cache`: expensive computed data with TTL and invalidation.

---

## 7. Services in Practice

### What belongs in Service

- Timer-based work (`interval`, `timeout`).
- Managed greenlets (`spawn`, `spawn_later`).
- Bus subscriptions (`listen`) and emits (`emit`).
- Child lifecycle ownership (`adopt`) and resource ownership (`own`).

### State semantics

- `set_state(partial)` is a shallow merge.
- Subscriber callbacks run synchronously after the lock is released.
- `subscribe(fn, owner=...)` wires auto-unsubscribe to an owner lifecycle.

### Spawn and timer guidance

- `spawn` and `spawn_later` require the service to be running.
- `interval` callback exceptions are logged and loop continues.
- `timeout` runs once and self-removes from tracking.

### Teardown guarantees

- Teardown order is deterministic.
- Cleanup callbacks are LIFO.
- Adopted child services stop in reverse adoption order.

---

## 8. QueueService in Practice

`QueueService` is the standard primitive for queue-backed background workers.

### Behavior

- Spawns `worker_count` managed workers in `on_start`.
- Uses `JoinableQueue` with optional `maxsize`.
- Updates state with `queue_size` and `worker_count`.

### Enqueue contract

- `enqueue(item, block=False, timeout=None) -> bool`
- Returns `False` on `Full` when non-blocking.

### Worker contract

- Override `handle_item(item)`.
- Worker exceptions are logged; worker loop keeps running.
- `task_done()` is called in `finally`.

### Hooks

- `on_workers_started()`
- `on_workers_stopping()`

---

## 9. Handlers and SocketIngress in Practice

### Handler class flow

```python
from specter import Handler


class PresenceHandler(Handler):
    name = 'presence'
    events = {
        'heartbeat': 'handle_heartbeat',
    }

    def handle_heartbeat(self, data=None):
        return {'ok': True}
```

### Event priorities

- Optional `event_priorities` mapping can be defined on handlers.
- Used only when `socket_ingress` exists in registry.

### Why SocketIngress exists

Flask-SocketIO effectively stores one callback per event. `SocketIngress` registers one dispatcher per event and fans out to ordered subscribers.

### Priority rules

- Lower numeric priority runs first.
- Ties preserve subscription order.
- Dispatch continues even when one subscriber raises.

### Default error fanout

- `subscribe_error_default(...)` and `on_error_default(...)` attach to `socketio.on_error_default` via shared dispatcher.

---

## 10. Controllers and Routers in Practice

### When to use a Controller

Use a `Controller` when a feature crosses three or more boundaries:
- HTTP routes
- socket events
- shared runtime state
- background work
- service dependencies

### Controller responsibilities

- Define route composition in `build_routes(router)`.
- Define socket ingress in `build_events(handler)`.
- Keep feature-local schemas on class attribute `schemas`.
- Own feature state with `create_model` / `create_store`.

### Router capabilities

- Class decorators with `@route(...)`.
- Instance-time dynamic route registration with `router.route(...)`.
- Optional per-route `json_errors` wrapping via `json_endpoint`.
- Auto conversion of returned `Outcome` to JSON response + status.

### Router ownership hooks

- `add_cleanup(fn)` and `own(resource)` allow route-level cleanup ownership.
- `teardown()` runs `on_unmount()` then all registered cleanups.

---

## 11. State Primitives in Practice

### `create_store`

Use for flat shared state.

```python
connections = create_store('connections', {'count': 0})
connections.set({'count': 1})
connections.update(lambda draft: draft.update({'count': draft['count'] + 1}))
connections.subscribe(lambda snapshot, store: log_state(snapshot), immediate=True)
```

Key points:
- Shallow snapshots.
- `update(mutator)` is atomic under lock.
- Optional bus emission with `emit_events=True`.

### `create_model`

Use for nested state graphs.

```python
tv_state = create_model('tv', {
    'playback': {'url': None, 'position': 0},
    'casting': False,
})

# path-based read/write
tv_state.set('playback.url', '/media/movie.mp4')
url = tv_state.get('playback.url')
```

Key points:
- Deep-copy snapshots.
- Selector subscriptions notify only when selected value changes.
- `subscribe(fn, selector=...)` callback receives `(value, model)`.

### `create_cache`

Use for expensive shared data with expiry.

```python
drives_cache = create_cache('drives', ttl=300, depends_on=['storage:mount_changed'])

# atomic cache miss
drives = drives_cache.get_or_compute(scan_drives)
```

Key points:
- `ttl=0` means no automatic expiry.
- `invalidate()` emits `'{name}:invalidated'` on bus.
- `depends_on=[...]` enables invalidation cascades.

---

## 12. Schemas and Validation in Practice

### Defining contracts

```python
from specter import Field, Schema


cast_schema = Schema('cast_media', {
    'media_path': Field(str, required=True),
    'loop': Field(bool, default=True),
    'start_time': Field(float, default=0),
    'mode': Field(str, choices=['video', 'image'], default='video'),
})
```

### Validation modes

- Safe mode: `validate(data)` returns `Outcome`.
- Assertive mode: `require(data)` returns cleaned dict or raises `HTTPError` (or `SchemaError` fallback).

### Coercion behavior

- Supports `int`, `float`, `str`, `bool`, `list`, `dict`, and callables.
- Bool coercion accepts common textual forms (`true/false`, `yes/no`, `1/0`, `on/off`).

### Composition and strict mode

- `extend(name, extra_fields, strict=...)` creates derived schemas.
- `strict=True` rejects unknown input keys.

---

## 13. Contracts: Outcome and Operation

### Outcome

Use `Outcome` for explicit success/failure semantics:

```python
result = Outcome.success({'id': 42}, status=201)
error = Outcome.failure('Not found', status=404)
```

### Operation

Use `Operation` to standardize validate + execute flows:

```python
from specter import Operation, OperationError


class CreateCategory(Operation):
    def validate(self, name):
        if not name:
            raise OperationError('name is required', status=400)

    def perform(self, name):
        return {'name': name}


outcome = CreateCategory().run('Movies')
```

Key points:
- `run(...)` always returns `Outcome`.
- Unhandled exceptions become failure outcomes with status `500`.

---

## 14. Registry and Bus in Practice

### Registry

Use `registry` in composition roots and independent services.

```python
registry.provide('thumbnail_service', thumbnail_service, owner=manager)
svc = registry.require('thumbnail_service')
```

Late binding:

```python
db = registry.wait_for('database_service', timeout=5.0)
```

Key points:
- `owner` must expose `add_cleanup()`.
- `provide(..., replace=True)` overwrites existing keys.
- `clear()` rejects pending waiters with exceptions.

### Bus

Use for internal cross-service fanout:

```python
bus.emit('storage:mount_changed', {'path': '/media/usb1'})
```

Key points:
- Synchronous delivery in emitter greenlet.
- Listener failures are logged; remaining listeners still execute.

---

## 15. Watchers and ManagedProcess in Practice

### Watcher modes

- Poll mode: periodic value sampling.
- Stream mode: iterate yielded items from a stream source.

```python
watcher = Watcher(
    'hdmi-status',
    poll=read_hdmi_status,
    interval=2.0,
    dedupe=True,
)
watcher.subscribe(lambda value, w: on_status(value))
watcher.start()
```

### Retry/backoff

- On loop exception, watcher increments error count.
- With `retry=True`, retries with exponential backoff up to `max_backoff`.
- With `retry=False`, watcher exits after first failure.

### ManagedProcess

```python
from subprocess import PIPE

from specter import ManagedProcess

proc = ManagedProcess('cloudflared')
proc.start(
    ['cloudflared', 'tunnel', '--url', 'http://127.0.0.1:5000'],
    stdout=PIPE,
    stderr=PIPE,
)

proc.watch_stream('stdout', handle_stdout)
proc.watch_stream('stderr', handle_stderr)
```

Key points:
- `stop()` handles terminate -> wait -> kill fallback.
- `watch_stream(...)` readers are owned and cleaned up on stop.
- `attach(owner)` wires process cleanup to lifecycle owner.

---

## 16. HTTP Helpers in Practice

### `json_endpoint`

- Catches `HTTPError` and generic exceptions.
- Auto-normalizes plain `dict`/`list` (and tuple payload variants) to JSON responses.

```python
@json_endpoint('Failed to create item')
def create_item():
    payload = expect_json(required=['name'])
    return {'ok': True, 'name': payload['name']}, 201
```

### `expect_json`

- Parses JSON body.
- Requires top-level object.
- Optionally enforces required keys.

### `require_fields`

- Validates presence/non-blank required fields.
- Raises `HTTPError(400)` with missing list in payload.

### `HTTPError`

- Structured exception with `message`, `status_code`, optional payload and logging metadata.

---

## 17. App Boot and ServiceManager

### `boot(...)` helper

`boot(app, socketio, services=..., handlers=..., controllers=...)` is a convenience wrapper that:

1. creates `ServiceManager`
2. registers provided services/handlers/controllers
3. calls `manager.boot()`
4. returns manager

### Manager registration order

- Services start in registration order.
- Handlers setup after services.
- Shutdown runs reverse order for handlers, then services.

### Registry integration

During `manager.boot()`, these keys are provided:
- `'service_manager'`
- `'socket_ingress'`

---

## 18. Architecture Pattern

```text
Composition root (boot / app startup)
  - create ServiceManager
  - construct and register services/controllers/handlers
  - provide shared state/services in registry with owner=manager
  - boot manager

Feature modules
  - own timers/greenlets/watchers/processes via Service lifecycle
  - validate ingress payloads with Schema
  - keep cross-feature fanout on bus
  - keep per-feature ingress wiring in Handler or Controller
```

Suggested ownership map:
- periodic/background orchestration -> `Service` / `QueueService`
- route + socket + state feature boundary -> `Controller`
- socket ingress only -> `Handler`
- multi-listener socket event fanout -> `SocketIngress`
- nested runtime graph -> `Model`
- mutable shared key/value state -> `Store`
- expensive shared computed data -> `Cache`
- internal notifications -> `bus`
- app wiring and late binding -> `registry`

---

## 19. Testing Checklist and Pitfalls

For any SPECTER change:

1. Verify lifecycle idempotency (`start/stop`, `setup/teardown`, `boot/shutdown`).
2. Verify teardown removes listeners/timers/subscriptions/resources.
3. Verify state primitives notify correctly and remain lock-safe.
4. Verify ingress validation and error envelopes for bad payloads.
5. Verify priority ordering when using `SocketIngress`.

High-impact pitfalls:

1. `bus.emit` is synchronous; long listeners block the emitter greenlet.
2. `Cache.get_or_compute` executes factory while holding cache lock.
3. `Handler` direct registrations on Flask-SocketIO are not fully unregisterable.
4. `Schema.require` raises; use with `json_endpoint` or explicit exception handling.
5. `Service.spawn`/`interval`/`timeout` require running service.
6. `registry.wait_for` can block forever without a timeout in failure scenarios.
7. Mixing socket events and bus events in one channel creates ownership ambiguity.

Related tests:
- `tests/test_specter_primitives.py`

---

## Part II: API Reference

## 20. Imports and Exports

`specter/__init__.py` is the canonical public surface.

Primary exports:

- Lifecycle: `Service`, `QueueService`, `Controller`, `Handler`, `Watcher`, `WatcherError`, `ServiceManager`, `SocketIngress`, `boot`
- State: `Model`, `create_model`, `Store`, `create_store`, `Cache`, `create_cache`
- Contracts: `Schema`, `Field`, `SchemaError`, `Outcome`, `Operation`, `OperationError`
- Process: `ManagedProcess`, `start_process`
- HTTP: `Router`, `route`, `HTTPError`, `json_endpoint`, `expect_json`, `require_fields`
- Communication and provisioning: `EventBus`, `bus`, `SPECTERRegistry`, `registry`

---

## 21. `Service` API Reference

### Constructor

- `Service(name: str, initial_state: dict | None = None)`

### Lifecycle hooks

- `on_start()`
- `on_stop()`

### Lifecycle control

- `start() -> Service`
- `stop() -> Service`
- `running -> bool`

### State APIs

- `set_state(partial: dict)`
- `get_state() -> dict`
- `subscribe(fn, immediate=False, owner=None) -> callable`
- `watch(fn, immediate=True) -> callable`

Subscriber callback: `fn(state_dict, service)`.

### Managed timer/greenlet APIs

- `spawn(callback, *args, label=None, **kwargs) -> Greenlet`
- `spawn_later(seconds, callback, *args, label=None, **kwargs) -> Greenlet`
- `interval(callback, seconds) -> str`
- `timeout(callback, seconds) -> str`
- `cancel_interval(interval_id)`
- `cancel_timeout(timeout_id)`
- `cancel_greenlet(glet) -> Service`

### Bus and ownership APIs

- `listen(event, handler) -> Service`
- `emit(event, data=None) -> Service`
- `adopt(child: Service, start=True) -> Service`
- `own(resource, stop_method=None) -> resource`
- `add_cleanup(fn) -> Service`
- `start_process(args, *, name=None, **popen_kwargs) -> ManagedProcess`

### Health

- `health() -> dict`

---

## 22. `QueueService` API Reference

### Constructor

```python
QueueService(
    name,
    *,
    worker_count=1,
    maxsize=0,
    poll_interval=0.5,
    initial_state=None,
)
```

### Hooks

- `handle_item(item)` (required override)
- `on_workers_started()`
- `on_workers_stopping()`

### Methods

- `enqueue(item, *, block=False, timeout=None) -> bool`
- `pending_count() -> int`

### State

- `queue_size`
- `worker_count`

---

## 23. `Controller` API Reference

### Constructor

- `Controller(name: str | None = None)`

### Class attributes

- `name`
- `schemas` (`dict[str, Schema]`)

### Subclass hooks

- `build_routes(router)`
- `build_events(handler)`
- inherited lifecycle hooks: `on_start`, `on_stop`

### Build methods

- `build_blueprint(url_prefix=None) -> flask.Blueprint`
- `create_handler() -> Handler`
- `build_handler(socketio=None) -> Handler`

### Convenience

- `schema(name) -> Schema`
- `create_model(name, initial_state=None) -> Model`
- `create_store(name, initial_state=None, **kwargs) -> Store`
- `socketio` property (active socketio via handler or manager)

### Lifecycle override

- `stop()` tears down controller-built handler before calling `Service.stop()`.

### Health

- `health() -> dict` includes controller extras (`has_blueprint`, `has_handler`, optional schema names).

---

## 24. `Handler` API Reference

### Class attributes

- `name = 'unnamed_handler'`
- `events = {event_name: method_name}`
- optional `event_priorities = {event_name: priority}`

### Lifecycle hooks

- `on_setup()`
- `on_teardown()`

### Lifecycle methods

- `setup(socketio) -> Handler`
- `teardown() -> Handler`
- `stop() -> Handler` (alias for teardown)

### Ownership and registry helpers

- `add_cleanup(fn) -> Handler`
- `own(resource, stop_method=None) -> resource`
- `require(key) -> value`
- `resolve(key) -> value | None`

### Properties

- `registered -> bool`
- `socketio`

---

## 25. `Watcher` API Reference

### Constructor

```python
Watcher(
    name,
    *,
    poll=None,
    stream=None,
    interval=5.0,
    retry=True,
    max_backoff=30.0,
    dedupe=False,
    transform=None,
)
```

Exactly one of `poll` or `stream` is required.

### Lifecycle

- `start() -> Watcher`
- `stop() -> Watcher`
- `teardown()` (alias for stop)

### Observation

- `subscribe(fn) -> callable`
- callback signature: `fn(value, watcher)`

### Diagnostics

- `health() -> dict`
- `running -> bool`
- `last_value`

---

## 26. `Schema` and `Field` API Reference

### `Field`

```python
Field(
    type=str,
    *,
    required=False,
    default=None,
    choices=None,
    validator=None,
    label=None,
)
```

### `Schema`

```python
Schema(name: str, fields: dict[str, Field], *, strict=False)
```

### Methods

- `validate(data: dict) -> Outcome`
- `require(data: dict) -> dict` (raises `HTTPError` when available, otherwise `SchemaError`)
- `extend(name: str, extra_fields: dict, **kwargs) -> Schema`

### Properties

- `field_names -> set[str]`
- `required_fields -> set[str]`

---

## 27. `Outcome` and `Operation` API Reference

### `Outcome`

- `Outcome.success(value=None, *, status=200, **meta) -> Outcome`
- `Outcome.failure(error, *, status=400, value=None, **meta) -> Outcome`
- `unwrap() -> value`
- `to_dict() -> dict`
- `to_tuple() -> tuple[bool, object, str | None]`

Fields:
- `ok`, `value`, `error`, `status`, `meta`

### `Operation`

- `validate(*args, **kwargs)`
- `perform(*args, **kwargs)` (required override)
- `run(*args, **kwargs) -> Outcome`
- `success(value=None, *, status=200, **meta) -> Outcome`
- `failure(error, *, status=400, **meta) -> Outcome`

### `OperationError`

- `OperationError(message, *, status=400, meta=None)`

---

## 28. `Model`, `Store`, and `Cache` API Reference

### `Store`

Factory:
- `create_store(name, initial_state=None, **kwargs) -> Store`

Core methods:
- `get(key=None, default=None)`
- `snapshot()`
- `access(reader)`
- `set(partial)`
- `replace(state)`
- `delete(*keys)`
- `clear()`
- `update(mutator)`
- `subscribe(fn, immediate=False, owner=None) -> unsub`
- `watch(fn, immediate=True) -> unsub`
- `destroy()`

Subscription callback: `fn(snapshot, store)`.

### `Model`

Factory:
- `create_model(name, initial_state=None, **kwargs) -> Model`

Core methods:
- `snapshot()`
- `get(path=None, default=None)`
- `select(selector, default=None)`
- `set(path, value)`
- `patch(partial)`
- `update(mutator)`
- `delete(path)`
- `clear()`
- `subscribe(fn, selector=None, immediate=False, owner=None) -> unsub`
- `watch(fn, selector=None, immediate=True) -> unsub`
- `destroy()`

Subscription callback: `fn(value, model)` where `value` is selected slice or full state.

### `Cache`

Factory:
- `create_cache(name, ttl=0, depends_on=None) -> Cache`

Core methods:
- `get(default=None)`
- `is_valid()`
- `set(value, ttl=None)`
- `get_or_compute(factory, ttl=None)`
- `invalidate()`
- `on_invalidate(callback) -> unsub`
- `destroy()`

Properties:
- `ttl`
- `updated_at`

---

## 29. `registry` and `bus` API Reference

### `registry` (`SPECTERRegistry`)

- `provide(key, value, owner=None, replace=False) -> value`
- `unregister(key) -> bool`
- `resolve(key)`
- `require(key)`
- `has(key) -> bool`
- `list() -> list[str]`
- `wait_for(key, timeout=None)`
- `clear()`

### `bus` (`EventBus`)

- `on(event, callback) -> unsub`
- `off(event, callback)`
- `once(event, callback) -> unsub`
- `emit(event, data=None)`
- `clear(event=None)`
- `has_listeners(event) -> bool`
- `listener_count(event=None) -> int`

---

## 30. `SocketIngress` and `ManagedProcess` API Reference

### `SocketIngress`

Constructor:
- `SocketIngress(socketio=None, *, name='socket_ingress')`

Attach and registration:
- `attach(socketio) -> SocketIngress`
- `on(event_name, callback=None, *, owner=None, priority=100)`
- `handler(event_name, *, owner=None, priority=100)` (decorator alias)
- `subscribe(event_name, callback, *, owner=None, priority=100) -> unsub`

Default error channel:
- `on_error_default(callback=None, *, owner=None, priority=100)`
- `subscribe_error_default(callback, *, owner=None, priority=100) -> unsub`

Unsubscribe and dispatch:
- `unsubscribe(event_name, callback) -> bool`
- `unsubscribe_error_default(callback) -> bool`
- `dispatch(event_name, *args, **kwargs)`
- `dispatch_default_error(*args, **kwargs)`
- `clear()`
- `socketio` property

### `ManagedProcess`

Constructor:
- `ManagedProcess(name, *, terminate_timeout=5.0, kill_timeout=1.0)`

Lifecycle/process methods:
- `start(args, *, owner=None, **popen_kwargs) -> ManagedProcess`
- `attach(owner) -> ManagedProcess`
- `watch_stream(stream_name, handler, *, chunk_size=None, strip=True, label=None, close_on_exit=False) -> Greenlet`
- `follow_output(*, stdout_handler=None, stderr_handler=None, stdout_label='stdout', stderr_label='stderr') -> list[Greenlet]`
- `wait(timeout=None)`
- `poll()`
- `stop(timeout=None) -> ManagedProcess`

Properties:
- `running`
- `pid`
- `returncode`

Factory helper:
- `start_process(name, args, *, owner=None, **popen_kwargs) -> ManagedProcess`

---

## 31. `Router`, HTTP Helpers, and `boot` API Reference

### `route` decorator

- `route(rule, *, methods=None, endpoint=None, json_errors=None)`

### `Router`

Class attributes:
- `name = 'router'`
- `url_prefix = ''`

Lifecycle/hooks:
- `on_mount()`
- `on_unmount()`

Core methods:
- `mount() -> Blueprint`
- `blueprint() -> Blueprint`
- `register(app) -> Router`
- `teardown() -> Router`
- `route(rule, *, methods=None, endpoint=None, json_errors=None)` (dynamic route registration)
- `require(key)` / `resolve(key)`
- `add_cleanup(fn) -> Router`
- `own(resource, stop_method=None)`

### HTTP helpers

- `HTTPError(message, status_code=400, *, status=None, payload=None, log_message=None, log_level=logging.WARNING)`
- `json_endpoint(error_message=None, *, logger=None, include_trace=True)`
- `expect_json(*, required=None, error='Request body is required', allow_empty=False)`
- `require_fields(payload, required, *, error=None, blank_is_missing=True)`

### `boot`

- `boot(app, socketio, *, services=None, handlers=None, controllers=None) -> ServiceManager`

---

## 32. `ServiceManager` API Reference

### Constructor

- `ServiceManager(app=None, socketio=None)`

### Registration

- `register_service(service) -> ServiceManager`
- `register_handler(handler) -> ServiceManager`
- `register_controller(controller, *, url_prefix=None) -> ServiceManager`

### Lifecycle

- `boot() -> ServiceManager`
- `shutdown() -> ServiceManager`

### Health and properties

- `health() -> dict`
- `app`
- `socketio`
- `socket_ingress`
- `booted`

---

## 33. Source Map

| File | Purpose |
|---|---|
| `specter/__init__.py` | Public import surface |
| `specter/boot.py` | App boot helper |
| `specter/http.py` | `HTTPError`, `json_endpoint`, `expect_json`, `require_fields` |
| `specter/router.py` | `Router` and `route` |
| `specter/core/lifecycle.py` | `Service` |
| `specter/core/queue_service.py` | `QueueService` |
| `specter/core/controller.py` | `Controller` |
| `specter/core/handler.py` | `Handler` |
| `specter/core/socket_ingress.py` | `SocketIngress` |
| `specter/core/watcher.py` | `Watcher` |
| `specter/core/process.py` | `ManagedProcess`, `start_process` |
| `specter/core/schema.py` | `Schema`, `Field`, `SchemaError` |
| `specter/core/outcome.py` | `Outcome` |
| `specter/core/operation.py` | `Operation`, `OperationError` |
| `specter/core/store.py` | `Store`, `create_store` |
| `specter/core/model.py` | `Model`, `create_model` |
| `specter/core/cache.py` | `Cache`, `create_cache` |
| `specter/core/bus.py` | `EventBus`, `bus` |
| `specter/core/registry.py` | `SPECTERRegistry`, `registry` |
| `specter/core/manager.py` | `ServiceManager` |
| `specter/core/ownership.py` | cleanup resolution for ownership APIs |

---

## 34. Maintenance Policy

When framework behavior changes:

1. Update this file.
2. Update or add tests (at minimum `tests/test_specter_primitives.py` and relevant feature tests).
3. Verify `specter/__init__.py` export surface is still accurate.

4. Run `python scripts/run_all_tests.py` before merge.
