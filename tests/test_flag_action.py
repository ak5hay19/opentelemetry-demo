"""Offline tests for the disable_fault_flag action. Every test uses a synthetic temp JSON file shaped like a flag
configuration (flags -> name -> variants, defaultVariant); the real configuration is never opened."""
import copy
import json
import os

import pytest

from actions import approval, audit, flag_adapter, incident_report, killswitch, registry, run_incident, runner, verify
from actions.remediations import REMEDIATIONS, RESTING_VARIANT
from tests.test_actions import Clock, FakeRun, fake_health, metrics_data, metrics_state, report, row

CATALOGUED = sorted({name for names in REMEDIATIONS.values() for name in names})
HEALTH = fake_health(row("cart", "running", "healthy"), row("checkout", "running", "healthy"),
                     row("kafka", "running", "healthy"), row("prometheus", "running", "healthy"))


def flag(default="on", **extra):
    return {"state": "ENABLED", "variants": {"on": True, "off": False}, "defaultVariant": default, **extra}


def document():
    flags = {name: flag() for name in CATALOGUED}
    flags["notInTheTable"] = flag()
    flags["emitRawPii"] = flag()
    return {"$schema": "https://example.invalid/schema.json", "flags": flags}


@pytest.fixture
def flag_file(tmp_path, monkeypatch):
    monkeypatch.setattr(flag_adapter, "DEFAULT_PATH", tmp_path / "never-used.json")
    path = tmp_path / "flags" / "demo.flags.json"
    path.parent.mkdir()
    path.write_text(json.dumps(document(), indent=2) + "\n")
    return path


def load(path):
    return json.loads(path.read_text())


@pytest.fixture
def toggle():
    return registry.propose(report("cart"), HEALTH)


# --- the table --------------------------------------------------------------------------------------

def test_the_table_is_exactly_the_agreed_one():
    assert REMEDIATIONS == {
        "ad": ("adFailure", "adHighCpu", "adManualGc"),
        "cart": ("cartFailure", "failedReadinessProbe"),
        "email": ("emailMemoryLeak",),
        "payment": ("paymentFailure", "paymentUnreachable"),
        "product-catalog": ("productCatalogFailure", "productCatalogLockContention"),
        "astronomy-db": ("productCatalogLockContention",),
        "recommendation": ("recommendationCacheFailure",),
        "shipping": ("intlShippingSlowdown",),
        "image-provider": ("imageSlowLoad",),
        "frontend": ("imageSlowLoad", "loadGeneratorFloodHomepage"),
        "load-generator": ("loadGeneratorFloodHomepage",),
        "kafka": ("kafkaConsumerDead", "kafkaQueueProblems"),
        "fraud-detection": ("kafkaConsumerDead", "kafkaQueueProblems"),
    }
    assert RESTING_VARIANT == "off"
    for left_out in ("emitRawPii", "cartMetricCardinalityOverflow", "aiRunawayAgent", "aiSlowResponse"):
        assert left_out not in CATALOGUED


# --- propose: restart vs toggle vs nothing ----------------------------------------------------------------

def test_a_stopped_container_gets_a_restart_not_a_toggle():
    proposal = registry.propose(report("cart"), fake_health(row("cart", "exited")))
    assert proposal["action_id"] == "restart_container" and proposal["preview"] == "docker start cart"


def test_a_running_component_in_the_table_gets_the_toggle(toggle):
    assert toggle["action_id"] == "disable_fault_flag" and toggle["component"] == "cart"
    assert toggle["params"] == {"component": "cart", "flags": ["cartFailure", "failedReadinessProbe"]}
    assert toggle["preview"] == "set cart fault toggles to off: cartFailure, failedReadinessProbe (nothing else changes)"
    assert toggle["risk"] == "low" and toggle["incident_id"] == "inc-1"


@pytest.mark.parametrize("component", sorted(REMEDIATIONS))
def test_every_table_entry_gives_a_toggle_for_its_running_container(component):
    proposal = registry.propose(report(component), fake_health(row(component, "running", "healthy")))
    assert proposal["params"]["flags"] == list(REMEDIATIONS[component])
    assert registry.validate(proposal, HEALTH)[0]["flags"] == list(REMEDIATIONS[component])


