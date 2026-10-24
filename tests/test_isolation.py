"""The agent side (oncall/, tools/) and the injector (dev/) must stay completely separate."""
import ast
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DYNAMIC_IMPORTS = {"__import__", "import_module"}


def imported_roots(path: Path) -> set[str]:
    """Top-level names a Python file imports (absolute imports only), plus dynamic-import calls."""
    roots = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in DYNAMIC_IMPORTS:
                roots.add(f"<dynamic import in {path.name}>")
    return roots


def offenders(package_dirs: list[str], forbidden: set[str]) -> list[str]:
    found = []
    for package in package_dirs:
        for path in sorted((REPO / package).rglob("*.py")):
            bad = imported_roots(path) & forbidden
            bad |= {r for r in imported_roots(path) if r.startswith("<dynamic")}
            if bad:
                found.append(f"{path.relative_to(REPO)}: {sorted(bad)}")
    return found


def test_agent_side_never_imports_dev():
    assert offenders(["oncall", "tools"], {"dev"}) == []


def test_faultctl_never_imports_agent_side():
    assert offenders(["dev"], {"oncall", "tools"}) == []


# The flag service name and the flag UI path may appear in exactly one agent-side file.
SANITISER = REPO / "tools" / "sanitize.py"
FORBIDDEN_TEXT = ("flagd", "/feature")


def agent_side_files() -> list[Path]:
    return sorted(
        p for package in ("oncall", "tools") for p in (REPO / package).rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix in {".py", ".md", ".json", ".txt", ".yaml", ".yml", ".toml"}
    )


def test_only_the_sanitiser_names_the_flag_service_or_ui_path():
    leaks = [
        f"{p.relative_to(REPO)}: {word!r}"
        for p in agent_side_files() if p != SANITISER
        for word in FORBIDDEN_TEXT if word in p.read_text(encoding="utf-8").lower()
    ]
    assert leaks == []


def test_sanitiser_contains_no_url():
    text = SANITISER.read_text(encoding="utf-8")
    assert "://" not in text and "localhost" not in text


# Actions and the detector may change the flag configuration only through actions/flag_adapter.py.
ADAPTER = REPO / "actions" / "flag_adapter.py"
FLAG_FILE_TEXT = ("flagd", "/feature", ".flagd.")


def action_side_files() -> list[Path]:
    return sorted(p for package in ("actions", "detector") for p in (REPO / package).rglob("*.py") if p != ADAPTER)


def imports_adapter(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.ImportFrom) and node.module == "actions" and any(a.name == "flag_adapter" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "actions.flag_adapter":
            return True
        if isinstance(node, ast.Import) and any(a.name == "actions.flag_adapter" for a in node.names):
            return True
    return False


def test_the_adapter_exists_and_is_the_only_file_naming_the_flag_file():
    assert "demo.flagd.json" in ADAPTER.read_text(encoding="utf-8")
    leaks = [f"{p.relative_to(REPO)}: {word!r}" for p in action_side_files()
             for word in FLAG_FILE_TEXT if word in p.read_text(encoding="utf-8").lower()]
    assert leaks == []


def test_only_the_runner_imports_the_flag_adapter():
    assert [p.name for p in action_side_files() if imports_adapter(p)] == ["runner.py"]


def test_agent_side_never_imports_actions_or_the_detector():
    assert offenders(["oncall", "tools"], {"actions", "detector"}) == []


def test_the_agent_toolbox_exposes_no_action_or_flag_tool():
    from actions.remediations import REMEDIATIONS
    from oncall import toolbox
    assert set(toolbox.TOOLS) == {"container_health", "query_metrics", "query_traces", "query_logs", "deploy_history"}
    assert all(tool.fn.__module__.startswith("tools.") for tool in toolbox.TOOLS.values())
    flag_names = {name for names in REMEDIATIONS.values() for name in names}
    for tool in toolbox.TOOLS.values():
        text = json.dumps(vars(tool.spec())).lower()
        assert not any(word in text for word in ("disable_fault_flag", "restart_container", "set_resting", "toggle"))
        assert not any(name.lower() in text for name in flag_names)
