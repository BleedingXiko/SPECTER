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

"""Internal typing vocabulary for SPECTER's dynamic framework surface."""

from typing import Any, Callable, Dict, List, Mapping, MutableMapping, Optional, Protocol, TypeVar, Union


JSONPrimitive = Optional[Union[str, int, float, bool]]
JSONValue = Any
JSONDict = Dict[str, Any]
JSONList = List[Any]
StateDict = Dict[str, Any]
Payload = Mapping[str, Any]
MutablePayload = MutableMapping[str, Any]

CleanupCallback = Callable[[], Any]
Unsubscribe = Callable[[], Any]
BusCallback = Callable[[Any], Any]
StateSubscriber = Callable[[StateDict, Any], Any]
WatcherSubscriber = Callable[[Any, Any], Any]
CacheInvalidationCallback = Callable[[], Any]
SocketCallback = Callable[..., Any]
RouteCallable = Callable[..., Any]
Mutator = Callable[[StateDict], Any]

T = TypeVar('T')
F = TypeVar('F', bound=Callable[..., Any])


class CleanupOwner(Protocol):
    """Object that can own cleanup callbacks."""

    def add_cleanup(self, fn: CleanupCallback) -> Any:
        ...


class HandlerLike(Protocol):
    """Object that participates in setup/teardown lifecycle."""

    name: str

    def setup(self, *args: Any, **kwargs: Any) -> Any:
        ...

    def teardown(self) -> Any:
        ...


class Stoppable(Protocol):
    """Object exposing a stop-style cleanup method."""

    def stop(self) -> Any:
        ...
