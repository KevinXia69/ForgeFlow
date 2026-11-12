"""Signals request shutdown; service locks and joins stay in normal flow."""
import importlib.util
import signal
from pathlib import Path
from types import SimpleNamespace


def test_signal_handler_only_marks_shutdown_and_stop_runs_once(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "supervisor_shutdown_test", Path(__file__).resolve().parents[1]
        / "scripts/run_supervisor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handlers = {signal.SIGINT: "old-int", signal.SIGTERM: "old-term"}
    calls = []

    def install(number, handler):
        old = handlers[number]
        handlers[number] = handler
        return old

    def sleep(_seconds):
        calls.append("signal")
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        assert calls == ["start", "signal"]  # no locks or joins inside the handler
        calls.append("handler-returned")

    monkeypatch.setattr(module.signal, "signal", install)
    monkeypatch.setattr(module.time, "sleep", sleep)
    supervisor = SimpleNamespace(
        start=lambda: calls.append("start"), stop=lambda: calls.append("stop"),
        _stop=SimpleNamespace(is_set=lambda: False))
    module._serve_until_signal(supervisor)
    assert calls == ["start", "signal", "handler-returned", "stop"]
    assert handlers == {signal.SIGINT: "old-int", signal.SIGTERM: "old-term"}
