"""Evidence validation against saved ledgers (offline: no Docker, no model)."""
import copy
import json
from pathlib import Path

import pytest

from oncall import schema as S
from oncall.validator import load_ledger, validate_evidence

LEDGERS = Path(__file__).parent / "fixtures" / "ledgers"


def ledger(name):
    return load_ledger(LEDGERS / name)


def item(ledger_id, kind, quote, value=None, claim="what it shows"):
    return {"ledger_id": ledger_id, "kind": kind, "claim": claim, "quote": quote, "value": value}


def candidate(*items, component="valkey-cart"):
    return {"component": component, "cause": "c", "stated_confidence": 0.9, "evidence": list(items)}


def first_trace_id(led):
    return led["T1"]["result"]["items"][0]["trace_id"]


def test_load_ledger_accepts_path_list_and_dict():
    by_path = ledger("live_container_fault.json")
    entries = json.loads((LEDGERS / "live_container_fault.json").read_text())
    assert set(by_path) == {"C1", "M1", "T1", "L1", "D1"}
    assert load_ledger(entries).keys() == by_path.keys()
    assert load_ledger(by_path).keys() == by_path.keys()


def test_valid_container_item_with_value():
    led = ledger("live_container_fault.json")
    out = validate_evidence(candidate(item("C1", "container", "valkey-cart", value="exited")), led)
    assert out.items[0][S.EV_VALIDATED] is True
    assert out.warnings == []


def test_valid_trace_id_and_log_message():
    led = ledger("live_flag_fault.json")
    trace = first_trace_id(led)
    log = led["L1"]["result"]["items"][0]["message"]
    out = validate_evidence(candidate(item("T1", "trace", trace), item("L1", "log", log), component="payment"), led)
    assert [i[S.EV_VALIDATED] for i in out.items] == [True, True]


def test_invented_ledger_id_is_rejected():
    led = ledger("live_container_fault.json")
    out = validate_evidence(candidate(item("C99", "container", "valkey-cart")), led)
    assert out.items[0][S.EV_VALIDATED] is False
    assert "does not exist" in out.items[0][S.EV_NOTE]


def test_non_text_ledger_id_is_rejected():
    led = ledger("live_container_fault.json")
    bad = item("C1", "container", "valkey-cart")
    bad["ledger_id"] = ["C1"]
    assert validate_evidence(candidate(bad), led).items[0][S.EV_VALIDATED] is False


def test_quote_not_in_entry_is_rejected():
    led = ledger("live_container_fault.json")
    out = validate_evidence(candidate(item("C1", "container", "no-such-container")), led)
    assert out.items[0][S.EV_VALIDATED] is False
    assert "not found" in out.items[0][S.EV_NOTE]


def test_trace_id_off_by_one_character_is_rejected():
    led = ledger("live_flag_fault.json")
    real = first_trace_id(led)
    wrong = real[:-1] + ("0" if real[-1] != "0" else "1")
    assert validate_evidence(candidate(item("T1", "trace", real), component="x"), led).items[0][S.EV_VALIDATED]
    assert not validate_evidence(candidate(item("T1", "trace", wrong), component="x"), led).items[0][S.EV_VALIDATED]


def test_quote_must_match_case_and_whitespace_exactly():
    led = ledger("live_container_fault.json")
    assert not validate_evidence(candidate(item("C1", "container", "Valkey-Cart")), led).items[0][S.EV_VALIDATED]
    assert not validate_evidence(candidate(item("C1", "container", " valkey-cart")), led).items[0][S.EV_VALIDATED]


def test_quote_from_the_query_text_does_not_count():
    led = ledger("live_container_fault.json")
    out = validate_evidence(candidate(item("C1", "container", "docker inspect")), led)
    assert out.items[0][S.EV_VALIDATED] is False


def test_empty_and_too_short_quotes_are_rejected():
    led = ledger("live_container_fault.json")
    assert not validate_evidence(candidate(item("C1", "container", "")), led).items[0][S.EV_VALIDATED]
    assert not validate_evidence(candidate(item("C1", "container", "   ")), led).items[0][S.EV_VALIDATED]
    assert not validate_evidence(candidate(item("C1", "container", "ex")), led).items[0][S.EV_VALIDATED]


