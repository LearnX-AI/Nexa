"""Mint a token from the command line. Used to bootstrap service callers (for
example the Model Gateway) and to make test tokens. See NEXA-ADR-0005.

    export NEXA_IDENTITY_SECRET=...
    python -m v1.identity.cli --subject service:model-gateway --roles service --ttl 86400
"""
from __future__ import annotations
import argparse

from . import tokens


def main() -> None:
    parser = argparse.ArgumentParser(description="Mint a NEXA identity token")
    parser.add_argument("--subject", required=True)
    parser.add_argument("--roles", default="service", help="comma separated")
    parser.add_argument("--type", default="service")
    parser.add_argument("--ttl", type=int, default=None)
    args = parser.parse_args()
    roles = [r.strip() for r in args.roles.split(",") if r.strip()]
    print(tokens.issue_token(args.subject, roles, args.type, args.ttl))


if __name__ == "__main__":
    main()
