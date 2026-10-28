"""Create or update a reviewer account.

    uv run scripts/add_user.py alice
    uv run scripts/add_user.py alice --password 'secret'

Passwords are hashed with stdlib scrypt and written into USERS_FILE
(default users.json). Never commit that file.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import auth  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Add or update a reviewer account.")
    parser.add_argument("username", help="Login name (no spaces).")
    parser.add_argument("--password", help="Password. Prompted if omitted.")
    args = parser.parse_args()

    username = args.username.strip()
    if not username or " " in username:
        print("Username must be a single non-empty word.", file=sys.stderr)
        return 1

    password = args.password
    if not password:
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords do not match.", file=sys.stderr)
            return 1
    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        return 1

    users = auth.load_users()
    users[username] = auth.hash_password(password)
    auth.save_users(users)
    print(f"Saved {username} to {auth.users_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
