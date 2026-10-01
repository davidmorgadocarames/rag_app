"""The filtered restore list of ``scripts/db/restore.sh`` (``rag_app.restore_toc``).

The first real rehearsal (2026-10-01) failed on ``CREATE EXTENSION azure``: a dump of the
Azure Flexible Server carries entries no other server can take (Azure-managed extensions,
grants on ``pg_catalog`` to ``azuresu``, the ``azure_pg_admin`` ACL of schema ``public``).
These tests use a TOC shaped like that dump; the end-to-end check (real pg_dump / age /
pg_restore, a role dropped after the backup, privileges compared) is the gate step
``backup-drill``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_app import restore_toc
from rag_app.restore_toc import RestoreListError, plan_restore, statement_roles

AZURE_TOC = """\
;
; Archive created at 2026-10-01 03:00:00 UTC
;     dbname: secrag
;
4; 3079 16390 EXTENSION - azure
4401; 0 0 COMMENT - EXTENSION azure
5; 3079 16400 EXTENSION - pgaadauth
4402; 0 0 COMMENT - EXTENSION pgaadauth
6; 3079 16500 EXTENSION - vector
4403; 0 0 COMMENT - EXTENSION vector
7; 2615 2200 SCHEMA - public azure_pg_admin
4404; 0 0 ACL - SCHEMA public azure_pg_admin
4405; 0 0 ACL pg_catalog FUNCTION pg_stat_reset() azuresu
4406; 0 0 ACL pg_catalog COLUMN pg_subscription.subconninfo azuresu
220; 1259 16600 TABLE public users secragadmin
221; 1259 16610 TABLE public messages secragadmin
4407; 0 0 ACL public TABLE users secragadmin
4408; 0 0 ACL public TABLE messages secragadmin
4409; 826 16700 DEFAULT ACL public DEFAULT PRIVILEGES FOR TABLES secragadmin
"""

# pg_restore -v -f … -L <acl-list> of the two table ACL entries (what restore.sh renders)
AZURE_ACL_SQL = """\
--
-- PostgreSQL database dump
--
SET statement_timeout = 0;

--
-- TOC entry 4407 (class 0 OID 0)
-- Dependencies: 220
-- Name: TABLE users; Type: ACL; Schema: public; Owner: secragadmin
--

GRANT SELECT ON TABLE public.users TO secrag_backup;
GRANT SELECT,DELETE ON TABLE public.users TO secrag_purger;
GRANT SELECT ON TABLE public.users TO azure_reader;
SET SESSION AUTHORIZATION azure_pg_admin;
GRANT SELECT ON TABLE public.users TO secrag_purger WITH GRANT OPTION;
RESET SESSION AUTHORIZATION;


--
-- TOC entry 4408 (class 0 OID 0)
-- Dependencies: 221
-- Name: TABLE messages; Type: ACL; Schema: public; Owner: secragadmin
--

