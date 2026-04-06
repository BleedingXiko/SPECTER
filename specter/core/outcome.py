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

"""SPECTER Outcome — structured service/operation result contract."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Outcome:
    """Structured success/failure result."""

    ok: bool
    value: object = None
    error: str = None
    status: int = 200
    meta: dict = field(default_factory=dict)

    @classmethod
    def success(cls, value=None, *, status=200, **meta):
        return cls(True, value=value, error=None, status=status, meta=dict(meta))

    @classmethod
    def failure(cls, error, *, status=400, value=None, **meta):
        return cls(False, value=value, error=str(error), status=status, meta=dict(meta))

    def unwrap(self):
        """Return the value or raise ``RuntimeError`` on failure."""
        if not self.ok:
            raise RuntimeError(self.error or 'Outcome is not successful')
        return self.value

    def to_dict(self):
        """Convert to a plain payload."""
        payload = dict(self.meta)
        payload['ok'] = self.ok
        payload['status'] = self.status
        if self.ok:
            payload['value'] = self.value
        else:
            payload['error'] = self.error
            payload['value'] = self.value
        return payload

    def to_tuple(self):
        """Return a compatibility tuple: ``(ok, value, error)``."""
        return self.ok, self.value, self.error
