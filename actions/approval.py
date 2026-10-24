"""Human approval gate. Only an explicit "y" or "yes" approves; anything else is a no."""
import getpass
import sys

from actions import audit, killswitch
from oncall import schema as S
from tools.result import iso, utc_now

YES = {"y", "yes"}


def _show(report: dict, proposal: dict) -> None:
    evidence = report.get(S.EVIDENCE) or []
    validated = sum(1 for e in evidence if e.get(S.EV_VALIDATED))
    print("\nPROPOSED ACTION")
    print(f"  component: {proposal.get('component')}")
    print(f"  cause: {report.get(S.CAUSE)}")
    print(f"  confidence: {report.get(S.CONFIDENCE)} final (agent stated {report.get(S.STATED_CONFIDENCE)})")
    print(f"  validated evidence: {validated} of {len(evidence)}")
    print(f"  action: {proposal.get('action_id')} {proposal.get('params')}  (risk {proposal.get('risk')})")
    print(f"  exact command: {proposal.get('preview')}")
    print("  Nothing changes unless you answer yes.")


def ask(report, proposal, *, input_fn=input, actions_dir=None) -> dict:
    ids = {"incident_id": (proposal or {}).get("incident_id"), "action_id": (proposal or {}).get("action_id"),
           "params": (proposal or {}).get("params"), "directory": actions_dir}

    def finish(decision: str, by: str, why: str) -> dict:
        answer = {"decision": decision, "at": iso(utc_now()), "by": by}
        audit.record("approval", mode=None, outcome=f"{decision}: {why}", **ids)
        return answer

    if killswitch.is_engaged(actions_dir):
        return finish("killed", "kill_switch", "kill switch engaged, not asked")
    if input_fn is input and not sys.stdin.isatty():
        return finish("denied", "non_interactive", "stdin is not a terminal")
    _show(report, proposal)
    try:
        text = input_fn("approve? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return finish("denied", "no_answer", "no answer (EOF or interrupt)")
    if killswitch.is_engaged(actions_dir):
        return finish("killed", "kill_switch", "kill switch engaged while waiting")
    if str(text).strip().lower() in YES:
        return finish("approved", getpass.getuser(), "typed yes")
    return finish("denied", getpass.getuser(), f"answered {str(text).strip()!r}")
