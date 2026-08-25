"""Environment-driven configuration and the backend factory."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .backends.base import ITSMBackend
from .backends.mock import MockBackend


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    backend: str = "mock"
    instance_url: str = ""
    username: str = ""
    password: str = ""
    read_only: bool = False
    max_results: int = 20
    actor: str = "claude.agent"
    audit_log: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            backend=os.environ.get("SNOW_BACKEND", "mock").strip().lower(),
            instance_url=os.environ.get("SNOW_INSTANCE_URL", "").strip(),
            username=os.environ.get("SNOW_USERNAME", "").strip(),
            password=os.environ.get("SNOW_PASSWORD", ""),
            read_only=_flag("SNOW_READ_ONLY"),
            max_results=int(os.environ.get("SNOW_MAX_RESULTS", "20")),
            actor=os.environ.get("SNOW_ACTOR", "claude.agent"),
            audit_log=os.environ.get("SNOW_AUDIT_LOG", "").strip(),
        )


def build_backend(settings: Settings | None = None) -> ITSMBackend:
    """Instantiate the configured backend."""
    settings = settings or Settings.from_env()
    if settings.backend in ("mock", "local", ""):
        return MockBackend(read_only=settings.read_only, actor=settings.actor)
    if settings.backend in ("servicenow", "snow", "pdi"):
        from .backends.servicenow import ServiceNowBackend

        return ServiceNowBackend(
            settings.instance_url,
            settings.username,
            settings.password,
            read_only=settings.read_only,
        )
    raise ValueError(f"Unknown SNOW_BACKEND={settings.backend!r}; expected 'mock' or 'servicenow'")
