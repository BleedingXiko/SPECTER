# Copyright 2026 BleedingXiko
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
SPECTER — Service Primitives for Event Control, Teardown, and Execution Runtime
================================================================================

Lifecycle-first backend framework for Python, Flask, and gevent.
Provides explicit ownership, deterministic cleanup, and clear service
boundaries for Flask/gevent/SQLite applications.

**Framework Surface:**

+--------------------+----------------------------------+
| Channel            | Role                             |
+====================+==================================+
| ``Service``        | Background lifecycle owner       |
+--------------------+----------------------------------+
| ``QueueService``   | Queue-backed worker lifecycle    |
+--------------------+----------------------------------+
| ``Controller``     | Feature composition root         |
+--------------------+----------------------------------+
| ``Handler``        | Socket event lifecycle owner     |
+--------------------+----------------------------------+
| ``create_model``   | Structured state graph           |
+--------------------+----------------------------------+
| ``create_store``   | Shared mutable state             |
+--------------------+----------------------------------+
| ``create_cache``   | Shared state with TTL + cascade  |
+--------------------+----------------------------------+
| ``ManagedProcess`` | Subprocess + stream ownership    |
+--------------------+----------------------------------+
| ``Watcher``        | Observation loop primitive       |
+--------------------+----------------------------------+
| ``Schema``         | Payload contract / validation    |
+--------------------+----------------------------------+
| ``Outcome``        | Structured operation result      |
+--------------------+----------------------------------+
| ``Operation``      | Structured backend action        |
+--------------------+----------------------------------+
| ``Router``         | Class-based HTTP composition     |
+--------------------+----------------------------------+
| ``registry``       | Composition-root service locator |
+--------------------+----------------------------------+
| ``bus``            | Internal pub/sub                 |
+--------------------+----------------------------------+
| ``json_endpoint``  | Flask route envelope helpers     |
+--------------------+----------------------------------+

**Import from here:**

    from specter import (
        Service, QueueService, Controller, Handler, create_store,
        create_cache, Watcher, Schema, Field,
        registry, bus, HTTPError, json_endpoint, expect_json,
    )

See ``specter.md`` for the full guide.
"""

# Core primitives
from .core.lifecycle import Service
from .core.queue_service import QueueService
from .core.controller import Controller
from .core.process import ManagedProcess, start_process
from .core.watcher import Watcher, WatcherError
from .core.model import Model, create_model
from .core.schema import Schema, Field, SchemaError
from .core.outcome import Outcome
from .core.operation import Operation, OperationError
from .core.store import Store, create_store
from .core.handler import Handler
from .core.cache import Cache, create_cache
from .core.bus import EventBus, bus
from .core.registry import SPECTERRegistry, registry
from .core.manager import ServiceManager
from .core.socket_ingress import SocketIngress
from .http import HTTPError, json_endpoint, expect_json, require_fields
from .router import Router, route
from .boot import boot

__all__ = [
    # Lifecycle
    'Service',
    'QueueService',
    'Controller',
    'ManagedProcess',
    'start_process',
    'Watcher',
    'WatcherError',
    'Outcome',
    'Operation',
    'OperationError',
    'Router',
    'route',
    'Handler',
    'ServiceManager',
    'SocketIngress',
    'boot',
    # State
    'Model',
    'create_model',
    'Store',
    'create_store',
    'Cache',
    'create_cache',
    # Contracts
    'Schema',
    'Field',
    'SchemaError',
    # Communication
    'EventBus',
    'bus',
    # Registry
    'SPECTERRegistry',
    'registry',
    # HTTP
    'HTTPError',
    'json_endpoint',
    'expect_json',
    'require_fields',
]
