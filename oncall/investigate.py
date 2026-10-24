"""Run one investigation from the command line.

    python -m oncall.investigate [--symptom TEXT] [--summary] [--steps N] [--token-budget N]

Prints the steps the agent took, the diagnosis candidate (NOT validated yet) and the tokens used,
and saves ledger.json, candidate.json and run_info.json (plus rejected_submissions.json when the
model's final answer was rejected) to reports/runs/<run id>/. It then validates the candidate and writes
report.json and report.md next to them (see oncall/report.py), and ends with a REPORT section.
--summary also prints a symptom summary built from the ledger: it lists what the tools showed and
cites ledger ids, and it does not say what the cause is.
Needs the demo running and a key in .env.incident (see `python -m oncall.llm --check`).
"""
import argparse
import json
import sys
from pathlib import Path

from oncall import toolbox
from oncall.llm import LLMNotConfigured, make_llm
from oncall.loop import LoopConfig, RunResult, run_investigation
from oncall import schema as S
from oncall.prompts import DEFAULT_SYMPTOM
from oncall.report import finalize
from tools.ledger import Ledger

RUNS_DIR = Path(__file__).resolve().parent.parent / "reports" / "runs"


def _trim(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _args_text(arguments: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in arguments.items())


# --- symptom summary (deterministic: built from the ledger, no extra model call) -----------

def _containers(view: dict) -> list[str]:
    s = view["summary"]
    lines = [f"{view['id']}: {s['running']} of {s['total']} containers running."]
    for c in view["abnormal"]:
        detail = [c["status"]]
        if c.get("exit_code"):
            detail.append(f"exit code {c['exit_code']}")
        if c.get("restart_count"):
            detail.append(f"{c['restart_count']} restarts")
        if c["status"] != "running" and c.get("finished_at"):
            detail.append(f"stopped {c['finished_at']}")
        if c.get("health") not in (None, "none", "healthy"):
            detail.append(f"health {c['health']}")
        lines.append(f"  - {c['name']}: {', '.join(detail)}")
    return lines


def _metrics(view: dict) -> list[str]:
    minutes = view["window"].get("minutes")
    services = view["services"]
    failing = [f"{s['service']} {s['error_rate_pct']}% errors" for s in services if s.get("error_rate_pct")]
    slowest = sorted((s for s in services if s.get("p95_ms") is not None), key=lambda s: -s["p95_ms"])[:2]
    lines = [f"{view['id']} (last {minutes} min): " + (", ".join(failing[:5]) if failing else "no service with errors") + "."]
    if slowest:
        lines.append("  - highest p95 latency: " + ", ".join(f"{s['service']} {s['p95_ms']} ms" for s in slowest))
    for span in view.get("worst_error_spans", [])[:2]:
        lines.append(f"  - erroring span: {span['service']} / {span['span_name']} [{span['span_kind']}] {span['error_rate_per_s']}/s")
    return lines


def _traces(view: dict) -> list[str]:
    groups = view.get("error_groups")
    if not groups:
        t = view["traces"][0]
        return [f"{view['id']}: slowest trace {t['trace_id']} ({t['root_service']}:{t['root_operation']}, {t['duration_ms']} ms)."]
    lines = [f"{view['id']}: {view['traces_total']} error traces; where they start:"]
    for g in groups[:3]:
        message = f" - {_trim(g['message'], 80)}" if g.get("message") else ""
        lines.append(f"  - {g['deepest_service']} / {g['deepest_span']} x{g['traces']}{message} (e.g. trace {g['example_trace_id']})")
    return lines


def _logs(view: dict) -> list[str]:
    lines = [f"{view['id']}: top error log lines:"]
    for g in view["lines"][:4]:
        lines.append(f"  - x{g['count']} {g['service']} [{g['level']}] first {g['first_seen']}: {_trim(g['message'], 100)}")
    return lines


def _deploys(view: dict) -> list[str]:
    names = [c["name"] for c in view["restarted_recently"]]
    text = ("restarted recently: " + ", ".join(names)) if names else "no container marked as restarted recently"
    return [f"{view['id']}: {text}."] + [f"  - {n}" for n in view.get("notes", [])[:1]]


SUMMARISERS = {"container_health": _containers, "query_metrics": _metrics, "query_traces": _traces,
               "query_logs": _logs, "deploy_history": _deploys}


def symptom_summary(ledger: Ledger) -> list[str]:
    entries = ledger.list()
    if not entries:
        return ["No tool results were collected."]
    lines, missing = [], []
    for entry in entries:
        view = toolbox.compact_view(entry)
        if view["state"] != "data":
            reason = view.get("error") or " ".join(view.get("notes", []))
            missing.append(f"{entry.id} {entry.tool}: {view['state']} - {_trim(reason, 120)}")
        else:
            lines += SUMMARISERS[entry.tool](view)
    if missing:
        lines.append("Not available (this is missing evidence, not a healthy signal):")
        lines += [f"  - {m}" for m in missing]
    return lines


# --- output ---------------------------------------------------------------------------------

def print_steps(steps: list[dict]) -> None:
    print("INVESTIGATION STEPS")
    if not steps:
        print("  (no tool was called)")
    for number, s in enumerate(steps, 1):
        print(f"{number:>3}. {s['tool']}({_args_text(s['arguments'])})")
        if s.get("why"):
            print(f"     why: {s['why']}")
        print(f"     -> {s['finding']}")


def print_candidate(candidate: dict) -> None:
    print("DIAGNOSIS CANDIDATE (not validated)")
    print(f"  component: {candidate['component']}    stated confidence: {candidate['stated_confidence']}")
    print(f"  cause: {candidate['cause']}")
    for e in candidate["evidence"]:
        value = f" ({e['value']})" if e.get("value") else ""
        print(f"  evidence [{e['ledger_id']}] {e['kind']}: {e['claim']}\n      quote: {e['quote']!r}{value}")
    for a in candidate["alternatives_considered"]:
        print(f"  rejected: {a['hypothesis']} - {a['why_rejected']}")
    for note in candidate["limitations"]:
        print(f"  limitation: {note}")


def save_run(result: RunResult, runs_dir: Path = RUNS_DIR) -> Path:
    folder = Path(runs_dir) / result.run_info["incident_id"]
    result.ledger.save(folder / "ledger.json")
    (folder / "candidate.json").write_text(json.dumps(result.candidate, indent=2) + "\n", encoding="utf-8")
    (folder / "run_info.json").write_text(json.dumps(result.run_info, indent=2) + "\n", encoding="utf-8")
    if result.rejected:
        (folder / "rejected_submissions.json").write_text(
            json.dumps(result.rejected, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    return folder


def failed_report(result: RunResult, reason: str) -> dict:
    """The escalated stand-in returned when finalize raised, so callers always get a report dict."""
    info = result.run_info
    report = {name: None for name in S.REPORT_FIELDS}
    report.update({
        S.INCIDENT_ID: info["incident_id"], S.STARTED_AT: info["started_at"], S.TRIGGER: info["trigger"],
        S.STATUS: S.STATUS_ESCALATED, S.COMPONENT: result.candidate[S.COMPONENT], S.CAUSE: result.candidate[S.CAUSE],
        S.CONFIDENCE: 0.0, S.STATED_CONFIDENCE: result.candidate[S.STATED_CONFIDENCE], S.CONFIDENCE_REASONS: [],
        S.EVIDENCE: [], S.ALTERNATIVES: [], S.STEPS: [], S.LIMITATIONS: [reason], S.ESCALATION_REASON: reason,
        S.WARNINGS: [],
    })
    return report


def investigate(symptom: str = DEFAULT_SYMPTOM, llm=None, config: LoopConfig | None = None,
                runs_dir: Path = RUNS_DIR, details: dict | None = None) -> dict:
    """Investigate `symptom`, save the run and return the final report dict (status diagnosed or escalated).

    A failure to build the report is printed as one line and never loses the saved run files; the returned
    report is then an escalated stand-in. `details`, when given, is filled with the RunResult ("result"), the
    run folder ("folder") and the error text ("report_error", None when the report was written)."""
    llm = llm if llm is not None else make_llm()
    result = run_investigation(llm, symptom, config or LoopConfig())
    folder = save_run(result, runs_dir)
    report_error = None
    try:
        report = finalize(result.candidate, result.ledger, result.run_info, folder)
    except Exception as exc:
        report_error = f"{type(exc).__name__}: {exc}"
        print(f"report failed: {report_error} (ledger.json, candidate.json and run_info.json were kept)", file=sys.stderr)
        report = failed_report(result, f"the report could not be written ({report_error})")
    if details is not None:
        details.update(result=result, folder=folder, report_error=report_error)
    return report


def print_report(report: dict, folder: Path, report_error: str | None) -> None:
    print("\nREPORT")
    print(f"  status: {report[S.STATUS]}")
    print(f"  component: {report[S.COMPONENT]}")
    print(f"  confidence: {report[S.CONFIDENCE]:.2f} (stated {report[S.STATED_CONFIDENCE]})")
    for reason in report[S.CONFIDENCE_REASONS]:
        print(f"    - {reason}")
    if report[S.ESCALATION_REASON]:
        print(f"  escalation reason: {report[S.ESCALATION_REASON]}")
    evidence = report[S.EVIDENCE]
    print(f"  evidence validated {sum(1 for e in evidence if e[S.EV_VALIDATED])}/{len(evidence)}")
    if report_error:
        print(f"  report files not written: {report_error}")
    else:
        print(f"  report: {folder / S.REPORT_MD_FILE}")


def main(argv: list[str] | None = None, llm=None, runs_dir: Path = RUNS_DIR) -> int:
    parser = argparse.ArgumentParser(description="Investigate the running application with an LLM and read-only tools")
    parser.add_argument("--symptom", default=DEFAULT_SYMPTOM, help="the problem report the agent starts from")
    parser.add_argument("--summary", action="store_true", help="also print a symptom summary built from the ledger")
    defaults = LoopConfig()
    parser.add_argument("--steps", type=int, default=defaults.step_budget, help="tool-call budget")
    parser.add_argument("--token-budget", type=int, default=defaults.token_budget, help="input+output token budget")
    args = parser.parse_args(argv)

    if llm is None:
        try:
            llm = make_llm()
        except LLMNotConfigured as exc:
            print(f"cannot start: {exc}", file=sys.stderr)
            return 2
    config = LoopConfig(token_budget=args.token_budget, step_budget=args.steps)
    details: dict = {}
    report = investigate(args.symptom, llm, config, runs_dir, details)
    result, folder = details["result"], details["folder"]

    info = result.run_info
    print(f"RUN {info['incident_id']}  started {info['started_at']}")
    print(f"symptom given: {info['trigger']}\n")
    print_steps(result.steps)
    if args.summary:
        print("\nSYMPTOM SUMMARY")
        print("\n".join(f"  {line}" for line in symptom_summary(result.ledger)))
    print()
    print_candidate(result.candidate)
    print(f"\nsteps used: {info['steps_used']}/{info['step_budget']}   stop reason: {info['stop_reason']}"
          f"   budget exhausted: {info['budget_exhausted']}   llm error: {info['llm_error']}")
    print(f"tokens used: {info['tokens_used']} of {info['token_budget']} (input {info['tokens_input']}, output {info['tokens_output']})")
    if result.rejected:
        print(f"rejected submissions: {len(result.rejected)} (see rejected_submissions.json)")
    print(f"saved to {folder}")
    print_report(report, folder, details["report_error"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
