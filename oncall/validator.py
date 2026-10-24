"""Check every evidence item of a candidate diagnosis against the evidence ledger.

An item is valid only when the model's own words can be found in what the tools returned:
  1. its ledger_id exists;
  2. its quote is an EXACT substring of a string value inside that entry's result
     (trace IDs, log messages and container names compare exactly, with no trimming or case change);
  3. for a metric entry the quote may instead be a number within METRIC_REL_TOL (2 %) or
     METRIC_ABS_TOL (0.05) of a numeric value in that entry, but only when it has at least
     MIN_SIGNIFICANT_DIGITS significant digits and is not zero; a shorter number such as "0" must equal a
     value exactly. Metric entries also carry text such as "errors 0.00% -> 3.38%"; a quote that is an
     exact substring of that text is valid by rule 2;
  4. if a value is given it must also occur in the same entry (exact text, or an equal number);
  5. a log or trace quote that is only a timestamp, or shorter than MIN_LOG_TRACE_QUOTE_CHARS, is rejected;
  6. for a metric or container entry a quote (or value) shaped like a key and a value, as copied from the compact
     JSON view the model was shown (`"p95_ms":15000.0`, `oom_killed":true`, `oom_killed true`), is parsed and
     checked structurally: the key must occur in that entry's view and the value must equal one of its values
     (numbers by rules 3, booleans and strings exactly). Rule 2 also accepts a string as the view renders it
     (JSON-escaped).
The kind used for rules 3 and 5 comes from the ledger entry (see `derived_kind`), not from the model's label.
A separate soft check only warns when no valid item's entry mentions the claimed component.

Only what the tool observed is searched (the entry's result without its query strings), so a model
cannot "prove" a claim by quoting the command that was run.

The ledger is the dict stored in ledger.json: a list of entries {id, tool, arguments, query, window,
timestamp, result}. `Ledger` has no load method, so `load_ledger` reads that file directly.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from oncall import schema as S
from oncall import toolbox
from tools.ledger import PREFIXES, LedgerEntry

METRIC_REL_TOL = 0.02
METRIC_ABS_TOL = 0.05
MIN_QUOTE_CHARS = 3  # a text quote shorter than this would match almost anything
MIN_LOG_TRACE_QUOTE_CHARS = 8  # container names are legitimately short, log lines and trace ids are not
MIN_SIGNIFICANT_DIGITS = 2

KIND_METRIC, KIND_CONTAINER, KIND_TRACE, KIND_LOG = "metric", "container", "trace", "log"
# Docker supplies both container health and deploy history, so they count as one kind of evidence.
KIND_BY_TOOL = {"query_metrics": KIND_METRIC, "container_health": KIND_CONTAINER, "deploy_history": KIND_CONTAINER,
                "query_traces": KIND_TRACE, "query_logs": KIND_LOG}
KIND_BY_PREFIX = {PREFIXES[tool]: kind for tool, kind in KIND_BY_TOOL.items()}

# A bare ISO 8601 timestamp such as 2026-10-24T10:00:00.123Z or "2026-10-24 10:00:00+00:00".
TIMESTAMP_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?")

# A key and a value as they appear in the compact JSON view: an optional opening quote, an identifier-like key,
# an optional closing quote, then ":" or "=" (spaces allowed around it) or just whitespace, then the value.
KEY_VALUE = re.compile(r'"?(?P<key>[A-Za-z_][\w.\-]*)"?(?:\s*[:=]\s*|\s+)(?P<value>\S.*)', re.DOTALL)


@dataclass
class ValidationResult:
    items: list[dict] = field(default_factory=list)  # one dict per evidence item, in order
    warnings: list[str] = field(default_factory=list)

    @property
    def validated(self) -> list[dict]:
        return [i for i in self.items if i[S.EV_VALIDATED]]

    @property
    def rejected(self) -> list[dict]:
        return [i for i in self.items if not i[S.EV_VALIDATED]]


def derived_kind(ledger_id, entry: dict | None = None) -> str | None:
    """The kind of evidence a ledger entry really is, from its id prefix (M1, C2, ...) or its tool field.
    Never taken from the label the model put on its evidence item."""
    if isinstance(ledger_id, str) and ledger_id[:1] in KIND_BY_PREFIX:
        return KIND_BY_PREFIX[ledger_id[:1]]
    tool = entry.get("tool") if isinstance(entry, dict) else None
    return KIND_BY_TOOL.get(tool) if isinstance(tool, str) else None


# --- ledger access -----------------------------------------------------------------------------

def load_ledger(source) -> dict[str, dict]:
    """Return {id: entry dict}. `source` is a path to ledger.json, a list of entries, a dict keyed by id,
    or a `tools.ledger.Ledger`. Entries may be dicts or objects with `.id` and `.result`."""
    if isinstance(source, (str, Path)):
        source = json.loads(Path(source).read_text(encoding="utf-8"))
    if hasattr(source, "list") and callable(source.list):
        source = source.list()
    if isinstance(source, dict):
        entries = [(key, value) for key, value in source.items()]
    else:
        entries = [(None, entry) for entry in source]
    loaded = {}
    for key, entry in entries:
        if isinstance(entry, dict):
            plain = entry
        else:
            plain = {"id": getattr(entry, "id", key), "tool": getattr(entry, "tool", None),
                     "result": getattr(entry, "result", None)}
        entry_id = plain.get("id", key)
        if entry_id is not None:
            loaded[str(entry_id)] = plain
    return loaded


def _leaves(node):
    """Yield every string and every number (not bool) inside a nested structure."""
    if isinstance(node, dict):
        for value in node.values():
            yield from _leaves(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _leaves(value)
    elif isinstance(node, str):
        yield node
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        yield node


def _observed(entry: dict):
    """What the tool observed: the entry's result without its copy of the query that was run."""
    result = entry.get("result")
    if isinstance(result, dict):
        return {key: value for key, value in result.items() if key != "query"}
    return result


