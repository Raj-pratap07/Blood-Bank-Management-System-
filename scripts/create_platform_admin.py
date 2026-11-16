"""Create a platform administrator account (manages the facility directory only).

Usage:
    python -m scripts.create_platform_admin --username platform.admin --full-name "Your Name"

The password is read interactively (not echoed, never printed or logged).
No web page can create a platform administrator, so this is the only way to
bootstrap one on an existing database.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from pydantic import ValidationError

from app.database import SessionLocal, init_db
from app.services import users as user_svc
from app.services.errors import ServiceError


def main(argv: list[str] | None = None, password_reader=getpass.getpass) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--username", required=True)
    parser.add_argument("--full-name", required=True)
    args = parser.parse_args(argv)

    password = password_reader("Password (8-72 characters): ")
    if password != password_reader("Repeat password: "):
        print("Passwords do not match. No account created.", file=sys.stderr)
        return 1
    init_db()
    with SessionLocal() as db:
        try:
            user = user_svc.create_platform_admin(db, args.username, args.full_name, password)
        except ValidationError as e:
            print("Invalid input: " + "; ".join(err["msg"].removeprefix("Value error, ") for err in e.errors()),
                  file=sys.stderr)
            return 1
        except ServiceError as e:
            print(e.message, file=sys.stderr)
            return 1
    print(f"Platform administrator '{user.username}' created.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
