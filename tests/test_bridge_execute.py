"""bridge.execute: an approved proposal goes through the Action API, and nothing else does. No Docker is touched."""
import json
from types import SimpleNamespace

from actions import audit, killswitch
from bridge import execute
from oncall import approve
from tools.result import ToolResult


def containers(*rows):
    items = [{"name": n, "compose_service": n, "status": s, "health": "unhealthy" if s != "running" else "healthy",
              "restart_count": 0, "oom_killed": False} for n, s in rows]
    return lambda include_stats=False: ToolResult("container_health", "data", ["q"], {}, items=items, total_items=len(items))


STOPPED = containers(("cache-store", "exited"), ("prometheus", "running"))


def proposal(action="restart_service", target="cache-store", **extra):
    base = {"status": "proposed", "based_on": {"component": target, "stated_confidence": 0.85, "cause": "it stopped"},
            "action": action, "target": target, "arguments": {}, "reason": "It exited.",
            "expected_recovery": "errors stop", "risk": "brief gap", "reversible": False,
            "needs_approval": True, "dry_run_first": True, "escalation_reason": None, "attempts": 1}
    return {**base, **extra}


def make_run(tmp_path, prop, answer="yes"):
    folder = tmp_path / "inc-1"
    folder.mkdir()
    (folder / "proposal.json").write_text(json.dumps(prop), encoding="utf-8")
    if answer is not None:
        decision = approve.decide(prop, ask=lambda _: answer)
        (folder / "decision.json").write_text(json.dumps(decision.to_dict()), encoding="utf-8")
    return folder


class FakeDocker:
    def __init__(self, returncode=0):
        self.commands, self.returncode = [], returncode

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        return SimpleNamespace(returncode=self.returncode, stderr="boom" if self.returncode else "")


def go(folder, tmp_path, docker=None, **kwargs):
    docker = docker or FakeDocker()
    kwargs.setdefault("health", STOPPED)
    out = execute.run(folder, command_runner=docker, actions_dir=tmp_path / "actions", **kwargs)
    return out, docker


def recovered(action_proposal, **kwargs):
    return {"state": "recovered", "detail": "running and healthy", "escalate": False}


def test_approved_proposal_is_a_dry_run_by_default(tmp_path):
    out, docker = go(make_run(tmp_path, proposal()), tmp_path)
    assert out["outcome"] == "dry_run" and docker.commands == []
    assert out["action_proposal"]["action_id"] == "restart_container" and out["action_proposal"]["preview"] == "docker start cache-store"
    assert json.loads((tmp_path / "inc-1" / "execution.json").read_text(encoding="utf-8"))["outcome"] == "dry_run"


def test_execute_runs_the_command_once_and_checks_recovery(tmp_path):
    out, docker = go(make_run(tmp_path, proposal()), tmp_path, execute=True, verify_fn=recovered)
    assert docker.commands == [["docker", "start", "cache-store"]]
    assert out["outcome"] == "executed_recovered" and out["verification"]["state"] == "recovered"
    assert [e["event"] for e in audit.read(tmp_path / "actions")] == ["execute_started", "execute_finished"]


def test_a_failed_command_is_reported_and_not_verified(tmp_path):
    out, _ = go(make_run(tmp_path, proposal()), tmp_path, FakeDocker(returncode=1), execute=True, verify_fn=recovered)
    assert out["outcome"] == "failed" and out["verification"] is None


def test_nothing_runs_without_an_approval(tmp_path):
    for answer, expect in (("no", "not approved"), (None, "no decision.json")):
        sub = tmp_path / str(answer)
        sub.mkdir()
        out, docker = go(make_run(sub, proposal(), answer), sub, execute=True)
        assert out["outcome"] == "refused" and expect in out["reason"] and docker.commands == []


def test_a_proposal_changed_after_approval_is_refused(tmp_path):
    folder = make_run(tmp_path, proposal())
    (folder / "proposal.json").write_text(json.dumps(proposal(target="prometheus")), encoding="utf-8")
    out, docker = go(folder, tmp_path, execute=True)
    assert out["outcome"] == "refused" and docker.commands == []


def test_an_action_the_action_api_does_not_have_stays_a_recommendation(tmp_path):
    prop = proposal(action="toggle_flag", target="checkout")
    out, docker = go(make_run(tmp_path, prop), tmp_path, execute=True)
    assert out["outcome"] == "refused" and docker.commands == []  # stopped already at the approval step


def test_the_action_apis_deny_list_still_applies(tmp_path):
    out, docker = go(make_run(tmp_path, proposal(target="prometheus")), tmp_path, execute=True)
    assert out["outcome"] == "refused" and "deny list" in out["reason"] and docker.commands == []


def test_a_target_that_is_not_a_container_is_refused(tmp_path):
    out, docker = go(make_run(tmp_path, proposal(target="no-such-thing")), tmp_path, execute=True)
    assert out["outcome"] == "refused" and docker.commands == []


def test_the_kill_switch_blocks_execution(tmp_path):
    killswitch.engage(tmp_path / "actions")
    out, docker = go(make_run(tmp_path, proposal()), tmp_path, execute=True)
    assert out["outcome"] == "blocked" and "kill switch" in out["reason"] and docker.commands == []


def test_no_proposal_means_nothing_to_run(tmp_path):
    prop = proposal(status="no_proposal", action=None, target=None, escalation_reason="unsure")
    out, docker = go(make_run(tmp_path, prop), tmp_path, execute=True)
    assert out["outcome"] == "refused" and docker.commands == []


def test_cli_dry_run_prints_the_command_and_saves(tmp_path, capsys):
    make_run(tmp_path, proposal())
    assert execute.main(["--run", "inc-1"], runs_dir=tmp_path, health=STOPPED, actions_dir=tmp_path / "actions") == 0
    out = capsys.readouterr().out
    assert "docker start cache-store" in out and "OUTCOME  dry_run" in out and "Add --execute" in out


def test_cli_without_any_run_explains(tmp_path, capsys):
    assert execute.main([], runs_dir=tmp_path) == 2
    assert "cannot find a run" in capsys.readouterr().err
