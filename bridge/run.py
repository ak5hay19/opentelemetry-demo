"""The whole flow in one command: investigate, propose a fix, ask a human, then hand it to the Action API.

    python -m bridge.run [--execute] [any option of oncall.investigate, e.g. --symptom TEXT --steps N]

It runs oncall.respond with --approve (diagnosis, fix suggestion, human approval), then bridge.execute on the run
that was just created. Without --execute the Action API only does a dry run and nothing is changed. With --execute
the approved command really runs, followed by a recovery check. Needs the demo running and a key in .env.incident.
"""
import sys
from pathlib import Path

from bridge import execute
from oncall import investigate, respond

RUNS_DIR = investigate.RUNS_DIR
EXECUTE_FLAG = "--execute"


def _run_folders(runs_dir: Path) -> set[Path]:
    return {p for p in Path(runs_dir).iterdir() if p.is_dir()} if Path(runs_dir).exists() else set()


def main(argv: list[str] | None = None, llm=None, runs_dir: Path = RUNS_DIR, ask=input, **dependencies) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    execute_requested = EXECUTE_FLAG in argv
    argv = [a for a in argv if a != EXECUTE_FLAG]
    before = _run_folders(runs_dir)
    status = respond.main([*argv, respond.APPROVE_FLAG], llm=llm, runs_dir=runs_dir, ask=ask)
    if status != 0:
        return status

    created = [p for p in _run_folders(runs_dir) - before if (p / "proposal.json").exists()]
    if len(created) != 1:
        print("cannot tell which run was just created, so nothing was handed to the Action API", file=sys.stderr)
        return 1

    print("\n" + "=" * 70)
    return execute.main(["--run-dir", str(created[0]), *([EXECUTE_FLAG] if execute_requested else [])],
                        runs_dir=runs_dir, **dependencies)


if __name__ == "__main__":
    sys.exit(main())
