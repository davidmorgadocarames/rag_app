"""scripts/azure/db-tunnel.sh (T11.1.2) against a fake `az`, `curl` and `psql`.

The fake `az` keeps the server's firewall rules in a JSON file, so each test can check that
the tunnel's rule exists only while the command runs: removed on success, on error, on
SIGINT/SIGTERM, and swept when an earlier run left one behind. No test talks to Azure.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TUNNEL = REPO_ROOT / "scripts" / "azure" / "db-tunnel.sh"
HOST = "secrag-db-dmc26.postgres.database.azure.com"
IP = "203.0.113.7"  # TEST-NET-3 — "public" as far as the script's check goes
PASSWORD = "s3cr:et\\pa%ss"  # ':' and '\' must be escaped in the pgpass file
ENCODED = "s3cr%3Aet%5Cpa%25ss"  # the same password, URL-encoded as in DATABASE_URL

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

FAKE_AZ = r"""
import json, os, re, sys
argv = sys.argv[1:]
state_path = os.environ["FAKE_AZ_STATE"]
with open(os.environ["FAKE_AZ_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(argv) + "\n")
state = json.load(open(state_path, encoding="utf-8"))
fail = os.environ.get("FAKE_AZ_FAIL", "").split(",")

def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None

def save():
    json.dump(state, open(state_path, "w", encoding="utf-8"))

cmd = " ".join(a for a in argv if not a.startswith("-"))[:80]
if argv[:3] == ["postgres", "flexible-server", "firewall-rule"]:
    verb = argv[3]
    if verb in fail:
        sys.stderr.write(f"fake az: {verb} failed\n"); sys.exit(1)
    assert opt("-g") == "rg-secrag" and opt("-s") == "secrag-db-dmc26", argv
    rules = state["rules"]
    if verb == "list":
        q = opt("--query") or ""
        m = re.search(r"starts_with\(name, '([^']*)'\)", q)
        e = re.search(r"name=='([^']*)'", q)
        names = [r["name"] for r in rules
                 if (m and r["name"].startswith(m.group(1))) or (e and r["name"] == e.group(1))]
        print("\n".join(names))
    elif verb == "create":
        rule = {"name": opt("-n"), "start": opt("--start-ip-address"),
                "end": opt("--end-ip-address")}
        if os.environ.get("FAKE_AZ_CREATE_LATE"):
            # The ARM operation outlives this client: a detached helper adds the rule after a
            # delay, while this `az` process hangs until the test interrupts it (DA-D-4).
            import subprocess, time
            helper = (
                "import json, sys, time\n"
                "time.sleep(float(sys.argv[3]))\n"
                "s = json.load(open(sys.argv[1]))\n"
                "s['rules'].append(json.loads(sys.argv[2]))\n"
                "json.dump(s, open(sys.argv[1], 'w'))\n"
            )
            subprocess.Popen([sys.executable, "-c", helper, state_path, json.dumps(rule),
                              os.environ["FAKE_AZ_CREATE_LATE"]], start_new_session=True)
            open(state_path + ".creating", "w").close()
            time.sleep(60)
        rules.append(rule)
        save()
    elif verb == "delete":
        if "--yes" not in argv:
            sys.exit(2)
        before = len(rules)
        state["rules"] = [r for r in rules if r["name"] != opt("-n")]
        if len(state["rules"]) == before:
            sys.stderr.write("not found\n"); sys.exit(3)
        save()
    sys.exit(0)
if argv[:3] == ["postgres", "flexible-server", "show"]:
    q = opt("--query")
    print({"fullyQualifiedDomainName": state.get("host", ""), "administratorLogin": "pgadmin"}[q])
    sys.exit(0)
if argv[:3] == ["containerapp", "secret", "show"]:
    assert opt("--secret-name") == "database-url" and opt("-n") == "secrag-backend", argv
    print(os.environ["FAKE_DB_URL"])
    sys.exit(0)
sys.stderr.write("fake az: unexpected call\n"); sys.exit(1)
"""

FAKE_CURL = r"""#!/bin/sh
url=""
for a in "$@"; do url="$a"; done
case "$url" in
  *ipify*) ip="${FAKE_IP_A-}" ;;
  *amazonaws*) ip="${FAKE_IP_B-}" ;;
  *) exit 7 ;;
