"""Session Channel — Task & Pane Schemas with Lease class config."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class TaskClass(str, Enum):
    SHORT = "short"  # CRUD / 一般腳本
    LLM = "llm"  # LLM 推理 / 長 RAG
    VIDEO = "video"  # 影片 / 長批次


# (lease_seconds, heartbeat_seconds)
LEASE_CONFIG: dict[TaskClass, dict[str, int]] = {
    TaskClass.SHORT: {"lease_seconds": 30, "heartbeat_seconds": 10},
    TaskClass.LLM: {"lease_seconds": 300, "heartbeat_seconds": 90},
    TaskClass.VIDEO: {"lease_seconds": 1800, "heartbeat_seconds": 600},
}


def lease_ms_for_class(task_class: TaskClass) -> int:
    return LEASE_CONFIG[task_class]["lease_seconds"] * 1000


def heartbeat_seconds_for_class(task_class: TaskClass) -> int:
    return LEASE_CONFIG[task_class]["heartbeat_seconds"]


class TaskPublish(BaseModel):
    """publish 時送的 task 結構。"""

    id: str
    desc: str
    task_class: TaskClass = TaskClass.SHORT
    required_caps: list[str] = Field(default_factory=list)  # W3-B
    assigned_to: str | None = None  # W4-A
    depends_on: list[str] = Field(default_factory=list)  # W4-B
    priority: Literal["normal", "high"] = "normal"


class TaskResult(BaseModel):
    """complete 時送的 result payload。"""

    status: Literal["ok", "error"] = "ok"
    payload: dict = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    tokens_used: int | None = None
    duration_ms: int | None = None
    error_message: str | None = None


class TaskProgress(BaseModel):
    """progress event payload。"""

    task_id: str
    percent: int = Field(ge=0, le=100)
    stage: str = ""
    note: str = ""


class PaneAdvertise(BaseModel):
    """capability registry advertise payload。"""

    pane_id: str
    cli_type: Literal["claude-code", "codex", "gemini", "unknown"] = "unknown"
    mcps: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    started_at: int  # unix ts
    last_seen: int  # unix ts


class TaskProjection(BaseModel):
    """board projection 中單個 task 的形狀。"""

    id: str
    desc: str
    task_class: TaskClass = TaskClass.SHORT
    status: Literal["open", "claimed", "done", "failed", "blocked"] = "open"
    claimed_by: str | None = None
    done_by: str | None = None
    progress: TaskProgress | None = None
    result: TaskResult | None = None
    delivery_count: int = 0
    lease_until: int | None = None  # unix ts
    required_caps: list[str] = Field(default_factory=list)
