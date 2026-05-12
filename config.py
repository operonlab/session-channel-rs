"""YAML config loader for Session Channel."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from sdk_client.station_bootstrap import load_yaml_config

_CONFIG_PATH = Path(__file__).parent / "config.yaml"


@dataclass
class Config:
    port: int = 10101
    host: str = "127.0.0.1"
    redis_url: str = "redis://127.0.0.1:6379/0"
    secret_key: str = "change-me-in-production"
    session_cookie_name: str = "workshop_session"
    session_max_age: int = 604800
    stream_prefix: str = "ws:channel:"
    topics_key: str = "ws:channel:__topics"
    ttl_seconds: int = 1800
    trim_interval: int = 60
    max_stream_len: int = 500
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://localhost:10101",
        ]
    )


def load_config(path: Path | None = None) -> Config:
    raw = load_yaml_config(path or _CONFIG_PATH)
    cfg = Config()
    for key in (
        "port",
        "host",
        "redis_url",
        "secret_key",
        "session_cookie_name",
        "session_max_age",
        "stream_prefix",
        "topics_key",
        "ttl_seconds",
        "trim_interval",
        "max_stream_len",
    ):
        if key in raw:
            val = raw[key]
            expected = type(getattr(cfg, key))
            setattr(cfg, key, expected(val))
    if "allowed_origins" in raw and isinstance(raw["allowed_origins"], list):
        cfg.allowed_origins = [str(o) for o in raw["allowed_origins"]]
    # Env override for port — convenient for parallel worktrees / validation
    port_env = os.environ.get("SESSION_CHANNEL_PORT")
    if port_env:
        try:
            cfg.port = int(port_env)
        except ValueError:
            pass
    # Env override for CORS origins (comma-separated)
    origins_env = os.environ.get("SESSION_CHANNEL_ALLOWED_ORIGINS")
    if origins_env:
        cfg.allowed_origins = [o.strip() for o in origins_env.split(",") if o.strip()]
    return cfg


config = load_config()
