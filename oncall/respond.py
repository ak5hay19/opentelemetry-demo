"""One command for the whole flow so far: investigate, then propose a fix. Nothing is executed.

    python -m oncall.respond [any option of oncall.investigate, e.g. --symptom TEXT --steps N]

It runs the diagnosis agent (oncall.investigate), which saves reports/runs/<id>/candidate.json, then runs the
fix suggester (oncall.suggest) on THAT run and saves proposal.json beside it. The two stay separate programs
that talk only through candidate.json, so either one can still be run on its own.
Needs the demo running and a key in .env.incident.
"""
import sys
from pathlib import Path

from oncall import investigate, suggest

RUNS_DIR = investigate.RUNS_DIR


def _run_folders(runs_dir: Path) -> set[Path]:
    return {p for p in Path(runs_dir).iterdir() if p.is_dir()} if Path(runs_dir).exists() else set()


def main(argv: list[str] | None = None, llm=None, runs_dir: Path = RUNS_DIR) -> int:
    before = _run_folders(runs_dir)
    status = investigate.main(argv, llm=llm, runs_dir=runs_dir)
    if status != 0:
        return status

    created = [p for p in _run_folders(runs_dir) - before if (p / "candidate.json").exists()]
    if len(created) != 1:
        print("cannot tell which run the investigation created, so no fix was proposed", file=sys.stderr)
        return 1

    print("\n" + "=" * 70)
    return suggest.main(["--candidate", str(created[0] / "candidate.json")], llm=llm, runs_dir=runs_dir)


if __name__ == "__main__":
    sys.exit(main())
