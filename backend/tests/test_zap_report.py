"""``zap-baseline`` gate step judgment logic (T12.2.6, E8): see
``rag_app.devtools.zap_report``'s module docstring. No DB, no docker, no ZAP -- pure JSON/
TSV parsing and the pass/fail rule, run as a plain ``pytest`` step."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rag_app.devtools.zap_report import Alert, check, main, parse_alerts, parse_rules


def _report(*alerts: dict[str, object]) -> dict[str, object]:
    return {"site": [{"@name": "http://example.test", "alerts": list(alerts)}]}


def test_parse_alerts_uses_riskcode_when_present() -> None:
    report = _report({"pluginid": "10038", "name": "CSP header not set", "riskcode": "3"})
    assert parse_alerts(report) == [Alert("10038", "CSP header not set", "High")]


def test_parse_alerts_falls_back_to_riskdesc() -> None:
    report = _report(
        {"pluginid": "10054", "name": "Cookie No SameSite", "riskdesc": "Medium (Medium)"}
    )
    assert parse_alerts(report) == [Alert("10054", "Cookie No SameSite", "Medium")]


def test_parse_alerts_across_multiple_sites() -> None:
    report = {
        "site": [
            {"alerts": [{"pluginid": "1", "name": "a", "riskcode": "1"}]},
            {"alerts": [{"pluginid": "2", "name": "b", "riskcode": "3"}]},
        ]
    }
    assert [a.plugin_id for a in parse_alerts(report)] == ["1", "2"]


def test_parse_alerts_tolerates_a_malformed_report() -> None:
    assert parse_alerts({}) == []
    assert parse_alerts({"site": "not a list"}) == []
    assert parse_alerts({"site": [{"alerts": "not a list"}]}) == []
    assert parse_alerts({"site": [{"alerts": ["not a dict"]}]}) == []


def test_parse_rules_skips_blank_lines_and_comments() -> None:
    text = "# header\n\n10038\treal reason here\n"
    assert parse_rules(text) == {"10038": "real reason here"}


@pytest.mark.parametrize(
    "bad",
    [
        "10038\n",  # no tab / reason
        "10038\t\n",  # empty reason
        "\treason\n",  # empty plugin id
        "10038\treason\textra\n",  # too many fields
    ],
)
def test_parse_rules_rejects_a_malformed_row(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_rules(bad)


def test_check_fails_only_unexempted_high_risk_alerts() -> None:
    alerts = [
        Alert("1", "high not exempt", "High"),
        Alert("2", "high exempt", "High"),
        Alert("3", "medium", "Medium"),
        Alert("4", "low", "Low"),
    ]
    failures = check(alerts, {"2": "known, accepted — see ADR"})
    assert [a.plugin_id for a in failures] == ["1"]


def test_check_passes_with_no_high_risk_alerts_at_all() -> None:
    alerts = [Alert("3", "medium", "Medium"), Alert("4", "low", "Low")]
    assert check(alerts, {}) == []


# --- CLI (main) ---------------------------------------------------------------------------


def test_main_passes_with_zero_alerts(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    rules = tmp_path / "zap-rules.tsv"
    report.write_text(json.dumps(_report()), encoding="utf-8")
    rules.write_text("# no exceptions needed\n", encoding="utf-8")
    assert main(["--report", str(report), "--rules", str(rules)]) == 0


def test_main_fails_on_an_unexempted_high_risk_alert(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    rules = tmp_path / "zap-rules.tsv"
    report.write_text(
        json.dumps(_report({"pluginid": "10038", "name": "CSP header not set", "riskcode": "3"})),
        encoding="utf-8",
    )
    rules.write_text("# no exceptions\n", encoding="utf-8")
    assert main(["--report", str(report), "--rules", str(rules)]) == 1


def test_main_passes_when_the_high_risk_alert_is_exempted_with_a_reason(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    rules = tmp_path / "zap-rules.tsv"
    report.write_text(
        json.dumps(_report({"pluginid": "10038", "name": "CSP header not set", "riskcode": "3"})),
        encoding="utf-8",
    )
    rules.write_text("10038\taccepted, see ADR phase 12\n", encoding="utf-8")
    assert main(["--report", str(report), "--rules", str(rules)]) == 0


def test_main_fails_when_the_report_file_is_missing(tmp_path: Path) -> None:
    rules = tmp_path / "zap-rules.tsv"
    rules.write_text("# empty\n", encoding="utf-8")
    assert main(["--report", str(tmp_path / "missing.json"), "--rules", str(rules)]) == 1


def test_main_fails_when_the_rules_file_is_missing(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_report()), encoding="utf-8")
    assert main(["--report", str(report), "--rules", str(tmp_path / "missing.tsv")]) == 1


def test_main_fails_on_malformed_json(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    rules = tmp_path / "zap-rules.tsv"
    report.write_text("not json", encoding="utf-8")
    rules.write_text("# empty\n", encoding="utf-8")
    assert main(["--report", str(report), "--rules", str(rules)]) == 1


def test_main_fails_on_a_malformed_rules_file(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    rules = tmp_path / "zap-rules.tsv"
    report.write_text(json.dumps(_report()), encoding="utf-8")
    rules.write_text("10038\n", encoding="utf-8")
    assert main(["--report", str(report), "--rules", str(rules)]) == 1


def test_the_committed_rules_file_itself_is_well_formed() -> None:
    """scripts/zap/zap-rules.tsv (the real, tracked exceptions file) must always parse --
    this fails the gate's `pytest` step (fast, no docker/ZAP needed) the moment someone edits
    it into a broken shape, rather than waiting for the much slower `zap-baseline` step."""
    root = Path(__file__).resolve().parents[2]
    text = (root / "scripts" / "zap" / "zap-rules.tsv").read_text(encoding="utf-8")
    parse_rules(text)  # raises on a malformed row
