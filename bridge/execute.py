"""The bridge from an approved proposal to the Action API (actions/). It adds nothing to actions/ or oncall/.

It lives in its own package because oncall/ may never import actions/ (tests/test_actions.py enforces that).

    python -m bridge.execute [--run RUN_ID | --run-dir PATH] [--execute] [--no-verify]

Reads proposal.json and decision.json from a run folder. Only a proposal that a human approved, unchanged,
goes further. It is translated into the Action API's own proposal by actions.registry.plan (so the Action API's
allowlist, deny list and container lookup decide), then handed to actions.runner.execute, which checks the kill
switch, keeps the audit log and runs nothing unless --execute is given. After a real run the recovery is checked
with actions.verify. Everything is saved to execution.json beside the proposal.

Translation of the suggester's actions:
    restart_service  ->  the Action API's restart_container
    throttle_load    ->  bridge.throttle (the Action API cannot do this one; the bridge applies the same kill switch,
                         dry run and audit log itself)
Anything else has no action yet, so it stays a recommendation for a human.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import requests

from actions import registry, runner, verify
from bridge import throttle
from oncall import approve
from oncall import schema as S
from oncall.suggest import RUNS_DIR
from tools.containers import container_health

ACTION_MAP = {"restart_service": registry.RESTART}


def to_action_proposal(proposal: dict, incident_id: str, health=container_health) -> tuple[dict | None, str]:
    """The Action API's proposal for this one, or (None, why it cannot be run)."""
    action = proposal.get("action")
    if action not in ACTION_MAP:
        return None, f"the Action API has no action for {action!r} yet; a human has to do this one"
    report = {S.INCIDENT_ID: incident_id, S.STATUS: S.STATUS_DIAGNOSED,
              S.COMPONENT: proposal.get("target"), S.CAUSE: proposal.get("reason")}
    return registry.plan(report, health)


def _incident_id(folder: Path) -> str:
    try:
        return json.loads((folder / "run_info.json").read_text(encoding="utf-8"))["incident_id"]
    except (OSError, ValueError, KeyError, TypeError):
        return folder.name


def _load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return data


def run(folder: Path, *, execute: bool = False, check_recovery: bool = True, health=container_health,
        command_runner=subprocess.run, verify_fn=verify.check_recovery, actions_dir=None,
        http_get=requests.get, http_post=requests.post, loadgen=None) -> dict:
    """Execute the approved proposal in a run folder. Returns {"outcome", "reason", ...} and saves execution.json."""
    folder = Path(folder)
    out = {"outcome": "refused", "reason": "", "action_proposal": None, "result": None, "verification": None}

    def finish(outcome: str, reason: str = "") -> dict:
        out["outcome"], out["reason"] = outcome, reason
        (folder / "execution.json").write_text(json.dumps(out, indent=2, default=str) + "\n", encoding="utf-8")
        return out

    try:
        proposal = _load(folder / "proposal.json")
    except (OSError, ValueError) as exc:
        return finish("refused", f"cannot read proposal.json: {exc}")
    try:
        decision = _load(folder / "decision.json")
    except (OSError, ValueError):
        return finish("refused", "no decision.json: the proposal has not been through the approval step")

    if proposal.get("status") != "proposed":
        return finish("refused", "there is no proposed action to run")
    if not approve.is_approved_for(decision, proposal):
        why = decision.get("reason") or decision.get("status")
        return finish("refused", f"not approved for exactly this proposal ({why})")

    incident_id = _incident_id(folder)
    if proposal.get("action") == throttle.THROTTLE:
        action_proposal, why = throttle.plan(proposal, incident_id, url=loadgen, http_get=http_get)
        verify_target = {"params": {"container": "load-generator"}}  # recovery = the shop's error rates, not the generator
        act = lambda a: throttle.execute(a, dry_run=not execute, url=loadgen, http_post=http_post, actions_dir=actions_dir)
    else:
        action_proposal, why = to_action_proposal(proposal, incident_id, health)
        verify_target = action_proposal
        act = lambda a: runner.execute(a, dry_run=not execute, runner=command_runner, health=health, actions_dir=actions_dir)
    if action_proposal is None:
        return finish("refused", why)
    out["action_proposal"] = action_proposal

    result = act(action_proposal)
    out["result"] = result
    if result["mode"] == "blocked":
        return finish("blocked", result["error"])
    if result["mode"] == "dry_run":
        return finish("dry_run", result["detail"])
    if not result["ok"]:
        return finish("failed", result["error"])
    if check_recovery:
        out["verification"] = verify_fn(verify_target, health=health)
        return finish("executed_" + out["verification"]["state"], out["verification"]["detail"])
    return finish("executed", result["detail"])


def _latest_run(runs_dir: Path) -> Path:
    runs = sorted((p for p in runs_dir.iterdir() if (p / "decision.json").exists()),
                  key=lambda p: (p / "decision.json").stat().st_mtime)
    if not runs:
        raise FileNotFoundError(f"no run with a decision.json in {runs_dir}")
    return runs[-1]


def main(argv: list[str] | None = None, runs_dir: Path = RUNS_DIR, **dependencies) -> int:
    parser = argparse.ArgumentParser(description="Run an approved fix through the Action API (dry run unless --execute)")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--run", help="run id under reports/runs")
    source.add_argument("--run-dir", help="path to a run folder")
    parser.add_argument("--execute", action="store_true", help="really run the command; without it nothing is changed")
    parser.add_argument("--no-verify", action="store_true", help="skip the recovery check after executing")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    try:
        folder = Path(args.run_dir) if args.run_dir else (Path(runs_dir) / args.run if args.run else _latest_run(Path(runs_dir)))
    except (OSError, FileNotFoundError) as exc:
        print(f"cannot find a run: {exc}", file=sys.stderr)
        return 2
    if not folder.is_dir():
        print(f"not a run folder: {folder}", file=sys.stderr)
        return 2

    out = run(folder, execute=args.execute, check_recovery=not args.no_verify, **dependencies)
    action = out["action_proposal"]
    if action:
        print(f"ACTION   {action['action_id']} {action['params']}   command: {action['preview']}")
    print(f"OUTCOME  {out['outcome']}" + (f": {out['reason']}" if out["reason"] else ""))
    if out["outcome"] == "dry_run":
        print("  nothing was changed. Add --execute to run the command.")
    if out["verification"] and out["verification"]["escalate"]:
        print("  recovery NOT confirmed: escalate to a human.")
    print(f"saved to {folder / 'execution.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
