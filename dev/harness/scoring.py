"""Score saved harness runs without importing or executing agent code."""
import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

from dev.harness.pricing import estimate_cost

COLUMNS = [
    "incident",
    "fault_type",
    "expected",
    "agent_said",
    "correct",
    "stated_confidence",
    "status",
    "evidence_valid",
    "steps",
    "tokens",
    "cost_usd",
    "verdict",
    "notes",
]
CONFIDENCE_BUCKETS = (
    (0.0, 0.3, "0-0.3"),
    (0.3, 0.6, "0.3-0.6"),
    (0.6, 0.8, "0.6-0.8"),
    (0.8, 1.0, "0.8-1.0"),
)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc


def _confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0 <= number <= 1 else None


def _display_confidence(value: float | None) -> str:
    return f"{value:g}" if value is not None else "n/a"


def _component_matches(expected: str, actual: str) -> bool:
    """Match case-insensitively when the expected service is a whole word in the diagnosis.

    This accepts compose/container forms such as ``paymentservice-1`` while rejecting
    unrelated names that merely share a substring.
    """
    if not expected.strip() or not actual.strip():
        return False
    pattern = rf"(?<!\w){re.escape(expected.strip())}(?!\w)"
    return re.search(pattern, actual, flags=re.IGNORECASE) is not None


def _as_object(value: Any, description: str, path: Path) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{description} in {path} must be a JSON object")
    return value


def _read_evidence_validity(report: dict) -> str:
    evidence = report.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return "n/a"
    validations = [
        item["validated"]
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("validated"), bool)
    ]
    if len(validations) != len(evidence):
        return "n/a"
    return f"{sum(validations)}/{len(validations)}"


def _load_run(run_dir_value: Any, base_dir: Path, notes: list[str]) -> tuple[dict, dict, dict | None]:
    if not isinstance(run_dir_value, str) or not run_dir_value.strip():
        notes.append("run directory is missing")
        return {}, {}, None

    run_dir = Path(run_dir_value)
    if not run_dir.is_absolute():
        repository_root = base_dir.parents[2]
        run_dir = repository_root / run_dir
    candidate_path = run_dir / "candidate.json"
    info_path = run_dir / "run_info.json"
    if not candidate_path.exists() or not info_path.exists():
        missing = [path.name for path in (candidate_path, info_path) if not path.exists()]
        notes.append(f"run data is missing: {', '.join(missing)}")
        return {}, {}, None
    candidate = _as_object(_read_json(candidate_path), "candidate", candidate_path)
    run_info = _as_object(_read_json(info_path), "run info", info_path)

    report_path = run_dir / "report.json"
    if not report_path.exists():
        return candidate, run_info, None
    report = _as_object(_read_json(report_path), "report", report_path)
    return candidate, run_info, report


def _tokens_value(run_info: dict) -> str:
    tokens = run_info.get("tokens_used")
    if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
        return str(tokens)
    input_tokens, output_tokens = run_info.get("tokens_input"), run_info.get("tokens_output")
    if all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in (input_tokens, output_tokens)):
        return str(input_tokens + output_tokens)
    return "n/a"


def _cost_value(run_info: dict) -> str:
    input_tokens, output_tokens = run_info.get("tokens_input"), run_info.get("tokens_output")
    if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in (input_tokens, output_tokens)):
        return "n/a"
    return f"{estimate_cost(input_tokens, output_tokens):.6f}"


def _build_row(entry: dict, base_dir: Path) -> tuple[dict[str, str], float | None, float | None]:
    notes: list[str] = []
    candidate, run_info, report = _load_run(entry.get("run_dir"), base_dir, notes)
    expected = entry.get("expected_component")
    expected = expected.strip() if isinstance(expected, str) else ""
    agent_said = candidate.get("component")
    agent_said = agent_said.strip() if isinstance(agent_said, str) and agent_said.strip() else "n/a"

    confidence_source = report.get("confidence") if report is not None else candidate.get("stated_confidence")
    confidence = _confidence(confidence_source)
    if report is not None and confidence is None:
        confidence = _confidence(candidate.get("stated_confidence"))

    baseline_clean = entry.get("baseline_clean") is True
    inject_verified = entry.get("inject_verified") is True
    eligible = baseline_clean and inject_verified
    correct_value: bool | None = _component_matches(expected, agent_said) if eligible else None
    status = report.get("status") if report is not None and isinstance(report.get("status"), str) else "n/a"
    evidence_valid = _read_evidence_validity(report) if report is not None else "n/a"

    if not baseline_clean:
        verdict = "unclean baseline"
    elif not inject_verified:
        verdict = "inject failed"
    elif report is not None and status == "escalated":
        verdict = "correct but escalated" if correct_value else "wrong, escalated"
    elif correct_value:
        verdict = "correct"
    elif confidence is not None and confidence >= 0.7:
        verdict = "confidently wrong"
    else:
        verdict = "wrong"

    if not expected:
        notes.append("expected component is missing")
    entry_notes = entry.get("notes")
    if isinstance(entry_notes, str) and entry_notes.strip():
        notes.insert(0, entry_notes.strip())
    if report is not None and isinstance(report.get("escalation_reason"), str) and report["escalation_reason"].strip():
        notes.append(f"escalation: {report['escalation_reason'].strip()}")

    steps = run_info.get("steps_used")
    steps_value = str(steps) if isinstance(steps, (int, float)) and not isinstance(steps, bool) else "n/a"
    input_tokens, output_tokens = run_info.get("tokens_input"), run_info.get("tokens_output")
    estimated = None
    if all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in (input_tokens, output_tokens)):
        estimated = estimate_cost(input_tokens, output_tokens)

    row = {
        "incident": str(entry.get("incident") or "n/a"),
        "fault_type": str(entry.get("fault_type") or "n/a"),
        "expected": expected or "n/a",
        "agent_said": agent_said,
        "correct": "yes" if correct_value is True else "no" if correct_value is False else "n/a",
        "stated_confidence": _display_confidence(confidence),
        "status": status,
        "evidence_valid": evidence_valid,
        "steps": steps_value,
        "tokens": _tokens_value(run_info),
        "cost_usd": _cost_value(run_info),
        "verdict": verdict,
        "notes": "; ".join(notes),
    }
    return row, confidence, estimated


