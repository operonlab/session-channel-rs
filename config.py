"""YAML config loader for Session Channel."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from workshop.station_bootstrap import load_yaml_config

_CONFIG_PATH = Path(__file__).parent / "config.yaml"


@dataclass
class Config:
    port: int = 4106
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


def load_config(path: Path | None = None) -> Config:
    raw = load_yaml_config(path or _CONFIG_PATH)
    cfg = Config()
    for key in (
        "port", "host", "redis_url", "secret_key",
        "session_cookie_name", "session_max_age",
        "stream_prefix", "topics_key",
        "ttl_seconds", "trim_interval", "max_stream_len",
    ):
        if key in raw:
            val = raw[key]
            expected = type(getattr(cfg, key))
            setattr(cfg, key, expected(val))
    return cfg


config = load_config()
