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
from typing import Any, Dict, Generic, Optional, Tuple, Type, TypeVar, cast, overload

T = TypeVar('T')


@dataclass(frozen=True)
class Outcome(Generic[T]):
    """Structured success/failure result."""

    ok: bool
    value: Optional[T] = None
    error: Optional[str] = None
    status: int = 200
    meta: Dict[str, Any] = field(default_factory=dict)

    @overload
    @classmethod
    def success(cls: Type['Outcome[T]'], value: T, *, status: int = 200, **meta: Any) -> 'Outcome[T]':
        ...

    @overload
    @classmethod
    def success(cls, value: None = None, *, status: int = 200, **meta: Any) -> 'Outcome[None]':
        ...

    @classmethod
    def success(cls, value: Any = None, *, status: int = 200, **meta: Any) -> 'Outcome[Any]':
        return cls(True, value=value, error=None, status=status, meta=dict(meta))

    @classmethod
    def failure(
        cls,
        error: object,
        *,
        status: int = 400,
        value: Optional[T] = None,
        **meta: Any,
    ) -> 'Outcome[T]':
        return cls(False, value=value, error=str(error), status=status, meta=dict(meta))

    def unwrap(self) -> T:
        """Return the value or raise ``RuntimeError`` on failure."""
        if not self.ok:
            raise RuntimeError(self.error or 'Outcome is not successful')
        return cast(T, self.value)

    def to_dict(self) -> Dict[str, Any]:
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

    def to_tuple(self) -> Tuple[bool, Optional[T], Optional[str]]:
        """Return a compatibility tuple: ``(ok, value, error)``."""
        return self.ok, self.value, self.error
