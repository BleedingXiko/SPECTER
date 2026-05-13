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
SPECTER Schema — lightweight, composable payload contract primitive.

Provides a declarative, composable way to define payload contracts across
HTTP routes, socket handlers, and service methods.  It is intentionally NOT
a full ORM or serialization framework — it solves the "extract, coerce,
validate" problem that every handler/route repeats manually:

  1. Extract fields from a dict/request.
  2. Apply type coercion (int, float, bool, str).
  3. Provide defaults for optional fields.
  4. Validate required fields.
  5. Return structured errors for missing or invalid data.

Usage::

    from specter import Schema, Field

    # Define a contract
    cast_media = Schema('cast_media', {
        'media_type':  Field(str, default='video'),
        'media_path':  Field(str, required=True),
        'loop':        Field(bool, default=True),
        'start_time':  Field(float, default=0),
        'duration':    Field(float, default=0),
        'category_id': Field(str),
        'subtitle_url': Field(str),
    })

    # Validate a dict (from request.json, socket data, etc.)
    result = cast_media.validate(data)
    if not result.ok:
        return jsonify({'error': result.error}), 400
    clean = result.value  # typed, defaulted, validated dict

    # Use in a route decorator (pair with json_endpoint)
    @json_endpoint
    def handle_cast(data):
        clean = cast_media.require(data)  # raises HTTPError on bad input
        ...

    # Reuse / compose schemas
    playback_control = Schema('playback_control', {
        'action': Field(str, required=True, choices=['play', 'pause', 'seek', 'sync']),
        'currentTime': Field(float, default=0),
    })
