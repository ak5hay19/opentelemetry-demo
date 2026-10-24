"""Run practice incidents through the faultctl and investigate command-line interfaces."""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from dev.faultctl.catalog import CATALOGUE, CONTAINER, FLAG, Incident
from dev.harness.pricing import estimate_cost

ROOT = Path(__file__).resolve().parents[2]
GENERIC_SYMPTOM = (
    "Users report that the shop is not behaving as expected. Investigate the available "
    "telemetry and identify the likely cause using evidence."
)
DEFAULT_AGENT_TIMEOUT = 600
STATUS_TIMEOUT = 120
INJECT_TIMEOUT = 180
RESET_TIMEOUT = 180
RESET_ALL_TIMEOUT = 600
SCORING_TIMEOUT = 120
FLAG_INCIDENT_SERVICES = {
    "ad-failure": "ad",
    "payment-failure": "payment",
    "cart-failure": "cart",
    "product-catalog-failure": "product-catalog",
    "product-catalog-lock-contention": "product-catalog",
    "ad-high-cpu": "ad",
}

CommandRunner = Callable[..., subprocess.CompletedProcess]


def _command(
    args: list[str], command_runner: CommandRunner, timeout: float | None = None
) -> subprocess.CompletedProcess:
    return command_runner(
        [sys.executable, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _known_incidents() -> dict[str, str]:
    return {incident_id: incident.mechanism for incident_id, incident in CATALOGUE.items()}


def _default_incidents(known_incidents: dict[str, str]) -> list[str]:
    containers = [name for name, fault_type in known_incidents.items() if fault_type == CONTAINER]
    flags = [name for name, fault_type in known_incidents.items() if fault_type == FLAG]
    if not containers or len(flags) < 2:
        raise RuntimeError("faultctl list must include at least one container and two flag incidents")
    return [containers[0], flags[0], flags[1]]


def _check_incidents(incidents: list[str], known_incidents: dict[str, str]) -> None:
    unknown = [incident for incident in incidents if incident not in known_incidents]
    if unknown:
        raise ValueError("unknown faultctl incident(s): " + ", ".join(unknown))


def _expected_component(incident: Incident) -> str | None:
    if incident.mechanism == CONTAINER:
        return incident.target
    return FLAG_INCIDENT_SERVICES.get(incident.id)


def _containers_running(output: str) -> bool:
    rows = []
    after_header = False
    for line in output.splitlines():
        if not after_header:
            columns = line.split()
            after_header = line[:1].isspace() and columns[:2] == ["name", "state"]
            continue
        if not line[:1].isspace():
            break
        columns = line.split()
        if len(columns) == 4:
            rows.append((columns[1], columns[2]))
        else:
            return False
    return bool(rows) and all(state == "running" and health in ("-", "healthy") for state, health in rows)


def _verified_injection(result: subprocess.CompletedProcess, incident: str) -> bool:
    return result.returncode == 0 and any(
        line.strip() == f"[verified] inject {incident}" for line in result.stdout.splitlines()
    )


def _run_directory(output: str) -> str | None:
    for line in reversed(output.splitlines()):
        if line.startswith("saved to "):
            saved = Path(line.removeprefix("saved to ")).resolve()
            try:
                relative = saved.relative_to(ROOT.resolve())
            except ValueError:
                return None
            if relative.parts[:2] == ("reports", "runs"):
                return relative.as_posix()
            return None
    return None


def _read_tokens(run_dir: str | None) -> tuple[int, int] | None:
    if run_dir is None:
        return None
    try:
        info = json.loads((ROOT / Path(run_dir)).joinpath("run_info.json").read_text(encoding="utf-8"))
        return int(info["tokens_input"]), int(info["tokens_output"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"could not read token usage for {run_dir}: {exc}", file=sys.stderr)
        return None


def _write_manifest(path: Path, entries: list[dict]) -> None:
    temporary = path.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _new_harness_dir(base_dir: Path, now: datetime) -> Path:
    timestamp = now.astimezone().strftime("%Y%m%dT%H%M%S%z")
    folder = base_dir / timestamp
    suffix = 1
    while folder.exists():
        folder = base_dir / f"{timestamp}-{suffix}"
        suffix += 1
    folder.mkdir(parents=True)
    return folder


def _entry(incident: Incident, started_at: str) -> dict:
    return {
        "incident": incident.id,
        "fault_type": incident.mechanism,
        "expected_component": _expected_component(incident) or "unknown",
        "inject_verified": False,
        "baseline_clean": False,
        "run_dir": None,
        "started_at": started_at,
        "ended_at": "",
        "notes": "",
    }


def _timeout_note(exc: subprocess.TimeoutExpired) -> str:
    return f"timed out after {exc.timeout:g}s: " + " ".join(str(part) for part in exc.cmd[1:])


def run_sweep(
    incidents: list[str] | None,
    baseline_wait: float = 540,
    settle: float = 90,
    max_cost: float = 0.10,
    dry_run: bool = False,
    agent_timeout: float = DEFAULT_AGENT_TIMEOUT,
    *,
    command_runner: CommandRunner = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    harness_root: Path = ROOT / "reports" / "harness",
) -> Path | None:
    known_incidents = _known_incidents()
    if incidents is None:
        incidents = _default_incidents(known_incidents)
    _check_incidents(incidents, known_incidents)

    if dry_run:
        print("Dry run: no commands or files will be created.")
        print("Incident order: " + ", ".join(incidents))
        print(f"Baseline wait: {baseline_wait:g}s; settle: {settle:g}s; agent timeout: {agent_timeout:g}s")
        print(f"Maximum estimated cost: ${max_cost:.2f}")
        return None

    harness_dir = _new_harness_dir(harness_root, now())
    manifest_path = harness_dir / "manifest.json"
    entries: list[dict] = []
    estimated_total = 0.0

    for incident_id in incidents:
        started_at = now().isoformat(timespec="seconds")
        entry = _entry(CATALOGUE[incident_id], started_at)
        notes = []
        interrupted: BaseException | None = None
        if entry["expected_component"] == "unknown":
            notes.append("no expected component is defined for this incident")
        try:
            reset = _command(["-m", "dev.faultctl", "reset-all", "--yes"], command_runner, RESET_ALL_TIMEOUT)
            if reset.returncode:
                notes.append(f"reset-all failed: {reset.stderr.strip() or reset.stdout.strip()}")
            sleep(baseline_wait)

            status = _command(["-m", "dev.faultctl", "status"], command_runner, STATUS_TIMEOUT)
            entry["baseline_clean"] = reset.returncode == 0 and status.returncode in (0, 1) and _containers_running(status.stdout)
            if not entry["baseline_clean"]:
                notes.append("baseline was not clean; incident skipped")
            else:
                injected = _command(["-m", "dev.faultctl", "inject", incident_id], command_runner, INJECT_TIMEOUT)
                entry["inject_verified"] = _verified_injection(injected, incident_id)
                if not entry["inject_verified"]:
                    notes.append(f"injection was not verified: {injected.stderr.strip() or injected.stdout.strip()}")
                else:
                    sleep(settle)
                    try:
                        investigation = _command(
                            ["-m", "oncall.investigate", "--symptom", GENERIC_SYMPTOM],
                            command_runner,
                            agent_timeout,
                        )
                    except subprocess.TimeoutExpired:
                        notes.append("agent timed out")
                    else:
                        entry["run_dir"] = _run_directory(investigation.stdout)
                        if investigation.returncode:
                            notes.append(
                                "investigation failed: "
                                + (investigation.stderr.strip() or investigation.stdout.strip())
                            )
                        if entry["run_dir"] is None:
                            notes.append("investigation did not report a reports/runs/<run_id> directory")

                        tokens = _read_tokens(entry["run_dir"])
                        if tokens is not None:
                            estimated_total += estimate_cost(*tokens)
        except subprocess.TimeoutExpired as exc:
            notes.append(f"harness command {_timeout_note(exc)}")
        except OSError as exc:
            notes.append(f"harness command failed: {exc}")
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            notes.append(f"harness could not complete incident: {exc}")
        except BaseException as exc:
            notes.append(f"interrupted ({type(exc).__name__})")
            interrupted = exc
        finally:
            try:
                reset_one = _command(["-m", "dev.faultctl", "reset", incident_id], command_runner, RESET_TIMEOUT)
            except subprocess.TimeoutExpired as exc:
                notes.append(f"final reset {_timeout_note(exc)}")
            except OSError as exc:
                notes.append(f"final reset failed: {exc}")
            else:
                if reset_one.returncode:
                    notes.append(f"final reset failed: {reset_one.stderr.strip() or reset_one.stdout.strip()}")

        entry["notes"] = "; ".join(notes)
        entry["ended_at"] = now().isoformat(timespec="seconds")
        entries.append(entry)
        _write_manifest(manifest_path, entries)
        if interrupted is not None:
            raise interrupted
        print(f"{incident_id}: baseline_clean={entry['baseline_clean']} inject_verified={entry['inject_verified']}")
        # The cost limit is only checked here, after each incident, so a sweep can overshoot it by one run.
        if entry["inject_verified"] and estimated_total > max_cost:
            print(
                f"Stopping sweep: estimated total ${estimated_total:.4f} exceeds "
                f"the ${max_cost:.2f} limit."
            )
            break

    print(f"Manifest: {manifest_path}")
    scoring = importlib.util.find_spec("dev.harness.scoring")
    if scoring is None:
        print("Scoring module is not available; run scoring separately.")
    else:
        try:
            result = _command(["-m", "dev.harness.scoring", str(harness_dir)], command_runner, SCORING_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError) as exc:
            print(f"Scoring did not complete: {exc}; run scoring separately.", file=sys.stderr)
            return manifest_path
        if result.stdout:
            print(result.stdout.rstrip())
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        if result.returncode:
            print(f"Scoring exited with status {result.returncode}.", file=sys.stderr)
    return manifest_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run practice incidents and save a harness manifest.")
    parser.add_argument(
        "--incidents",
        help="comma-separated faultctl incident names (default: first container and first two flags from faultctl list)",
    )
    parser.add_argument("--baseline-wait", type=float, default=540, metavar="N", help="seconds to wait after reset-all")
    parser.add_argument("--settle", type=float, default=90, metavar="N", help="seconds to wait after verified injection")
    parser.add_argument("--max-cost", type=float, default=0.10, metavar="USD", help="stop after estimated total exceeds this amount")
    parser.add_argument("--agent-timeout", type=float, default=DEFAULT_AGENT_TIMEOUT, metavar="N", help="seconds to let one investigation run")
    parser.add_argument("--dry-run", action="store_true", help="validate and print the incident order without running commands or writing files")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    incidents = [name.strip() for name in args.incidents.split(",") if name.strip()] if args.incidents else None
    if incidents == []:
        parser.error("--incidents must contain at least one incident")
    if args.baseline_wait < 0 or args.settle < 0 or args.max_cost < 0:
        parser.error("--baseline-wait, --settle and --max-cost must be non-negative")
    if args.agent_timeout <= 0:
        parser.error("--agent-timeout must be positive")
    try:
        run_sweep(
            incidents,
            args.baseline_wait,
            args.settle,
            args.max_cost,
            args.dry_run,
            args.agent_timeout,
        )
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    return 0