def test_quote_cannot_span_two_values():
    led = ledger("live_container_fault.json")
    joined = "valkey-cartvalkey-cart"
    assert not validate_evidence(candidate(item("C1", "container", joined)), led).items[0][S.EV_VALIDATED]


# --- metrics ------------------------------------------------------------------------------------

def metric_value(led, entry="M1"):
    return led[entry]["result"]["items"][0]["p95_ms"]


def test_metric_number_within_relative_tolerance():
    led = ledger("live_container_fault.json")
    actual = metric_value(led)  # 15000.0
    inside = f"{actual * 1.015:.1f}"
    assert validate_evidence(candidate(item("M1", "metric", inside), component="cart"), led).items[0][S.EV_VALIDATED]


def test_metric_number_outside_relative_tolerance():
    led = ledger("live_container_fault.json")
    actual = metric_value(led)
    outside = f"{actual * 1.05:.1f}"
    out = validate_evidence(candidate(item("M1", "metric", outside), component="cart"), led)
    assert out.items[0][S.EV_VALIDATED] is False
    assert "within 2%" in out.items[0][S.EV_NOTE]


def test_metric_small_number_uses_absolute_tolerance():
    led = {"M1": {"id": "M1", "result": {"items": [{"error_rate_per_s": 0.0361}]}}}
    assert validate_evidence(candidate(item("M1", "metric", "0.07")), led).items[0][S.EV_VALIDATED] is False
    assert validate_evidence(candidate(item("M1", "metric", "0.039")), led).items[0][S.EV_VALIDATED]
    assert not validate_evidence(candidate(item("M1", "metric", "0.2")), led).items[0][S.EV_VALIDATED]


def test_metric_quote_with_percent_sign_is_a_number():
    led = {"M1": {"id": "M1", "result": {"items": [{"error_rate_pct": 100.0}]}}}
    assert validate_evidence(candidate(item("M1", "metric", "100%")), led).items[0][S.EV_VALIDATED]


def test_metric_change_string_quote_is_valid():
    led = {"M1": {"id": "M1", "result": {"items": [{"service": "cart", "change": "errors 0.00% -> 3.38%; p95 40 ms -> 900 ms"}]}}}
    out = validate_evidence(candidate(item("M1", "metric", "errors 0.00% -> 3.38%"), component="cart"), led)
    assert out.items[0][S.EV_VALIDATED] is True


def test_metric_quote_that_is_neither_a_number_nor_in_the_text_is_rejected():
    led = ledger("live_container_fault.json")
    out = validate_evidence(candidate(item("M1", "metric", "very high"), component="cart"), led)
    assert out.items[0][S.EV_VALIDATED] is False
    assert "not a number" in out.items[0][S.EV_NOTE]


def test_zero_and_one_digit_quotes_do_not_match_near_zero_values():
    led = {"M1": {"id": "M1", "result": {"items": [{"error_rate_per_s": 0.0338}]}}}
    for quote in ("0", "-0.03", "0.0", "0.03", "-0"):
        out = validate_evidence(candidate(item("M1", "metric", quote)), led)
        assert out.items[0][S.EV_VALIDATED] is False, quote
    assert "too imprecise" in out.items[0][S.EV_NOTE]


def test_two_significant_digits_still_use_the_tolerance():
    led = {"M1": {"id": "M1", "result": {"items": [{"error_rate_per_s": 0.3412}]}}}
    assert validate_evidence(candidate(item("M1", "metric", "0.34")), led).items[0][S.EV_VALIDATED]
    assert not validate_evidence(candidate(item("M1", "metric", "0.5")), led).items[0][S.EV_VALIDATED]


def test_short_quote_equal_to_a_value_exactly_is_valid():
    led = {"M1": {"id": "M1", "result": {"items": [{"errors": 0, "n": 5.0}]}}}
    assert validate_evidence(candidate(item("M1", "metric", "0")), led).items[0][S.EV_VALIDATED]
    assert validate_evidence(candidate(item("M1", "metric", "5")), led).items[0][S.EV_VALIDATED]
    assert not validate_evidence(candidate(item("M1", "metric", "6")), led).items[0][S.EV_VALIDATED]


