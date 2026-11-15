"""``e2e`` gate step helper (T13.2.10): seeds one known account directly in the e2e
backend's own database, through the SAME ``rag_app.admin.create_verified_user``/
``create_admin`` the real admin bootstrap CLI uses -- never a backend route (``role`` is
never writable through any route, T12.3.3; and seeding the admin's erasure TARGET this way
too keeps it out of the signup-risk velocity counter, DA-13-C-1 -- see below). The
Playwright admin spec then enrols/confirms/steps-up the admin's MFA for real through the
UI, exactly like a real admin would, computing a valid TOTP code with ``e2e/totp.ts`` from
the real provisioning secret the backend returns.

    DATABASE_URL=... E2E_ADMIN_PASSWORD=... python -m rag_app.devtools.e2e_seed_admin \\
        --email e2e-admin@example.test --role admin

The password is read from ``E2E_ADMIN_PASSWORD`` (never an argument -- would land in the
process list/shell history) -- the SAME throwaway-secret pattern ``scripts/gate.sh``'s
``ensure_e2e_env`` already uses for ``E2E_JWT_SECRET``/``E2E_MASTER_KEY``/etc.: generated
once per gate run into a git-ignored, 0600 env file, scoped to this one e2e stack, never a
real credential, shared by both the admin and the plain-user target seeded with it.
Idempotent across repeated local runs against the SAME (not yet recreated) gate database:
an email collision from an earlier run is treated as "already seeded", not a failure
(``ensure_e2e_env`` reuses the same password across runs too, so the existing row still
matches).

**DA-13-C-1 (why a plain user is seeded here too, not registered through the UI):** the
admin-dashboard erase-account spec needs an existing ordinary account to erase. Registering
one through the real `/auth/register` form, like every other e2e spec's throwaway users,
adds ONE MORE signup to `rag_app.risk.SignupTracker`'s one-hour, per-IP sliding window
(`>= 5` recent signups scores `is_high_risk` -- found for real running this: 5 pre-existing
registrations across auth/admin/mfa.spec.ts already left no headroom, and a 6th tripped the
block, non-deterministically depending on worker scheduling). Seeding directly here -- the
same mechanism the admin account itself already uses -- tests the ERASE action, not
registration, and spends none of that budget.

Exit code: 0 on success (seeded or already present), 1 if ``E2E_ADMIN_PASSWORD`` is unset.
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy.orm import Session

from rag_app.admin import ADMIN_ROLE, USER_ROLE, AdminCreateError, create_verified_user
from rag_app.db.session import make_engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rag_app.devtools.e2e_seed_admin")
    parser.add_argument("--email", required=True)
    parser.add_argument("--role", choices=(ADMIN_ROLE, USER_ROLE), default=ADMIN_ROLE)
    args = parser.parse_args(argv)

    password = os.environ.get("E2E_ADMIN_PASSWORD")
    if not password:
        print("e2e_seed_admin: FAIL -- E2E_ADMIN_PASSWORD not set", file=sys.stderr)
        return 1

    engine = make_engine()
    try:
        with Session(engine) as session:
            try:
                user = create_verified_user(session, args.email, password, role=args.role)
            except AdminCreateError:
                print("e2e_seed_admin: account already exists, reusing it")
                return 0
            print(f"e2e_seed_admin: seeded {args.role} {user.id}")  # id only, never the email
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
