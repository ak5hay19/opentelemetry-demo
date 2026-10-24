"""Offline tests for actions/: fake health, fake command runner, fake clock. No Docker, no LLM."""
import inspect
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from actions import approval, audit, incident_report, kill, killswitch, registry, runner, verify
from oncall import schema as S
from tests.test_isolation import REPO, imported_roots
from tools.result import ToolResult


def row(name, status="exited", health="none", service=None):
    return {"name": name, "compose_service": service or name, "status": status, "health": health,
            "restart_count": 0, "exit_code": 0, "oom_killed": False}


def fake_health(*rows):
    def health(include_stats=True):
        return ToolResult("container_health", "data", ["q"], {"at": "now"}, items=list(rows), total_items=len(rows))
    return health


def broken_health(include_stats=True):
    return ToolResult("container_health", "backend_error", ["q"], {}, error="docker down")


def report(component="valkey-cart", status="diagnosed"):
    return {S.INCIDENT_ID: "inc-1", S.STARTED_AT: "2026-10-24T05:00:00+00:00", S.STATUS: status, S.COMPONENT: component,
            S.CAUSE: "cache stopped", S.CONFIDENCE: 0.8, S.STATED_CONFIDENCE: 0.8, S.ESCALATION_REASON: None,
            S.EVIDENCE: [{S.EV_LEDGER_ID: "C1", S.EV_KIND: "container", S.EV_CLAIM: "stopped", S.EV_VALIDATED: True}]}


HEALTH = fake_health(row("valkey-cart"), row("cart", "running", "healthy"), row("prometheus", "running"))


@pytest.fixture
def proposal():
    return registry.propose(report(), HEALTH)


class FakeRun:
    def __init__(self, returncode=0, raises=None):
        self.calls, self.returncode, self.raises = [], returncode, raises

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.raises:
            raise self.raises
        return SimpleNamespace(returncode=self.returncode, stderr="boom", stdout="")


# --- registry ---------------------------------------------------------------------------------

def test_propose_builds_the_exact_command(proposal):
    assert proposal["params"] == {"container": "valkey-cart"}
    assert proposal["preview"] == "docker start valkey-cart"
    assert proposal["risk"] == "low" and proposal["action_id"] == "restart_container"


def test_running_container_gets_restart():
    health = fake_health(row("cart", "running", "healthy"))
    assert registry.propose(report("cart"), health)["preview"] == "docker restart cart"


@pytest.mark.parametrize("name", sorted(registry.DENY) + ["flagd", "flagd-ui"])
def test_every_deny_list_name_is_rejected(name):
    health = fake_health(row(name, "running"))
    assert registry.propose(report(name), health) is None
    assert "not an allowed" in registry.explain_no_proposal(report(name), health)
    fake = {"incident_id": "i", "action_id": "restart_container", "params": {"container": name}, "preview": f"docker restart {name}"}
    assert registry.validate(fake, health)[0] is None


def test_no_proposal_when_escalated_or_not_allowed_or_unknown():
    assert registry.propose(report(status="escalated"), HEALTH) is None
    assert "escalated" in registry.explain_no_proposal(report(status="escalated"), HEALTH)
    assert registry.propose(report("no-such-thing"), HEALTH) is None
    assert registry.propose(report("../etc"), HEALTH) is None
    assert registry.propose(report(), broken_health) is None
    assert "unavailable" in registry.explain_no_proposal(report(), broken_health)


def test_toggle_flag_is_disabled():
    assert registry.ACTIONS["toggle_flag"]["enabled"] is False
    fake = {"incident_id": "i", "action_id": "toggle_flag", "params": {}, "preview": ""}
    assert registry.validate(fake, HEALTH)[0] is None


# --- runner -----------------------------------------------------------------------------------

def test_execute_defaults_to_dry_run_and_changes_nothing(proposal, tmp_path):
    fake = FakeRun()
    result = runner.execute(proposal, runner=fake, health=HEALTH, actions_dir=tmp_path)
    assert result["mode"] == "dry_run" and result["ok"] and fake.calls == []
    assert "docker start valkey-cart" in result["detail"]


def test_real_execute_uses_argument_list_no_shell_and_timeout(proposal, tmp_path):
    fake = FakeRun()
    result = runner.execute(proposal, dry_run=False, runner=fake, health=HEALTH, actions_dir=tmp_path)
    assert result["mode"] == "executed" and result["ok"] and result["error"] is None
    command, kwargs = fake.calls[0]
    assert command == ["docker", "start", "valkey-cart"]
    assert "shell" not in kwargs and kwargs["timeout"] == runner.COMMAND_TIMEOUT_S


@pytest.mark.parametrize("fake", [FakeRun(returncode=1), FakeRun(raises=subprocess.TimeoutExpired("docker", 60)),
                                  FakeRun(raises=FileNotFoundError("docker"))])
