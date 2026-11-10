"""Mint a token from the command line. Used to bootstrap service callers (for
example the Model Gateway) and to make test tokens. See NEXA-ADR-0005.

    export NEXA_IDENTITY_SECRET=...
    python -m v1.identity.cli --subject service:model-gateway --roles service --ttl 86400
    python -m v1.identity.cli --subject lms:edunex-ncd --roles lms_sync --lms-site edunex-ncd \
        --school-id pg-ncd-0042 --ttl 31536000
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
    parser.add_argument("--school-id", default=None, help="school_id claim (school-scoped content)")
    parser.add_argument("--lms-site", default=None, help="lms_site claim for an LMS sync token")
    parser.add_argument("--lms-national", action="store_true",
                        help="allow this LMS sync token to publish national content")
    args = parser.parse_args()
    roles = [r.strip() for r in args.roles.split(",") if r.strip()]
    extra = {k: v for k, v in (("school_id", args.school_id), ("lms_site", args.lms_site)) if v}
    if args.lms_national:
        extra["lms_national"] = True
    print(tokens.issue_token(args.subject, roles, args.type, args.ttl, extra=extra or None))


if __name__ == "__main__":
    main()
