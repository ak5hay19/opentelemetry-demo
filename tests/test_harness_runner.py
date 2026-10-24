import json
import subprocess
from pathlib import Path

import pytest

from dev.harness import runner


def status_output(state="running"):
    return (
        "containers in compose project ('opentelemetry-demo') (1):\n"
        "  name                  state        health     restarts\n"
        f"  shop                  {state:12} healthy    0\n"
    )


class FakeCommands:
    def __init__(self, tmp_path, *, injections=None, status=None, investigation_error=False, tokens=None):
        self.tmp_path = tmp_path
        self.injections = injections or {}
        self.status = status or status_output()
        self.investigation_error = investigation_error
        self.tokens = tokens or (100, 50)
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(args[1:])
        command = args[1:]
        if command == ["-m", "dev.faultctl", "list"]:
            return subprocess.CompletedProcess(args, 0, "id mechanism target inject-default\ncache-unavailable container cache -\npayment-failure flag payment 50%\nproduct-catalog-failure flag catalog on\n", "")
        if command == ["-m", "dev.faultctl", "reset-all", "--yes"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if command == ["-m", "dev.faultctl", "status"]:
            code = 0 if self.status == status_output() else 1
            return subprocess.CompletedProcess(args, code, self.status, "")
        if len(command) == 4 and command[:3] == ["-m", "dev.faultctl", "inject"]:
            incident = command[3]
            output = self.injections.get(incident, f"[verified] inject {incident}\n")
            return subprocess.CompletedProcess(args, 0, output, "")
        if len(command) == 4 and command[:3] == ["-m", "dev.faultctl", "reset"]:
            return subprocess.CompletedProcess(args, 0, f"[verified] reset {command[3]}\n", "")
        if command[:2] == ["-m", "oncall.investigate"]:
            if self.investigation_error:
                raise OSError("investigator unavailable")
            run_dir = self.tmp_path / "reports" / "runs" / "run-1"
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "run_info.json").write_text(
                json.dumps({"tokens_input": self.tokens[0], "tokens_output": self.tokens[1]}),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(args, 0, f"saved to {run_dir}\n", "")
        if command[:2] == ["-m", "dev.harness.scoring"]:
            return subprocess.CompletedProcess(args, 0, "scoring complete\n", "")
        raise AssertionError(f"unexpected command: {command}")


@pytest.fixture
def harness_env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "reports" / "runs").mkdir(parents=True)
    (repo / "tests").mkdir()
    ground_truth = repo / "tests" / "ground_truth.md"
    ground_truth.write_text(
        "| time | action | incident | mechanism | target | variant | outcome | note |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| 2026-10-24 | inject | cache-unavailable | container | cache-service | - | verified | ok |\n"
        "| 2026-10-24 | inject | payment-failure | flag | paymentFailure | 50% | verified | ok |\n"
        "| 2026-10-24 | inject | product-catalog-failure | flag | productCatalogFailure | on | verified | ok |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "ROOT", repo)
    monkeypatch.setattr(runner, "RUNS_DIR", repo / "reports" / "runs")
    monkeypatch.setattr(runner, "GROUND_TRUTH", ground_truth)
    return repo


def execute(harness_env, commands, incidents=None, **kwargs):
    return runner.run_sweep(
        incidents or ["cache-unavailable"],
        baseline_wait=0,
        settle=0,
        command_runner=commands,
        sleep=lambda _: None,
        now=lambda: runner.datetime(2026, 10, 24, 12, 0, tzinfo=runner.datetime.now().astimezone().tzinfo),
        harness_root=harness_env / "reports" / "harness",
        **kwargs,
    )


def test_only_verified_injections_run_the_investigator(harness_env, capsys):
    commands = FakeCommands(harness_env, injections={"cache-unavailable": "[applied-unverified] inject cache-unavailable\n"})
    manifest_path = execute(harness_env, commands)
    entry = json.loads(manifest_path.read_text(encoding="utf-8"))[0]
    assert entry["inject_verified"] is False
    assert not any(command[:2] == ["-m", "oncall.investigate"] for command in commands.calls)
    assert any(command[:3] == ["-m", "dev.faultctl", "reset"] for command in commands.calls)


def test_investigation_failure_still_resets_and_records_entry(harness_env):
    commands = FakeCommands(harness_env, investigation_error=True)
    manifest_path = execute(harness_env, commands)
    entry = json.loads(manifest_path.read_text(encoding="utf-8"))[0]
    assert entry["inject_verified"] is True
    assert entry["run_dir"] is None
    assert "investigator unavailable" in entry["notes"]
    assert any(command == ["-m", "dev.faultctl", "reset", "cache-unavailable"] for command in commands.calls)


def test_manifest_is_valid_and_written_after_each_incident(harness_env):
    class InspectingCommands(FakeCommands):
        def __call__(self, args, **kwargs):
            command = args[1:]
            if command == ["-m", "dev.faultctl", "reset-all", "--yes"] and len(self.calls) >= 5:
                manifest = next((harness_env / "reports" / "harness").glob("*/manifest.json"))
                previous = json.loads(manifest.read_text(encoding="utf-8"))
                assert len(previous) == 1
            return super().__call__(args, **kwargs)

    commands = InspectingCommands(harness_env)
    manifest_path = execute(harness_env, commands, ["cache-unavailable", "payment-failure"])
    entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [entry["incident"] for entry in entries] == ["cache-unavailable", "payment-failure"]
    assert entries[0]["expected_component"] == "cache-service"


def test_unclean_baseline_skips_injection(harness_env):
    commands = FakeCommands(harness_env, status=status_output("exited"))
    manifest_path = execute(harness_env, commands)
    entry = json.loads(manifest_path.read_text(encoding="utf-8"))[0]
    assert entry["baseline_clean"] is False
    assert entry["inject_verified"] is False
    assert not any(command[:3] == ["-m", "dev.faultctl", "inject"] for command in commands.calls)
    assert any(command == ["-m", "dev.faultctl", "reset", "cache-unavailable"] for command in commands.calls)


def test_max_cost_stops_before_next_incident(harness_env):
    commands = FakeCommands(harness_env, tokens=(2_000_000, 0))
    manifest_path = execute(
        harness_env,
        commands,
        ["cache-unavailable", "payment-failure"],
        max_cost=0.10,
    )
    entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(entries) == 1
    assert not any(command == ["-m", "dev.faultctl", "inject", "payment-failure"] for command in commands.calls)


def test_dry_run_does_not_call_commands_or_create_files(tmp_path, capsys):
    harness_root = tmp_path / "not-created"

    def unexpected_command(*args, **kwargs):
        raise AssertionError("dry-run executed a command")

    assert runner.run_sweep(
        ["cache-unavailable", "payment-failure", "product-catalog-failure"],
        dry_run=True,
        command_runner=unexpected_command,
        harness_root=harness_root,
    ) is None
    assert not harness_root.exists()
    assert "cache-unavailable, payment-failure, product-catalog-failure" in capsys.readouterr().out


def test_default_selection_uses_faultctl_types():
    assert runner._default_incidents({
        "first-container": "container",
        "first-flag": "flag",
        "second-flag": "flag",
        "another-container": "container",
    }) == ["first-container", "first-flag", "second-flag"]
    with pytest.raises(RuntimeError, match="one container and two flag"):
        runner._default_incidents({"first-flag": "flag"})


def test_missing_scoring_module_is_tolerated(harness_env, capsys, monkeypatch):
    commands = FakeCommands(harness_env)
    monkeypatch.setattr(runner.importlib.util, "find_spec", lambda _: None)
    execute(harness_env, commands)
    assert "run scoring separately" in capsys.readouterr().out
