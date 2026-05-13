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
SPECTER Router — class-based HTTP route composition.

This is the HTTP-side counterpart to ``Handler``. It gives Specter a real
route primitive instead of leaving future HTTP modules as loose Flask
functions plus ad hoc helpers.
"""

from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable, Iterable, Iterator, List, Optional, Tuple

from flask import Blueprint, jsonify

from .core.outcome import Outcome
from .core.ownership import resolve_cleanup
from .core.registry import registry
from .core._typing import CleanupCallback, F
from .http import json_endpoint


@dataclass(frozen=True)
class RouteSpec:
    rule: str
    methods: Tuple[str, ...]
    endpoint: Optional[str] = None
    json_errors: Optional[str] = None


def route(
    rule: str,
    *,
    methods: Optional[Iterable[str]] = None,
    endpoint: Optional[str] = None,
    json_errors: Optional[str] = None,
) -> Callable[[F], F]:
    """Declare a class-based HTTP route on a ``Router`` method."""
    methods = tuple(methods or ('GET',))

    def decorator(fn: F) -> F:
        specs = list(getattr(fn, '_specter_routes', ()))
        specs.append(
            RouteSpec(
                rule=rule,
                methods=methods,
                endpoint=endpoint,
                json_errors=json_errors,
            )
        )
        setattr(fn, '_specter_routes', tuple(specs))
        return fn

    return decorator


class Router:
    """Class-based Flask router with registry access and owned cleanup."""

    name = 'router'
    url_prefix = ''

    def __init__(
        self,
        name: Optional[str] = None,
        *,
        url_prefix: Optional[str] = None,
        import_name: Optional[str] = None,
    ) -> None:
        self.name = str(name or getattr(self.__class__, 'name', 'router'))
        if url_prefix is not None:
            self.url_prefix = url_prefix
        self._blueprint = Blueprint(
            self.name,
            import_name or self.__class__.__module__,
        )
        setattr(self._blueprint, 'strict_slashes', False)
        self._mounted = False
        self._cleanups: List[CleanupCallback] = []
        self._dynamic_routes: List[Tuple[str, Callable[..., Any], RouteSpec]] = []

    def on_mount(self) -> None:
        """Hook called after route registration."""

    def on_unmount(self) -> None:
        """Hook called before cleanups are run."""

    def mount(self) -> Blueprint:
        """Build and return the router blueprint."""
        if self._mounted:
            return self._blueprint

        for attr_name, attr in self._iter_route_methods():
            for spec in getattr(attr, '_specter_routes', ()):
                view = self._build_view(attr, spec)
                self._blueprint.add_url_rule(
                    spec.rule,
                    endpoint=spec.endpoint or attr_name,
                    view_func=view,
                    methods=list(spec.methods),
                )

        for attr_name, attr, spec in self._iter_dynamic_routes():
            view = self._build_view(attr, spec)
            self._blueprint.add_url_rule(
                spec.rule,
                endpoint=spec.endpoint or attr_name,
                view_func=view,
                methods=list(spec.methods),
            )

        self.on_mount()
        self._mounted = True
        return self._blueprint

    def blueprint(self) -> Blueprint:
        """Return the mounted blueprint."""
        return self.mount()

    def register(self, app: Any) -> 'Router':
        """Register the blueprint on a Flask app."""
        app.register_blueprint(self.mount(), url_prefix=self.url_prefix)
        return self

    def teardown(self) -> 'Router':
        """Run router cleanup callbacks."""
        try:
            self.on_unmount()
        finally:
            while self._cleanups:
                fn = self._cleanups.pop()
                try:
                    fn()
                except Exception:
                    pass
        return self

    def require(self, key: str) -> Any:
        """Resolve a required registry entry."""
        return registry.require(key)

    def resolve(self, key: str) -> Any:
        """Resolve an optional registry entry."""
        return registry.resolve(key)

    def add_cleanup(self, fn: CleanupCallback) -> 'Router':
        """Register cleanup for router teardown."""
        if callable(fn):
            self._cleanups.append(fn)
        return self

    def own(self, resource: Optional[Any], stop_method: Optional[str] = None) -> Optional[Any]:
        """Own a non-router resource for teardown cleanup."""
        if resource is None:
            return None
        self.add_cleanup(resolve_cleanup(resource, stop_method=stop_method))
        return resource

    def route(
        self,
        rule: str,
        *,
        methods: Optional[Iterable[str]] = None,
        endpoint: Optional[str] = None,
        json_errors: Optional[str] = None,
    ) -> Callable[[F], F]:
        """Register a route against this router instance."""
        spec = RouteSpec(
            rule=rule,
            methods=tuple(methods or ('GET',)),
            endpoint=endpoint,
            json_errors=json_errors,
        )

        def decorator(fn: F) -> F:
            self._dynamic_routes.append((fn.__name__, fn, spec))
            return fn

        return decorator

    def _build_view(self, method: Callable[..., Any], spec: RouteSpec) -> Callable[..., Any]:
        view = method

        if spec.json_errors:
            view = json_endpoint(spec.json_errors)(view)

        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            result = view(*args, **kwargs)
            if isinstance(result, Outcome):
                return jsonify(result.to_dict()), result.status
            return result

        return wrapped

    def _iter_route_methods(self) -> Iterator[Tuple[str, Callable[..., Any]]]:
        seen = set()
        for cls in reversed(type(self).__mro__):
            for attr_name, attr in cls.__dict__.items():
                if attr_name in seen:
                    continue
                if callable(attr) and getattr(attr, '_specter_routes', None):
                    seen.add(attr_name)
                    yield attr_name, getattr(self, attr_name)

    def _iter_dynamic_routes(self) -> Iterator[Tuple[str, Callable[..., Any], RouteSpec]]:
        for attr_name, attr, spec in self._dynamic_routes:
            yield attr_name, attr, spec
