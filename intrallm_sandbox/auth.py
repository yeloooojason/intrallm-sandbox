"""Bearer-token auth with three roles.

* ``admin``  - sees and controls every sandbox, manages tokens.
* ``agent``  - a service identity (e.g. the IntraLLM agent). Creates sandboxes on
               behalf of end users (``owner``) and controls the ones it created.
* ``user``   - an end user; sees and controls only sandboxes allocated to them.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass

ROLES = ("admin", "agent", "user")


@dataclass(frozen=True)
class Principal:
    name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def can_access(self, sandbox: dict) -> bool:
        if self.role == "admin":
            return True
        if self.role == "agent":
            return sandbox["created_by"] == self.name
        return sandbox["owner"] == self.name


def new_token() -> str:
    return "isb_" + secrets.token_urlsafe(32)


def resolve(token: str, admin_token: str, db) -> Principal | None:
    if not token:
        return None
    if admin_token and hmac.compare_digest(token, admin_token):
        return Principal("admin", "admin")
    row = db.lookup_token(token)
    if row:
        return Principal(row["principal"], row["role"])
    return None
