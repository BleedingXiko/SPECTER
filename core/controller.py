"""
SPECTER Controller — feature-level composition primitive.

Features that span multiple subsystems (routes, socket handlers, runtime
state, service calls, and background work) often have no single composition
root — logic ends up scattered across handler files, route files, and service
files.

A Controller provides a first-class composition root that binds all of these
together under one lifecycle:

  1. Owns runtime state (Model/Store).
  2. Exposes HTTP routes (Router).
  3. Handles socket events (Handler).
  4. Depends on services (via registry).
  5. Spawns background work (greenlets, watchers).
  6. Tears everything down deterministically on shutdown.

Usage::

    from specter import Controller, Schema, Field

    class TVCastController(Controller):
        name = 'tv_cast'

        # Declare schemas for this feature
        schemas = {
            'cast_media': Schema('cast_media', {
                'media_type': Field(str, default='video'),
                'media_path': Field(str, required=True),
                'loop':       Field(bool, default=True),
            }),
        }

        def on_start(self):
            self.state = self.create_model('tv_cast.state', {
                'tv_sid': None,
                'casting': False,
                'playback': {},
            })

        def routes(self, router):
            @router.route('/api/tv/status', methods=['GET'])
            def get_status(self):
                return self.state.get()

        def events(self, handler):
            handler.on('tv_connected', self.handle_connect)
            handler.on('cast_media', self.handle_cast)

        def handle_connect(self, data):
            ...

        def handle_cast(self, data):
            clean = self.schemas['cast_media'].require(data)
            ...
"""

import logging

from .handler import Handler
from .lifecycle import Service
from .registry import registry

logger = logging.getLogger(__name__)


