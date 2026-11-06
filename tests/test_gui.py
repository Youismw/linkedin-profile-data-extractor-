"""Unit and integration tests for profile_extractor.gui."""

import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import pytest

from profile_extractor.cli import build_parser
from profile_extractor.gui import ExtractorApp, _load_run_state, _save_run_state


def test_cli_gui_subcommand_registration() -> None:
    """Verify that 'gui' is registered as a recognized CLI subcommand."""
    parser = build_parser()
    args = parser.parse_args(["gui"])
    assert args.command == "gui"


def test_gui_run_state_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify loading and saving path history in the GUI module."""
    mock_state_file = tmp_path / ".run_state.json"
    monkeypatch.setattr("profile_extractor.gui.STATE_FILE", mock_state_file)

    # Empty initially
    assert _load_run_state() == {}

    # Save and reload
    _save_run_state({"last_input": "candidates.xlsx", "last_output": "out_dir"})
    loaded = _load_run_state()
    assert loaded["last_input"] == "candidates.xlsx"
    assert loaded["last_output"] == "out_dir"


def test_gui_app_initialization_and_slider_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify ExtractorApp initializes with expected defaults and handles slider callbacks."""
    mock_state_file = tmp_path / ".run_state.json"
    monkeypatch.setattr("profile_extractor.gui.STATE_FILE", mock_state_file)

    root = tk.Tk()
    root.withdraw()  # Hide window during test execution
    try:
        app = ExtractorApp(root)

        # Check default reactive variable values
        assert app.disextra_var.get() is True
        assert app.fast_var.get() is False
        assert app.visible_var.get() is False
        assert app.prune_var.get() is False
        assert app.scroll_steps_var.get() == 25
        assert app.step_time_var.get() == 250
        assert app.about_wait_var.get() == 1000
        assert app.polite_delay_var.get() == 1.5
        assert app.status_var.get() == "Ready"

        # Test slider change callbacks
        app._on_scroll_slider_change("42")
        assert "42 steps" in app.scroll_lbl.cget("text")

        app._on_steptime_slider_change("300")
        assert "300 ms" in app.step_time_lbl.cget("text")

        app._on_aboutwait_slider_change("2500")
        assert "2500 ms" in app.about_wait_lbl.cget("text")

        app._on_polite_slider_change("3.5")
        assert "3.5 s" in app.polite_delay_lbl.cget("text")

        # Test logging helper
        app._log_line("[INFO] Testing log output\n")
        log_content = app.log_text.get("1.0", tk.END)
        assert "Testing log output" in log_content

        # Test log clear
        app._clear_log()
        assert app.log_text.get("1.0", tk.END).strip() == ""

        # Test toggle UI state helper
        app._set_running_state(True)
        assert str(app.start_btn.cget("state")) == str(tk.DISABLED)
        assert str(app.login_btn.cget("state")) == str(tk.DISABLED)
        assert str(app.purge_btn.cget("state")) == str(tk.DISABLED)
        assert str(app.stop_btn.cget("state")) == str(tk.NORMAL)

        app._set_running_state(False)
        assert str(app.start_btn.cget("state")) == str(tk.NORMAL)
        assert str(app.login_btn.cget("state")) == str(tk.NORMAL)
        assert str(app.purge_btn.cget("state")) == str(tk.NORMAL)
        assert str(app.stop_btn.cget("state")) == str(tk.DISABLED)
    finally:
        root.destroy()


def test_gui_purge_presets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that _purge_presets deletes auth and state files and clears entry variables."""
    mock_state_file = tmp_path / ".run_state.json"
    mock_auth_file = tmp_path / "mock_auth_state.json"
    mock_state_file.write_text('{"last_input": "abc", "last_output": "xyz"}', encoding="utf-8")
    mock_auth_file.write_text('{"cookies": []}', encoding="utf-8")

    monkeypatch.setattr("profile_extractor.gui.STATE_FILE", mock_state_file)
    monkeypatch.setattr("profile_extractor.gui.AUTH_STATE_FILE", mock_auth_file)

    def _mock_confirm(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(messagebox, "askyesno", _mock_confirm)

    root = tk.Tk()
    root.withdraw()
    try:
        app = ExtractorApp(root)
        app.input_var.set("some_path.xlsx")
        app.output_var.set("custom_output")

        app._purge_presets()

        assert not mock_state_file.exists()
        assert not mock_auth_file.exists()
        assert app.input_var.get() == ""
        assert app.output_var.get() == "output"
        assert app.status_var.get() == "Presets Purged"
    finally:
        root.destroy()


def test_gui_workers_in_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that specifying workers in GUI appends --workers to the CLI command."""
    mock_state_file = tmp_path / ".run_state.json"
    monkeypatch.setattr("profile_extractor.gui.STATE_FILE", mock_state_file)

    mock_input = tmp_path / "test.xlsx"
    mock_input.write_text("dummy", encoding="utf-8")

    spawned_cmd: list[str] = []

    class DummyProc:
        stdout: object = None

        def poll(self) -> int:
            return 0

    def mock_popen(cmd: object, **_kwargs: object) -> DummyProc:
        nonlocal spawned_cmd
        if isinstance(cmd, (list, tuple)):
            spawned_cmd = [str(x) for x in cmd]
        return DummyProc()

    monkeypatch.setattr("subprocess.Popen", mock_popen)

    root = tk.Tk()
    root.withdraw()
    try:
        app = ExtractorApp(root)
        app.input_var.set(str(mock_input))
        app.output_var.set(str(tmp_path / "out"))
        app.workers_var.set(4)

        app._start_extraction()

        assert "--workers" in spawned_cmd
        w_idx = spawned_cmd.index("--workers")
        assert spawned_cmd[w_idx + 1] == "4"
    finally:
        root.destroy()

