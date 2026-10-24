"""The approval gate: a human says yes or no to a proposed fix. Nothing is executed here.

It reads proposal.json and writes decision.json beside it. The decision records the hash of the exact
proposal it covers, so the Action API can refuse to run anything that was changed after approval.

  1. escalation: a proposal with status "no_proposal" has nothing to approve, so nobody is asked
  2. re-check:   the proposal is validated against the runbook again, because proposal.json is only a file
  3. ask:        the human sees the proposal and must type exactly "yes"; anything else, or no input, is a no

Run:  python -m oncall.approve [--run RUN_ID | --proposal PATH]      (default: the newest run in reports/runs)
"""
import argparse
import getpass
import hashlib
import json
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from oncall.suggest import RUNS_DIR, Proposal, ProposalError, check_choice, print_proposal

APPROVED = "approved"
REJECTED = "rejected"
NOT_APPLICABLE = "not_applicable"
YES = "yes"


@dataclass
class Decision:
    status: str  # "approved" | "rejected" | "not_applicable"
    reason: str
    proposal_sha256: str
    decided_by: str
    decided_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def proposal_hash(proposal: dict) -> str:
    canonical = json.dumps(proposal, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_approved_for(decision: dict, proposal: dict) -> bool:
    """True only when the decision is an approval of exactly this proposal. The Action API should check this."""
    return decision.get("status") == APPROVED and decision.get("proposal_sha256") == proposal_hash(proposal)


def recheck(proposal: dict) -> str | None:
    """Why the proposal no longer passes the runbook rules, or None when it is still valid."""
    based_on = proposal.get("based_on")
    if not isinstance(based_on, dict) or not based_on.get("component"):
        return "the proposal does not say which component was diagnosed"
    args = {"action": proposal.get("action"), "target": proposal.get("target"), **(proposal.get("arguments") or {}),
            "reason": proposal.get("reason"), "expected_recovery": proposal.get("expected_recovery"),
            "risk": proposal.get("risk")}
    try:
        check_choice(args, {"component": str(based_on["component"])})
    except ProposalError as exc:
        return str(exc)
    return None


def _decision(status: str, reason: str, proposal: dict) -> Decision:
    return Decision(status, reason, proposal_hash(proposal), getpass.getuser(),
                    datetime.now(timezone.utc).isoformat(timespec="seconds"))


def decide(proposal: dict, ask: Callable[[str], str]) -> Decision:
    if proposal.get("status") != "proposed":
        return _decision(NOT_APPLICABLE, "there is no proposed action, so nothing to approve", proposal)
    problem = recheck(proposal)
    if problem:
        return _decision(REJECTED, f"the proposal failed validation and was not shown: {problem}", proposal)
    try:
        answer = ask(f"Run this action? Type '{YES}' to approve, anything else rejects: ")
    except (EOFError, KeyboardInterrupt):
        return _decision(REJECTED, "no answer was given", proposal)
    if answer.strip().lower() == YES:
        return _decision(APPROVED, "approved by the human", proposal)
    return _decision(REJECTED, "rejected by the human", proposal)


# --- command line ----------------------------------------------------------------------------

def _latest_run(runs_dir: Path) -> Path:
    runs = sorted((p for p in runs_dir.iterdir() if (p / "proposal.json").exists()),
                  key=lambda p: (p / "proposal.json").stat().st_mtime)
    if not runs:
        raise FileNotFoundError(f"no run with a proposal.json in {runs_dir}")
    return runs[-1]


def main(argv: list[str] | None = None, ask: Callable[[str], str] = input, runs_dir: Path = RUNS_DIR) -> int:
    parser = argparse.ArgumentParser(description="Ask a human to approve or reject a proposed fix (never runs it)")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--run", help="run id under reports/runs")
    source.add_argument("--proposal", help="path to a proposal.json")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    try:
        path = Path(args.proposal) if args.proposal else (Path(runs_dir) / args.run if args.run else _latest_run(Path(runs_dir)))
        if path.is_dir():
            path = path / "proposal.json"
        proposal = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(proposal, dict):
            raise ValueError("proposal.json is not a JSON object")
    except (OSError, ValueError) as exc:
        print(f"cannot read the proposal: {exc}", file=sys.stderr)
        return 2

    if proposal.get("status") == "proposed" and recheck(proposal) is None:
        print_proposal(Proposal(**proposal))
    decision = decide(proposal, ask)
    print(f"\n{decision.status.upper()}: {decision.reason}")
    out = path.parent / "decision.json"
    out.write_text(json.dumps(decision.to_dict(), indent=2) + "\n", encoding="utf-8")
    print(f"saved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
