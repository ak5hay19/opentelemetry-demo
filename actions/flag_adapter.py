"""The only file in actions/ or detector/ that touches the flag configuration.

set_resting puts catalogued toggles back on their resting variant. It never turns anything on, never touches
other flags or keys, and never returns or logs what a toggle was set to before. Any problem raises
FlagAdapterError before anything is written.

A toggle may carry one targeting shape, {"if": [condition, A, B]} with A and B variant names. Its resting state
is defaultVariant, A and B all on the resting variant; the condition is left as it is.
"""
import json
import os
import shutil
import tempfile
from pathlib import Path

from actions.remediations import REMEDIATIONS, RESTING_VARIANT

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "src" / "flagd" / "demo.flagd.json"
CATALOGUED = frozenset(name for names in REMEDIATIONS.values() for name in names)


class FlagAdapterError(Exception):
    """The change cannot be made safely. Nothing was written."""


def exact_change(names) -> str:
    return f'set defaultVariant to "{RESTING_VARIANT}" for: {", ".join(names)} (no other flag or key changes)'


def _load(path) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        raise FlagAdapterError("the flag configuration file does not exist") from None
    except (OSError, ValueError) as exc:
        raise FlagAdapterError(f"the flag configuration cannot be read as JSON ({type(exc).__name__})") from None
    if not isinstance(document, dict) or not isinstance(document.get("flags"), dict):
        raise FlagAdapterError("the flag configuration has an unexpected structure (no 'flags' object)")
    return document


def _check_names(names) -> list[str]:
    if not isinstance(names, (list, tuple)) or not names:
        raise FlagAdapterError("no toggle names were given")
    unknown = [n for n in names if not isinstance(n, str) or n not in CATALOGUED]
    if unknown:
        raise FlagAdapterError(f"not catalogued toggles: {unknown!r}")
    return list(dict.fromkeys(names))


def _rule(name: str, flag: dict) -> list | None:
    """The [condition, A, B] list of the one accepted targeting shape, or None when the toggle has no targeting."""
    targeting = flag.get("targeting")
    if targeting is None or targeting == {}:
        return None
    rule = targeting.get("if") if isinstance(targeting, dict) and list(targeting) == ["if"] else None
    if (isinstance(rule, list) and len(rule) == 3 and all(isinstance(b, str) and b in flag["variants"] for b in rule[1:])):
        return rule
    raise FlagAdapterError(f"toggle {name} has a targeting rule of a shape that is not handled; nothing was changed")


def _check_document(document: dict, names: list[str]) -> None:
    for name in names:
        flag = document["flags"].get(name)
        if not isinstance(flag, dict):
            raise FlagAdapterError(f"toggle {name} is not in the flag configuration")
        variants = flag.get("variants")
        if not isinstance(variants, dict) or RESTING_VARIANT not in variants:
            raise FlagAdapterError(f"toggle {name} has no {RESTING_VARIANT!r} variant; nothing was changed")
        _rule(name, flag)


def check(names, path=None) -> None:
    """Raise FlagAdapterError unless set_resting(names, path) would be accepted. Writes nothing."""
    _check_document(_load(path or DEFAULT_PATH), _check_names(names))


def _write_atomically(path: Path, document: dict) -> None:
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        shutil.copymode(path, temp)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def set_resting(names, path=None) -> list[str]:
    """Set each named toggle's defaultVariant to the resting variant. Returns the names that were set."""
    path = Path(path or DEFAULT_PATH)
    names = _check_names(names)
    document = _load(path)
    _check_document(document, names)
    for name in names:
        flag = document["flags"][name]
        flag["defaultVariant"] = RESTING_VARIANT
        rule = _rule(name, flag)
        if rule:
            rule[1] = rule[2] = RESTING_VARIANT
    try:
        _write_atomically(path, document)
    except OSError as exc:
        raise FlagAdapterError(f"the flag configuration could not be written ({type(exc).__name__})") from None
    return names
