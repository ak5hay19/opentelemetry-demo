"""One incident from alert to report: investigate, propose, ask a human, act, verify, write the incident report.

    python -m actions.run_incident --alert "TEXT" [--execute] [--no-act]

Without --execute even an approved action is only a dry run: nothing is changed. --no-act stops after the
proposal. The kill switch is read at every step: when it is engaged the investigation still runs (it only
reads), but no proposal can be approved and nothing is executed.
Exit code 0 for every handled outcome (escalation, denial, a failed or unconfirmed recovery are all recorded
in the incident report); 1 only when the run itself broke (no LLM configured, the investigation crashed,
a file could not be written); 2 for bad arguments.
"""
import argparse
import sys
import time
from pathlib import Path

from actions import approval, audit, incident_report, killswitch, registry, runner, verify
from oncall import schema as S
from oncall.investigate import RUNS_DIR, investigate
from oncall.llm import LLMNotConfigured
from tools.containers import container_health
from tools.metrics import query_metrics
from tools.result import iso, utc_now


def _failure(message: str) -> dict:
    print(f"run failed: {message}", file=sys.stderr)
    return {"exit_code": 1, "outcome": "failed", "error": message}


def _print_proposal(proposal: dict) -> None:
    print("\nPROPOSAL")
    print(f"  action: {proposal['action_id']} {proposal['params']}  (risk {proposal['risk']})")
    print(f"  exact command: {proposal['preview']}")


def _print_execution(result: dict) -> None:
    if result["mode"] == "dry_run":
        print("\nDRY RUN: nothing was changed. The action was approved, but --execute was not given, "
              "so the command was NOT run.")
        print(f"  {result['detail']}")
        print("  Run again with --execute to really do it.")
    elif result["mode"] == "blocked":
        print(f"\nBLOCKED: nothing was changed. {result['error']}")
    elif not result["ok"]:
        print(f"\nACTION FAILED: {result['error']}")
    else:
        print(f"\nEXECUTED: {result['detail']}")


def _print_verification(verification: dict) -> None:
    print(f"\nRECOVERY CHECK: {verification['state']}")
    print(f"  {verification['detail']}")
    if verification["escalate"]:
        print(f"  ESCALATE TO A HUMAN. {verification['note']}")


def _taken(proposal, result) -> str:
    if not proposal:
        return "none (no action was proposed)"
    text = f"{proposal['action_id']} {proposal['params']}"
    if result and result["mode"] == "executed" and result["ok"]:
        return f"{text}: executed ({proposal['preview']})"
    if result and result["mode"] == "executed":
        return f"{text}: tried but failed ({proposal['preview']})"
    return f"none (proposed {text}, nothing was changed)"


def run(alert, *, execute=False, act=True, detected_at=None, investigate_fn=investigate, input_fn=input,
        command_runner=None, health=container_health, metrics=query_metrics, clock=time.monotonic,
        sleep=time.sleep, now=utc_now, actions_dir=None, runs_dir=RUNS_DIR) -> dict:
    """Handle one alert. Returns {"exit_code", "outcome", "folder", "document"}; every dependency can be injected."""
    timeline = {"detected": detected_at or iso(now())}
    print(f"alert: {alert}")
    if killswitch.is_engaged(actions_dir):
        print("KILL SWITCH ENGAGED: investigating read-only; nothing can be approved or executed.")

    details: dict = {}
    try:
        report = investigate_fn(alert, details=details)
    except LLMNotConfigured as exc:
        return _failure(f"cannot start the investigation: {exc}")
    except Exception as exc:
        return _failure(f"the investigation crashed: {type(exc).__name__}: {exc}")
    if not isinstance(report, dict):
        return _failure("the investigation returned no report")
    timeline["investigation_end"] = iso(now())
    folder = Path(details.get("folder") or Path(runs_dir) / str(report.get(S.INCIDENT_ID) or "unknown"))

    print(f"\nDIAGNOSIS: {report.get(S.STATUS)}  component {report.get(S.COMPONENT)}  "
          f"confidence {report.get(S.CONFIDENCE)}")
    if report.get(S.ESCALATION_REASON):
        print(f"  escalation reason: {report[S.ESCALATION_REASON]}")

    proposal = approved = result = verification = None
    if report.get(S.STATUS) == S.STATUS_ESCALATED:
        print("\nThe agent escalated this incident to a human. No action is proposed.")
    else:
        proposal, why = registry.plan(report, health)
        if proposal is None:
            print(f"\nNo action proposed: {why}")
        else:
            timeline["proposed"] = iso(now())
            _print_proposal(proposal)

    if proposal is not None and not act:
        print("\n--no-act: stopping after the proposal. Nothing was asked, run or written.")
        return {"exit_code": 0, "outcome": "proposal_only", "folder": folder, "document": None}

    try:
        if proposal is not None:
            approved = approval.ask(report, proposal, input_fn=input_fn, actions_dir=actions_dir)
            if approved["decision"] == "approved":
                result = runner.execute(proposal, dry_run=not execute, runner=command_runner, health=health,
                                        actions_dir=actions_dir)
                _print_execution(result)
                if result["mode"] == "executed" and result["ok"]:
                    print(f"\nChecking recovery of {proposal['params']['container']} (read-only)...")
                    verification = verify.check_recovery(proposal, clock=clock, sleep=sleep, health=health,
                                                         metrics=metrics)
                    _print_verification(verification)
            elif approved["decision"] == "killed":
                print("\nKILL SWITCH ENGAGED: the action was not approved and nothing was run.")
            else:
                print("\nApproval denied: nothing was changed.")
        document = incident_report.build(report, proposal, approved, result, verification, timeline, folder)
    except OSError as exc:
        return _failure(f"a file could not be read or written: {exc}")

    audit_path = Path(actions_dir or killswitch.ACTIONS_DIR) / audit.AUDIT_FILE
    print("\nINCIDENT SUMMARY")
    print(f"  outcome: {document['outcome_text']}")
    print(f"  action taken: {_taken(proposal, result)}")
    print(f"  verification: {verification['state'] if verification else 'not run'}")
    print(f"  incident report: {folder / incident_report.MD_FILE}")
    print(f"  audit log: {audit_path}")
    return {"exit_code": 0, "outcome": document["outcome"], "folder": folder, "document": document}


def main(argv: list[str] | None = None, *, detected_at: str | None = None, **dependencies) -> int:
    parser = argparse.ArgumentParser(
        description="Investigate an alert, propose one allowlisted action, ask for approval, act and verify")
    parser.add_argument("--alert", required=True, help="the alert text the investigation starts from")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true",
                      help="really run an approved action (without it an approved action is only a dry run)")
    mode.add_argument("--no-act", action="store_true", help="stop after the proposal: ask nothing, run nothing")
    args = parser.parse_args(argv)
    if not args.alert.strip():
        parser.error("--alert must not be empty")
    outcome = run(args.alert.strip(), execute=args.execute, act=not args.no_act, detected_at=detected_at, **dependencies)
    return outcome["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