class Controller(Service):
    """
    Feature-level composition root.

    A Controller is a ``Service`` that additionally provides a declarative
    surface for binding routes, events, schemas, and state under one
    lifecycle.  It answers the question: "Where does this feature live?"

    Subclasses override lifecycle hooks just like ``Service``, but also
    get a structured place to declare their routes and event bindings:

    - ``on_start()`` — initialize state, spawn watchers/background work.
    - ``on_stop()``  — custom teardown (auto-cleanup handles most cases).
    - ``build_routes(router)`` — define HTTP routes for this feature.
    - ``build_events(handler)`` — define socket events for this feature.

    The Controller can produce a Flask Blueprint via ``build_blueprint()``
    and a SPECTER Handler via ``build_handler(socketio)``.

    Controllers are **not** mandatory.  Simple services that don't span
    routes + sockets + state don't need one.  Use a Controller when a
    feature is scattered across 3+ subsystem boundaries.
    """

    schemas = {}  # Override with {name: Schema} in subclasses
    url_prefix = None  # Override with string path in subclasses

    def __init__(self, name=None):
        super().__init__(name or getattr(self.__class__, 'name', 'controller'))
        self._blueprint = None
        self._router = None
        self._handler_instance = None

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------

    def build_routes(self, router):
        """
        Override to declare HTTP routes for this feature.

        Called once when ``build_blueprint()`` is invoked.  Receives a
        SPECTER ``Router`` instance.

        Example::

            def build_routes(self, router):
                @router.route('/api/myfeature/status')
                def get_status():
                    return jsonify(self.state.get())
        """

    def build_events(self, handler):
        """
        Override to declare socket events for this feature.

        Called once when ``build_handler()`` is invoked.  Receives a
        SPECTER ``Handler``-compatible object.

        Example::

            def build_events(self, handler):
                handler.on('my_event', self.handle_event)
        """

    # ------------------------------------------------------------------
    # Blueprint builder
    # ------------------------------------------------------------------

    def build_blueprint(self, url_prefix=None):
        """
        Build a Flask Blueprint from this controller's route definitions.

        Returns a Flask ``Blueprint`` that can be registered with
        ``app.register_blueprint(bp)``.

        This lazily creates a ``Router`` and calls ``build_routes()``.
        """
        if self._blueprint is not None:
            return self._blueprint

        from ..router import Router

        router = Router(
            self.name,
            url_prefix=url_prefix or '',
            import_name=self.__class__.__module__,
        )

        # Let the subclass define routes
        self.build_routes(router)

        self._router = router
        self._blueprint = router.blueprint()
        return self._blueprint

    # ------------------------------------------------------------------
    # Handler builder
    # ------------------------------------------------------------------

    def create_handler(self):
        """
        Create the controller-backed socket handler.

        The handler is created once and reused on subsequent calls.
        """
        if self._handler_instance is not None:
            return self._handler_instance

        handler = _ControllerHandler(
            name=f'{self.name}_events',
            controller=self,
        )
        self.build_events(handler)
        self._handler_instance = handler
        self.own(handler)
        return handler

    def build_handler(self, socketio=None):
        """
        Build and optionally register socket event handlers from this
        controller's event definitions.

        Returns the ``_ControllerHandler`` instance.
        """
        handler = self.create_handler()
        if socketio is not None:
            handler.setup(socketio)
        return handler

    # ------------------------------------------------------------------
    # Schema convenience
    # ------------------------------------------------------------------

    def schema(self, name):
        """Look up a schema by name. Raises KeyError if not found."""
        if name not in self.schemas:
            raise KeyError(
                f"[SPECTER:controller] '{self.name}' has no schema '{name}'. "
                f"Available: {list(self.schemas.keys())}"
            )
        return self.schemas[name]

    # ------------------------------------------------------------------
    # State convenience (delegates to Service)
    # ------------------------------------------------------------------

    def create_model(self, name, initial_state=None):
        """Create a Model owned by this controller and return it."""
        from .model import create_model
        model = create_model(name, initial_state)
        self.own(model)
        return model

    def create_store(self, name, initial_state=None, **kwargs):
        """Create a Store owned by this controller and return it."""
        from .store import create_store
        store = create_store(name, initial_state, **kwargs)
        self.own(store)
        return store

    # ------------------------------------------------------------------
    # Lifecycle overrides
    # ------------------------------------------------------------------

    def stop(self):
        """Stop the controller and tear down its handler."""
        if self._handler_instance:
            try:
                self._handler_instance.teardown()
            except Exception as e:
                logger.warning(
                    f"[SPECTER:controller] '{self.name}' handler "
                    f"teardown error: {e}"
                )
            self._handler_instance = None
        super().stop()

    def health(self):
        """Aggregate health from this controller and its handler."""
        report = super().health()
        report['has_blueprint'] = self._blueprint is not None
        report['has_handler'] = self._handler_instance is not None
        if self.schemas:
            report['schemas'] = list(self.schemas.keys())
        return report

    def __repr__(self):
        parts = [f"Controller '{self.name}'"]
        if self.schemas:
            parts.append(f"{len(self.schemas)} schemas")
        if self._blueprint:
            parts.append("routes")
        if self._handler_instance:
            parts.append("events")
        return f"<{', '.join(parts)}>"

    @property
    def socketio(self):
        """Return the active Flask-SocketIO instance for controller events."""
        if self._handler_instance is not None and getattr(self._handler_instance, '_socketio', None) is not None:
            return self._handler_instance._socketio

        manager = registry.resolve('service_manager')
        return getattr(manager, 'socketio', None) if manager is not None else None


class _ControllerHandler(Handler):
    """Dynamic event handler built from a controller's ``build_events()``."""

    def __init__(self, name, controller):
        super().__init__()
        self.name = name
        self._controller = controller
        self._events = []

    def on(self, event, callback, priority=100):
        """Register a socket event handler callback."""
        self._events.append((event, callback, priority))
        return self

    def setup(self, socketio):
        """Wire registered callbacks into the Socket.IO instance."""
        if self._registered:
            return self

        self._socketio = socketio
        ingress = registry.resolve('socket_ingress')

        for event, callback, priority in self._events:
            if ingress is not None:
                ingress.subscribe(
                    event,
                    callback,
                    owner=self,
                    priority=priority,
                )
            else:
                socketio.on_event(event, callback)
            self._registered_handlers.append((event, callback))

        self._registered = True
        logger.debug(
            f"[SPECTER:controller] '{self.name}' registered "
            f"{len(self._events)} events"
        )

        try:
            self.on_setup()
        except Exception as e:
            logger.error(
                f"[SPECTER] Handler '{self.name}' on_setup failed: {e}",
                exc_info=True,
            )

        return self
