from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class DomainEntry(BaseModel):
    name: str
    sourcetype: str
    description: str = ""


class DomainRegistry(BaseModel):
    domains: list[DomainEntry] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> DomainRegistry:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        items = raw.get("domains") or []
        return cls(domains=[DomainEntry.model_validate(d) for d in items])

    def by_name(self) -> dict[str, DomainEntry]:
        return {d.name.lower(): d for d in self.domains}

    def prompt_block(self) -> str:
        lines = []
        for d in self.domains:
            lines.append(f"- {d.name}: {d.description}")
        return "\n".join(lines)

    def resolve(self, name: str) -> DomainEntry | None:
        return self.by_name().get(name.strip().lower())
