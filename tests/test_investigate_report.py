"""investigate() writes the report next to the saved run and prints a REPORT section. Fake LLM, stub tools, no network."""
import json

import pytest

import oncall.investigate as investigate
from oncall.investigate import investigate as run_and_report, main
from tests.test_loop import GOOD_DIAGNOSIS, ScriptedLLM, call, ok, say, stubs, submit

INVENTED_EVIDENCE = [{"ledger_id": "C9", "kind": "container", "claim": "svc-b is not running", "quote": "svc-b", "value": "exited"}]


@pytest.fixture
def with_stub_tools(monkeypatch):
    tools, _ = stubs(container_health=ok("container_health"), query_metrics=ok("query_metrics"))
    original = investigate.run_investigation
    monkeypatch.setattr(investigate, "run_investigation", lambda llm, symptom, config: original(llm, symptom, config, tools))


TWO_KINDS = GOOD_DIAGNOSIS["evidence"] + [
    {"ledger_id": "M1", "kind": "metric", "claim": "svc-b shows up in the metrics", "quote": "svc-b"}]


def llm_with(diagnosis):
    return ScriptedLLM([say(call("container_health")), say(call("query_metrics")), say(diagnosis)])


def only_run_folder(runs_dir):
    (folder,) = list(runs_dir.iterdir())
    return folder


def test_a_diagnosed_run_writes_report_json_and_report_md(tmp_path, with_stub_tools):
    report = run_and_report("checkout is slow", llm_with(submit(evidence=TWO_KINDS)), runs_dir=tmp_path)
    folder = only_run_folder(tmp_path)
    assert sorted(p.name for p in folder.iterdir()) == [
        "candidate.json", "ledger.json", "report.json", "report.md", "run_info.json"]
    saved = json.loads((folder / "report.json").read_text())
    assert saved == report
    assert report["status"] == "diagnosed" and report["escalation_reason"] is None
    assert report["component"] == "svc-b" and report["trigger"] == "checkout is slow"
    assert report["incident_id"] == folder.name
    assert [e["validated"] for e in report["evidence"]] == [True, True]
    assert "diagnosed" in (folder / "report.md").read_text()


def test_an_invented_ledger_id_is_escalated(tmp_path, with_stub_tools):
    report = run_and_report("x", llm_with(submit(evidence=INVENTED_EVIDENCE)), runs_dir=tmp_path)
    assert report["status"] == "escalated" and report["escalation_reason"]
    assert [e["validated"] for e in report["evidence"]] == [False]
    assert json.loads((only_run_folder(tmp_path) / "report.json").read_text())["status"] == "escalated"


def test_finalize_raising_does_not_lose_the_candidate(tmp_path, with_stub_tools, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(investigate, "finalize", boom)
    details = {}
    report = run_and_report("x", llm_with(submit(evidence=TWO_KINDS)), runs_dir=tmp_path, details=details)
    folder = only_run_folder(tmp_path)
    assert sorted(p.name for p in folder.iterdir()) == ["candidate.json", "ledger.json", "run_info.json"]
    assert json.loads((folder / "candidate.json").read_text())["component"] == "svc-b"
    assert report["status"] == "escalated" and "disk full" in report["escalation_reason"]
    assert details["report_error"] == "RuntimeError: disk full"
    assert "report failed: RuntimeError: disk full" in capsys.readouterr().err


def test_the_cli_survives_a_failing_report_and_still_exits_zero(tmp_path, with_stub_tools, monkeypatch, capsys):
    monkeypatch.setattr(investigate, "finalize", lambda *a, **k: 1 / 0)
    assert main([], llm=llm_with(submit(evidence=TWO_KINDS)), runs_dir=tmp_path) == 0
    out = capsys.readouterr().out
    assert "REPORT" in out and "report files not written" in out and "DIAGNOSIS CANDIDATE" in out
    assert (only_run_folder(tmp_path) / "candidate.json").is_file()


def test_the_printed_output_ends_with_a_report_section(tmp_path, with_stub_tools, capsys):
    assert main(["--summary"], llm=llm_with(submit(evidence=TWO_KINDS)), runs_dir=tmp_path) == 0
    out = capsys.readouterr().out
    section = out[out.index("\nREPORT\n"):]
    assert out.index("SYMPTOM SUMMARY") < out.index("DIAGNOSIS CANDIDATE") < out.index("saved to") < out.index("\nREPORT\n")
    assert "status: diagnosed" in section and "component: svc-b" in section
    assert f"(stated {GOOD_DIAGNOSIS['stated_confidence']})" in section
    assert "evidence validated 2/2" in section
    assert str(only_run_folder(tmp_path) / "report.md") in section
    assert section.splitlines()[-1].startswith("  report: ")


def test_an_escalated_run_prints_the_reason_and_exits_zero(tmp_path, with_stub_tools, capsys):
    assert main([], llm=llm_with(submit(evidence=INVENTED_EVIDENCE)), runs_dir=tmp_path) == 0
    section = capsys.readouterr().out.split("\nREPORT\n")[1]
    assert "status: escalated" in section and "escalation reason:" in section and "evidence validated 0/1" in section
