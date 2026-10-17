"""``zap-baseline`` gate step (T12.2.6, E8): judge an OWASP ZAP baseline scan's JSON report
against the project's own rule: any **High**-risk alert fails the step unless its ZAP plugin
id is listed, with a reason, in the committed exceptions file (``scripts/zap/zap-rules.tsv``).

``zap-baseline.py``'s own exit code (its ``-c``/threshold mechanism, WARN-by-default on every
rule) is deliberately not used for pass/fail here — it has no notion of "fail only on High".
This module reads the real ``risk``/``riskcode`` of every alert in the JSON report
(``-J report.json``) instead, which is independent of any per-rule threshold configuration.
Medium/Low/Informational alerts are always reported (so nothing is hidden) but never fail
the step — a passive baseline scan against a small app routinely turns up a few of those
(missing cache headers, etc.) that are not what T12.2.6's Done-when ("any High finding
fails") is about.

    python -m rag_app.devtools.zap_report --report <report.json> --rules <rules.tsv>

Exit code: 0 no unexempted High-risk alert; 1 otherwise, or the report/rules file is
missing/unreadable/malformed.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

HIGH = "High"
# ZAP's own numeric risk code -> name (seen in every alert's "riskcode" field; the human
# "riskdesc" field, e.g. "Medium (Medium)", is "<risk> (<confidence>)" and is only a fallback
# here in case a future ZAP version drops "riskcode").
RISK_CODE_NAMES = {"0": "Informational", "1": "Low", "2": "Medium", "3": "High"}


@dataclass(frozen=True)
class Alert:
    plugin_id: str
    name: str
    risk: str


def _risk_of(item: dict[str, object]) -> str:
    code = str(item.get("riskcode", "")).strip()
    if code in RISK_CODE_NAMES:
        return RISK_CODE_NAMES[code]
    desc = str(item.get("riskdesc", "")).strip()
    return desc.split(" ", 1)[0] if desc else "Unknown"


def parse_alerts(report: dict[str, object]) -> list[Alert]:
    """Every alert across every ``site`` in a ZAP baseline ``-J`` report."""
    alerts: list[Alert] = []
    sites = report.get("site", [])
    if not isinstance(sites, list):
        return alerts
    for site in sites:
        if not isinstance(site, dict):
            continue
        items = site.get("alerts", [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            alerts.append(
                Alert(
                    plugin_id=str(item.get("pluginid", "")),
                    name=str(item.get("name", "unknown")),
                    risk=_risk_of(item),
                )
            )
    return alerts


def parse_rules(text: str) -> dict[str, str]:
    """``plugin id -> reason`` from the committed exceptions file. Blank lines and ``#``
    comments are skipped; every data row MUST be exactly ``<plugin id>\\t<reason>`` (E8: "a
    reason per exception") — a row missing either field is a hard error, never silently
    ignored (an exceptions file that quietly stops excluding something must not look like it
    still does)."""
    exceptions: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            raise ValueError(f"line {lineno}: expected '<plugin id>\\t<reason>', got: {line!r}")
        exceptions[parts[0].strip()] = parts[1].strip()
    return exceptions


def check(alerts: list[Alert], exceptions: dict[str, str]) -> list[Alert]:
    """Unexempted High-risk alerts; empty means the step passes."""
    return [a for a in alerts if a.risk == HIGH and a.plugin_id not in exceptions]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--rules", type=Path, required=True)
    args = parser.parse_args(argv)

    if not args.report.is_file():
        print(
            f"zap-baseline: FAIL — report not found at {args.report} "
            "(did the ZAP container actually run?)",
            file=sys.stderr,
        )
        return 1
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"zap-baseline: FAIL — report is not valid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(report, dict):
        print("zap-baseline: FAIL — report is not a JSON object", file=sys.stderr)
        return 1

    if not args.rules.is_file():
        print(f"zap-baseline: FAIL — rules file not found at {args.rules}", file=sys.stderr)
        return 1
    try:
        exceptions = parse_rules(args.rules.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"zap-baseline: FAIL — {exc}", file=sys.stderr)
        return 1

    alerts = parse_alerts(report)
    by_risk: dict[str, int] = {}
    for alert in alerts:
        by_risk[alert.risk] = by_risk.get(alert.risk, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(by_risk.items()))
    print(f"zap-baseline: {len(alerts)} alert(s) ({summary})")

    for alert in alerts:
        if alert.risk == HIGH and alert.plugin_id in exceptions:
            print(
                f"  exempted (High): plugin {alert.plugin_id} ({alert.name}) — "
                f"{exceptions[alert.plugin_id]}"
            )

    failures = check(alerts, exceptions)
    for alert in failures:
        print(
            f"  FAIL High-risk, not exempted: plugin {alert.plugin_id} ({alert.name})",
            file=sys.stderr,
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
