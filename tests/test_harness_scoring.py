import csv
import json
import shutil
from pathlib import Path

import pytest

from dev.harness.pricing import estimate_cost
from dev.harness.scoring import build_rows, main, write_results

FIXTURE = Path(__file__).parent / "fixtures" / "harness" / "sample"


def make_harness(tmp_path, entries):
    harness_dir = tmp_path / "reports" / "harness" / "test-run"
    harness_dir.mkdir(parents=True)
    manifest_entries = []
    for entry in entries:
        manifest_entry = {key: value for key, value in entry.items() if not key.startswith("_")}
        manifest_entries.append(manifest_entry)
        run_dir = entry.get("run_dir")
        if not run_dir:
            continue
        folder = tmp_path / run_dir
        folder.mkdir(parents=True, exist_ok=True)
        candidate = entry.get("_candidate")
        run_info = entry.get("_run_info")
        report = entry.get("_report")
        if candidate is not None:
            (folder / "candidate.json").write_text(json.dumps(candidate), encoding="utf-8")
        if run_info is not None:
            (folder / "run_info.json").write_text(json.dumps(run_info), encoding="utf-8")
        if report is not None:
            (folder / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (harness_dir / "manifest.json").write_text(json.dumps(manifest_entries), encoding="utf-8")
    return harness_dir


def entry(
    incident="test-incident",
    expected="paymentservice",
    inject_verified=True,
    baseline_clean=True,
    run_dir="reports/runs/test-run",
    *,
    component="PAYMENTSERVICE-1",
    confidence=0.8,
    report=None,
):
    result = {
        "incident": incident,
        "fault_type": "container",
        "expected_component": expected,
        "inject_verified": inject_verified,
        "baseline_clean": baseline_clean,
        "run_dir": run_dir,
        "started_at": "2026-10-24T10:00:00+05:30",
        "ended_at": "2026-10-24T10:01:00+05:30",
        "notes": "",
    }
    if run_dir:
        result["_candidate"] = {"component": component, "stated_confidence": confidence, "evidence": []}
        result["_run_info"] = {
            "steps_used": 4,
            "tokens_used": 1200,
            "tokens_input": 1000,
            "tokens_output": 200,
        }
        if report is not None:
            result["_report"] = report
    return result


def test_build_rows_scores_correct_case_insensitive_compose_name(tmp_path):
    harness_dir = make_harness(tmp_path, [entry()])
    row = build_rows(harness_dir)[0]
    assert row["correct"] == "yes"
    assert row["verdict"] == "correct"


def test_confidently_wrong_uses_candidate_confidence_without_report(tmp_path):
    harness_dir = make_harness(tmp_path, [entry(component="shipping", confidence=0.9)])
    row = build_rows(harness_dir)[0]
    assert row["correct"] == "no"
    assert row["verdict"] == "confidently wrong"
    assert row["status"] == "n/a"
    assert row["evidence_valid"] == "n/a"


def test_report_confidence_status_and_validated_evidence(tmp_path):
    report = {
        "status": "diagnosed",
        "confidence": 0.65,
        "evidence": [{"validated": True}, {"validated": False}],
    }
    harness_dir = make_harness(tmp_path, [entry(component="shipping", confidence=0.99, report=report)])
    row = build_rows(harness_dir)[0]
    assert row["stated_confidence"] == "0.65"
    assert row["status"] == "diagnosed"
    assert row["evidence_valid"] == "1/2"
    assert row["verdict"] == "wrong"


@pytest.mark.parametrize(
    ("component", "report", "verdict"),
    [
        ("paymentservice", {"status": "escalated"}, "correct but escalated"),
        ("shipping", {"status": "escalated"}, "wrong, escalated"),
    ],
)
def test_escalated_verdicts(tmp_path, component, report, verdict):
    report["confidence"] = 0.5
    harness_dir = make_harness(tmp_path, [entry(component=component, report=report)])
    assert build_rows(harness_dir)[0]["verdict"] == verdict


def test_missing_report_and_missing_run_dir_are_supported(tmp_path):
    with_run = entry()
    without_run = entry(incident="missing-run", run_dir=None)
    harness_dir = make_harness(tmp_path, [with_run, without_run])
    rows = build_rows(harness_dir)
    assert rows[0]["status"] == "n/a"
    assert rows[0]["evidence_valid"] == "n/a"
    assert rows[1]["agent_said"] == "n/a"
    assert rows[1]["verdict"] == "wrong"
    assert "run directory is missing" in rows[1]["notes"]


@pytest.mark.parametrize(
    ("inject_verified", "baseline_clean", "verdict"),
    [(False, True, "inject failed"), (False, False, "unclean baseline"), (True, False, "unclean baseline")],
)
def test_unscored_manifest_entries(tmp_path, inject_verified, baseline_clean, verdict):
    harness_dir = make_harness(
        tmp_path,
        [entry(inject_verified=inject_verified, baseline_clean=baseline_clean)],
    )
    row = build_rows(harness_dir)[0]
    assert row["verdict"] == verdict
    assert row["correct"] == "n/a"


def test_component_matching_uses_whole_words(tmp_path):
    entries = [
        entry(incident="container-form", expected="paymentservice", component="opentelemetry-demo-paymentservice-1"),
        entry(incident="substring-only", expected="payment", component="paymentservice"),
    ]
    harness_dir = make_harness(tmp_path, entries)
    rows = build_rows(harness_dir)
    assert rows[0]["correct"] == "yes"
    assert rows[1]["correct"] == "no"


def test_cost_estimation_and_output_files(tmp_path):
    harness_dir = make_harness(tmp_path, [entry()])
    csv_path, markdown_path = write_results(harness_dir)
    with csv_path.open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["cost_usd"] == "0.000200"
    assert row["tokens"] == "1200"
    assert estimate_cost(1_000_000, 1_000_000) == pytest.approx(0.6)
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Accuracy: 1/1 (100.0%)" in markdown
    assert "fewer than 3 runs" in markdown


def test_cli_writes_both_results_files(tmp_path, capsys):
    harness_dir = make_harness(tmp_path, [entry()])
    assert main([str(harness_dir)]) == 0
    output = capsys.readouterr().out
    assert "results.csv" in output
    assert "results.md" in output
    assert (harness_dir / "results.csv").is_file()
    assert (harness_dir / "results.md").is_file()


def test_calibration_buckets_and_small_sample_notice(tmp_path):
    entries = [
        entry(
            incident=f"confidence-{confidence}",
            confidence=confidence,
            run_dir=f"reports/runs/run-{confidence}",
        )
        for confidence in (0.1, 0.3, 0.6, 0.8, 1.0)
    ]
    harness_dir = make_harness(tmp_path, entries)
    _, markdown_path = write_results(harness_dir)
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "| 0-0.3 | 1 | 1 | 100.0% (fewer than 3 runs; n=1) |" in markdown
    assert "| 0.3-0.6 | 1 | 1 | 100.0% (fewer than 3 runs; n=1) |" in markdown
    assert "| 0.6-0.8 | 1 | 1 | 100.0% (fewer than 3 runs; n=1) |" in markdown
    assert "| 0.8-1.0 | 2 | 2 | 100.0% (fewer than 3 runs; n=2) |" in markdown


def test_rescoring_picks_up_report_created_after_initial_run(tmp_path):
    target = tmp_path / "reports" / "harness" / "sample"
    target.parent.mkdir(parents=True)
    shutil.copytree(FIXTURE, target)
    _, markdown_path = write_results(target)
    assert "| status |" in markdown_path.read_text(encoding="utf-8")

    run_dir = tmp_path / "reports" / "runs" / "sample-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    for name in ("candidate.json", "run_info.json"):
        shutil.copyfile(FIXTURE / "reports" / "runs" / "sample-run" / name, run_dir / name)
    (run_dir / "report.json").write_text(
        json.dumps({"status": "escalated", "confidence": 0.8, "evidence": [{"validated": True}]}),
        encoding="utf-8",
    )
    _, markdown_path = write_results(target)
    updated = markdown_path.read_text(encoding="utf-8")
    assert "correct but escalated" in updated
    assert "1/1" in updated


def test_missing_run_artifacts_are_reported_without_crashing(tmp_path):
    harness_dir = make_harness(
        tmp_path,
        [{"incident": "missing-files", "expected_component": "paymentservice", "inject_verified": True,
          "baseline_clean": True, "run_dir": "reports/runs/not-present", "notes": ""}],
    )
    row = build_rows(harness_dir)[0]
    assert row["agent_said"] == "n/a"
    assert "run data is missing" in row["notes"]