def build_rows(manifest_dir: str | Path) -> list[dict[str, str]]:
    """Build scoring rows from a harness manifest and its saved run folders."""
    directory = Path(manifest_dir)
    manifest_path = directory / "manifest.json"
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, list):
        raise ValueError(f"manifest in {manifest_path} must be a JSON list")

    rows = []
    for index, item in enumerate(manifest):
        if not isinstance(item, dict):
            raise ValueError(f"manifest entry {index} in {manifest_path} must be a JSON object")
        row, _, _ = _build_row(item, directory)
        rows.append(row)
    return rows


def _summary(rows: list[dict[str, str]], details: list[tuple[float | None, float | None]]) -> list[str]:
    scored = [row for row in rows if row["verdict"] not in {"inject failed", "unclean baseline"}]
    correct_count = sum(row["correct"] == "yes" for row in scored)
    accuracy = correct_count / len(scored) if scored else None
    confident_wrong = sum(row["verdict"] == "confidently wrong" for row in rows)
    correct_escalated = sum(row["verdict"] == "correct but escalated" for row in rows)
    costs = [cost for _, cost in details if cost is not None]
    average_cost = sum(costs) / len(costs) if costs else None

    lines = [
        "## Summary",
        "",
        f"- Accuracy: {correct_count}/{len(scored)} ({accuracy:.1%})" if accuracy is not None
        else "- Accuracy: n/a (no scored incidents)",
        f"- Confidently wrong: {confident_wrong}",
        f"- Correct but escalated: {correct_escalated}",
        f"- Average estimated cost: ${average_cost:.6f}" if average_cost is not None
        else "- Average estimated cost: n/a",
        "",
        "### Confidence calibration",
        "",
        "| Confidence | Runs | Correct | Fraction correct |",
        "|---|---:|---:|---:|",
    ]
    for lower, upper, label in CONFIDENCE_BUCKETS:
        bucket = [
            row for row, (confidence, _) in zip(rows, details)
            if confidence is not None
            and (lower <= confidence < upper or (upper == 1.0 and confidence == 1.0))
            and row["verdict"] not in {"inject failed", "unclean baseline"}
        ]
        count_correct = sum(row["correct"] == "yes" for row in bucket)
        fraction = f"{count_correct / len(bucket):.1%}" if bucket else "n/a"
        if len(bucket) < 3:
            fraction += f" (fewer than 3 runs; n={len(bucket)})"
        lines.append(f"| {label} | {len(bucket)} | {count_correct} | {fraction} |")
    return lines


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def write_results(manifest_dir: str | Path) -> tuple[Path, Path]:
    directory = Path(manifest_dir)
    manifest_path = directory / "manifest.json"
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, list):
        raise ValueError(f"manifest in {manifest_path} must be a JSON list")

    rows: list[dict[str, str]] = []
    details: list[tuple[float | None, float | None]] = []
    for index, item in enumerate(manifest):
        if not isinstance(item, dict):
            raise ValueError(f"manifest entry {index} in {manifest_path} must be a JSON object")
        row, confidence, cost = _build_row(item, directory)
        rows.append(row)
        details.append((confidence, cost))

    csv_path = directory / "results.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    markdown_path = directory / "results.md"
    lines = [
        "| " + " | ".join(COLUMNS) + " |",
        "|" + "|".join("---" for _ in COLUMNS) + "|",
    ]
    lines.extend("| " + " | ".join(_markdown_cell(row[column]) for column in COLUMNS) + " |" for row in rows)
    lines.extend(["", *_summary(rows, details), ""])
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, markdown_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score a saved harness run.")
    parser.add_argument("harness_dir", type=Path, help="directory containing manifest.json")
    args = parser.parse_args(argv)
    try:
        csv_path, markdown_path = write_results(args.harness_dir)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Wrote {csv_path}")
    print(f"Wrote {markdown_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