def test_command_failures_are_reported(proposal, tmp_path, fake):
    result = runner.execute(proposal, dry_run=False, runner=fake, health=HEALTH, actions_dir=tmp_path)
    assert result["ok"] is False and result["mode"] == "executed" and result["error"]


def test_kill_switch_blocks_even_a_dry_run(proposal, tmp_path):
    killswitch.engage(tmp_path)
    fake = FakeRun()
    for dry in (True, False):
        result = runner.execute(proposal, dry_run=dry, runner=fake, health=HEALTH, actions_dir=tmp_path)
        assert result["mode"] == "blocked" and runner.KILL_TEXT in result["error"]
    assert fake.calls == []


def test_kill_switch_engaged_between_check_and_command_blocks(proposal, tmp_path, monkeypatch):
    calls = {"n": 0}

    def flaky(directory=None):
        calls["n"] += 1
        return calls["n"] >= 2  # off at the first check, on at the second (just before the command)

    monkeypatch.setattr(killswitch, "is_engaged", flaky)
    fake = FakeRun()
    result = runner.execute(proposal, dry_run=False, runner=fake, health=HEALTH, actions_dir=tmp_path)
    assert result["mode"] == "blocked" and fake.calls == [] and calls["n"] == 2


def test_tampered_proposals_are_rejected(proposal, tmp_path):
    fake = FakeRun()
    for change in ({"params": {"container": "prometheus"}}, {"params": {"container": "cart", "extra": "x"}},
                   {"action_id": "toggle_flag"}, {"preview": "docker start valkey-cart; rm -rf /"},
                   {"params": {"container": "valkey-cart; id"}}, {"incident_id": ""}):
        result = runner.execute({**proposal, **change}, dry_run=False, runner=fake, health=HEALTH, actions_dir=tmp_path)
        assert result["mode"] == "blocked", change
    assert runner.execute("not a dict", dry_run=False, runner=fake, health=HEALTH, actions_dir=tmp_path)["mode"] == "blocked"
    assert fake.calls == []


def test_second_action_for_one_incident_is_blocked(proposal, tmp_path):
    fake = FakeRun()
    first = runner.execute(proposal, dry_run=False, runner=fake, health=HEALTH, actions_dir=tmp_path)
    second = runner.execute(proposal, dry_run=False, runner=fake, health=HEALTH, actions_dir=tmp_path)
    assert first["mode"] == "executed" and second["mode"] == "blocked" and len(fake.calls) == 1
    assert "second" in second["error"]


def test_dry_run_does_not_use_up_the_incident_action(proposal, tmp_path):
    fake = FakeRun()
    runner.execute(proposal, runner=fake, health=HEALTH, actions_dir=tmp_path)
    assert runner.execute(proposal, dry_run=False, runner=fake, health=HEALTH, actions_dir=tmp_path)["mode"] == "executed"


# --- audit and kill switch cli ----------------------------------------------------------------

def test_audit_lines_are_valid_json_and_append_only(proposal, tmp_path):
    runner.execute(proposal, runner=FakeRun(), health=HEALTH, actions_dir=tmp_path)
    path = tmp_path / "audit.jsonl"
    first = path.read_text().splitlines()
    runner.execute(proposal, dry_run=False, runner=FakeRun(), health=HEALTH, actions_dir=tmp_path)
    lines = path.read_text().splitlines()
    assert lines[:len(first)] == first and len(lines) > len(first)
    for line in lines:
        entry = json.loads(line)
        assert set(entry) == {"ts", "incident_id", "event", "action_id", "params", "mode", "outcome"}
        assert entry["ts"].endswith("+00:00") and entry["incident_id"] == "inc-1"
    assert [json.loads(l)["event"] for l in lines] == ["dry_run", "execute_started", "execute_finished"]


def test_kill_cli(tmp_path, capsys):
    assert kill.main(["status"], tmp_path) == 0 and "off" in capsys.readouterr().out
    kill.main(["on"], tmp_path)
    assert killswitch.is_engaged(tmp_path) and "ENGAGED" in capsys.readouterr().out
    kill.main(["off"], tmp_path)
    assert not killswitch.is_engaged(tmp_path)
    assert [e["event"] for e in audit.read(tmp_path)] == ["kill_switch_engaged", "kill_switch_cleared"]


# --- approval ---------------------------------------------------------------------------------

def answer(text, tmp_path, proposal):
    def input_fn(prompt):
        assert prompt == "approve? [y/N] "
        if isinstance(text, Exception):
            raise text
        return text
    return approval.ask(report(), proposal, input_fn=input_fn, actions_dir=tmp_path)


