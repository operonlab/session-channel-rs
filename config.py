"""YAML config loader for Session Channel."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sdk_client.station_bootstrap import load_yaml_config

_CONFIG_PATH = Path(__file__).parent / "config.yaml"


def _env_bool(name: str, default: bool) -> bool:
    """Parse env flag — accepts 0/1/true/false/yes/no (case-insensitive)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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
    # Feature flag: BOARD_V2=1 → 使用 Streams 原生 consumer group 路徑（v2，預設）
    # BOARD_V2=0 → 切回 v1 自寫 Lua CAS + claims hash 路徑（rollback safety net）
    # 用途：W5-C runbook Tier 1 緊急 rollback；不需要 git revert 即可切舊邏輯
    board_v2: bool = True


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
    # Env override（feature flag 優先吃 env，方便 launchctl env 切換）
    cfg.board_v2 = _env_bool(
        "BOARD_V2",
        raw.get("board_v2", True) if isinstance(raw.get("board_v2", True), bool) else True,
    )
    return cfg


config = load_config()
