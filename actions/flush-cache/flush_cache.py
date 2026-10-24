"""Explicitly approved, allowlisted flush for the demo cart Valkey database."""
from __future__ import annotations

import argparse
import hashlib
import json

from actions import approval, audit, killswitch, registry, runner as action_runner
from tools.containers import container_health

from actions.registry import FLUSH_CACHE, FLUSH_CACHE_COMMAND, FLUSH_CACHE_CONTAINER
from actions.runner import COMMAND_TIMEOUT_S

ACTION_ID = FLUSH_CACHE
CONTAINER = FLUSH_CACHE_CONTAINER
COMMAND = FLUSH_CACHE_COMMAND
RISK = "high: clears all cart data in the dedicated valkey-cart database"


def _canonical_hash(proposal: dict) -> str:
    body = json.dumps(proposal, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _proposal(incident_id: str) -> dict:
    return {
        "incident_id": incident_id,
        "action_id": ACTION_ID,
        "component": CONTAINER,
        "params": {"container": CONTAINER, "scope": "database"},
        "reason": "Clear the dedicated demo cart cache database.",
        "risk": RISK,
        "preview": " ".join(COMMAND),
    }


def _result(mode: str, ok: bool, detail: str, *, error: str | None = None) -> dict:
    return {"ok": ok, "mode": mode, "action_id": ACTION_ID, "container": CONTAINER,
            "scope": "database", "detail": detail, "error": error}

def run(
    incident_id: str,
    *,
    execute: bool = False,
    approval_fn=approval.ask,
    command_runner=None,
    health=container_health,
    actions_dir=None,
    timeout: int = COMMAND_TIMEOUT_S,
) -> dict:
    """Ask approval, then dry-run by default or execute the one fixed command."""
    if not isinstance(incident_id, str) or not incident_id.strip():
        return _result("blocked", False, "incident_id is required", error="missing incident_id")
    incident_id = incident_id.strip()
    proposal = _proposal(incident_id)
    proposal_hash = _canonical_hash(proposal)

    if killswitch.is_engaged(actions_dir):
        return _result("blocked", False, "not executed", error="kill switch engaged; nothing was run")
    _, validation_error = registry.validate(proposal, health)
    if validation_error:
        return _result("blocked", False, "not executed", error=f"proposal rejected: {validation_error}")
    if any(entry.get("incident_id") == incident_id and entry.get("event") == "execute_started"
           for entry in audit.read(actions_dir)):
        return _result("blocked", False, "not executed", error="this incident already executed an action")

    report = {"incident_id": incident_id, "component": CONTAINER, "cause": proposal["reason"]}
    decision = approval_fn(report, proposal, actions_dir=actions_dir)
    if not isinstance(decision, dict) or decision.get("decision") != "approved":
        return _result("rejected", False, "human approval was not granted; nothing was run",
                       error="approval denied or unavailable")

    if _canonical_hash(proposal) != proposal_hash:
        return _result("blocked", False, "not executed", error="proposal changed after it was presented for approval")

    result = action_runner.execute(
        proposal,
        dry_run=not execute,
        runner=command_runner,
        health=health,
        actions_dir=actions_dir,
        timeout=timeout,
    )
    detail = result["detail"]
    if execute and result["ok"] and result["mode"] == "executed":
        detail = "flush command completed; cache recovery has not been verified"
    return _result(result["mode"], result["ok"], detail, error=result["error"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Human-approved flush of the demo cart cache")
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--execute", action="store_true", help="run the command after explicit approval")
    args = parser.parse_args(argv)
    result = run(args.incident_id, execute=args.execute)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
