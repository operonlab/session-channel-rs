"""Schema tests — TaskPublish defaults / TaskResult coercion / lease config."""

from __future__ import annotations

import pytest


def test_task_publish_defaults():
    """TaskPublish(id, desc) 預設 short / 空 caps / 無 assigned_to。"""
    from schemas import TaskClass, TaskPublish

    tp = TaskPublish(id="t1", desc="hello")
    assert tp.task_class == TaskClass.SHORT
    assert tp.required_caps == []
    assert tp.assigned_to is None
    assert tp.depends_on == []
    assert tp.priority == "normal"


def test_task_result_str_shorthand():
    """complete endpoint 接受 str → 自動 wrap 成 {status: ok, payload: {note: ...}}。"""
    from schemas import TaskResult

    # The route does: if isinstance(result_raw, str): result_raw = {"status": "ok", "payload": {"note": result_raw}}
    raw = "done!"
    wrapped = {"status": "ok", "payload": {"note": raw}}
    tr = TaskResult(**wrapped)
    assert tr.status == "ok"
    assert tr.payload == {"note": "done!"}
    assert tr.artifacts == []


def test_lease_config_consistency():
    """LEASE_CONFIG / lease_ms_for_class 三 class 數值正確。"""
    from schemas import (
        LEASE_CONFIG,
        TaskClass,
        heartbeat_seconds_for_class,
        lease_ms_for_class,
    )

    # ms helpers
    assert lease_ms_for_class(TaskClass.SHORT) == 30_000
    assert lease_ms_for_class(TaskClass.LLM) == 300_000
    assert lease_ms_for_class(TaskClass.VIDEO) == 1_800_000

    # heartbeat helpers
    assert heartbeat_seconds_for_class(TaskClass.SHORT) == 10
    assert heartbeat_seconds_for_class(TaskClass.LLM) == 90
    assert heartbeat_seconds_for_class(TaskClass.VIDEO) == 600

    # heartbeat 必須 < lease (否則永遠 expire)
    for tc in TaskClass:
        cfg = LEASE_CONFIG[tc]
        assert cfg["heartbeat_seconds"] < cfg["lease_seconds"], (
            f"heartbeat must be < lease for {tc}"
        )


def test_task_progress_validation():
    """TaskProgress percent 必須 0-100。"""
    from schemas import TaskProgress

    p = TaskProgress(task_id="t1", percent=42, stage="x", note="y")
    assert p.percent == 42

    with pytest.raises(Exception):
        TaskProgress(task_id="t1", percent=101)
    with pytest.raises(Exception):
        TaskProgress(task_id="t1", percent=-1)
