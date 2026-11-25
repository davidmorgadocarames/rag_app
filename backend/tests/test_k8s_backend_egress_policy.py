"""T16.2.2/T16.2.4 self-reported correction to ``deploy/k8s/base/ollama/networkpolicy.yaml.tmpl``
(the ``backend-egress-ollama`` NetworkPolicy, T16.0.4/16-A).

16-A's own comment claimed k3d's default CNI does not enforce NetworkPolicy, so the ONLY
egress rule (Ollama, port 11434) was believed to be a harmless no-op. That claim is false on
this cluster's actual k3s version (confirmed live while building this block's chaos drills): a
real backend image went CrashLoopBackOff with a DNS resolution failure, and a raw TCP probe to
both kube-dns:53 and Postgres:5432 came back "Connection refused" -- deleting the NetworkPolicy
made both connect immediately. The original policy therefore silently broke the backend's own
DNS/Postgres egress the moment a real (non-``:placeholder``) image replaced the placeholder.

This is a pure YAML-shape assertion on the rendered template (``${OLLAMA_HOST_IP}`` substituted
with a throwaway value, same convention ``envsubst`` uses in ``scripts/k8s/cluster-up.sh`` --
no cluster, no envsubst binary needed since Python does the substitution directly). Wired into
the ``pytest`` gate step (no ``db`` marker, no cluster dependency)."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "deploy" / "k8s" / "base" / "ollama" / "networkpolicy.yaml.tmpl"


def _rendered() -> dict:
    text = TEMPLATE.read_text(encoding="utf-8").replace("${OLLAMA_HOST_IP}", "203.0.113.10")
    doc = yaml.safe_load(text)
    assert doc["kind"] == "NetworkPolicy"
    assert doc["spec"]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "backend"}
    return doc


def _egress_rules() -> list[dict]:
    return _rendered()["spec"]["egress"]


def test_template_renders_as_valid_yaml() -> None:
    assert _rendered()


def test_ollama_rule_is_still_present() -> None:
    """Regression guard: the fix must not drop the original T16.0.4 rule."""
    rules = _egress_rules()
    assert any(
        rule.get("to", [{}])[0].get("ipBlock", {}).get("cidr") == "203.0.113.10/32"
        and any(p.get("port") == 11434 for p in rule.get("ports", []))
        for rule in rules
    )


def test_dns_egress_to_kube_dns_is_allowed() -> None:
    """The fix this test is written for: without this rule the backend cannot resolve ANY
    in-cluster Service name, confirmed live (see module docstring)."""
    rules = _egress_rules()
    dns_rules = [
        rule
        for rule in rules
        for to in rule.get("to", [])
        if to.get("podSelector", {}).get("matchLabels", {}).get("k8s-app") == "kube-dns"
    ]
    assert dns_rules, "no egress rule targets kube-dns by podSelector"
    ports = {p["port"] for rule in dns_rules for p in rule.get("ports", [])}
    assert 53 in ports


def test_postgres_egress_to_cnpg_is_allowed() -> None:
    rules = _egress_rules()
    pg_rules = [
        rule
        for rule in rules
        for to in rule.get("to", [])
        if to.get("podSelector", {}).get("matchLabels", {}).get("cnpg.io/cluster") == "secrag-pg"
    ]
    assert pg_rules, "no egress rule targets the CNPG cluster's pods by podSelector"
    ports = {p["port"] for rule in pg_rules for p in rule.get("ports", [])}
    assert 5432 in ports


def test_mailpit_and_prometheus_egress_are_allowed() -> None:
    rules = _egress_rules()

    def targets(name: str) -> list[dict]:
        return [
            rule
            for rule in rules
            for to in rule.get("to", [])
            if to.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/name")
            == name
        ]

    mailpit_rules = targets("mailpit")
    assert mailpit_rules and {p["port"] for r in mailpit_rules for p in r["ports"]} == {1025}

    prometheus_rules = targets("prometheus")
    assert prometheus_rules and {p["port"] for r in prometheus_rules for p in r["ports"]} == {9090}
