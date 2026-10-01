"""The restore list for ``scripts/db/restore.sh``: which entries of a dump go to the target.

A dump of the Azure Flexible Server carries entries that only exist there, and a restore
runs ``pg_restore`` in ONE transaction, so a single one of them failing restores nothing
(found by the first real rehearsal, 2026-10-01: ``extension "azure" is not available``).
``restore.sh`` therefore restores from a filtered ``pg_restore -l`` list (``-L``). Skipped:

- the extensions the cloud provider manages (``--skip-extension``; restore.sh passes its
  ``MANAGED_EXTENSIONS`` list — ``azure``, ``pgaadauth`` — plus ``RESTORE_SKIP_EXTENSIONS``)
  and their ``COMMENT``. Any OTHER extension in the dump (``vector``) is restored, and must
  be available on the target: it is checked up front, with a clear error;
- every ACL entry in schema ``pg_catalog`` (grants on system catalogs — Azure's ``azuresu``;
  the target's catalogs are the target's own business) and the ACL of schema ``public``
  (created by initdb on the target; ``db/roles.sql`` grants what the app needs on it);
- ``DEFAULT ACL`` entries (default privileges of the SOURCE owner; ``db/roles.sql`` sets them
  for the target owner);
- any other ACL entry that names a role the target does not have. Its statements that name
  only existing roles are written to ``--extra-sql`` (applied right after ``pg_restore``),
  so e.g. a grant to ``secrag_purger`` next to a grant to a cloud-only role survives.

Ownership needs no filter: restore.sh runs ``pg_restore --no-owner`` (every object is owned
by the restoring owner). ACL entries of ``public`` objects are KEPT, so the grants of
migration 0005 to ``secrag_purger`` / ``secrag_backup`` come back as they were.

    python -m rag_app.restore_toc acl-list --toc TOC   # the ACL entries to render (-L)
    python -m rag_app.restore_toc filter --toc TOC --acl-sql RENDERED --roles ROLES \\
        --extensions AVAILABLE [--skip-extension NAME …] --out LIST --extra-sql FILE

``RENDERED`` is ``pg_restore -v -f RENDERED -L <acl-list>`` (``-v`` writes the
``-- TOC entry <id>`` header that maps each statement to its entry); ``ROLES`` and
``AVAILABLE`` hold one name per line (``pg_roles.rolname`` / ``pg_available_extensions``).
Exit 2 (message on stderr) when the dump needs an extension the target lacks, or when an ACL
entry cannot be read — a restore never guesses.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

TOC_LINE = re.compile(r"^(?P<id>\d+); \d+ \d+ (?P<rest>.*)$")
RENDERED_ENTRY = re.compile(r"^-- TOC entry (\d+) ")
ROLE_TOKEN = re.compile(r'"(?:[^"]|"")*"|[^,\s]+')


class RestoreListError(Exception):
    """The dump cannot be restored as it is (the message says why)."""


@dataclass
class Plan:
    keep: list[str] = field(default_factory=list)
    extra_sql: list[str] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    skipped_extensions: list[str] = field(default_factory=list)
    missing_roles: set[str] = field(default_factory=set)
    reapplied: int = 0

    def summary(self) -> str:
        total = sum(self.skipped.values())
        parts = [f"{n} {kind}" for kind, n in sorted(self.skipped.items())]
        line = f"restore: kept {len(self.keep)} entries, skipped {total}"
        if parts:
            line += ": " + ", ".join(parts)
        if self.skipped_extensions:
            line += f" [extensions: {', '.join(sorted(self.skipped_extensions))}]"
        if self.missing_roles:
            line += f" [roles missing here: {', '.join(sorted(self.missing_roles))}"
            line += f"; {self.reapplied} statement(s) of those entries re-applied]"
        return line


def _unquote(name: str) -> str:
    return name[1:-1].replace('""', '"') if name.startswith('"') else name


def fixed_skip(fields: list[str], skip_extensions: set[str]) -> str | None:
    """The kind of a TOC entry that is always skipped, or None. ``fields`` = the entry after
    ``<id>; <class> <oid>`` split on spaces: desc, namespace, tag…, owner."""
    if fields[:1] == ["EXTENSION"] and len(fields) > 2 and fields[2] in skip_extensions:
        return "managed extensions"
    if fields[:3] == ["COMMENT", "-", "EXTENSION"] and len(fields) > 3:
        if fields[3] in skip_extensions:
            return "managed-extension comments"
    if fields[:2] == ["DEFAULT", "ACL"]:
        return "default-privilege entries (db/roles.sql sets them)"
    if fields[:2] == ["ACL", "pg_catalog"]:
        return "pg_catalog ACLs"
    if fields[:4] == ["ACL", "-", "SCHEMA", "public"]:
        return "public-schema ACLs"
    return None


def acl_entries(toc: str, skip_extensions: set[str]) -> list[str]:
    """The ACL lines of the TOC that the filter must read (not skipped by a fixed rule)."""
    out = []
    for line in toc.splitlines():
        m = TOC_LINE.match(line)
        if not m:
            continue
        fields = m["rest"].split()
        if fields[:1] == ["ACL"] and fixed_skip(fields, skip_extensions) is None:
            out.append(line)
    return out


def rendered_statements(sql: str) -> dict[int, list[str]]:
    """``-- TOC entry <id>`` → the SQL statements of that entry (one per line)."""
    blocks: dict[int, list[str]] = {}
    current: list[str] | None = None
    for line in sql.splitlines():
        m = RENDERED_ENTRY.match(line)
        if m:
            current = blocks.setdefault(int(m[1]), [])
        elif current is not None and line.strip() and not line.startswith(("--", "\\")):
            current.append(line.strip())
    return blocks


def statement_roles(statement: str) -> tuple[str, set[str]]:
    """(kind, roles named) of one ACL statement; kind ``set``/``reset``/``grant``.
    Raises RestoreListError on anything else (fail closed)."""
    if statement == "RESET SESSION AUTHORIZATION;":
        return "reset", set()
    m = re.fullmatch(r"SET SESSION AUTHORIZATION (.+);", statement)
    if m:
        return "set", {_unquote(m[1])}
    if statement.startswith("GRANT ") and " TO " in statement:
        tail = statement.rsplit(" TO ", 1)[1]
    elif statement.startswith("REVOKE ") and " FROM " in statement:
        tail = statement.rsplit(" FROM ", 1)[1]
    else:
        raise RestoreListError(f"unexpected statement in an ACL entry: {statement[:80]!r}")
    tail = tail.removesuffix(";")
    roles: set[str] = set()
    if " GRANTED BY " in tail:
        tail, grantor = tail.rsplit(" GRANTED BY ", 1)
        roles.add(_unquote(grantor.strip()))
    tail = tail.removesuffix(" WITH GRANT OPTION").removesuffix(" CASCADE")
    for token in ROLE_TOKEN.findall(tail):
        if token != "PUBLIC":  # the keyword; a role named "PUBLIC" would be quoted
            roles.add(_unquote(token))
    return "grant", roles


def plan_restore(
    toc: str,
    rendered_sql: str,
    roles: set[str],
    available_extensions: set[str],
    skip_extensions: set[str],
) -> Plan:
    plan = Plan()
    blocks = rendered_statements(rendered_sql)
    for line in toc.splitlines():
        m = TOC_LINE.match(line)
        if not m:
            continue  # comments of pg_restore -l
        fields = m["rest"].split()
        kind = fixed_skip(fields, skip_extensions)
        if kind:
            plan.skipped[kind] += 1
            if kind == "managed extensions":
                plan.skipped_extensions.append(fields[2])
            continue
        if fields[:1] == ["EXTENSION"] and len(fields) > 2:
            if fields[2] not in available_extensions:
                raise RestoreListError(
                    f'the dump needs extension "{fields[2]}", which is not available on the '
                    "target server — install it there or, if the cloud provider manages it, "
                    "add it to RESTORE_SKIP_EXTENSIONS"
                )
        if fields[:1] == ["ACL"]:
            dump_id = int(m["id"])
            if dump_id not in blocks:
                raise RestoreListError(f"ACL entry {dump_id} is missing from the rendered SQL")
            statements = blocks[dump_id]
            named = [statement_roles(s) for s in statements]
            missing = {role for _kind, names in named for role in names} - roles
            if missing:
                plan.skipped["ACL entries naming a missing role"] += 1
                plan.missing_roles |= missing
                kept = _statements_without(statements, named, missing)
                grants = [s for s in kept if s.startswith(("GRANT ", "REVOKE "))]
                if grants:
                    plan.extra_sql.append(f"-- TOC entry {dump_id}, without {sorted(missing)}")
                    plan.extra_sql.extend(kept)
                    plan.reapplied += len(grants)
                continue
        plan.keep.append(line)
    return plan


def _statements_without(
    statements: list[str], named: list[tuple[str, set[str]]], missing: set[str]
) -> list[str]:
    """The statements that name no missing role. A ``SET SESSION AUTHORIZATION`` of a missing
    role (a grantor that does not exist here) and its ``RESET`` are dropped, but the grants
    made under it to EXISTING roles are kept — they run as the restoring owner, so e.g. a
    grant to ``secrag_purger`` made by a cloud-only admin still comes back."""
    kept: list[str] = []
    in_missing_session = False
    for statement, (kind, roles) in zip(statements, named, strict=True):
        if kind == "set":
            in_missing_session = bool(roles & missing)
            if not in_missing_session:
                kept.append(statement)
        elif kind == "reset":
            if not in_missing_session:
                kept.append(statement)
            in_missing_session = False
        elif not roles & missing:
            kept.append(statement)
    return kept


def _names(path: Path) -> set[str]:
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rag_app.restore_toc")
    sub = parser.add_subparsers(dest="cmd", required=True)
    acl = sub.add_parser("acl-list")
    acl.add_argument("--toc", type=Path, required=True)
    acl.add_argument("--skip-extension", action="append", default=[])
    flt = sub.add_parser("filter")
    flt.add_argument("--toc", type=Path, required=True)
    flt.add_argument("--acl-sql", type=Path, required=True)
    flt.add_argument("--roles", type=Path, required=True)
    flt.add_argument("--extensions", type=Path, required=True)
    flt.add_argument("--skip-extension", action="append", default=[])
    flt.add_argument("--out", type=Path, required=True)
    flt.add_argument("--extra-sql", type=Path, required=True)
    args = parser.parse_args(argv)
    skip = set(args.skip_extension)
    toc = args.toc.read_text(encoding="utf-8")
    if args.cmd == "acl-list":
        for line in acl_entries(toc, skip):
            print(line)
        return 0
    try:
        plan = plan_restore(
            toc,
            args.acl_sql.read_text(encoding="utf-8"),
            _names(args.roles),
            _names(args.extensions),
            skip,
        )
    except RestoreListError as exc:
        print(f"restore_toc: {exc}", file=sys.stderr)
        return 2
    args.out.write_text("".join(f"{line}\n" for line in plan.keep), encoding="utf-8")
    args.extra_sql.write_text("".join(f"{s}\n" for s in plan.extra_sql), encoding="utf-8")
    print(plan.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