GRANT SELECT ON TABLE public.messages TO secrag_backup;
GRANT SELECT,DELETE ON TABLE public.messages TO secrag_purger;
"""

TARGET_ROLES = {"postgres", "secrag_owner", "secrag_purger", "secrag_backup", "secragadmin"}
TARGET_EXTENSIONS = {"plpgsql", "vector", "pgcrypto"}
MANAGED = {"azure", "pgaadauth"}


def _plan(**kw: object) -> restore_toc.Plan:
    args: dict[str, object] = {
        "toc": AZURE_TOC,
        "rendered_sql": AZURE_ACL_SQL,
        "roles": TARGET_ROLES,
        "available_extensions": TARGET_EXTENSIONS,
        "skip_extensions": MANAGED,
    }
    args.update(kw)
    return plan_restore(**args)  # type: ignore[arg-type]


def _ids(lines: list[str]) -> set[int]:
    return {int(line.split(";", 1)[0]) for line in lines}


def test_azure_only_entries_are_skipped_and_the_app_is_kept() -> None:
    plan = _plan()
    kept = _ids(plan.keep)
    # managed extensions + comments, pg_catalog ACLs, public-schema ACL, default ACL: gone
    assert kept.isdisjoint({4, 4401, 5, 4402, 4404, 4405, 4406, 4409})
    # vector (+ its comment), schema, tables and the grants of messages: restored
    assert {6, 4403, 7, 220, 221, 4408} <= kept
    assert plan.skipped == {
        "managed extensions": 2,
        "managed-extension comments": 2,
        "pg_catalog ACLs": 2,
        "public-schema ACLs": 1,
        "default-privilege entries (db/roles.sql sets them)": 1,
        "ACL entries naming a missing role": 1,
    }
    assert sorted(plan.skipped_extensions) == ["azure", "pgaadauth"]


def test_grants_to_existing_roles_survive_an_entry_that_names_a_missing_role() -> None:
    plan = _plan()
    assert 4407 not in _ids(plan.keep)
    assert plan.missing_roles == {"azure_reader", "azure_pg_admin"}
    sql = [s for s in plan.extra_sql if not s.startswith("--")]
    assert sql == [
        "GRANT SELECT ON TABLE public.users TO secrag_backup;",
        "GRANT SELECT,DELETE ON TABLE public.users TO secrag_purger;",
        # made by a grantor that does not exist here: applied as the restoring owner
        "GRANT SELECT ON TABLE public.users TO secrag_purger WITH GRANT OPTION;",
    ]
    assert plan.reapplied == 3


def test_the_summary_is_one_line_with_counts_by_kind() -> None:
    line = _plan().summary()
    assert "\n" not in line
    assert line.startswith("restore: kept ")
    for part in (
        "skipped 9",
        "2 managed extensions",
        "2 pg_catalog ACLs",
        "1 public-schema ACLs",
        "1 ACL entries naming a missing role",
        "[extensions: azure, pgaadauth]",
        "roles missing here: azure_pg_admin, azure_reader",
    ):
        assert part in line, part


def test_the_managed_list_is_what_restore_sh_passes() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts" / "db" / "restore.sh").read_text()
    assert "MANAGED_EXTENSIONS=(azure pgaadauth)" in script
    assert '-L "$work/toc.restore"' in script and "--no-owner" in script


def test_without_the_managed_list_azure_fails_closed_with_a_clear_message() -> None:
    with pytest.raises(RestoreListError, match='extension "azure".*RESTORE_SKIP_EXTENSIONS'):
        _plan(skip_extensions=set())


def test_an_unmanaged_extension_missing_on_the_target_fails_closed() -> None:
    with pytest.raises(RestoreListError, match='extension "vector"'):
        _plan(available_extensions={"plpgsql"})


def test_an_acl_entry_that_was_not_rendered_fails_closed() -> None:
    with pytest.raises(RestoreListError, match="4408"):
        _plan(rendered_sql=AZURE_ACL_SQL.split("-- TOC entry 4408")[0])


def test_an_unexpected_statement_in_an_acl_entry_fails_closed() -> None:
    with pytest.raises(RestoreListError, match="unexpected statement"):
        statement_roles("ALTER TABLE public.users OWNER TO azure_pg_admin;")


@pytest.mark.parametrize(
    ("statement", "roles"),
    [
        ("GRANT ALL ON TABLE public.t TO secrag_purger;", {"secrag_purger"}),
        ("GRANT SELECT ON TABLE public.t TO PUBLIC;", set()),
        ('GRANT SELECT ON TABLE public.t TO "Odd, Role";', {"Odd, Role"}),
        ("REVOKE ALL ON TABLE public.t FROM a, b;", {"a", "b"}),
        ("GRANT SELECT ON TABLE public.t TO a WITH GRANT OPTION;", {"a"}),
    ],
)
def test_statement_roles(statement: str, roles: set[str]) -> None:
    assert statement_roles(statement) == ("grant", roles)


def test_the_cli_writes_the_list_and_the_extra_sql(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "toc").write_text(AZURE_TOC)
    (tmp_path / "acl.sql").write_text(AZURE_ACL_SQL)
    (tmp_path / "roles").write_text("\n".join(sorted(TARGET_ROLES)) + "\n")
    (tmp_path / "ext").write_text("\n".join(sorted(TARGET_EXTENSIONS)) + "\n")
    skip = ["--skip-extension", "azure", "--skip-extension", "pgaadauth"]
    assert restore_toc.main(["acl-list", "--toc", str(tmp_path / "toc"), *skip]) == 0
    assert _ids(capsys.readouterr().out.splitlines()) == {4407, 4408}
    rc = restore_toc.main(
        [
            "filter",
            "--toc", str(tmp_path / "toc"),
            "--acl-sql", str(tmp_path / "acl.sql"),
            "--roles", str(tmp_path / "roles"),
            "--extensions", str(tmp_path / "ext"),
            *skip,
            "--out", str(tmp_path / "list"),
            "--extra-sql", str(tmp_path / "extra.sql"),
        ]
    )  # fmt: skip
    assert rc == 0
    assert capsys.readouterr().out.startswith("restore: kept ")
    assert {6, 220, 4408} <= _ids((tmp_path / "list").read_text().splitlines())
    assert "TO secrag_purger" in (tmp_path / "extra.sql").read_text()
    assert restore_toc.main(["filter", *_cli_args(tmp_path), "--out", "x", "--extra-sql", "y"]) == 2
    assert "not available on the target" in capsys.readouterr().err


def _cli_args(tmp_path: Path) -> list[str]:
    (tmp_path / "noext").write_text("plpgsql\n")
    return [
        "--toc", str(tmp_path / "toc"),
        "--acl-sql", str(tmp_path / "acl.sql"),
        "--roles", str(tmp_path / "roles"),
        "--extensions", str(tmp_path / "noext"),
        "--skip-extension", "azure",
        "--skip-extension", "pgaadauth",
    ]  # fmt: skip
