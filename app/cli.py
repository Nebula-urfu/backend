import argparse
import getpass
import sys

from .auth import PASSWORDS
from .db import contractors, engine, orgs, uid, users


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    org = commands.add_parser("org")
    org.add_argument("id")
    org.add_argument("name")
    user = commands.add_parser("user")
    user.add_argument("org")
    user.add_argument("role", choices=["RESIDENT", "STAFF", "REVIEWER"])
    user.add_argument("username")
    user.add_argument("name")
    user.add_argument("--address", default="")
    user.add_argument("--password-stdin", action="store_true")
    contractor = commands.add_parser("contractor")
    contractor.add_argument("org")
    contractor.add_argument("name")
    contractor.add_argument("email")
    contractor.add_argument("--nonstandard", action="store_true")
    args = parser.parse_args()
    with engine.begin() as conn:
        if args.command == "org":
            conn.execute(orgs.insert().values(id=args.id, name=args.name))
        elif args.command == "user":
            if args.role == "RESIDENT" and not args.address.strip():
                parser.error("a resident needs --address")
            password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass()
            if not 12 <= len(password) <= 256:
                parser.error("password must have 12 to 256 characters")
            conn.execute(users.insert().values(
                id=uid(), org=args.org, role=args.role, username=args.username.strip().lower(),
                name=args.name, address=args.address, password=PASSWORDS.hash(password),
            ))
        else:
            identifier = uid()
            conn.execute(contractors.insert().values(id=identifier, org=args.org, data={
                "id": identifier, "name": args.name, "email": args.email, "nonstandardContract": args.nonstandard,
            }))


if __name__ == "__main__":
    main()