def test_metric_tolerance_is_decided_by_the_entry_not_the_label():
    led = {"L1": {"id": "L1", "result": {"items": [{"count": 3.4, "message": "something failed"}]}}}
    assert not validate_evidence(candidate(item("L1", "metric", "3.4")), led).items[0][S.EV_VALIDATED]


def test_number_quote_for_a_non_metric_kind_is_not_matched_numerically():
    led = {"L1": {"id": "L1", "result": {"items": [{"count": 13, "message": "something failed"}]}}}
    assert not validate_evidence(candidate(item("L1", "log", "13.0")), led).items[0][S.EV_VALIDATED]


# --- trivial text quotes ----------------------------------------------------------------------

def test_log_quote_that_is_only_a_timestamp_is_rejected():
    led = {"L1": {"id": "L1", "tool": "query_logs", "result": {"items": [
        {"timestamp": "2026-10-24T10:00:00.123Z", "message": "payment failed: upstream timed out"}]}}}
    for quote in ("2026-10-24T10:00:00.123Z", " 2026-10-24T10:00:00.123Z ", "2026-10-24T10:00:00"):
        out = validate_evidence(candidate(item("L1", "log", quote)), led)
        assert out.items[0][S.EV_VALIDATED] is False, quote
        assert "only a timestamp" in out.items[0][S.EV_NOTE]


def test_full_log_message_passes():
    led = {"L1": {"id": "L1", "tool": "query_logs", "result": {"items": [
        {"timestamp": "2026-10-24T10:00:00.123Z", "message": "payment failed: upstream timed out"}]}}}
    assert validate_evidence(candidate(item("L1", "log", "payment failed: upstream timed out")), led).items[0][S.EV_VALIDATED]


def test_short_log_and_trace_quotes_are_rejected_but_short_container_names_are_not():
    led = {"L1": {"id": "L1", "result": {"items": [{"message": "an error happened"}]}},
           "T1": {"id": "T1", "result": {"items": [{"trace_id": "abc12345def"}]}},
           "C1": {"id": "C1", "result": {"items": [{"name": "cart", "state": "exited"}]}}}
    assert not validate_evidence(candidate(item("L1", "log", "error")), led).items[0][S.EV_VALIDATED]
    assert not validate_evidence(candidate(item("T1", "trace", "abc1234")), led).items[0][S.EV_VALIDATED]
    assert validate_evidence(candidate(item("T1", "trace", "abc12345")), led).items[0][S.EV_VALIDATED]
    assert validate_evidence(candidate(item("C1", "container", "cart")), led).items[0][S.EV_VALIDATED]


def test_a_container_label_does_not_exempt_a_log_entry_from_the_length_rule():
    led = {"L1": {"id": "L1", "result": {"items": [{"message": "an error happened"}]}}}
    assert not validate_evidence(candidate(item("L1", "container", "error")), led).items[0][S.EV_VALIDATED]


# --- value --------------------------------------------------------------------------------------

def test_value_missing_from_entry_is_rejected():
    led = ledger("live_container_fault.json")
    out = validate_evidence(candidate(item("C1", "container", "valkey-cart", value="paused")), led)
    assert out.items[0][S.EV_VALIDATED] is False
    assert "value" in out.items[0][S.EV_NOTE]


def test_numeric_value_matches_an_equal_number():
    led = ledger("live_container_fault.json")
    assert validate_evidence(candidate(item("C1", "container", "valkey-cart", value="0")), led).items[0][S.EV_VALIDATED]


def test_empty_value_is_treated_as_not_given():
    led = ledger("live_container_fault.json")
    assert validate_evidence(candidate(item("C1", "container", "valkey-cart", value="")), led).items[0][S.EV_VALIDATED]


# --- component soft check -----------------------------------------------------------------------

