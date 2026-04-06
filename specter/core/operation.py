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
SPECTER Operation — structured execution primitive for backend actions.

Structured execution primitive for service-layer actions that validate input,
perform work, and return a typed result instead of ad hoc tuples.
"""

import logging

from .outcome import Outcome

logger = logging.getLogger(__name__)


class OperationError(Exception):
    """Typed operation failure with status and metadata."""

    def __init__(self, message, *, status=400, meta=None):
        super().__init__(message)
        self.message = str(message)
        self.status = status
        self.meta = dict(meta or {})


class Operation:
    """
    Base class for structured backend actions.

    Subclasses override ``validate()`` and/or ``perform()`` and return values
    or explicit ``Outcome`` instances.
    """

    name = 'operation'

    def __init__(self, *, logger_instance=None):
        self.logger = logger_instance or logging.getLogger(type(self).__module__)

    def validate(self, *args, **kwargs):
        """Optional validation hook. Raise ``OperationError`` to fail."""

    def perform(self, *args, **kwargs):
        """Required work hook."""
        raise NotImplementedError(
            f"{type(self).__name__}.perform() must be implemented"
        )

    def run(self, *args, **kwargs):
        """Execute the operation and always return an ``Outcome``."""
        try:
            self.validate(*args, **kwargs)
            result = self.perform(*args, **kwargs)
            if isinstance(result, Outcome):
                return result
            return Outcome.success(result)
        except OperationError as exc:
            return Outcome.failure(exc.message, status=exc.status, **exc.meta)
        except Exception as exc:
            self.logger.error(
                f"[SPECTER:operation] {type(self).__name__} failed: {exc}",
                exc_info=True,
            )
            return Outcome.failure(str(exc), status=500)

    def success(self, value=None, *, status=200, **meta):
        """Helper for explicit success outcomes."""
        return Outcome.success(value, status=status, **meta)

    def failure(self, error, *, status=400, **meta):
        """Helper for explicit failure outcomes."""
        return Outcome.failure(error, status=status, **meta)
