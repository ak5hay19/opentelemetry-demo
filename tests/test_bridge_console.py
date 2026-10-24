from bridge import console
from actions import killswitch


def _scripted(*answers):
    it = iter(answers)
    return lambda prompt="": next(it)


def test_quit(capsys):
    assert console.menu(ask=_scripted("q")) == 0


def test_execute_needs_typed_confirmation(monkeypatch):
    calls = []
    monkeypatch.setattr(console, "_run", lambda *a: calls.append(a))
    monkeypatch.setattr(killswitch, "is_engaged", lambda d=None: False)
    console.menu(ask=_scripted("4", "yes", "q"))
    assert calls == []


def test_execute_confirmed_passes_flag(monkeypatch):
    calls = []
    monkeypatch.setattr(console, "_run", lambda *a: calls.append(a))
    monkeypatch.setattr(killswitch, "is_engaged", lambda d=None: False)
    console.menu(ask=_scripted("4", "EXECUTE", "checkout is slow", "q"))
    assert calls == [("bridge.run", "--execute", "--symptom", "checkout is slow")]


def test_execute_refused_when_kill_switch_engaged(monkeypatch):
    calls = []
    monkeypatch.setattr(console, "_run", lambda *a: calls.append(a))
    monkeypatch.setattr(killswitch, "is_engaged", lambda d=None: True)
    console.menu(ask=_scripted("4", "q"))
    assert calls == []


def test_dry_run_has_no_execute_flag(monkeypatch):
    calls = []
    monkeypatch.setattr(console, "_run", lambda *a: calls.append(a))
    monkeypatch.setattr(killswitch, "is_engaged", lambda d=None: False)
    console.menu(ask=_scripted("3", "", "q"))
    assert calls == [("bridge.run",)]