"""

import logging
from typing import Any, Callable, Dict, Optional, Sequence, Set, Type, Union, cast

from .outcome import Outcome

logger = logging.getLogger(__name__)


class Field:
    """
    Declarative field specification for a Schema.

    Parameters
    ----------
    type : type or callable
        The expected type or a coercion callable.  Built-in types like
        ``int``, ``float``, ``str``, ``bool`` are applied as coercers.
        Custom callables receive the raw value and should return the
        coerced value or raise.
    required : bool
        Whether the field must be present.  Default: ``False``.
    default : any
        Default value when the field is absent and not required.
        If ``default`` is a callable, it's called with no arguments to
        produce the value (like ``dataclasses.field(default_factory=...)``).\
    choices : sequence, optional
        If provided, the coerced value must be one of these choices.
    validator : callable, optional
        Extra validation function ``(value) -> bool``.  If it returns
        ``False``, validation fails.  Can also raise with a message.
    label : str, optional
        Human-friendly name for error messages (defaults to the dict key).
    """

    def __init__(
        self,
        type: Union[Type[Any], Callable[[Any], Any]] = str,
        *,
        required: bool = False,
        default: Any = None,
        choices: Optional[Sequence[Any]] = None,
        validator: Optional[Callable[[Any], bool]] = None,
        label: Optional[str] = None,
    ) -> None:
        self.type = type
        self.required = required
        self.default = default
        self.choices = tuple(choices) if choices else None
        self.validator = validator
        self.label = label

    def _resolve_default(self) -> Any:
        if callable(self.default) and self.default is not None:
            # Check it's a factory, not a type
            if not isinstance(self.default, type):
                return self.default()
        return self.default


class SchemaError(Exception):
    """Raised when schema validation fails."""

    def __init__(
        self,
        message: str,
        *,
        field: Optional[str] = None,
        errors: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.errors = errors or {}


class Schema:
    """
    Declarative payload contract.

    A Schema is a named collection of ``Field`` specs that can validate and
    coerce a dict-like payload.

    Parameters
    ----------
    name : str
        Schema name (for logging/error messages).
    fields : dict[str, Field]
        Mapping of field names to ``Field`` specifications.
    strict : bool
        If ``True``, unknown keys in the payload cause validation failure.
        Default: ``False`` (unknown keys are silently ignored).
    """

    def __init__(self, name: str, fields: Dict[str, Field], *, strict: bool = False) -> None:
        self.name = name
        self._fields: Dict[str, Field] = {}
        self._strict = strict

        for key, spec in fields.items():
            if not isinstance(spec, Field):
                raise TypeError(
                    f"[SPECTER:schema] '{name}': field '{key}' must be a "
                    f"Field instance, got {type(spec).__name__}"
                )
            self._fields[key] = spec

    def validate(self, data: Dict[str, Any]) -> Outcome[Dict[str, Any]]:
        """
        Validate and coerce a payload.

        Returns an ``Outcome`` where:
        - ``ok=True``  → ``value`` is the cleaned dict.
        - ``ok=False`` → ``error`` is a human-readable message,
          ``meta['errors']`` is a dict of ``{field: message}``.
        """
        if not isinstance(data, dict):
            return Outcome.failure(
                f"[{self.name}] Expected a dict payload, got "
                f"{type(data).__name__}",
                status=400,
            )

        errors: Dict[str, str] = {}
        result: Dict[str, Any] = {}

        # Check for unknown keys in strict mode
        if self._strict:
            unknown = set(data.keys()) - set(self._fields.keys())
            if unknown:
                for key in unknown:
                    errors[key] = f"Unknown field '{key}'"

        for key, spec in self._fields.items():
            label = spec.label or key
            raw = data.get(key)

            # Missing field handling
            if raw is None and key not in data:
                if spec.required:
                    errors[key] = f"'{label}' is required"
                    continue
                result[key] = spec._resolve_default()
                continue

            # Explicit None for optional fields
            if raw is None and not spec.required:
                result[key] = spec._resolve_default()
                continue

            # Type coercion
            try:
                coerced = _coerce(raw, spec.type)
            except (ValueError, TypeError) as exc:
                errors[key] = (
                    f"'{label}' must be {spec.type.__name__}: {exc}"
                    if hasattr(spec.type, '__name__')
                    else f"'{label}' coercion failed: {exc}"
                )
                continue

            # Choices check
            if spec.choices and coerced not in spec.choices:
                errors[key] = (
                    f"'{label}' must be one of {list(spec.choices)}, "
                    f"got '{coerced}'"
                )
                continue

            # Custom validator
            if spec.validator:
                try:
                    valid = spec.validator(coerced)
                    if valid is False:
                        errors[key] = f"'{label}' failed validation"
                        continue
                except Exception as exc:
                    errors[key] = f"'{label}' validation error: {exc}"
                    continue

            result[key] = coerced

        if errors:
            # Build a single error message from all field errors
            parts = [f"{k}: {v}" for k, v in errors.items()]
            msg = f"[{self.name}] Validation failed: {'; '.join(parts)}"
            return Outcome.failure(msg, status=400, errors=errors)

        return Outcome.success(result)

    def require(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate and return the cleaned dict, or raise.

        Raises ``SchemaError`` on validation failure.  Designed for use
        inside ``json_endpoint`` handlers where the decorator catches
        exceptions and converts them to JSON error responses.

        Also raises ``HTTPError`` if available, for seamless integration
        with the existing ``json_endpoint`` decorator.
        """
        outcome = self.validate(data or {})
        if not outcome.ok:
            # Try HTTPError for Flask integration
            try:
                from ..http import HTTPError
                raise HTTPError(
                    outcome.error or 'Validation failed',
                    status=outcome.status,
                    payload={'errors': outcome.meta.get('errors', {})},
                )
            except ImportError:
                raise SchemaError(
                    outcome.error or 'Validation failed',
                    errors=outcome.meta.get('errors', {}),
                )
        return cast(Dict[str, Any], outcome.value)

    def extend(self, name: str, extra_fields: Dict[str, Field], **kwargs: Any) -> 'Schema':
        """
        Create a new Schema that inherits this one's fields plus extras.

        Useful for composing related contracts without duplication.
        """
        merged = dict(self._fields)
        for key, spec in extra_fields.items():
            if not isinstance(spec, Field):
                raise TypeError(
                    f"[SPECTER:schema] extend '{name}': field '{key}' must "
                    f"be a Field instance"
                )
            merged[key] = spec

        strict = kwargs.get('strict', self._strict)
        return Schema(name, merged, strict=strict)

    @property
    def field_names(self) -> Set[str]:
        """Return the set of declared field names."""
        return set(self._fields.keys())

    @property
    def required_fields(self) -> Set[str]:
        """Return the set of required field names."""
        return {k for k, f in self._fields.items() if f.required}

    def __repr__(self) -> str:
        req = len(self.required_fields)
        total = len(self._fields)
        return f"<Schema '{self.name}' {total} fields ({req} required)>"


# ------------------------------------------------------------------
# Coercion helpers
# ------------------------------------------------------------------

def _coerce(value: Any, target_type: Union[Type[Any], Callable[[Any], Any]]) -> Any:
    """
    Coerce a raw value to the target type.

    Handles common Python type conversions for route and handler payloads.
    """
    # Bool coercion (must come before int, since bool is a subclass of int)
    if target_type is bool:
        return _coerce_bool(value)

    # Already the right type
    if isinstance(target_type, type):
        if isinstance(value, target_type) and not isinstance(value, bool):
            return value
        if target_type in (list, dict):
            raise TypeError(
                f"Expected {target_type.__name__}, got {type(value).__name__}"
            )

    # Numeric coercion
    if target_type in (int, float):
        try:
            return target_type(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"Cannot convert {type(value).__name__} "
                f"'{value}' to {target_type.__name__}"
            ) from exc

    # String coercion
    if target_type is str:
        return str(value)

    # Custom callable coercer
    if callable(target_type):
        return target_type(value)

    raise TypeError(f"Unsupported coercion target: {target_type}")


def _coerce_bool(value: Any) -> bool:
    """
    Coerce a value to bool.

    Mirrors the legacy bool-coercion helpers that used to be duplicated
    across multiple backend ingress modules.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ('1', 'true', 'yes', 'on'):
            return True
        if v in ('0', 'false', 'no', 'off', ''):
            return False
    return bool(value)