@pytest.mark.parametrize("text,decision", [("y", "approved"), ("YES", "approved"), (" Yes ", "approved"), ("n", "denied"),
                                           ("", "denied"), ("yep", "denied"), ("ok", "denied"), (EOFError(), "denied"),
                                           (KeyboardInterrupt(), "denied")])
def test_approval_variants(text, decision, tmp_path, proposal):
    result = answer(text, tmp_path, proposal)
    assert result["decision"] == decision and set(result) == {"decision", "at", "by"}
    assert audit.read(tmp_path)[-1]["event"] == "approval"


def test_approval_killed_without_asking(tmp_path, proposal):
    killswitch.engage(tmp_path)

    def never(prompt):
        raise AssertionError("must not ask")

    assert approval.ask(report(), proposal, input_fn=never, actions_dir=tmp_path)["decision"] == "killed"


def test_non_interactive_stdin_is_denied(tmp_path, proposal, monkeypatch):
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    assert approval.ask(report(), proposal, actions_dir=tmp_path)["decision"] == "denied"


def test_approval_screen_shows_the_facts(tmp_path, proposal, capsys):
    answer("n", tmp_path, proposal)
    out = capsys.readouterr().out
    for text in ("valkey-cart", "cache stopped", "0.8", "validated evidence: 1 of 1", "docker start valkey-cart",
                 "Nothing changes unless you answer yes"):
        assert text in out


# --- verify -----------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def metrics_data(*pcts):
    items = [{"service": f"svc{i}", "error_rate_pct": p} for i, p in enumerate(pcts)]
    return lambda **kw: ToolResult("query_metrics", "data", ["q"], {}, items=items, total_items=len(items))


def metrics_state(state):
    def metrics(**kw):
        return ToolResult("query_metrics", state, ["q"], {}, error="prom down" if state == "backend_error" else None)
    return metrics


UP = fake_health(row("valkey-cart", "running", "healthy"))
DOWN = fake_health(row("valkey-cart", "exited"))


def run_check(proposal, health, metrics, timeout_s=30):
    clock = Clock()
    return verify.check_recovery(proposal, timeout_s=timeout_s, poll_s=10, clock=clock, sleep=clock.sleep,
                                 health=health, metrics=metrics)


def test_verify_recovered(proposal):
    out = run_check(proposal, UP, metrics_data(0.0, 1.2))
    assert out["state"] == "recovered" and len(out["checks"]) == 1 and out["escalate"] is False
    assert out["checks"][0]["at"] and "nothing to roll back" in out["note"]


def test_verify_not_recovered_only_when_the_container_never_becomes_healthy(proposal):
    out = run_check(proposal, DOWN, metrics_data(0.0))
    assert out["state"] == "not_recovered" and out["escalate"] and len(out["checks"]) == 4
    assert "exited" in out["detail"]
    unhealthy = fake_health(row("valkey-cart", "running", "unhealthy"))
    assert run_check(proposal, unhealthy, metrics_data(0.0))["state"] == "not_recovered"
    assert run_check(proposal, DOWN, metrics_data(40.0))["state"] == "not_recovered"


def test_verify_elevated_errors_with_a_healthy_container_is_unverified_not_failed(proposal):
    out = run_check(proposal, UP, metrics_data(0.0, 40.0))
    assert out["state"] == "recovered_unverified_metrics" and out["escalate"] is False
    assert "svc1" in out["detail"] and "pre-restart errors" in out["detail"]
    assert out["checks"][-1]["metrics"]["above_threshold"] == ["svc1"]
    assert len(out["checks"]) == 4


def test_verify_errors_that_clear_during_the_wait_become_recovered(proposal):
    results = iter([metrics_data(40.0), metrics_data(40.0), metrics_data(1.0)])
    out = run_check(proposal, UP, lambda **kw: next(results)(**kw))
    assert out["state"] == "recovered" and len(out["checks"]) == 3


def test_verify_ignores_the_load_generator(proposal):
    assert verify.IGNORED_SERVICES == {"load-generator"}
    items = [{"service": "load-generator", "error_rate_pct": 90.0}, {"service": "cart", "error_rate_pct": 0.5}]
    metrics = lambda **kw: ToolResult("query_metrics", "data", ["q"], {}, items=items, total_items=2)
    out = run_check(proposal, UP, metrics)
    assert out["state"] == "recovered" and out["checks"][0]["metrics"]["above_threshold"] == []
    only_loadgen = lambda **kw: ToolResult("query_metrics", "data", ["q"], {}, items=items[:1], total_items=1)
    assert run_check(proposal, UP, only_loadgen)["state"] == "recovered"


def test_verify_default_timeout_is_180_seconds():
    assert inspect.signature(verify.check_recovery).parameters["timeout_s"].default == 180


def test_verify_recovers_after_a_few_polls(proposal):
    states = iter([DOWN, DOWN, UP])
    out = run_check(proposal, lambda include_stats=True: next(states)(include_stats), metrics_data(0.0))
    assert out["state"] == "recovered" and len(out["checks"]) == 3