def _split(observed) -> tuple[list[str], list[float]]:
    texts, numbers = [], []
    for leaf in _leaves(observed):
        (texts if isinstance(leaf, str) else numbers).append(leaf)
    return texts, numbers


def _as_number(text: str):
    """Parse '3.38', '3.38%' or ' 12 ' as a number; None when the text is not a plain number."""
    cleaned = text.strip().rstrip("%").strip()
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _within_tolerance(quoted: float, actual: float) -> bool:
    return abs(quoted - actual) <= max(METRIC_REL_TOL * abs(actual), METRIC_ABS_TOL)


def _significant_digits(text: str) -> int:
    digits = text.strip().lstrip("+-").rstrip("%").strip().replace(".", "")
    return len(digits.lstrip("0"))


def _is_precise(quote: str, quoted: float) -> bool:
    """Only a non-zero quote with enough significant digits earns the tolerance; anything else must be exact."""
    return quoted != 0 and _significant_digits(quote) >= MIN_SIGNIFICANT_DIGITS


def _number_matches(quote: str, quoted: float, numbers: list[float]) -> tuple[float | None, str]:
    """(a number of the entry the quote matches, how) or (None, why not). Tolerance needs a precise quote."""
    if _is_precise(quote, quoted):
        close = [n for n in numbers if _within_tolerance(quoted, n)]
        return (close[0], "within tolerance") if close else (None, f"no number within 2% or 0.05 of {quote}")
    exact = [n for n in numbers if n == quoted]
    return (exact[0], "exactly") if exact else (
        None, f"metric quote {quote!r} is too imprecise for a tolerance match and equals no value exactly")


def _trivial_text_quote(quote: str, kind: str | None) -> str | None:
    """Why a log or trace quote is too thin to prove anything, or None when it is fine."""
    if kind not in (KIND_LOG, KIND_TRACE):
        return None
    if TIMESTAMP_ONLY.fullmatch(quote.strip()):
        return "is only a timestamp"
    if len(quote) < MIN_LOG_TRACE_QUOTE_CHARS:
        return f"is shorter than {MIN_LOG_TRACE_QUOTE_CHARS} characters"
    return None


# --- key and value quotes ----------------------------------------------------------------------

def _searchable(entry: dict):
    """What the model was shown for this entry (the toolbox's compact view, without the id), or, when the entry
    is too thin to render, what the tool observed."""
    try:
        view = toolbox.compact_view(LedgerEntry(
            id=str(entry.get("id")), tool=entry.get("tool"), arguments={}, query=[], window={}, timestamp="",
            result=entry.get("result")))
    except (KeyError, TypeError, AttributeError):
        return _observed(entry)
    view.pop("id", None)
    return view


def _values_of_key(node, key: str):
    """Every value stored under `key` anywhere in a nested structure (the elements too when it is a list)."""
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                yield value
                if isinstance(value, (list, tuple)):
                    yield from value
            yield from _values_of_key(value, key)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _values_of_key(value, key)


def _parse_value(text: str):
    """(value, the text it came from) for the value half of a key/value quote: JSON when it is JSON,
    true/false in any case as booleans, otherwise the bare text as a string."""
    text = text.strip()
    for candidate in (text, text.rstrip(",} ").rstrip()):
        try:
            return json.loads(candidate), candidate
        except ValueError:
            continue
    bare = text.rstrip(",} ").strip()
    return ({"true": True, "false": False}.get(bare.lower(), bare)), bare


def _value_agrees(parsed, raw: str, actual) -> bool:
    if isinstance(parsed, bool) or isinstance(actual, bool):
        return isinstance(parsed, bool) and isinstance(actual, bool) and parsed == actual
    if isinstance(actual, (int, float)):
        quoted = parsed if isinstance(parsed, (int, float)) else _as_number(parsed) if isinstance(parsed, str) else None
        if quoted is None:
            return False
        return _within_tolerance(quoted, actual) if _is_precise(raw, quoted) else quoted == actual
    return parsed == actual


