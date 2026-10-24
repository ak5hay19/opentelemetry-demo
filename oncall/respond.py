"""One command for the whole flow so far: investigate, then propose a fix. Nothing is executed.

    python -m oncall.respond [--approve] [any option of oncall.investigate, e.g. --symptom TEXT --steps N]

With --approve it then asks a human to approve the proposal (oncall.approve) and saves decision.json. It runs the diagnosis agent (oncall.investigate), which saves reports/runs/<id>/candidate.json, then runs the
fix suggester (oncall.suggest) on THAT run and saves proposal.json beside it. The two stay separate programs
that talk only through candidate.json, so either one can still be run on its own.
Needs the demo running and a key in .env.incident.
"""
import sys
from pathlib import Path

from oncall import approve, investigate, suggest

RUNS_DIR = investigate.RUNS_DIR
APPROVE_FLAG = "--approve"


def _run_folders(runs_dir: Path) -> set[Path]:
    return {p for p in Path(runs_dir).iterdir() if p.is_dir()} if Path(runs_dir).exists() else set()


def main(argv: list[str] | None = None, llm=None, runs_dir: Path = RUNS_DIR, ask=input) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    approve_requested = APPROVE_FLAG in argv
    argv = [a for a in argv if a != APPROVE_FLAG]
    before = _run_folders(runs_dir)
    status = investigate.main(argv, llm=llm, runs_dir=runs_dir)
    if status != 0:
        return status

    created = [p for p in _run_folders(runs_dir) - before if (p / "candidate.json").exists()]
    if len(created) != 1:
        print("cannot tell which run the investigation created, so no fix was proposed", file=sys.stderr)
        return 1

    print("\n" + "=" * 70)
    status = suggest.main(["--candidate", str(created[0] / "candidate.json")], llm=llm, runs_dir=runs_dir)
    if status != 0 or not approve_requested:
        return status

    print("\n" + "=" * 70)
    return approve.main(["--proposal", str(created[0] / "proposal.json")], ask=ask, runs_dir=runs_dir)


if __name__ == "__main__":
    sys.exit(main())
