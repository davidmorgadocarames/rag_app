"""Regression guard for OWASP-A02 (``threat-catalogue``, T12.2.3): the REAL proof that the
frontend's nonce CSP is enforcing with zero violations is a real-browser Playwright walk
(``frontend/e2e/security-headers.spec.ts``, ``scripts/gate.sh``'s `security-headers` step) --
not Python-testable here. ``rag_app.devtools.threat_catalogue`` requires every `mitigated`
catalogue row to point at a resolvable `path.py::test_function`, so this fast, no-browser
test is the one that actually backs that row in CI/the cheap `pytest` step: it catches a
silent revert of `CSP_MODE` (``frontend/lib/csp.ts``) back to `"report-only"` long before the
much slower `security-headers` gate step (`--full` only) would."""

from __future__ import annotations

from pathlib import Path

FRONTEND_CSP = Path(__file__).resolve().parents[2] / "frontend" / "lib" / "csp.ts"


def test_csp_mode_is_enforce_not_report_only() -> None:
    text = FRONTEND_CSP.read_text(encoding="utf-8")
    assert 'return "enforce";' in text, (
        'frontend/lib/csp.ts\'s cspMode() no longer returns "enforce" -- T12.2.3 and '
        "OWASP-A02 (threat-catalogue) both depend on CSP_MODE staying enforcing once proven "
        "clean by the security-headers gate step; re-run it before reverting this"
    )