@pytest.mark.parametrize("name", ["checkout", "prometheus", "valkey-cart", "no-such-thing", "../etc", "", None, 5])
def test_a_component_not_in_the_table_gets_no_proposal(name):
    health = fake_health(row("checkout", "running"), row("prometheus", "running"), row("valkey-cart", "running"))
    assert registry.propose(report(name), health) is None
    assert registry.explain_no_proposal(report(name), health)


def test_the_component_name_is_normalised():
    assert registry.propose(report("  CART \n"), HEALTH)["component"] == "cart"
    assert registry.propose(report(" Product-Catalog"), fake_health(row("product-catalog", "running")))["params"]["component"] == "product-catalog"


@pytest.mark.parametrize("state", ["no_data", "backend_error"])
def test_unreadable_container_state_gives_no_proposal_and_says_why(state):
    def health(include_stats=True):
        from tools.result import ToolResult
        return ToolResult("container_health", state, ["q"], {}, error="docker down" if state == "backend_error" else None)

    assert registry.propose(report("cart"), health) is None
    why = registry.explain_no_proposal(report("cart"), health)
    assert "unavailable" in why and state in why


def test_the_restart_deny_list_does_not_apply_to_the_table_but_still_blocks_restarts():
    assert registry.propose(report("kafka"), HEALTH)["action_id"] == "disable_fault_flag"
    stopped = registry.propose(report("kafka"), fake_health(row("kafka", "exited")))
    assert stopped is None
    assert "deny list" in registry.explain_no_proposal(report("kafka"), fake_health(row("kafka", "exited")))


def test_an_escalated_report_gets_no_toggle():
    assert registry.propose(report("cart", status="escalated"), HEALTH) is None


# --- the adapter ------------------------------------------------------------------------------------

def test_set_resting_changes_only_the_named_defaults_to_off(flag_file):
    before = load(flag_file)
    assert flag_adapter.set_resting(["cartFailure", "failedReadinessProbe"], flag_file) == ["cartFailure", "failedReadinessProbe"]
    expected = copy.deepcopy(before)
    expected["flags"]["cartFailure"]["defaultVariant"] = "off"
    expected["flags"]["failedReadinessProbe"]["defaultVariant"] = "off"
    assert load(flag_file) == expected
    assert list(load(flag_file)) == list(before) and list(load(flag_file)["flags"]) == list(before["flags"])


def test_set_resting_is_idempotent_and_never_turns_anything_on(flag_file):
    data = load(flag_file)
    data["flags"]["cartFailure"]["defaultVariant"] = "off"
    flag_file.write_text(json.dumps(data))
    flag_adapter.set_resting(["cartFailure"], flag_file)
    first = flag_file.read_text()
    flag_adapter.set_resting(["cartFailure"], flag_file)
    assert flag_file.read_text() == first
    assert load(flag_file)["flags"]["cartFailure"]["defaultVariant"] == "off"
    assert all(f["defaultVariant"] == "on" for name, f in load(flag_file)["flags"].items() if name != "cartFailure")


def test_set_resting_returns_and_logs_no_prior_state(flag_file, caplog, capsys):
    result = flag_adapter.set_resting(["cartFailure"], flag_file)
    assert result == ["cartFailure"] and "on" not in json.dumps(result)
    assert caplog.text == "" and capsys.readouterr().out == ""


@pytest.mark.parametrize("names", [["emitRawPii"], ["notInTheTable"], ["cartFailure", "notInTheTable"], ["cartMetricCardinalityOverflow"],
                                   ["CARTFAILURE"], [], None, "cartFailure", [5], ["cartFailure", None]])
def test_only_catalogued_names_are_accepted_and_nothing_is_written(flag_file, names):
    before = flag_file.read_bytes()
    with pytest.raises(flag_adapter.FlagAdapterError):
        flag_adapter.set_resting(names, flag_file)
    assert flag_file.read_bytes() == before


