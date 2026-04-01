"""SPECTER boot helpers for app-level composition roots."""

from .core.manager import ServiceManager


def boot(
    app,
    socketio,
    *,
    services=None,
    handlers=None,
    controllers=None,
):
    """
    Create and boot a ``ServiceManager`` with the supplied components.

    Args:
        app: Flask app instance.
        socketio: Flask-SocketIO instance.
        services: Optional iterable of ``Service`` instances.
        handlers: Optional iterable of handler instances.
        controllers: Optional iterable of ``Controller`` instances.

    Returns:
        A booted ``ServiceManager``.
    """
    manager = ServiceManager(app, socketio)

    for service in services or ():
        manager.register_service(service)

    for handler in handlers or ():
        manager.register_handler(handler)

    for controller in controllers or ():
        manager.register_controller(controller)

    manager.boot()
    return manager
