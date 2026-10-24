"""Offline tests for actions.run_incident: fake investigation, fake input, fake docker, fake clock."""
import json

import pytest

from actions import audit, killswitch, run_incident
from oncall.llm import LLMNotConfigured
from tests.test_actions import Clock, FakeRun, fake_health, metrics_data, metrics_state, report, row
from tools.result import ToolResult

DETECTED = "2026-10-24T04:59:00+00:00"


class World:
    """A fake docker: valkey-cart is stopped until the (fake) docker command runs, if `heals` is true."""

    def __init__(self, heals=True, returncode=0):
        self.docker = FakeRun(returncode=returncode)
        self.heals = heals

    def health(self, include_stats=True):
        up = self.heals and bool(self.docker.calls) and self.docker.returncode == 0
        rows = [row("valkey-cart", "running", "healthy") if up else row("valkey-cart"), row("cart", "running", "healthy")]
        return fake_health(*rows)(include_stats)


def investigating(rep, folder):
    alerts = []

    def investigate(alert, details=None):
        alerts.append(alert)
        if details is not None:
            details["folder"] = folder
        return rep
    investigate.alerts = alerts
    return investigate


def never(prompt):
    raise AssertionError("must not ask for approval")


@pytest.fixture
def env(tmp_path):
    world, clock = World(), Clock()
    folder = tmp_path / "runs" / "inc-1"
    return type("Env", (), {"world": world, "clock": clock, "folder": folder, "dir": tmp_path / "actions", "tmp": tmp_path})


def go(env, rep=None, answer="y", execute=False, act=True, metrics=None, world=None, investigate=None):
    world = world or env.world
    answers = iter([answer])
    return run_incident.run(
        "ALERT: container valkey-cart is not running", execute=execute, act=act, detected_at=DETECTED,
        investigate_fn=investigate or investigating(rep or report(), env.folder),
        input_fn=(never if answer is None else lambda prompt: next(answers)), command_runner=world.docker,
        health=world.health, metrics=metrics or metrics_data(0.0), clock=env.clock, sleep=env.clock.sleep,
        actions_dir=env.dir, runs_dir=env.tmp / "runs")


def saved(env):
    return json.loads((env.folder / "incident_report.json").read_text())


def test_escalated_report_writes_the_incident_report_and_stops(env, capsys):
    out = go(env, report(status="escalated"), answer=None)
    assert out["exit_code"] == 0 and out["outcome"] == "escalated" and env.world.docker.calls == []
    assert saved(env)["timeline"]["detected"] == DETECTED
    assert "escalated this incident to a human" in capsys.readouterr().out
    assert audit.read(env.dir) == []


def test_no_proposal_says_why_and_writes_the_incident_report(env, capsys):
    out = go(env, report("no-such-thing"), answer=None)
    assert out["exit_code"] == 0 and out["outcome"] == "escalated"
    assert "No action proposed" in capsys.readouterr().out and (env.folder / "incident_report.md").is_file()
    assert saved(env)["execution"] is None


def test_denied_changes_nothing(env, capsys):
    out = go(env, answer="n", execute=True)
    assert out["outcome"] == "denied" and out["exit_code"] == 0 and env.world.docker.calls == []
    assert "Approval denied" in capsys.readouterr().out
    assert saved(env)["verification"] is None


def test_approved_without_execute_is_a_dry_run_and_says_so(env, capsys):
    out = go(env, answer="y", metrics=metrics_state("backend_error"))
    text = capsys.readouterr().out
    assert out["outcome"] == "dry_run" and env.world.docker.calls == []
    assert "DRY RUN: nothing was changed" in text and "NOT run" in text and "--execute" in text
    assert "verification: not run" in text
    assert [e["event"] for e in audit.read(env.dir)] == ["approval", "dry_run"]
    assert saved(env)["execution"]["mode"] == "dry_run"


def test_approved_and_recovered(env, capsys):
    out = go(env, execute=True)
    text = capsys.readouterr().out
    assert out["outcome"] == "recovered" and out["exit_code"] == 0
    assert env.world.docker.calls[0][0] == ["docker", "start", "valkey-cart"]
    assert "verification: recovered" in text and "action taken: restart_container" in text
    assert str(env.folder / "incident_report.md") in text and str(env.dir / "audit.jsonl") in text
    doc = saved(env)
    assert doc["timeline"]["executed"] != "n/a" and doc["timeline"]["verified"] != "n/a"
    assert doc["timeline"]["proposed"] != "n/a" and doc["timeline"]["investigation_end"] != "n/a"
    assert [e["event"] for e in audit.read(env.dir)] == ["approval", "execute_started", "execute_finished"]


