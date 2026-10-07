"""CLI: ``python -m intrallm_sandbox serve`` / ``create-token`` / ``list-tokens``."""

from __future__ import annotations

import argparse

from . import auth
from .config import get_settings
from .db import Database


def main() -> None:
    parser = argparse.ArgumentParser(prog="intrallm-sandbox")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the API server and dashboard")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)

    tok = sub.add_parser("create-token", help="issue an API token")
    tok.add_argument("principal", help="user name or agent service name")
    tok.add_argument("--role", choices=auth.ROLES, default="user")
    tok.add_argument("--description", default="")

    sub.add_parser("list-tokens", help="list issued tokens")

    args = parser.parse_args()
    settings = get_settings()

    if args.cmd == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port)
    elif args.cmd == "create-token":
        token = auth.new_token()
        Database(settings.db_path).insert_token(token, args.principal, args.role, args.description)
        print(token)
    elif args.cmd == "list-tokens":
        for t in Database(settings.db_path).list_tokens():
            state = "revoked" if t["revoked"] else "active"
            print(f"{t['id']}  {t['role']:<6} {t['principal']:<24} {state}  {t['description']}")


if __name__ == "__main__":
    main()
