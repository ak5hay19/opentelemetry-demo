import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from actions import killswitch
from tools.result import ToolResult


MODULE_PATH = Path(__file__).resolve().parents[1] / "actions" / "flush-cache" / "flush_cache.py"
SPEC = importlib.util.spec_from_file_location("flush_cache_action", MODULE_PATH)
flush_cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(flush_cache)


def healthy(*items):
    return ToolResult("container_health", "data", ["query"], {}, items=list(items), total_items=len(items))


def cart_row(status="running", health="healthy"):
    return {"name": "valkey-cart", "status": status, "health": health}


def approved(report, proposal, *, actions_dir=None):
    return {"decision": "approved"}


class FakeCommand:
    def __init__(self, returncode=0, raises=None):
        self.calls = []
        self.returncode = returncode
        self.raises = raises

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.raises:
            raise self.raises
        return SimpleNamespace(returncode=self.returncode, stderr="error", stdout="ok")


def test_defaults_to_approved_dry_run_without_calling_docker(tmp_path):
    command = FakeCommand()
    result = flush_cache.run("incident-1", approval_fn=approved, command_runner=command,
                             health=lambda **_: healthy(cart_row()), actions_dir=tmp_path)
    assert result["mode"] == "dry_run" and result["ok"]
    assert command.calls == []
    assert "FLUSHDB ASYNC" in result["detail"]


def test_rejection_does_not_execute(tmp_path):
    command = FakeCommand()
    result = flush_cache.run("incident-1", approval_fn=lambda *_args, **_kwargs: {"decision": "denied"},
                             command_runner=command, health=lambda **_: healthy(cart_row()), actions_dir=tmp_path)
    assert result["mode"] == "rejected"
    assert command.calls == []


def test_only_fixed_cart_database_command_runs_after_approval(tmp_path):
    command = FakeCommand()
    result = flush_cache.run("incident-1", execute=True, approval_fn=approved, command_runner=command,
                             health=lambda **_: healthy(cart_row()), actions_dir=tmp_path)
    assert result["mode"] == "executed" and result["ok"]
    assert command.calls[0][0] == ["docker", "exec", "valkey-cart", "valkey-cli", "FLUSHDB", "ASYNC"]
    assert "shell" not in command.calls[0][1]
    assert command.calls[0][1]["timeout"] == flush_cache.COMMAND_TIMEOUT_S
    assert "not been verified" in result["detail"]


def test_kill_switch_blocks_before_approval_or_execution(tmp_path):
    killswitch.engage(tmp_path)
    calls = []
    command = FakeCommand()
    result = flush_cache.run("incident-1", execute=True,
                             approval_fn=lambda *_args, **_kwargs: calls.append("approval"),
                             command_runner=command, health=lambda **_: healthy(cart_row()), actions_dir=tmp_path)
    assert result["mode"] == "blocked"
    assert calls == [] and command.calls == []


def test_unhealthy_cache_container_blocks_before_approval(tmp_path):
    calls = []
    command = FakeCommand()
    result = flush_cache.run("incident-1", execute=True,
                             approval_fn=lambda *_args, **_kwargs: calls.append("approval"),
                             command_runner=command, health=lambda **_: healthy(cart_row("exited", "none")),
                             actions_dir=tmp_path)
    assert result["mode"] == "blocked"
    assert calls == [] and command.calls == []


def test_cache_health_is_rechecked_after_approval(tmp_path):
    checks = iter([healthy(cart_row()), healthy(cart_row("running", "unhealthy"))])
    command = FakeCommand()
    result = flush_cache.run("incident-1", execute=True, approval_fn=approved, command_runner=command,
                             health=lambda **_: next(checks), actions_dir=tmp_path)
    assert result["mode"] == "blocked"
    assert command.calls == []


def test_mutated_proposal_after_human_review_is_blocked(tmp_path):
    command = FakeCommand()

    def mutate_and_approve(_report, proposal, **_kwargs):
        proposal["params"]["scope"] = "all"
        return {"decision": "approved"}

    result = flush_cache.run("incident-1", execute=True, approval_fn=mutate_and_approve,
                             command_runner=command, health=lambda **_: healthy(cart_row()), actions_dir=tmp_path)
    assert result["mode"] == "blocked"
    assert "proposal changed" in result["error"]
    assert command.calls == []


@pytest.mark.parametrize("fake", [
    FakeCommand(returncode=1),
    FakeCommand(raises=subprocess.TimeoutExpired("docker", 60)),
    FakeCommand(raises=FileNotFoundError("docker")),
])
def test_command_failures_are_reported(tmp_path, fake):
    result = flush_cache.run("incident-1", execute=True, approval_fn=approved, command_runner=fake,
                             health=lambda **_: healthy(cart_row()), actions_dir=tmp_path)
    assert result["mode"] == "executed" and not result["ok"] and result["error"]


def test_second_flush_for_same_incident_is_blocked(tmp_path):
    command = FakeCommand()
    approval_calls = []

    def approve(*args, **kwargs):
        approval_calls.append(True)
        return approved(*args, **kwargs)

    kwargs = {"execute": True, "approval_fn": approved, "command_runner": command,
              "health": lambda **_: healthy(cart_row()), "actions_dir": tmp_path}
    assert flush_cache.run("incident-1", **kwargs)["mode"] == "executed"
    kwargs["approval_fn"] = approve
    assert flush_cache.run("incident-1", **kwargs)["mode"] == "blocked"
    assert approval_calls == []
    assert len(command.calls) == 1


def test_registry_rejects_changed_target_scope_or_preview(tmp_path):
    proposal = flush_cache._proposal("incident-1")
    for change in (
        {"params": {"container": "prometheus", "scope": "database"}},
        {"params": {"container": "valkey-cart", "scope": "all"}},
        {"preview": "docker exec valkey-cart valkey-cli FLUSHALL"},
    ):
        invalid = {**proposal, **change}
        command, reason = flush_cache.registry.validate(
            invalid, lambda **_: healthy(cart_row())
        )
        assert command is None and reason
