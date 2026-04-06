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
SPECTER HTTP helpers for Flask request handlers.

These primitives target repeated backend invariants that are framework-level:
JSON body extraction, required-field validation, and consistent JSON error
envelopes. They are intentionally generic.
"""

import logging
import traceback
from functools import wraps

from flask import jsonify, request


class HTTPError(Exception):
    """Structured error for predictable JSON HTTP responses."""

    def __init__(
        self,
        message,
        status_code=400,
        *,
        status=None,
        payload=None,
        log_message=None,
        log_level=logging.WARNING,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status if status is not None else status_code
        self.payload = dict(payload or {})
        self.log_message = log_message
        self.log_level = log_level

    def to_response(self):
        """Return a Flask JSON response tuple."""
        body = dict(self.payload)
        body.setdefault('error', self.message)
        return jsonify(body), self.status_code


def json_endpoint(error_message=None, *, logger=None, include_trace=True):
    """
    Decorate a Flask handler with consistent JSON exception handling.

    The wrapped handler may return:
    - a Flask Response
    - ``dict`` / ``list`` payloads, which will be ``jsonify()``-ed
    - ``(payload, status[, headers])`` tuples
    """
    if callable(error_message):
        fn = error_message
        return json_endpoint(
            'Internal server error',
            logger=logger,
            include_trace=include_trace,
        )(fn)

    message = error_message or 'Internal server error'
    route_logger = logger

    def decorator(fn):
        nonlocal route_logger
        route_logger = route_logger or logging.getLogger(fn.__module__)

        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return _normalize_response(fn(*args, **kwargs))
            except HTTPError as exc:
                if exc.log_message:
                    route_logger.log(exc.log_level, exc.log_message)
                return exc.to_response()
            except Exception as exc:
                route_logger.error(f"{message}: {exc}")
                if include_trace:
                    route_logger.debug(traceback.format_exc())
                return jsonify({'error': message}), 500

        return wrapper

    return decorator


def expect_json(*, required=None, error='Request body is required', allow_empty=False):
    """
    Parse and validate a JSON object request body.

    Args:
        required: Optional iterable of required field names.
        error: Error message when the body is missing.
        allow_empty: If ``False``, ``{}`` is treated as missing.

    Returns:
        Parsed JSON object.
    """
    payload = request.get_json(silent=True)
    if payload is None:
        raise HTTPError(error, 400)
    if not isinstance(payload, dict):
        raise HTTPError('JSON body must be an object', 400)
    if not allow_empty and not payload:
        raise HTTPError(error, 400)
    if required:
        require_fields(payload, required)
    return payload


def require_fields(payload, required, *, error=None, blank_is_missing=True):
    """
    Validate that a mapping contains the required keys.

    Returns the original payload for convenient chaining.
    """
    missing = []
    for field in required:
        if field not in payload:
            missing.append(field)
            continue

        value = payload[field]
        if value is None:
            missing.append(field)
            continue

        if blank_is_missing and isinstance(value, str) and not value.strip():
            missing.append(field)

    if missing:
        raise HTTPError(
            error or f"Missing required field(s): {', '.join(missing)}",
            400,
            payload={'missing': missing},
        )

    return payload


def _normalize_response(result):
    """Auto-jsonify plain payloads from decorated handlers."""
    if isinstance(result, tuple):
        body = result[0]
        if isinstance(body, (dict, list)):
            return (jsonify(body), *result[1:])
        return result

    if isinstance(result, (dict, list)):
        return jsonify(result)

    return result
