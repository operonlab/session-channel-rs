"""Adversarial tests for Wave 2+3: tmux-relay CLI-agnostic + board_worker + maestro cli-rosetta.

Six Iron Laws:
1. Mutation thinking — every assert catches a single-char mutation
2. Write/test separation — independent agent, no implementation copying
3. Invariant-first — properties over fixed I/O
4. Boundary / error paths — runtime → regression
5. Mock only external I/O — tmux primitives, HTTP, Redis
6. Draft is not production — reviewed with mutation lens
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Path bootstrap ──────────────────────────────────────────────────
_SDK_DIR = str(Path(__file__).parent.parent)
_TMUX_DIR = str(Path(__file__).resolve().parent.parent.parent / "tmux-lib")
_CLI_DIC_DIR = str(Path(__file__).resolve().parent.parent.parent / "cli-rosetta")
for p in [_SDK_DIR, _TMUX_DIR, _CLI_DIC_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest

# ══════════════════════════════════════════════════════════════════════
# Section 1 — SessionChannelClient.board_next_unclaimed
# ══════════════════════════════════════════════════════════════════════


class TestBoardNextUnclaimed:
    """Tests for SessionChannelClient.board_next_unclaimed."""

    def _make_client(self):
        from sdk_client.session_channel import SessionChannelClient

        c = SessionChannelClient.__new__(SessionChannelClient)
        c._client = MagicMock()
        c.base_url = "http://localhost:10101"
        return c

    def test_returns_first_open_task(self):
        c = self._make_client()
        c.board_show = MagicMock(
            return_value={
                "tasks": [
                    {"id": "t1", "status": "done"},
                    {"id": "t2", "status": "open", "desc": "Fix bug"},
                    {"id": "t3", "status": "open", "desc": "Add test"},
                ]
            }
        )
        task = c.board_next_unclaimed("board-1")
        assert task is not None
        assert task["id"] == "t2", "Should return FIRST open, not t3"
        assert task["status"] == "open"

    def test_all_done_returns_none(self):
        c = self._make_client()
        c.board_show = MagicMock(
            return_value={
                "tasks": [
                    {"id": "t1", "status": "done"},
                    {"id": "t2", "status": "done"},
                ]
            }
        )
        assert c.board_next_unclaimed("board-1") is None

    def test_empty_board_returns_none(self):
        c = self._make_client()
        c.board_show = MagicMock(return_value={"tasks": []})
        assert c.board_next_unclaimed("board-1") is None

    def test_board_show_failure_returns_none(self):
        """board_show raises → should return None, not propagate."""
        c = self._make_client()
        c.board_show = MagicMock(side_effect=Exception("connection refused"))
        result = c.board_next_unclaimed("board-1")
        assert result is None

    def test_missing_tasks_key_returns_none(self):
        c = self._make_client()
        c.board_show = MagicMock(return_value={"board_id": "x"})
        assert c.board_next_unclaimed("board-1") is None


# ══════════════════════════════════════════════════════════════════════
# Section 2 — TmuxRelayClient profile-driven methods
# ══════════════════════════════════════════════════════════════════════


class TestPaneStatusLiveProfile:
    """_pane_status_live with profile parameter."""

    def _make_client(self):
        from sdk_client.tmux_relay import TmuxRelayClient

        c = TmuxRelayClient.__new__(TmuxRelayClient)
        c._cache = MagicMock()
        c.STALE_PENDING_THRESHOLD = 1800
        return c

    @patch("sdk_client.tmux_relay.display")
    @patch("sdk_client.tmux_relay.capture")
    def test_default_profile_uses_claude_prompt(self, mock_capture, mock_display):
        """No profile → defaults to CLAUDE_CODE (prompt ❯)."""
        mock_display.return_value = "%123"
        mock_capture.return_value = "some output\n❯"
        c = self._make_client()
        status = c._pane_status_live("pane-1", "123")
        assert status == "idle", f"Expected idle with ❯, got {status}"

    @patch("sdk_client.tmux_relay.display")
    @patch("sdk_client.tmux_relay.capture")
    def test_custom_profile_prompt(self, mock_capture, mock_display):
        """Custom profile with different prompt pattern."""
        from tmux_lib.patterns import CLIProfile

        custom = CLIProfile(
            name="custom",
            prompt_pattern=re.compile(r"READY>"),
            process_names=frozenset({"custom-cli"}),
        )
        mock_display.return_value = "%123"
        mock_capture.return_value = "output\nREADY>"
        c = self._make_client()
        status = c._pane_status_live("pane-1", "123", profile=custom)
        assert status == "idle", f"Custom prompt READY> should be idle, got {status}"

    @patch("sdk_client.tmux_relay.display")
    @patch("sdk_client.tmux_relay.capture")
    def test_custom_profile_indicator_busy(self, mock_capture, mock_display):
        """Custom profile processing indicators → busy:active."""
        from tmux_lib.patterns import CLIProfile

        custom = CLIProfile(
            name="custom",
            prompt_pattern=re.compile(r"READY>"),
            process_names=frozenset({"custom-cli"}),
            processing_indicators=re.compile(r"WORKING\.\.\."),
        )
        mock_display.return_value = "%123"
        mock_capture.return_value = "WORKING..."
        c = self._make_client()
        status = c._pane_status_live("pane-1", "123", profile=custom)
        assert status == "busy:active", f"WORKING... should be busy:active, got {status}"

    @patch("sdk_client.tmux_relay.display")
    @patch("sdk_client.tmux_relay.capture")
    def test_no_indicators_profile_skips_indicator_check(self, mock_capture, mock_display):
        """Profile with no processing_indicators → skip busy:active, go to busy:unknown."""
        from tmux_lib.patterns import CLIProfile

        bare = CLIProfile(
            name="bare",
            prompt_pattern=re.compile(r">>>"),
            process_names=frozenset({"bare"}),
            processing_indicators=None,
        )
        mock_display.return_value = "%123"
        mock_capture.return_value = "some random output"
        c = self._make_client()
        status = c._pane_status_live("pane-1", "123", profile=bare)
        assert status == "busy:unknown", f"No prompt, no indicators → busy:unknown, got {status}"


# ══════════════════════════════════════════════════════════════════════
# Section 3 — detect_cli_in_pane
# ══════════════════════════════════════════════════════════════════════


class TestDetectCliInPane:
    def _make_client(self):
        from sdk_client.tmux_relay import TmuxRelayClient

        c = TmuxRelayClient.__new__(TmuxRelayClient)
        c._cache = MagicMock()
        return c

    @patch("sdk_client.tmux_relay.display", return_value="claude")
    def test_detect_claude(self, mock_display):
        c = self._make_client()
        profile = c.detect_cli_in_pane("pane-1")
        assert profile.name == "claude-code", f"Expected claude-code, got {profile.name}"

    @patch("sdk_client.tmux_relay.display", return_value="gemini")
    def test_detect_gemini(self, mock_display):
        c = self._make_client()
        profile = c.detect_cli_in_pane("pane-1")
        assert profile.name == "gemini-cli", f"Expected gemini-cli, got {profile.name}"

    @patch("sdk_client.tmux_relay.display", return_value="codex")
    def test_detect_codex(self, mock_display):
        c = self._make_client()
        profile = c.detect_cli_in_pane("pane-1")
        assert profile.name == "codex-cli", f"Expected codex-cli, got {profile.name}"

    @patch("sdk_client.tmux_relay.display", return_value="qwen")
    def test_detect_qwen(self, mock_display):
        c = self._make_client()
        profile = c.detect_cli_in_pane("pane-1")
        assert profile.name == "qwen-code", f"Expected qwen-code, got {profile.name}"

    @patch("sdk_client.tmux_relay.display", return_value="copilot")
    def test_detect_copilot(self, mock_display):
        c = self._make_client()
        profile = c.detect_cli_in_pane("pane-1")
        assert profile.name == "copilot-cli", f"Expected copilot-cli, got {profile.name}"

    @patch("sdk_client.tmux_relay.display", return_value="unknown_process")
    def test_detect_unknown_falls_back_to_claude(self, mock_display):
        c = self._make_client()
        profile = c.detect_cli_in_pane("pane-1")
        assert profile.name == "claude-code", "Unknown process should fall back to claude-code"

    @patch("sdk_client.tmux_relay.display", return_value=None)
    def test_detect_none_falls_back_to_claude(self, mock_display):
        c = self._make_client()
        profile = c.detect_cli_in_pane("pane-1")
        assert profile.name == "claude-code", "None command should fall back to claude-code"


# ══════════════════════════════════════════════════════════════════════
# Section 4 — board_worker loop
# ══════════════════════════════════════════════════════════════════════


class TestBoardWorker:
    def _make_client(self):
        from sdk_client.tmux_relay import TmuxRelayClient

        c = TmuxRelayClient.__new__(TmuxRelayClient)
        c._cache = MagicMock()
        c._CHANNEL_URL = "http://localhost:10101"
        c._CHANNEL_KEY = "test"
        return c

    @patch("sdk_client.tmux_relay.send_enter")
    @patch("sdk_client.tmux_relay.send_text")
    @patch("sdk_client.tmux_relay.capture", return_value="output\n❯")
    @patch("sdk_client.tmux_relay.display", return_value="%42")
    def test_empty_board_returns_empty_list(
        self, mock_display, mock_capture, mock_send, mock_enter
    ):
        from tmux_lib.patterns import CLAUDE_CODE

        c = self._make_client()
        c.detect_cli_in_pane = MagicMock(return_value=CLAUDE_CODE)
        c.board_next_unclaimed = MagicMock(return_value=None)
        c._notify_channel = MagicMock()

        results = c.board_worker("board-empty", "pane-1", profile=CLAUDE_CODE)
        assert results == [], f"Empty board should return [], got {results}"

    @patch("sdk_client.tmux_relay.send_enter")
    @patch("sdk_client.tmux_relay.send_text")
    @patch("sdk_client.tmux_relay.capture", return_value="done\n❯")
    @patch("sdk_client.tmux_relay.display", return_value="%42")
    def test_single_task_completes(self, mock_display, mock_capture, mock_send, mock_enter):
        from tmux_lib.patterns import CLAUDE_CODE

        c = self._make_client()
        c.detect_cli_in_pane = MagicMock(return_value=CLAUDE_CODE)

        # First call: return task. Second call: no more tasks.
        c.board_next_unclaimed = MagicMock(
            side_effect=[
                {"id": "t1", "status": "open", "desc": "Fix the bug"},
                None,
            ]
        )
        c.claim_board_task = MagicMock(return_value={"ok": True})
        c.complete_board_task = MagicMock()
        c._notify_channel = MagicMock()
        c._wait_for_idle = MagicMock(return_value=True)

        results = c.board_worker("board-1", "pane-1", profile=CLAUDE_CODE, timeout_per_task=10)

        assert len(results) == 1
        assert results[0]["task_id"] == "t1"
        assert results[0]["status"] == "done"
        c.claim_board_task.assert_called_once_with("board-1", "t1")
        c.complete_board_task.assert_called_once()

    @patch("sdk_client.tmux_relay.send_enter")
    @patch("sdk_client.tmux_relay.send_text")
    @patch("sdk_client.tmux_relay.capture", return_value="output\n❯")
    @patch("sdk_client.tmux_relay.display", return_value="%42")
    def test_claim_failure_skips_task(self, mock_display, mock_capture, mock_send, mock_enter):
        """If claim fails (someone else got it), skip and try next."""
        from tmux_lib.patterns import CLAUDE_CODE

        c = self._make_client()
        c.detect_cli_in_pane = MagicMock(return_value=CLAUDE_CODE)
        c.board_next_unclaimed = MagicMock(
            side_effect=[
                {"id": "t1", "status": "open", "desc": "Task 1"},
                {"id": "t2", "status": "open", "desc": "Task 2"},
                None,
            ]
        )
        # First claim fails, second succeeds
        c.claim_board_task = MagicMock(side_effect=[None, {"ok": True}])
        c.complete_board_task = MagicMock()
        c._notify_channel = MagicMock()
        c._wait_for_idle = MagicMock(return_value=True)

        results = c.board_worker("board-1", "pane-1", profile=CLAUDE_CODE, timeout_per_task=10)

        assert len(results) == 1
        assert results[0]["task_id"] == "t2", "Should skip t1 (claim failed) and do t2"

    @patch("sdk_client.tmux_relay.send_enter")
    @patch("sdk_client.tmux_relay.send_text")
    @patch("sdk_client.tmux_relay.capture", return_value="timeout\n")
    @patch("sdk_client.tmux_relay.display", return_value="%42")
    def test_timeout_marks_as_timeout(self, mock_display, mock_capture, mock_send, mock_enter):
        from tmux_lib.patterns import CLAUDE_CODE

        c = self._make_client()
        c.detect_cli_in_pane = MagicMock(return_value=CLAUDE_CODE)
        c.board_next_unclaimed = MagicMock(
            side_effect=[
                {"id": "t1", "status": "open", "desc": "Slow task"},
                None,
            ]
        )
        c.claim_board_task = MagicMock(return_value={"ok": True})
        c.complete_board_task = MagicMock()
        c._notify_channel = MagicMock()
        c._wait_for_idle = MagicMock(return_value=False)  # timeout!

        results = c.board_worker("board-1", "pane-1", profile=CLAUDE_CODE, timeout_per_task=10)

        assert len(results) == 1
        assert results[0]["status"] == "timeout", f"Should be timeout, got {results[0]['status']}"


# ══════════════════════════════════════════════════════════════════════
# Section 5 — _board_http GET fix
# ══════════════════════════════════════════════════════════════════════


class TestBoardHttpGetFix:
    def _make_client(self):
        from sdk_client.tmux_relay import TmuxRelayClient

        c = TmuxRelayClient.__new__(TmuxRelayClient)
        c._CHANNEL_URL = "http://localhost:10101"
        c._CHANNEL_KEY = "test"
        return c

    @patch("urllib.request.urlopen")
    def test_get_sends_no_body(self, mock_urlopen):
        """GET requests must not send a body (data=None)."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"tasks": []}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        c = self._make_client()
        c._board_http("GET", "/api/board/test", {})

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.data is None, f"GET should have data=None, got {req.data}"

    @patch("urllib.request.urlopen")
    def test_post_sends_body(self, mock_urlopen):
        """POST requests must send a body."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        c = self._make_client()
        c._board_http("POST", "/api/board/test/claim", {"task_id": "t1"})

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.data is not None, "POST should have body"
        body = json.loads(req.data)
        assert body["task_id"] == "t1"


# ══════════════════════════════════════════════════════════════════════
# Section 6 — maestro build_cli_cmd
# ══════════════════════════════════════════════════════════════════════


class TestMaestroBuildCliCmd:
    @pytest.fixture(autouse=True)
    def _setup_path(self):
        maestro_dir = str(Path.home() / ".claude" / "skills" / "maestro" / "scripts")
        if maestro_dir not in sys.path:
            sys.path.insert(0, maestro_dir)

    def test_claude_has_output_format_json(self):
        import maestro

        cmd = maestro.build_cli_cmd("claude", "test", None, False)
        cmd_str = " ".join(cmd)
        assert "json" in cmd_str, "Claude should have json output format"

    def test_codex_has_full_auto(self):
        import maestro

        cmd = maestro.build_cli_cmd("codex", "test", None, False)
        cmd_str = " ".join(cmd)
        assert "--full-auto" in cmd_str, f"Codex should have --full-auto, got: {cmd_str}"

    def test_gemini_has_approval_mode_yolo(self):
        import maestro

        cmd = maestro.build_cli_cmd("gemini", "test", None, False)
        cmd_str = " ".join(cmd)
        assert "yolo" in cmd_str, f"Gemini should have yolo, got: {cmd_str}"

    def test_codex_does_not_have_yolo(self):
        """Mutation killer: codex flag != gemini flag."""
        import maestro

        cmd = maestro.build_cli_cmd("codex", "test", None, False)
        cmd_str = " ".join(cmd)
        assert "yolo" not in cmd_str, "Codex must NOT have yolo"

    def test_gemini_does_not_have_full_auto(self):
        """Mutation killer: gemini flag != codex flag."""
        import maestro

        cmd = maestro.build_cli_cmd("gemini", "test", None, False)
        cmd_str = " ".join(cmd)
        assert "--full-auto" not in cmd_str, "Gemini must NOT have --full-auto"

    def test_unknown_cli_raises_valueerror(self):
        import maestro

        with pytest.raises(ValueError):
            maestro.build_cli_cmd("nonexistent", "test", None, False)

    def test_prompt_in_cmd(self):
        """Prompt must appear in the command for all CLIs."""
        import maestro

        for cli in ["claude", "codex", "gemini"]:
            cmd = maestro.build_cli_cmd(cli, "fix the auth bug", None, False)
            assert "fix the auth bug" in " ".join(cmd), f"{cli}: prompt missing"

    def test_cwd_injected_for_claude(self):
        import maestro

        cmd = maestro.build_cli_cmd("claude", "test", "/tmp/proj", False)
        cmd_str = " ".join(cmd)
        assert "/tmp/proj" in cmd_str, "Claude cwd should be injected"

    def test_background_flag(self):
        import maestro

        cmd = maestro.build_cli_cmd("claude", "test", None, True)
        assert "--background" in cmd, "background=True should add --background"

    def test_no_background_flag(self):
        import maestro

        cmd = maestro.build_cli_cmd("claude", "test", None, False)
        assert "--background" not in cmd, "background=False should not add --background"

    def test_headless_has_five_clis(self):
        import maestro

        assert len(maestro.HEADLESS) == 5, (
            f"HEADLESS should have 5 CLIs, got {len(maestro.HEADLESS)}"
        )
        for cli in ["claude", "codex", "gemini", "qwen", "copilot"]:
            assert cli in maestro.HEADLESS, f"{cli} missing from HEADLESS"

    def test_routing_values_all_in_headless(self):
        """All CLI references in routing must exist in HEADLESS."""
        import maestro

        for category, routes in maestro.CLI_ROUTING.items():
            for tier, cli in routes.items():
                assert cli in maestro.HEADLESS, (
                    f"CLI_ROUTING['{category}']['{tier}'] = '{cli}' not in HEADLESS"
                )