def test_warns_when_no_validated_evidence_mentions_the_component():
    led = ledger("live_container_fault.json")
    out = validate_evidence(candidate(item("C1", "container", "valkey-cart"), component="payment-gateway-xyz"), led)
    assert out.items[0][S.EV_VALIDATED] is True
    assert len(out.warnings) == 1 and "payment-gateway-xyz" in out.warnings[0]


def test_no_warning_when_component_is_mentioned_or_unknown_or_nothing_validated():
    led = ledger("live_container_fault.json")
    assert validate_evidence(candidate(item("C1", "container", "valkey-cart")), led).warnings == []
    assert validate_evidence(candidate(item("C1", "container", "valkey-cart"), component="unknown"), led).warnings == []
    assert validate_evidence(candidate(item("C99", "container", "x"), component="zzz"), led).warnings == []


def test_candidate_without_evidence_gives_no_items():
    out = validate_evidence({"component": "unknown", "evidence": []}, ledger("live_healthy.json"))
    assert out.items == [] and out.warnings == []


def test_input_candidate_is_not_modified():
    led = ledger("live_container_fault.json")
    cand = candidate(item("C1", "container", "valkey-cart"))
    before = copy.deepcopy(cand)
    validate_evidence(cand, led)
    assert cand == before


@pytest.mark.parametrize("name", ["live_container_fault.json", "live_flag_fault.json", "live_healthy.json"])
def test_every_fixture_loads_and_can_be_validated(name):
    led = ledger(name)
    assert led and all("result" in e for e in led.values())
    assert validate_evidence(candidate(item("C1", "container", "zz-not-there")), led).items[0][S.EV_VALIDATED] is False


# --- key/value quotes copied from the compact view -------------------------------------------------

def tool_entry(entry_id, tool, items, extra=None):
    result = {"tool": tool, "state": "data", "query": ["a query"], "window": {"minutes": 5}, "items": items,
              "total_items": len(items), "extra": extra or {}, "notes": [], "error": None}
    return {"id": entry_id, "tool": tool, "arguments": {}, "result": result}


def keyed_ledger():
    return load_ledger([
        tool_entry("M1", "query_metrics", [
            {"service": "cart", "p95_ms": 15000.0, "error_rate_pct": 3.38},
            {"service": "frontend", "p95_ms": 120.5, "error_rate_pct": 0.0}]),
        tool_entry("M2", "query_metrics", [{"service": "checkout", "p95_ms": 10250.0}]),
        tool_entry("C1", "container_health", [
            {"name": "valkey-cart", "status": "exited", "health": "unhealthy", "restart_count": 2,
             "exit_code": 137, "oom_killed": True}],
            {"summary": {"total": 1, "running": 0}}),
    ])


def ok(ledger_id, kind, quote, value=None, led=None):
    return validate_evidence(candidate(item(ledger_id, kind, quote, value)), led or keyed_ledger()).items[0]


@pytest.mark.parametrize("ledger_id,kind,quote", [
    ("M1", "metric", '"p95_ms":15000.0'),
    ("M1", "metric", 'p95_ms":15000.0'),
    ("M1", "metric", '"p95_ms": 15000.0'),
    ("M1", "metric", '"p95_ms" : 15000.0,'),
    ("M1", "metric", "p95_ms 15000"),
    ("M1", "metric", "p95_ms: 15000.0"),
    ("M1", "metric", '"p95_ms":15100.0'),
    ("M1", "metric", '"error_rate_pct":0.0'),
    ("M1", "metric", '"service":"cart"'),
    ("M2", "metric", 'p95_ms":10250.0'),
    ("C1", "container", '"oom_killed":true'),
    ("C1", "container", 'oom_killed":true'),
    ("C1", "container", "oom_killed true"),
    ("C1", "container", "oom_killed True"),
    ("C1", "container", '"status":"exited"'),
    ("C1", "container", "status exited"),
    ("C1", "container", '"exit_code":137'),
])
def test_key_value_quote_from_the_compact_view_is_valid(ledger_id, kind, quote):
    assert ok(ledger_id, kind, quote)[S.EV_VALIDATED] is True, quote