def test_a_missing_off_variant_writes_nothing_even_for_the_valid_names(flag_file):
    data = load(flag_file)
    del data["flags"]["failedReadinessProbe"]["variants"]["off"]
    flag_file.write_text(json.dumps(data))
    before = flag_file.read_bytes()
    with pytest.raises(flag_adapter.FlagAdapterError, match="failedReadinessProbe"):
        flag_adapter.set_resting(["cartFailure", "failedReadinessProbe"], flag_file)
    assert flag_file.read_bytes() == before


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"flags": []}', '{"other": {}}', '{"flags": {}}',
                                     '{"flags": {"cartFailure": "on"}}', '{"flags": {"cartFailure": {"defaultVariant": "on"}}}'])
def test_a_bad_file_fails_safely_and_writes_nothing(flag_file, content):
    flag_file.write_text(content)
    with pytest.raises(flag_adapter.FlagAdapterError):
        flag_adapter.set_resting(["cartFailure"], flag_file)
    assert flag_file.read_text() == content
    assert [p.name for p in flag_file.parent.iterdir()] == [flag_file.name]


def test_a_missing_file_fails_safely_and_creates_nothing(tmp_path):
    with pytest.raises(flag_adapter.FlagAdapterError, match="does not exist"):
        flag_adapter.set_resting(["cartFailure"], tmp_path / "gone.json")
    assert list(tmp_path.iterdir()) == []


def test_a_toggle_with_a_targeting_rule_is_refused(flag_file):
    data = load(flag_file)
    data["flags"]["cartFailure"]["targeting"] = {"if": [{"==": [1, 1]}, "on", "off"]}
    flag_file.write_text(json.dumps(data))
    before = flag_file.read_bytes()
    with pytest.raises(flag_adapter.FlagAdapterError, match="targeting"):
        flag_adapter.set_resting(["cartFailure"], flag_file)
    assert flag_file.read_bytes() == before


