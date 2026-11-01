"""Create an account, or reset an existing account's password.

    uv run scripts/add_user.py alice --full-name "Alice Ng" --role admin
    uv run scripts/add_user.py bob --full-name "Bob Rao"            # reviewer
    uv run scripts/add_user.py carol --full-name "Carol Li" --role monitor
    uv run scripts/add_user.py alice --password '…'                  # reset

Accounts live in the database (DATABASE_URL). Usernames are never reused, and
the full name is what appears on that person's electronic signatures. Every
change made here is written to the audit trail as made by "system".
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import audit  # noqa: E402
import auth  # noqa: E402
import db  # noqa: E402


async def run(args: argparse.Namespace, password: str) -> int:
    await db.init()
    username = args.username.strip()
    async with db.session() as session:
        user = await session.get(db.User, username)
        if user is None:
            if not args.full_name:
                print("New accounts need --full-name (it appears on signatures).", file=sys.stderr)
                return 1
            user = db.User(username=username, full_name=args.full_name.strip(),
                           role=args.role or "reviewer",
                           password_hash=auth.hash_password(password),
                           password_changed_at=db.utcnow())
            session.add(user)
            await session.flush()
            await audit.record(session, "user.created", username="system", field=username,
                               new_value=user.role, reason="Created with scripts/add_user.py")
            verb = "Created"
        else:
            user.password_hash = auth.hash_password(password)
            user.password_changed_at = db.utcnow()
            user.locked_until, user.failed_logins = None, 0
            await audit.record(session, "user.password_changed", username="system",
                               field=username, reason="Reset with scripts/add_user.py")
            if args.full_name and args.full_name.strip() != user.full_name:
                await audit.record(session, "user.updated", username="system",
                                   field=f"{username}.full_name", old_value=user.full_name,
                                   new_value=args.full_name.strip())
                user.full_name = args.full_name.strip()
            if args.role and args.role != user.role:
                await audit.record(session, "user.updated", username="system",
                                   field=f"{username}.role", old_value=user.role,
                                   new_value=args.role)
                user.role = args.role
            verb = "Updated"
        await session.commit()
        print(f"{verb} {user.username} ({user.full_name}, {user.role}) in {db.database_url().split('@')[-1]}")
    await db.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or update an account.")
    parser.add_argument("username", help="Login name (one word; never reused).")
    parser.add_argument("--full-name", help="Printed name shown on signatures.")
    parser.add_argument("--role", choices=auth.ROLES, help="reviewer (default), monitor or admin.")
    parser.add_argument("--password", help="Password. Prompted if omitted.")
    args = parser.parse_args()

    if not args.username.strip() or any(c.isspace() for c in args.username.strip()):
        print("Username must be a single non-empty word.", file=sys.stderr)
        return 1
    password = args.password
    if not password:
        password = getpass.getpass("Password: ")
        if getpass.getpass("Confirm password: ") != password:
            print("Passwords do not match.", file=sys.stderr)
            return 1
    problem = auth.password_problem(password, args.username.strip())
    if problem:
        print(problem, file=sys.stderr)
        return 1
    return asyncio.run(run(args, password))


if __name__ == "__main__":
    raise SystemExit(main())
