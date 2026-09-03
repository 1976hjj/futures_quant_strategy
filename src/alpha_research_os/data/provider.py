"""Provider boundary: acquisition only, never research trust."""

from __future__ import annotations

from typing import Protocol

from .contracts import FetchRequest, ProviderResponse, ProviderSpec


class DataProvider(Protocol):
    @property
    def spec(self) -> ProviderSpec: ...

    def fetch(self, request: FetchRequest) -> ProviderResponse: ...
