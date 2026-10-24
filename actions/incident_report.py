"""Write incident_report.json and incident_report.md into the run folder, for every path an incident can take.

Every argument except `report` may be None (no proposal, no approval, nothing executed, nothing verified).
"""
import json
from pathlib import Path

from actions.runner import KILL_TEXT
from oncall import schema as S

NA = "n/a"
JSON_FILE = "incident_report.json"
MD_FILE = "incident_report.md"

OUTCOMES = {
    "recovered": "Recovered",
    "recovered_unverified_metrics": "Recovered, but only partly verified (metrics gave no usable answer)",
    "not_recovered": "NOT recovered: escalated to a human",
    "timed_out": "Recovery check timed out: escalated to a human",
    "denied": "Approval denied: nothing was changed",
    "escalated": "Escalated to a human: no action was proposed",
    "dry_run": "Dry run: nothing was changed",
    "blocked_by_kill_switch": "Blocked by the kill switch: nothing was changed",
    "blocked": "Blocked by the safety checks: nothing was changed",
    "execution_failed": "The action failed: escalated to a human",
    "executed_not_verified": "Action executed but recovery was not checked: a human must verify",
}

# Generic and keyed only on the action type; it is not derived from this incident's data.
PREVENTION = {
    "restart_container": [
        "Run the service with a restart policy (restart: unless-stopped) so a crash heals itself.",
        "Add a health check and an alert on 'container not running' so it is caught before users see errors.",
        "Find out why the container stopped (its logs, exit code, memory limit) before relying on restarts.",
    ],
}
PREVENTION_NONE = ["Review the findings and decide the fix by hand; no automatic action applies to this incident."]


def outcome_of(report, proposal, approval, result, verification) -> str:
    if not proposal:
        return "escalated"
    decision = (approval or {}).get("decision")
    if decision == "killed":
        return "blocked_by_kill_switch"
    if decision != "approved":
        return "denied" if decision == "denied" else "escalated"
    if not result:
        return "escalated"
    if result["mode"] == "dry_run":
        return "dry_run"
    if result["mode"] == "blocked":
        return "blocked_by_kill_switch" if KILL_TEXT in (result.get("error") or "") else "blocked"
    if not result["ok"]:
        return "execution_failed"
    return verification["state"] if verification else "executed_not_verified"


def _timeline(report, proposal, approval, result, verification, given) -> dict:
    checks = (verification or {}).get("checks") or []
    executed = result["started_at"] if result and result["mode"] == "executed" else None
    decision = (approval or {}).get("decision")
    derived = {
        "detected": None,
        "investigation_start": report.get(S.STARTED_AT),
        "investigation_end": None,
        "proposed": None,
        "approved_or_denied": f"{decision} at {approval['at']}" if approval else None,
        "executed": executed,
        "verified": checks[-1]["at"] if checks else None,
    }
    merged = {**derived, **{k: v for k, v in (given or {}).items() if v}}
    return {key: merged.get(key) or NA for key in derived}


def build(report, proposal, approval, result, verification, timeline, out_dir) -> dict:
    outcome = outcome_of(report, proposal, approval, result, verification)
    action = (proposal or {}).get("action_id")
    document = {
        S.INCIDENT_ID: report.get(S.INCIDENT_ID),
        "outcome": outcome,
        "outcome_text": OUTCOMES[outcome],
        "timeline": _timeline(report, proposal, approval, result, verification, timeline),
        "root_cause": {
            S.COMPONENT: report.get(S.COMPONENT), S.CAUSE: report.get(S.CAUSE), S.STATUS: report.get(S.STATUS),
            S.CONFIDENCE: report.get(S.CONFIDENCE), S.STATED_CONFIDENCE: report.get(S.STATED_CONFIDENCE),
            S.ESCALATION_REASON: report.get(S.ESCALATION_REASON),
        },
        S.EVIDENCE: report.get(S.EVIDENCE) or [],
        "action": proposal or {"taken": False, "why_none": report.get(S.ESCALATION_REASON) or "no allowlisted action applies"},
        "approval": approval or {"decision": NA},
        "execution": result,
        "verification": verification,
        "prevention": {"kind": "generic suggestion, not derived from this incident's data",
                       "suggestions": PREVENTION.get(action, PREVENTION_NONE)},
    }
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / JSON_FILE).write_text(json.dumps(document, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    (folder / MD_FILE).write_text(render_markdown(document), encoding="utf-8")
    return document


def render_markdown(doc: dict) -> str:
    cause = doc["root_cause"]
    lines = [f"# Incident report {doc[S.INCIDENT_ID] or '(no id)'}", "", f"**Outcome: {doc['outcome_text']}**", "",
             "## Timeline", ""]
    lines += [f"- {key.replace('_', ' ')}: {value}" for key, value in doc["timeline"].items()]
    lines += ["", "## Root cause", "",
              f"- Component: {cause[S.COMPONENT]}", f"- Cause: {cause[S.CAUSE]}",
              f"- Confidence: {cause[S.CONFIDENCE]} (agent stated {cause[S.STATED_CONFIDENCE]}); status {cause[S.STATUS]}"]
    if cause[S.ESCALATION_REASON]:
        lines.append(f"- Why a human should look: {cause[S.ESCALATION_REASON]}")
    lines += ["", "## Evidence", ""]
    for e in doc[S.EVIDENCE]:
        lines.append(f"- {'✓' if e.get(S.EV_VALIDATED) else '✗'} [{e.get(S.EV_LEDGER_ID)}] {e.get(S.EV_KIND)}: {e.get(S.EV_CLAIM)}")
    if not doc[S.EVIDENCE]:
        lines.append("- none cited")

    action = doc["action"]
    lines += ["", "## Action", ""]
    if action.get("taken") is False:
        lines.append(f"- No action proposed: {action['why_none']}")
    else:
        lines += [f"- Proposed: {action['action_id']} {action['params']} (risk {action['risk']})",
                  f"- Exact command: {action['preview']}"]
    approval = doc["approval"]
    lines.append(f"- Approval: {approval['decision']}" + (f" by {approval['by']} at {approval['at']}" if "at" in approval else ""))
    execution = doc["execution"]
    if execution:
        lines.append(f"- Execution: {execution['mode']}, ok={execution['ok']}: {execution['detail']}"
                     + (f" ({execution['error']})" if execution["error"] else ""))

    lines += ["", "## Verification", ""]
    verification = doc["verification"]
    if not verification:
        lines.append("- not run")
    else:
        lines.append(f"- Result: {verification['state']} (error threshold {verification['threshold_pct']}%)")
        for check in verification["checks"]:
            lines.append(f"  - {check['at']}: {check['verdict']}; container {check['container'].get('status', check['container'].get('state'))}"
                         f", metrics {check['metrics'].get('state')}")
        lines.append(f"- {verification['note']}")

    lines += ["", f"## What would prevent a repeat ({doc['prevention']['kind']})", ""]
    lines += [f"- {s}" for s in doc["prevention"]["suggestions"]]
    return "\n".join(lines) + "\n"
