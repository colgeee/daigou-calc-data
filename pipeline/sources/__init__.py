"""Adapter protocol. Each source module registers one Adapter instance in REGISTRY."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Protocol

from pipeline.census import Census
from pipeline.model import ZipRate


class Adapter(Protocol):
    name: str
    states: tuple[str, ...]

    def rows(self, census: Census, on: date) -> Iterable[ZipRate]: ...


REGISTRY: list[Adapter] = []