@pytest.mark.parametrize("quote", ['oom_killed":true', "oom_killed true", '"oom_killed":true'])
def test_key_value_shape_is_valid_as_the_value_field(quote):
    out = ok("C1", "container", "valkey-cart", value=quote)
    assert out[S.EV_VALIDATED] is True, out[S.EV_NOTE]
    assert ok("M1", "metric", "15000.0", value='"p95_ms":15000.0')[S.EV_VALIDATED] is True


@pytest.mark.parametrize("ledger_id,kind,quote", [
    ("M1", "metric", '"latency_ms":15000.0'),  # key not in the entry
    ("M1", "metric", '"p95_ms":900.0'),  # wrong value
    ("M1", "metric", '"p95_ms":16000.0'),  # number outside the tolerance
    ("M1", "metric", '"p95_ms":150'),  # a prefix of the real digits
    ("M1", "metric", '"p95_ms":10250.0'),  # taken from M2
    ("M1", "metric", '"service":"checkout"'),  # taken from M2
    ("M1", "metric", '"error_rate_pct":3.5'),
    ("M1", "metric", '"p95_ms":true'),
    ("C1", "container", "oom_killed false"),  # boolean mismatch
    ("C1", "container", '"oom_killed":false'),
    ("C1", "container", '"oom_killed":1'),  # a number is not a boolean
    ("C1", "container", '"exit_code":0'),
    ("C1", "container", '"exit_code":true'),
    ("C1", "container", "status running"),
    ("C1", "container", '"health":"healthy"'),
])
def test_key_value_quote_that_is_wrong_is_rejected(ledger_id, kind, quote):
    assert ok(ledger_id, kind, quote)[S.EV_VALIDATED] is False, quote


def test_key_value_quote_is_rejected_when_the_value_belongs_to_another_entry():
    out = ok("M2", "metric", '"p95_ms":15000.0')
    assert out[S.EV_VALIDATED] is False
    assert "never with the value" in out[S.EV_NOTE]


def test_key_value_value_field_rejects_wrong_and_foreign_values():
    for wrong in ("oom_killed false", '"p95_ms":15000.0', '"restart_count":9', "nonexistent_key true"):
        assert ok("C1", "container", "valkey-cart", value=wrong)[S.EV_VALIDATED] is False, wrong


def test_key_value_check_only_sees_what_the_view_shows():
    led = load_ledger([tool_entry("C1", "container_health", [
        {"name": "web", "status": "running", "health": "healthy", "restart_count": 0, "oom_killed": False}])])
    assert ok("C1", "container", "oom_killed false", led=led)[S.EV_VALIDATED] is False  # healthy rows are not shown


def test_key_value_quote_works_for_an_entry_too_thin_to_render():
    led = {"M1": {"id": "M1", "result": {"items": [{"p95_ms": 15000.0}]}}}
    assert ok("M1", "metric", '"p95_ms":15000.0', led=led)[S.EV_VALIDATED] is True
    assert ok("M1", "metric", '"p95_ms":9000.0', led=led)[S.EV_VALIDATED] is False


def test_key_value_shape_is_not_applied_to_log_entries():
    led = {"L1": {"id": "L1", "tool": "query_logs", "result": {"items": [{"count": 15000, "message": "something failed"}]}}}
    assert ok("L1", "log", '"count":15000', led=led)[S.EV_VALIDATED] is False


def test_quote_may_be_a_string_as_the_view_escapes_it():
    message = 'upstream said "no route" to host'
    led = {"L1": {"id": "L1", "tool": "query_logs", "result": {"items": [{"message": message}]}}}
    assert ok("L1", "log", message, led=led)[S.EV_VALIDATED] is True
    assert ok("L1", "log", r'upstream said \"no route\" to host', led=led)[S.EV_VALIDATED] is True
    assert ok("L1", "log", r'upstream said \"no road\" to host', led=led)[S.EV_VALIDATED] is False


def test_json_fragments_that_span_values_are_still_rejected():
    for quote in ('15000.0,"error_rate_pct"', '":15000.0,"', '{"service":"cart","p95_ms"'):
        assert ok("M1", "metric", quote)[S.EV_VALIDATED] is False, quote