def _check_key_value(text: str, searchable) -> tuple[bool, str] | None:
    """None when `text` is not key/value shaped or its key is not in the entry (the ordinary rules then decide);
    otherwise (whether some occurrence of the key has that value, why)."""
    match = KEY_VALUE.fullmatch(text.strip())
    if not match:
        return None
    key = match["key"]
    actuals = list(_values_of_key(searchable, key))
    if not actuals:
        return None
    parsed, raw = _parse_value(match["value"])
    if any(_value_agrees(parsed, raw, actual) for actual in actuals):
        return True, f"{key} = {raw}"
    return False, f"{key} is in the entry but never with the value {raw}"


# --- one item ----------------------------------------------------------------------------------

def _check_item(item: dict, ledger: dict[str, dict]) -> tuple[bool, str]:
    ledger_id = item.get(S.EV_LEDGER_ID)
    quote = item.get(S.EV_QUOTE)
    value = item.get(S.EV_VALUE)

    entry = ledger.get(ledger_id) if isinstance(ledger_id, str) else None
    if entry is None:
        return False, f"ledger id {ledger_id!r} does not exist"
    if not isinstance(quote, str) or not quote.strip():
        return False, f"{ledger_id}: the quote is empty"

    kind = derived_kind(ledger_id, entry) or item.get(S.EV_KIND)
    trivial = _trivial_text_quote(quote, kind)
    if trivial:
        return False, f"{ledger_id}: quote {quote!r} {trivial}"

    texts, numbers = _split(_observed(entry))
    keyed = kind in (KIND_METRIC, KIND_CONTAINER)
    searchable = _searchable(entry) if keyed else None

    found = False
    note = ""
    if len(quote) >= MIN_QUOTE_CHARS and any(quote in text or quote in json.dumps(text)[1:-1] for text in texts):
        found, note = True, f"quote found verbatim in {ledger_id}"
    elif keyed and (pair := _check_key_value(quote, searchable)) is not None:
        if not pair[0]:
            return False, f"{ledger_id}: quote {quote!r}: {pair[1]}"
        found, note = True, f"quote {pair[1]} found in {ledger_id}"
    elif kind == KIND_METRIC:
        quoted = _as_number(quote)
        if quoted is None:
            return False, f"{ledger_id}: metric quote {quote!r} is not a number and not found in the entry text"
        match, how = _number_matches(quote, quoted, numbers)
        if match is None:
            return False, f"{ledger_id}: {how}"
        found, note = True, f"metric quote {quote} matches {match} in {ledger_id} ({how})"
    if not found:
        reason = "too short to check" if len(quote) < MIN_QUOTE_CHARS else "not found in the entry"
        return False, f"{ledger_id}: quote {quote!r} {reason}"

    if value:  # None and "" both mean "no value given"
        if not isinstance(value, str):
            return False, f"{ledger_id}: value must be text"
        number = _as_number(value)
        in_text = any(value in text for text in texts)
        in_numbers = number is not None and any(number == n for n in numbers)
        pair = _check_key_value(value, searchable) if keyed and not (in_text or in_numbers) else None
        if pair is not None and not pair[0]:
            return False, f"{ledger_id}: value {value!r}: {pair[1]}"
        if not (in_text or in_numbers or pair):
            return False, f"{ledger_id}: value {value!r} not found in the entry"
        note += f"; value {value!r} found"
    return True, note


def _mentions(entry: dict, component: str) -> bool:
    return component.lower() in json.dumps(_observed(entry), default=str).lower()


# --- whole candidate ---------------------------------------------------------------------------

def validate_evidence(candidate: dict, ledger_source) -> ValidationResult:
    """Validate every evidence item. Returns the items (each with validated and validation_note) and warnings."""
    ledger = load_ledger(ledger_source)
    raw = candidate.get(S.EVIDENCE)
    evidence = [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []

    result = ValidationResult()
    for item in evidence:
        ok, note = _check_item(item, ledger)
        result.items.append({
            S.EV_LEDGER_ID: item.get(S.EV_LEDGER_ID),
            S.EV_KIND: item.get(S.EV_KIND),
            S.EV_CLAIM: item.get(S.EV_CLAIM),
            S.EV_QUOTE: item.get(S.EV_QUOTE),
            S.EV_VALUE: item.get(S.EV_VALUE),
            S.EV_VALIDATED: ok,
            S.EV_NOTE: note,
        })

    component = candidate.get(S.COMPONENT)
    if isinstance(component, str) and component.strip() and component.strip().lower() != S.UNKNOWN_COMPONENT:
        checked = [ledger[i[S.EV_LEDGER_ID]] for i in result.validated]
        if checked and not any(_mentions(entry, component.strip()) for entry in checked):
            result.warnings.append(f"none of the validated evidence mentions the component {component.strip()!r}")
    return result
