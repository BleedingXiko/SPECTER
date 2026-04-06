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