def test_the_write_is_atomic_and_leaves_no_temp_file(flag_file, monkeypatch):
    mode = flag_file.stat().st_mode
    flag_adapter.set_resting(["cartFailure"], flag_file)
    assert [p.name for p in flag_file.parent.iterdir()] == [flag_file.name] and flag_file.stat().st_mode == mode

    before = flag_file.read_bytes()

    def broken(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(flag_adapter.FlagAdapterError, match="could not be written"):
        flag_adapter.set_resting(["failedReadinessProbe"], flag_file)
    assert flag_file.read_bytes() == before and [p.name for p in flag_file.parent.iterdir()] == [flag_file.name]


def test_the_default_path_is_used_when_none_is_given(flag_file, monkeypatch):
    monkeypatch.setattr(flag_adapter, "DEFAULT_PATH", flag_file)
    flag_adapter.set_resting(["cartFailure"])
    assert load(flag_file)["flags"]["cartFailure"]["defaultVariant"] == "off"
    assert flag_adapter.__dict__["DEFAULT_PATH"] == flag_file


def test_the_real_default_location_is_the_demo_flag_file():
    assert flag_adapter.DEFAULT_PATH.parts[-3:] == ("src", "flagd", "demo.flagd.json")


# --- runner ------------------------------------------------------------------------------------------

def run(proposal, flag_file, tmp_path, dry_run=True, health=HEALTH, docker=None):
    return runner.execute(proposal, dry_run=dry_run, runner=docker or FakeRun(), health=health,
                          actions_dir=tmp_path / "audit", flag_path=flag_file)


def test_dry_run_is_the_default_changes_nothing_and_states_the_exact_change(toggle, flag_file, tmp_path):
    before = flag_file.read_bytes()
    docker = FakeRun()
    result = runner.execute(toggle, runner=docker, health=HEALTH, actions_dir=tmp_path / "audit", flag_path=flag_file)
    assert result["mode"] == "dry_run" and result["ok"] and docker.calls == []
    assert flag_file.read_bytes() == before
    assert 'Would set defaultVariant to "off" for: cartFailure, failedReadinessProbe' in result["detail"]
    assert "no other flag or key changes" in result["detail"]
    assert [e["event"] for e in audit.read(tmp_path / "audit")] == ["dry_run"]


def test_dry_run_with_an_unusable_file_is_blocked(toggle, flag_file, tmp_path):
    flag_file.write_text("garbage")
    result = run(toggle, flag_file, tmp_path)
    assert result["mode"] == "blocked" and "cannot be made" in result["error"] and flag_file.read_text() == "garbage"


def test_a_real_run_sets_only_the_catalogued_toggles_to_off_and_runs_no_command(toggle, flag_file, tmp_path):
    docker = FakeRun()
    result = run(toggle, flag_file, tmp_path, dry_run=False, docker=docker)
    assert result["mode"] == "executed" and result["ok"] and result["error"] is None and docker.calls == []
    assert result["detail"] == "set to off: cartFailure, failedReadinessProbe"
    flags = load(flag_file)["flags"]
    assert {n for n, f in flags.items() if f["defaultVariant"] == "off"} == {"cartFailure", "failedReadinessProbe"}
    events = audit.read(tmp_path / "audit")
    assert [e["event"] for e in events] == ["execute_started", "execute_finished"]
    assert events[0]["outcome"] == toggle["preview"] and '"on"' not in json.dumps(events)


def test_a_failing_write_is_reported_and_recorded(toggle, flag_file, tmp_path):
    flag_file.write_text("garbage")
    result = run(toggle, flag_file, tmp_path, dry_run=False)
    assert result["ok"] is False and result["mode"] == "executed" and "cannot be read" in result["error"]
    assert "tried to set" in result["detail"] and flag_file.read_text() == "garbage"
    assert audit.read(tmp_path / "audit")[-1]["outcome"] == result["error"]


def test_kill_switch_blocks_dry_and_real_runs(toggle, flag_file, tmp_path):
    killswitch.engage(tmp_path / "audit")
    before = flag_file.read_bytes()
    for dry in (True, False):
        result = run(toggle, flag_file, tmp_path, dry_run=dry)
        assert result["mode"] == "blocked" and runner.KILL_TEXT in result["error"]
    assert flag_file.read_bytes() == before


def test_kill_switch_is_checked_a_second_time_just_before_the_write(toggle, flag_file, tmp_path, monkeypatch):
    calls = {"n": 0}

    def flaky(directory=None):
        calls["n"] += 1
        return calls["n"] >= 2

    monkeypatch.setattr(killswitch, "is_engaged", flaky)
    before = flag_file.read_bytes()
    result = run(toggle, flag_file, tmp_path, dry_run=False)
    assert result["mode"] == "blocked" and calls["n"] == 2 and flag_file.read_bytes() == before


@pytest.mark.parametrize("change", [
    {"params": {"component": "cart", "flags": ["cartFailure"]}},
    {"params": {"component": "cart", "flags": ["cartFailure", "failedReadinessProbe", "emitRawPii"]}},
    {"params": {"component": "cart", "flags": ["failedReadinessProbe", "cartFailure"]}},
    {"params": {"component": "cart", "flags": ("cartFailure", "failedReadinessProbe")}},
    {"params": {"component": "cart", "flags": "cartFailure"}},
    {"params": {"component": "cart", "flags": ["adFailure"]}},
    {"params": {"component": "cart", "flags": []}},
    {"params": {"component": "checkout", "flags": ["cartFailure", "failedReadinessProbe"]}},
    {"params": {"component": "Cart", "flags": ["cartFailure", "failedReadinessProbe"]}},
    {"params": {"component": ["cart"], "flags": ["cartFailure", "failedReadinessProbe"]}},
    {"params": {"component": "cart", "flags": ["cartFailure", "failedReadinessProbe"], "path": "/etc/x"}},
    {"params": {"component": "cart"}},
    {"preview": "set cart fault toggles to off: cartFailure (nothing else changes)"},
    {"preview": "docker restart cart"},
    {"action_id": "toggle_flag"},
    {"action_id": ["disable_fault_flag"]},
    {"incident_id": ""},
])
def test_tampered_toggle_proposals_are_rejected(toggle, flag_file, tmp_path, change):
    before = flag_file.read_bytes()
    docker = FakeRun()
    for dry in (True, False):
        result = run({**toggle, **change}, flag_file, tmp_path, dry_run=dry, docker=docker)
        assert result["mode"] == "blocked" and "rejected" in result["error"], change
    assert flag_file.read_bytes() == before and docker.calls == []


def test_a_restart_proposal_cannot_carry_flag_params(flag_file, tmp_path):
    restart = registry.propose(report("cart"), fake_health(row("cart", "exited")))
    assert run({**restart, "params": {"component": "cart", "flags": ["cartFailure", "failedReadinessProbe"]}}, flag_file, tmp_path)["mode"] == "blocked"


def test_a_second_action_for_the_incident_is_blocked(toggle, flag_file, tmp_path):
    first = run(toggle, flag_file, tmp_path, dry_run=False)
    flag_file.write_text(json.dumps(document()))
    snapshot = flag_file.read_bytes()
    again = run(toggle, flag_file, tmp_path, dry_run=False)
    stopped = fake_health(row("cart", "exited"))
    restart = run(registry.propose(report("cart"), stopped), flag_file, tmp_path, dry_run=False, health=stopped)
    assert first["mode"] == "executed" and again["mode"] == "blocked" and restart["mode"] == "blocked"
    assert "second" in again["error"] and "second" in restart["error"] and flag_file.read_bytes() == snapshot


def test_a_dry_run_does_not_use_up_the_incident_action(toggle, flag_file, tmp_path):
    run(toggle, flag_file, tmp_path)
    assert run(toggle, flag_file, tmp_path, dry_run=False)["mode"] == "executed"


# --- verify ------------------------------------------------------------------------------------------------

def check(proposal, health, metrics, **kwargs):
    clock = Clock()
    return verify.check_recovery(proposal, poll_s=10, clock=clock, sleep=clock.sleep, health=health, metrics=metrics, **kwargs)


def test_verify_watches_the_component_container_and_reuses_the_states(toggle):
    up, down = fake_health(row("cart", "running", "healthy")), fake_health(row("cart", "exited"))
    assert check(toggle, up, metrics_data(0.0))["state"] == "recovered"
    errors = check(toggle, up, metrics_data(0.0, 40.0))
    assert errors["state"] == "recovered_unverified_metrics" and "pre-change errors" in errors["detail"]
    assert check(toggle, down, metrics_data(0.0))["state"] == "not_recovered"
    assert check(toggle, up, metrics_state("backend_error"))["state"] == "recovered_unverified_metrics"
    assert check(toggle, fake_health(row("other")), metrics_data(0.0))["state"] == "timed_out"


def test_verify_default_timeout_is_300_seconds_for_the_toggle_and_180_for_a_restart(toggle):
    down = fake_health(row("cart", "exited"))
    assert check(toggle, down, metrics_data(0.0))["timeout_s"] == 300
    assert len(check(toggle, down, metrics_data(0.0))["checks"]) == 31
    restart = registry.propose(report("cart"), down)
    assert check(restart, down, metrics_data(0.0))["timeout_s"] == 180
    assert check(toggle, down, metrics_data(0.0), timeout_s=30)["timeout_s"] == 30


def test_verify_never_reads_flag_state():
    import ast
    source = open(verify.__file__, encoding="utf-8").read()
    assert "flag_adapter" not in source and "set_resting" not in source
    assert not any(isinstance(n, ast.Import | ast.ImportFrom) and "flag_adapter" in ast.dump(n) for n in ast.walk(ast.parse(source)))


def test_the_toggle_note_says_nothing_is_undone_automatically(toggle):
    out = check(toggle, fake_health(row("cart", "running", "healthy")), metrics_data(0.0))
    assert "not undone automatically" in out["note"]


# --- approval screen and incident report ----------------------------------------------------------------------------

def test_the_approval_screen_shows_the_action_and_the_flags(toggle, tmp_path, capsys):
    approval.ask(report("cart"), toggle, input_fn=lambda prompt: "n", actions_dir=tmp_path)
    out = capsys.readouterr().out
    for text in ("disable_fault_flag", "toggles to set to off: cartFailure, failedReadinessProbe", "exact change:",
                 toggle["preview"], "Nothing changes unless you answer yes"):
        assert text in out
    assert "exact command" not in out


def test_the_incident_report_shows_the_action_and_the_flags_and_a_toggle_specific_prevention(toggle, tmp_path):
    approved = {"decision": "approved", "at": "2026-10-24T05:01:00+00:00", "by": "me"}
    executed = {"ok": True, "mode": "executed", "started_at": "2026-10-24T05:01:05+00:00", "ended_at": "x", "detail": "set", "error": None}
    ver = check(toggle, fake_health(row("cart", "running", "healthy")), metrics_data(0.0))
    doc = incident_report.build(report("cart"), toggle, approved, executed, ver, None, tmp_path)
    text = (tmp_path / "incident_report.md").read_text()
    for needed in ("disable_fault_flag", "Toggles set to off: cartFailure, failedReadinessProbe", "Exact change: " + toggle["preview"],
                   "generic suggestion", "toggle"):
        assert needed in text
    assert doc["outcome"] == "recovered" and "restart policy" not in text
    assert doc["prevention"]["suggestions"] == incident_report.PREVENTION["disable_fault_flag"]


# --- the whole flow --------------------------------------------------------------------------------------------------

def investigating(rep, folder):
    def investigate(alert, details=None):
        details["folder"] = folder
        return rep
    return investigate


def flow(tmp_path, flag_file, rep=None, answer="y", execute=False, health=HEALTH, metrics=None):
    answers = iter([answer])
    folder = tmp_path / "runs" / "inc-1"
    out = run_incident.run("ALERT: error ratio 0.40 on cart above 0.05", execute=execute,
                           investigate_fn=investigating(rep or report("cart"), folder), input_fn=lambda p: next(answers),
                           command_runner=FakeRun(), health=health, metrics=metrics or metrics_data(0.0), clock=(c := Clock()),
                           sleep=c.sleep, actions_dir=tmp_path / "audit", runs_dir=tmp_path / "runs", flag_path=flag_file)
    return out, folder


def test_flow_dry_run_by_default_leaves_the_file_alone(tmp_path, flag_file, capsys):
    before = flag_file.read_bytes()
    out, folder = flow(tmp_path, flag_file)
    text = capsys.readouterr().out
    assert out["outcome"] == "dry_run" and flag_file.read_bytes() == before
    assert "exact change: set cart fault toggles to off" in text and "DRY RUN" in text
    assert json.loads((folder / "incident_report.json").read_text())["action"]["params"]["flags"] == ["cartFailure", "failedReadinessProbe"]


def test_flow_execute_sets_the_toggles_then_verifies_the_container(tmp_path, flag_file, capsys):
    out, folder = flow(tmp_path, flag_file, execute=True)
    text = capsys.readouterr().out
    assert out["outcome"] == "recovered" and "Checking recovery of cart" in text and "action taken: disable_fault_flag" in text
    assert load(flag_file)["flags"]["cartFailure"]["defaultVariant"] == "off"
    assert load(flag_file)["flags"]["adFailure"]["defaultVariant"] == "on"
    assert json.loads((folder / "incident_report.json").read_text())["verification"]["timeout_s"] == 300
    assert [e["event"] for e in audit.read(tmp_path / "audit")] == ["approval", "execute_started", "execute_finished"]


def test_flow_denied_and_kill_switch_change_nothing(tmp_path, flag_file):
    before = flag_file.read_bytes()
    assert flow(tmp_path, flag_file, answer="n", execute=True)[0]["outcome"] == "denied"
    killswitch.engage(tmp_path / "audit")
    assert flow(tmp_path, flag_file, execute=True)[0]["outcome"] == "blocked_by_kill_switch"
    assert flag_file.read_bytes() == before


def test_flow_with_unreadable_container_state_proposes_nothing(tmp_path, flag_file, capsys):
    from tools.result import ToolResult

    def broken(include_stats=True):
        return ToolResult("container_health", "backend_error", ["q"], {}, error="docker down")

    before = flag_file.read_bytes()
    out, folder = flow(tmp_path, flag_file, execute=True, health=broken, answer="y")
    assert out["outcome"] == "escalated" and "No action proposed" in capsys.readouterr().out
    assert flag_file.read_bytes() == before and audit.read(tmp_path / "audit") == []


def test_flow_with_a_bad_flag_file_ends_blocked_or_failed_and_writes_nothing(tmp_path, flag_file):
    flag_file.write_text("garbage")
    assert flow(tmp_path, flag_file)[0]["outcome"] == "blocked"
    out, folder = flow(tmp_path, flag_file, execute=True)
    assert out["outcome"] == "execution_failed" and flag_file.read_text() == "garbage"