esac
[ -n "$ip" ] || exit 7
echo "$ip"
"""

FAKE_PSQL = r"""
import json, os, signal, sys, time
if os.environ.get("FAKE_PSQL_IGNORE_INT"):
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # like interactive psql at its prompt
state = json.load(open(os.environ["FAKE_AZ_STATE"], encoding="utf-8"))
pgpass = os.environ.get("PGPASSFILE")
record = {
    "pid": os.getpid(),
    "sigint": str(signal.getsignal(signal.SIGINT)),
    "stdin": sys.stdin.read() if os.environ.get("FAKE_PSQL_STDIN") else None,
    "argv": sys.argv[1:],
    "rules": state["rules"],
    "env": {k: os.environ.get(k) for k in (
        "PGHOST", "PGPORT", "PGUSER", "PGDATABASE", "PGSSLMODE", "PGOPTIONS", "PGPASSWORD")},
    "pgpass": open(pgpass, encoding="utf-8").read() if pgpass else None,
    "pgpass_mode": oct(os.stat(pgpass).st_mode & 0o777) if pgpass else None,
}
json.dump(record, open(os.environ["FAKE_PSQL_OUT"], "w", encoding="utf-8"))
if os.environ.get("FAKE_PSQL_SLEEP"):
    open(os.environ["FAKE_PSQL_OUT"] + ".started", "w").close()
    time.sleep(float(os.environ["FAKE_PSQL_SLEEP"]))
