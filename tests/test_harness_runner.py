import json
import subprocess
from pathlib import Path

import pytest

from dev.faultctl import __main__ as faultctl
from dev.faultctl.catalog import CATALOGUE, CONTAINER, FLAG
from dev.harness import runner


def status_output(state="running"):
    return (
        "containers in compose project 'opentelemetry-demo' (1):\n"
        "  name                   state        health     restarts\n"
        f"  shop                   {state:12} healthy    0\n"
        "shop front page: HTTP 200\n"
        "flag UI: HTTP 200\n"
    )


class FakeHttp:
    def get_status(self, path):
        return 200


class FakeDocker:
    def __init__(self, states):
        self.states = states

    def project_containers(self, project):
        return [
            {"Name": f"/{name}", "State": {"Status": state}, "RestartCount": 0}
            for name, state in self.states.items()
        ]


def real_status_output(capsys, states):
    faultctl.cmd_status(FakeHttp(), FakeDocker(states))
    return capsys.readouterr().out


class FakeCommands:
    def __init__(self, tmp_path, *, injections=None, status=None, investigation_error=False, tokens=None,
                 investigation_timeout=False, investigation_interrupt=False):
        self.tmp_path = tmp_path
        self.injections = injections or {}
        self.status = status or status_output()
        self.investigation_error = investigation_error
        self.investigation_timeout = investigation_timeout
        self.investigation_interrupt = investigation_interrupt
        self.tokens = tokens or (100, 50)
        self.calls = []
        self.timeouts = {}

    def __call__(self, args, **kwargs):
        self.calls.append(args[1:])
        self.timeouts[args[3] if args[2] == "dev.faultctl" else args[2]] = kwargs.get("timeout")
        command = args[1:]
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
            if self.investigation_timeout:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            if self.investigation_interrupt:
                raise KeyboardInterrupt
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
    monkeypatch.setattr(runner, "ROOT", repo)
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
    assert entries[0]["expected_component"] == "valkey-cart"
    assert entries[1]["expected_component"] == "payment"


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


def test_dry_run_validates_incident_names(tmp_path):
    def unexpected_command(*args, **kwargs):
        raise AssertionError("dry-run executed a command")

    with pytest.raises(ValueError, match="no-such-incident"):
        runner.run_sweep(
            ["cache-unavailable", "no-such-incident"],
            dry_run=True,
            command_runner=unexpected_command,
            harness_root=tmp_path / "not-created",
        )


def test_dry_run_default_selection_uses_catalog_without_commands(tmp_path, capsys):
    def unexpected_command(*args, **kwargs):
        raise AssertionError("dry-run executed a command")

    runner.run_sweep(None, dry_run=True, command_runner=unexpected_command, harness_root=tmp_path / "not-created")
    assert "Incident order: cache-unavailable, payment-failure, cart-failure" in capsys.readouterr().out
    assert not (tmp_path / "not-created").exists()


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


def test_healthy_cmd_status_output_is_a_clean_baseline(capsys):
    output = real_status_output(capsys, {"opentelemetry-demo-ad-1": "running", "valkey-cart": "running"})
    assert "flag UI: HTTP 200" in output
    assert runner._containers_running(output) is True


def test_stopped_container_in_cmd_status_output_is_not_clean(capsys):
    output = real_status_output(capsys, {"opentelemetry-demo-ad-1": "running", "valkey-cart": "exited"})
    assert runner._containers_running(output) is False


def test_status_with_no_containers_is_not_clean(capsys):
    assert runner._containers_running(real_status_output(capsys, {})) is False


def test_trailer_lines_are_not_container_rows():
    output = status_output() + "unrelated line that is not indented\n"
    assert runner._containers_running(output) is True


def test_every_catalogue_incident_has_an_expected_component():
    for incident in CATALOGUE.values():
        assert runner._expected_component(incident), incident.id
    flag_ids = {i.id for i in CATALOGUE.values() if i.mechanism == FLAG}
    assert set(runner.FLAG_INCIDENT_SERVICES) == flag_ids


def test_expected_component_is_the_container_or_the_mapped_service():
    assert runner._expected_component(CATALOGUE["cache-unavailable"]) == "valkey-cart"
    assert runner._expected_component(CATALOGUE["ad-failure"]) == "ad"
    assert runner._expected_component(CATALOGUE["product-catalog-lock-contention"]) == "product-catalog"
    assert all(
        runner._expected_component(i) == i.target for i in CATALOGUE.values() if i.mechanism == CONTAINER
    )


def test_agent_timeout_is_recorded_and_sweep_continues(harness_env):
    commands = FakeCommands(harness_env, investigation_timeout=True)
    manifest_path = execute(harness_env, commands, ["cache-unavailable", "payment-failure"], agent_timeout=7)
    entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [entry["incident"] for entry in entries] == ["cache-unavailable", "payment-failure"]
    assert all("agent timed out" in entry["notes"] for entry in entries)
    assert commands.timeouts["oncall.investigate"] == 7
    assert commands.calls.count(["-m", "dev.faultctl", "reset", "cache-unavailable"]) == 1


def test_faultctl_timeout_is_recorded_and_final_reset_still_runs(harness_env):
    class SlowResetAll(FakeCommands):
        def __call__(self, args, **kwargs):
            if args[1:] == ["-m", "dev.faultctl", "reset-all", "--yes"]:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return super().__call__(args, **kwargs)

    commands = SlowResetAll(harness_env)
    manifest_path = execute(harness_env, commands)
    entry = json.loads(manifest_path.read_text(encoding="utf-8"))[0]
    assert "timed out" in entry["notes"]
    assert entry["baseline_clean"] is False
    assert ["-m", "dev.faultctl", "reset", "cache-unavailable"] in commands.calls


def test_interrupt_is_recorded_reset_runs_and_error_is_reraised(harness_env):
    commands = FakeCommands(harness_env, investigation_interrupt=True)
    with pytest.raises(KeyboardInterrupt):
        execute(harness_env, commands)
    manifest = next((harness_env / "reports" / "harness").glob("*/manifest.json"))
    entry = json.loads(manifest.read_text(encoding="utf-8"))[0]
    assert "interrupted" in entry["notes"]
    assert entry["ended_at"]
    assert ["-m", "dev.faultctl", "reset", "cache-unavailable"] in commands.calls


def test_run_directory_uses_the_last_saved_to_line(harness_env):
    first = harness_env / "reports" / "runs" / "first"
    last = harness_env / "reports" / "runs" / "last"
    assert runner._run_directory(f"saved to {first}\nnoise\nsaved to {last}\nREPORT\n") == "reports/runs/last"
    assert runner._run_directory(f"saved to {first}\nsaved to /elsewhere/run\n") is None