def test_verify_times_out_when_container_state_unknown(proposal):
    out = run_check(proposal, broken_health, metrics_data(0.0))
    assert out["state"] == "timed_out" and out["escalate"]
    assert run_check(proposal, fake_health(row("other")), metrics_data(0.0))["state"] == "timed_out"


@pytest.mark.parametrize("state", ["no_data", "backend_error"])
def test_no_data_is_never_proof_of_health(proposal, state):
    out = run_check(proposal, UP, metrics_state(state))
    assert out["state"] == "recovered_unverified_metrics" and out["escalate"] is False
    assert out["checks"][-1]["metrics"]["state"] == state
    assert state in out["detail"] and "not confirmed" in out["detail"]


# --- incident report --------------------------------------------------------------------------

APPROVED = {"decision": "approved", "at": "2026-10-24T05:01:00+00:00", "by": "me"}
EXECUTED = {"ok": True, "mode": "executed", "started_at": "2026-10-24T05:01:05+00:00", "ended_at": "x", "detail": "ran", "error": None}


def verification(proposal, state, metrics=None):
    ver = run_check(proposal, UP, metrics or metrics_data(0.0))
    return {**ver, "state": state}


def paths(proposal):
    return {
        "recovered": (proposal, APPROVED, EXECUTED, "recovered"),
        "recovered_unverified_metrics": (proposal, APPROVED, EXECUTED, "recovered_unverified_metrics"),
        "not_recovered": (proposal, APPROVED, EXECUTED, "not_recovered"),
        "timed_out": (proposal, APPROVED, EXECUTED, "timed_out"),
        "denied": (proposal, {**APPROVED, "decision": "denied"}, None, None),
        "escalated": (None, None, None, None),
        "dry_run": (proposal, APPROVED, {**EXECUTED, "mode": "dry_run"}, None),
        "blocked_by_kill_switch": (proposal, {**APPROVED, "decision": "killed"}, None, None),
        "blocked": (proposal, APPROVED, {**EXECUTED, "mode": "blocked", "ok": False, "error": "proposal rejected"}, None),
        "execution_failed": (proposal, APPROVED, {**EXECUTED, "ok": False, "error": "exit 1"}, None),
        "executed_not_verified": (proposal, APPROVED, EXECUTED, None),
    }


@pytest.mark.parametrize("outcome", list(incident_report.OUTCOMES))
def test_incident_report_for_every_outcome(outcome, proposal, tmp_path):
    prop, appr, res, state = paths(proposal)[outcome]
    ver = verification(proposal, state) if state else None
    doc = incident_report.build(report(), prop, appr, res, ver, {"detected": "2026-10-24T04:59:00+00:00"}, tmp_path)
    assert doc["outcome"] == outcome
    saved = json.loads((tmp_path / "incident_report.json").read_text())
    assert saved == json.loads(json.dumps(doc))
    text = (tmp_path / "incident_report.md").read_text()
    assert incident_report.OUTCOMES[outcome] in text and "inc-1" in text
    assert "generic suggestion" in text and "✓ [C1]" in text
    assert set(saved["timeline"]) == {"detected", "investigation_start", "investigation_end", "proposed",
                                      "approved_or_denied", "executed", "verified"}
    assert saved["timeline"]["detected"] == "2026-10-24T04:59:00+00:00"
    assert saved["timeline"]["investigation_end"] == "n/a"


def test_incident_report_for_an_escalated_report_with_nothing_else(tmp_path):
    doc = incident_report.build(report(status="escalated"), None, None, None, None, None, tmp_path)
    assert doc["outcome"] == "escalated" and doc["timeline"]["executed"] == "n/a"
    assert "No action proposed" in (tmp_path / "incident_report.md").read_text()


def test_executed_run_fills_the_timeline(proposal, tmp_path):
    doc = incident_report.build(report(), proposal, APPROVED, EXECUTED, verification(proposal, "recovered"), None, tmp_path)
    assert doc["timeline"]["executed"] == EXECUTED["started_at"] and doc["timeline"]["verified"] != "n/a"
    assert doc["timeline"]["approved_or_denied"].startswith("approved")


# --- layering ---------------------------------------------------------------------------------

def test_agent_side_never_imports_actions():
    found = [str(p) for package in ("oncall", "tools") for p in (REPO / package).rglob("*.py")
             if imported_roots(p) & {"actions", "dev"}]
    assert found == []


def test_actions_never_import_dev_and_name_no_flag_service():
    for path in sorted((REPO / "actions").glob("*.py")):
        assert "dev" not in imported_roots(path), path
        text = path.read_text(encoding="utf-8").lower()
        assert "flagd" not in text and "/feature" not in text, path