sys.exit(int(os.environ.get("FAKE_PSQL_RC", "0")))
"""


@pytest.fixture
def fake(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("az", FAKE_AZ), ("psql", FAKE_PSQL), ("pg_dump", FAKE_PSQL)):
        (bin_dir / f"{name}.py").write_text(body, encoding="utf-8")
        wrapper = bin_dir / name
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / name}.py" "$@"\n')
        wrapper.chmod(0o755)
    (bin_dir / "curl").write_text(FAKE_CURL)
    (bin_dir / "curl").chmod(0o755)
    state = tmp_path / "az_state.json"

    class Fake:
        def __init__(self) -> None:
            self.tmp = tmp_path
            self.state = state
            self.az_log = tmp_path / "az.log"
            self.psql_out = tmp_path / "psql.json"
            self.set_rules([])
            self.env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "FAKE_AZ_STATE": str(state),
                "FAKE_AZ_LOG": str(self.az_log),
                "FAKE_PSQL_OUT": str(self.psql_out),
                "FAKE_IP_A": IP,
                "FAKE_IP_B": IP,
                "FAKE_DB_URL": f"postgresql://secragadmin:{ENCODED}@{HOST}/rag?sslmode=require",
                "TMPDIR": str(tmp_path),
            }
            for var in (
                "PGPASSWORD",
                "PGPASSFILE",
                "PGOPTIONS",
                "DB_TUNNEL_RG",
                "DB_TUNNEL_SERVER",
            ):
                self.env.pop(var, None)

        def set_rules(self, rules: list[dict[str, str]], host: str = HOST) -> None:
            state.write_text(json.dumps({"rules": rules, "host": host}), encoding="utf-8")

        def rules(self) -> list[str]:
            return [r["name"] for r in json.loads(state.read_text(encoding="utf-8"))["rules"]]

        def az_calls(self) -> list[list[str]]:
            if not self.az_log.exists():
                return []
            return [json.loads(line) for line in self.az_log.read_text().splitlines()]

        def created(self) -> list[list[str]]:
            return [c for c in self.az_calls() if c[3:4] == ["create"]]

        def psql(self) -> dict:
            return json.loads(self.psql_out.read_text(encoding="utf-8"))

        def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["bash", str(TUNNEL), *args],
                env={**self.env, **env},
                capture_output=True,
                text=True,
                timeout=60,
            )

    return Fake()


APP_ARGS = ("--password-from-app", "--", "psql", "-X", "-At", "-c", "select 1")


def test_rule_exists_only_during_the_call(fake) -> None:
    proc = fake.run(*APP_ARGS)
    assert proc.returncode == 0, proc.stderr
    during = fake.psql()
    assert len(during["rules"]) == 1
    rule = during["rules"][0]
    assert rule["name"].startswith("secrag-tunnel-")
    assert rule["start"] == rule["end"] == IP  # exactly this machine, /32
    assert fake.rules() == []  # removed afterwards
    assert "removed" in proc.stderr


def test_connection_is_tls_read_only_and_password_free(fake) -> None:
    proc = fake.run(*APP_ARGS)
    assert proc.returncode == 0, proc.stderr
    rec = fake.psql()
    env = rec["env"]
    assert env["PGHOST"] == HOST and env["PGPORT"] == "5432"
    assert env["PGUSER"] == "secragadmin" and env["PGDATABASE"] == "rag"
    assert env["PGSSLMODE"] == "require"
    assert env["PGOPTIONS"] == "-c default_transaction_read_only=on"
    assert env["PGPASSWORD"] is None
    # libpq gets the decoded password from a private pgpass file (':' and '\' escaped)…
    assert rec["pgpass"] == f"{HOST}:5432:rag:secragadmin:s3cr\\:et\\\\pa%ss\n"
    assert rec["pgpass_mode"] == "0o600"
    # …which is gone afterwards, and the password is never printed or passed on argv.
    assert not list(fake.tmp.glob("secrag-pgpass.*"))
    for text in (proc.stdout, proc.stderr, fake.az_log.read_text(), json.dumps(rec["argv"])):
        assert PASSWORD not in text and ENCODED not in text


def test_read_write_is_explicit(fake) -> None:
    proc = fake.run("--read-write", *APP_ARGS)
    assert proc.returncode == 0, proc.stderr
    assert fake.psql()["env"]["PGOPTIONS"] is None
    assert "READ-WRITE" in proc.stderr


def test_rule_removed_when_the_command_fails(fake) -> None:
    proc = fake.run(*APP_ARGS, FAKE_PSQL_RC="3")
    assert proc.returncode == 3
    assert len(fake.psql()["rules"]) == 1
    assert fake.rules() == []


SIGNALS = [(signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)]
SIGNAL_IDS = ["SIGINT", "SIGTERM", "SIGHUP"]


def _start_long_psql(fake, **env: str) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        ["bash", str(TUNNEL), *APP_ARGS],
        env={**fake.env, "FAKE_PSQL_SLEEP": "30", "DB_TUNNEL_GRACE": "1", **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # own process group, like a terminal's foreground job
    )
    started = Path(str(fake.psql_out) + ".started")
    deadline = time.monotonic() + 30
    while not started.exists():
        assert proc.poll() is None, proc.communicate()
        assert time.monotonic() < deadline, "psql never started"
        time.sleep(0.05)
    assert len(fake.rules()) == 1
    return proc


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path(f"/proc/{pid}/stat")
    return not (status.exists() and status.read_text().split()[2] == "Z")


@pytest.mark.parametrize(("sig", "rc"), SIGNALS, ids=SIGNAL_IDS)
def test_rule_removed_on_a_signal_to_the_process_group(fake, sig: signal.Signals, rc: int) -> None:
    proc = _start_long_psql(fake)
    os.killpg(proc.pid, sig)  # Ctrl-C / a closed terminal reach the whole foreground group
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == rc, err
    assert fake.rules() == []
    assert "removed" in err


@pytest.mark.parametrize(("sig", "rc"), SIGNALS, ids=SIGNAL_IDS)
def test_a_signal_to_the_script_pid_only_ends_the_tunnel_promptly(
    fake, sig: signal.Signals, rc: int
) -> None:
    """DA-D-4: `kill <script pid>` while psql runs is handled at once (not after psql exits):
    forwarded to psql, the rule removed, psql gone. FAKE_PSQL_IGNORE_INT: a psql that ignores
    the forwarded signal (interactive psql on SIGINT) is ended by the watchdog."""
    proc = _start_long_psql(fake, FAKE_PSQL_IGNORE_INT="1")
    t0 = time.monotonic()
    os.kill(proc.pid, sig)  # the script only
    _, err = proc.communicate(timeout=30)
    elapsed = time.monotonic() - t0
    assert proc.returncode == rc, err
    assert elapsed < 12, f"took {elapsed:.1f}s (psql would have run 30 s)"
    assert fake.rules() == []
    assert "removed" in err and f"received {sig.name}" in err
    assert not _alive(fake.psql()["pid"])


def test_the_command_keeps_default_sigint_and_the_callers_stdin(fake) -> None:
    """Background children of a non-interactive shell would ignore SIGINT and read /dev/null;
    the tunnel restores both (psql's Ctrl-C handling, `psql -f -`, interactive use)."""
    proc = subprocess.run(
        ["bash", str(TUNNEL), *APP_ARGS],
        env={**fake.env, "FAKE_PSQL_STDIN": "1"},
        input="select 42;\n",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    rec = fake.psql()
    assert rec["stdin"] == "select 42;\n"
    assert "default_int_handler" in rec["sigint"]


def test_an_interrupted_create_is_watched_for_a_late_rule(fake) -> None:
    """DA-D-4: Ctrl-C during `fw create` kills the az client, but the ARM operation completes
    later and adds the rule; the cleanup keeps watching and removes it."""
    proc = subprocess.Popen(
        ["bash", str(TUNNEL), *APP_ARGS],
        env={
            **fake.env,
            "FAKE_AZ_CREATE_LATE": "1.5",
            "DB_TUNNEL_RECHECK": "5",
            "DB_TUNNEL_RECHECK_POLL": "0.5",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    creating = Path(str(fake.state) + ".creating")
    deadline = time.monotonic() + 30
    while not creating.exists():
        assert proc.poll() is None, proc.communicate()
        assert time.monotonic() < deadline, "create never started"
        time.sleep(0.05)
    assert fake.rules() == []  # not there yet
    os.killpg(proc.pid, signal.SIGINT)
    _, err = proc.communicate(timeout=60)
    assert proc.returncode == 130, err
    assert "late rule" in err and "removed" in err
    assert fake.rules() == []
    assert not fake.psql_out.exists()


def test_stale_rules_are_swept_at_the_start_of_every_call(fake) -> None:
    fake.set_rules(
        [
            {
                "name": "secrag-tunnel-20260901T000000Z-1",
                "start": "198.51.100.1",
                "end": "198.51.100.1",
            },
            {"name": "AllowOffice", "start": "198.51.100.9", "end": "198.51.100.9"},
        ]
    )
    proc = fake.run(*APP_ARGS)
    assert proc.returncode == 0, proc.stderr
    assert [r["name"] for r in fake.psql()["rules"]][0] == "AllowOffice"
    assert len(fake.psql()["rules"]) == 2  # the stale one was gone before ours was created
    assert fake.rules() == ["AllowOffice"]  # unrelated rules are never touched


def test_sweep_only(fake) -> None:
    fake.set_rules([{"name": "secrag-tunnel-x-1", "start": IP, "end": IP}])
    proc = fake.run("--sweep", FAKE_IP_A="")  # no IP lookup, no rule, no psql
    assert proc.returncode == 0, proc.stderr
    assert fake.rules() == []
    assert not fake.created() and not fake.psql_out.exists()


@pytest.mark.parametrize(
    ("ip_a", "ip_b"),
    [
        ("", IP),  # a source does not answer
        (IP, "198.51.100.23"),  # the sources disagree
        ("192.168.1.10", "192.168.1.10"),  # private
        ("10.0.0.1", "10.0.0.1"),
        ("100.64.0.1", "100.64.0.1"),  # CGNAT
        ("not-an-ip", "not-an-ip"),
        ("2001:db8::1", "2001:db8::1"),  # IPv6: the rule is IPv4 only
    ],
)
def test_unknown_public_ip_fails_closed(fake, ip_a: str, ip_b: str) -> None:
    proc = fake.run(*APP_ARGS, FAKE_IP_A=ip_a, FAKE_IP_B=ip_b)
    assert proc.returncode == 1
    assert "public IP unknown" in proc.stderr
    assert not fake.created() and not fake.psql_out.exists()


def test_a_failing_delete_is_loud_and_fails(fake) -> None:
    proc = fake.run(*APP_ARGS, FAKE_AZ_FAIL="delete")
    assert proc.returncode == 1
    assert "could not confirm that firewall rule" in proc.stderr
    assert "--sweep" in proc.stderr
    assert len(fake.rules()) == 1  # (the fake refused; the operator is told to remove it)


def test_a_secret_for_another_host_is_refused(fake) -> None:
    fake.set_rules([], host="other-server.postgres.database.azure.com")
    proc = fake.run(*APP_ARGS)
    assert proc.returncode == 1 and "another host" in proc.stderr
    assert not fake.created()


def test_without_the_app_secret_the_admin_login_and_db_are_used(fake) -> None:
    proc = fake.run("--db", "rag", "--", "pg_dump", "-Fc", "-f", "x.dump")
    assert proc.returncode == 0, proc.stderr
    env = fake.psql()["env"]
    assert env["PGUSER"] == "pgadmin" and env["PGDATABASE"] == "rag"
    assert fake.psql()["pgpass"] is None  # password: PGPASSWORD / ~/.pgpass / psql's prompt


@pytest.mark.parametrize(
    "cmd",
    [
        ["bash", "-c", "true"],
        ["psql", "postgresql://u:p@h/db"],
        ["psql", "host=h password=p"],
        # DA-D-5: a URI or password anywhere in an argument, not only at its start
        ["psql", "--dbname=postgresql://u:p@h/db"],
        ["psql", "--dbname=postgres://u@h/db"],
        ["psql", "-dpostgresql://u:p@h/db"],
        ["psql", "-d", "postgresql://u@h/db"],
        ["psql", "-d", "dbname=rag password=p"],
        ["pg_dump", "--dbname=host=h PASSWORD=p"],
        [],
    ],
)
def test_only_psql_or_pg_dump_without_connection_strings(fake, cmd: list[str]) -> None:
    proc = fake.run("--password-from-app", "--", *cmd)
    assert proc.returncode in (1, 2)
    assert fake.az_calls() == []  # refused before any Azure call