def test_approved_but_not_recovered_escalates_with_exit_zero(env, capsys):
    world = World(heals=False)
    out = go(env, execute=True, world=world)
    text = capsys.readouterr().out
    assert out["outcome"] == "not_recovered" and out["exit_code"] == 0
    assert len(world.docker.calls) == 1 and "ESCALATE TO A HUMAN" in text
    assert saved(env)["verification"]["state"] == "not_recovered"


def test_recovered_with_elevated_errors_is_reported_as_unverified(env):
    assert go(env, execute=True, metrics=metrics_data(0.0, 40.0))["outcome"] == "recovered_unverified_metrics"


def test_a_failed_command_is_a_handled_outcome_and_skips_verification(env):
    world = World(returncode=1)
    out = go(env, execute=True, world=world)
    assert out["outcome"] == "execution_failed" and out["exit_code"] == 0
    assert saved(env)["verification"] is None


def test_kill_switch_engaged_blocks_approval_and_execution(env, capsys):
    killswitch.engage(env.dir)
    out = go(env, answer=None, execute=True)
    text = capsys.readouterr().out
    assert out["outcome"] == "blocked_by_kill_switch" and out["exit_code"] == 0 and env.world.docker.calls == []
    assert text.count("KILL SWITCH ENGAGED") == 2 and saved(env)["verification"] is None


def test_kill_switch_engaged_while_waiting_for_the_answer(env):
    def input_fn(prompt):
        killswitch.engage(env.dir)
        return "y"

    out = run_incident.run("ALERT: x", execute=True, investigate_fn=investigating(report(), env.folder), input_fn=input_fn,
                           command_runner=env.world.docker, health=env.world.health, metrics=metrics_data(0.0),
                           clock=env.clock, sleep=env.clock.sleep, actions_dir=env.dir, runs_dir=env.tmp / "runs")
    assert out["outcome"] == "blocked_by_kill_switch" and env.world.docker.calls == []


def test_no_act_stops_after_the_proposal(env, capsys):
    out = go(env, answer=None, act=False, execute=True)
    text = capsys.readouterr().out
    assert out["outcome"] == "proposal_only" and out["exit_code"] == 0 and env.world.docker.calls == []
    assert "docker start valkey-cart" in text and "--no-act" in text
    assert not (env.folder / "incident_report.md").exists() and audit.read(env.dir) == []


def test_an_investigation_that_crashes_or_has_no_llm_is_a_real_failure(env, capsys):
    def crash(alert, details=None):
        raise RuntimeError("boom")

    def no_llm(alert, details=None):
        raise LLMNotConfigured("no key")

    assert go(env, investigate=crash)["exit_code"] == 1
    assert go(env, investigate=no_llm)["exit_code"] == 1
    assert go(env, investigate=lambda alert, details=None: None)["exit_code"] == 1
    assert "run failed" in capsys.readouterr().err


def test_an_unwritable_incident_report_is_a_real_failure(env):
    (env.tmp / "runs").write_text("a file where the folder should be")
    assert go(env, answer="n")["exit_code"] == 1


def test_the_alert_text_reaches_the_investigation_unchanged(env):
    investigate = investigating(report(), env.folder)
    go(env, answer="n", investigate=investigate)
    assert investigate.alerts == ["ALERT: container valkey-cart is not running"]


def test_main_parses_the_flags_and_passes_the_detection_time(env):
    answers = iter(["y"])
    deps = dict(investigate_fn=investigating(report(), env.folder), input_fn=lambda p: next(answers), command_runner=env.world.docker,
                health=env.world.health, metrics=metrics_data(0.0), clock=env.clock, sleep=env.clock.sleep,
                actions_dir=env.dir, runs_dir=env.tmp / "runs")
    assert run_incident.main(["--alert", "ALERT: x", "--execute"], detected_at=DETECTED, **deps) == 0
    assert saved(env)["timeline"]["detected"] == DETECTED and saved(env)["outcome"] == "recovered"


def test_main_rejects_bad_arguments(capsys):
    for argv in ([], ["--alert", "x", "--execute", "--no-act"], ["--alert", "  "]):
        with pytest.raises(SystemExit) as exc:
            run_incident.main(argv)
        assert exc.value.code == 2


def test_dry_run_is_the_default_without_the_execute_flag(env):
    answers = iter(["y"])
    run_incident.main(["--alert", "ALERT: x"], investigate_fn=investigating(report(), env.folder), input_fn=lambda p: next(answers),
                      command_runner=env.world.docker, health=env.world.health, metrics=metrics_data(0.0),
                      clock=env.clock, sleep=env.clock.sleep, actions_dir=env.dir, runs_dir=env.tmp / "runs")
    assert env.world.docker.calls == [] and saved(env)["outcome"] == "dry_run"
