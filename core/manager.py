"""
SPECTER ServiceManager — Composition root orchestrator.

The ServiceManager is the root-level owner that manages ordered startup
and shutdown of all services and handlers.  It also provides a health
endpoint and integrates with Flask's ``atexit`` for graceful shutdown.

Usage::

    from specter.core.manager import ServiceManager

    manager = ServiceManager(app, socketio)

    manager.register_service(ThumbnailService())
    manager.register_service(HDMIService())

    manager.register_handler(TVHandler())
    manager.register_handler(SyncHandler())

    manager.boot()
    # All services started in registration order,
    # all handlers set up, atexit hook installed.

    manager.shutdown()
    # All handlers torn down (reverse), then services stopped (reverse).
"""

import atexit
import logging

from .lifecycle import Service
from .registry import registry
from .socket_ingress import SocketIngress

logger = logging.getLogger(__name__)


class ServiceManager(Service):
    """
    Root-level service orchestrator.

    Inherits from ``Service`` so it can be used as an owner for registry
    entries (``registry.provide(..., owner=manager)``).

    The manager maintains separate ordered lists for services and handlers,
    ensuring deterministic startup/shutdown.
    """

    def __init__(self, app=None, socketio=None):
        """
        Args:
            app: The Flask app instance.
            socketio: The Flask-SocketIO instance.
        """
        super().__init__('service_manager')
        self._app = app
        self._socketio = socketio
        self._socket_ingress = SocketIngress(socketio=socketio)
        self._services = []   # Ordered list of Service instances
        self._handlers = []   # Ordered list of Handler instances
        self._booted = False
        self._atexit_registered = False

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_service(self, service):
        """
        Register a service for managed lifecycle.

        Services are started in registration order during ``boot()``
        and stopped in reverse order during ``shutdown()``.

        Args:
            service: A ``Service`` instance.

        Returns:
            ``self`` for chaining.
        """
        if not isinstance(service, Service):
            raise TypeError(
                f"[SPECTER:manager] register_service() requires a Service "
                f"instance, got {type(service).__name__}"
            )
        self._services.append(service)
        if self._booted:
            self._start_registered_service(service)
        return self

    def register_handler(self, handler):
        """
        Register a handler for managed lifecycle.

        Handlers are set up after all services start during ``boot()``
        and torn down before services stop during ``shutdown()``.

        Args:
            handler: A ``Handler`` instance.

        Returns:
            ``self`` for chaining.
        """
        if (
            not hasattr(handler, 'setup') or
            not callable(handler.setup) or
            not hasattr(handler, 'teardown') or
            not callable(handler.teardown)
        ):
            raise TypeError(
                "[SPECTER:manager] register_handler() requires a handler-like "
                f"object exposing setup()/teardown(), got {type(handler).__name__}"
            )
        self._handlers.append(handler)
        if self._booted and self._socketio:
            self._setup_registered_handler(handler)
        return self

    def register_controller(self, controller, *, url_prefix=None):
        """
        Register a controller as a managed service plus managed handler.

        If an app is available, the controller blueprint is registered
        immediately. If Socket.IO is available, the controller handler is
        created and lifecycle-managed by the manager.
        """
        from .controller import Controller

        if not isinstance(controller, Controller):
            raise TypeError(
                "[SPECTER:manager] register_controller() requires a Controller "
                f"instance, got {type(controller).__name__}"
            )

        if self._app:
            # Use a strictly unique name if registered at root to avoid collisions
            blueprint = controller.build_blueprint(url_prefix=url_prefix)
            prefix = str(url_prefix or '')
            self._app.register_blueprint(blueprint, url_prefix=prefix)

        handler = controller.build_handler()
        self.register_service(controller)
        self.register_handler(handler)
        return self

    # ------------------------------------------------------------------
    # Boot / Shutdown
    # ------------------------------------------------------------------

    def boot(self):
        """
        Boot the application.

        1. Starts the manager itself (so it can be used as a registry owner).
        2. Starts all registered services in order.
        3. Sets up all registered handlers.
        4. Provides the manager to the registry.
        5. Installs an ``atexit`` shutdown hook.

        Idempotent — safe to call multiple times.

        Returns:
            ``self`` for chaining.
        """
        if self._booted:
            return self

        logger.info("[SPECTER] ========== BOOT SEQUENCE START ==========")

        # Start the manager itself (enables owned registry entries)
        self.start()

        # Provide the manager to the registry so services can access it
        registry.provide('service_manager', self, owner=self, replace=True)
        registry.provide(
            'socket_ingress',
            self._socket_ingress,
            owner=self,
            replace=True,
        )

        # Start services in registration order
        for service in self._services:
            self._start_registered_service(service)

        # Set up handlers (after services, since handlers may depend on them)
        if self._socketio:
            for handler in self._handlers:
                self._setup_registered_handler(handler)

        # Install atexit hook for graceful shutdown.
        # Wrapped in a helper that suppresses logging.raiseExceptions so that
        # harmless "I/O operation on closed file" errors from StreamHandlers
        # being closed before atexit fires do not pollute stderr.
        if not self._atexit_registered:
            def _atexit_shutdown():
                import logging as _logging
                _prev = _logging.raiseExceptions
                _logging.raiseExceptions = False
                try:
                    self.shutdown()
                finally:
                    _logging.raiseExceptions = _prev

            atexit.register(_atexit_shutdown)
            self._atexit_registered = True

        self._booted = True
        logger.info(
            f"[SPECTER] ========== BOOT COMPLETE "
            f"({len(self._services)} services, "
            f"{len(self._handlers)} handlers) =========="
        )
        return self

    def shutdown(self):
        """
        Gracefully shut down the application.

        1. Tears down all handlers (reverse order).
        2. Stops all services (reverse order).
        3. Stops the manager itself.
        4. Clears the registry.

        Idempotent — safe to call multiple times.

        Returns:
            ``self`` for chaining.
        """
        if not self._booted:
            return self

        # Mark booted=False immediately so re-entrant calls are no-ops.
        self._booted = False

        logger.info("[SPECTER] ========== SHUTDOWN SEQUENCE START ==========")

        # Tear down handlers first (reverse order)
        for handler in reversed(self._handlers):
            try:
                handler.teardown()
                logger.info(
                    f"[SPECTER] ✓ Handler '{handler.name}' torn down"
                )
            except Exception as e:
                logger.error(
                    f"[SPECTER] ✗ Handler '{handler.name}' "
                    f"teardown failed: {e}",
                    exc_info=True,
                )

        # Stop services (reverse order — last started = first stopped)
        for service in reversed(self._services):
            try:
                service.stop()
                logger.info(
                    f"[SPECTER] ✓ Service '{service.name}' stopped"
                )
            except Exception as e:
                logger.error(
                    f"[SPECTER] ✗ Service '{service.name}' "
                    f"stop failed: {e}",
                    exc_info=True,
                )

        # Clear service list to prevent double-stop
        self._services.clear()
        self._handlers.clear()
        self._socket_ingress.clear()

        # Clear registry
        registry.clear()

        # Stop the manager itself
        self.stop()

        logger.info("[SPECTER] ========== SHUTDOWN COMPLETE ==========")
        return self

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self):
        """
        Aggregate health report for all managed services.

        Returns:
            A dict with the manager's status and each service's health.
        """
        result = {
            'status': 'ok' if self._booted else 'stopped',
            'services': {},
            'handlers': {
                h.name: {'registered': getattr(h, 'registered', False)}
                for h in self._handlers
            },
        }
        for service in self._services:
            try:
                result['services'][service.name] = service.health()
            except Exception as e:
                result['services'][service.name] = {
                    'status': 'error',
                    'error': str(e),
                }
        return result

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def app(self):
        """The Flask app instance."""
        return self._app

    @property
    def socket_ingress(self):
        """The shared Socket.IO ingress dispatcher."""
        return self._socket_ingress

    @property
    def socketio(self):
        """The Flask-SocketIO instance used by this manager."""
        return self._socketio

    @property
    def booted(self):
        """``True`` if boot() has been called successfully."""
        return self._booted

    def _start_registered_service(self, service):
        try:
            # Provide before start so that on_start() can resolve dependencies (including self)
            registry.provide(
                service.name,
                service,
                owner=self,
                replace=True,
            )
            service.start()
            logger.info(
                f"[SPECTER] ✓ Service '{service.name}' started"
            )
        except Exception as e:
            # Unregister if start fails
            registry.unregister(service.name)
            logger.error(
                f"[SPECTER] ✗ Service '{service.name}' failed to start: {e}",
                exc_info=True,
            )

    def _setup_registered_handler(self, handler):
        try:
            handler.setup(self._socketio)
            logger.info(
                f"[SPECTER] ✓ Handler '{handler.name}' set up"
            )
        except Exception as e:
            logger.error(
                f"[SPECTER] ✗ Handler '{handler.name}' "
                f"failed to set up: {e}",
                exc_info=True,
            )
