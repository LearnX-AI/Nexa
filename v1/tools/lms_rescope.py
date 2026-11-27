"""Re-scope an LMS site's synced content from one school to another (NEXA-M4-004).

The EduNeX courses were synced under the test school edunex-core-test, so the
school they belong to (POMNSOE) cannot see them. This replays the last
applied event of every active LMS item, unchanged except for its id and time,
through the normal sync endpoint as an administrator of the new school. The
Knowledge Service then re-governs each item (same content, new school) and
re-indexes it; the old school stops seeing it. Every replayed event is stored
and audited like any LMS event.

Making content visible to a school's learners is a publication decision:
run --apply only with the approval recorded (M4-004: after the CDD sign-off).

Dry run (default): what would change, nothing is sent.

    (set -a; . <(sudo cat /etc/nexa/identity.env); set +a; \\
     python -m v1.tools.lms_rescope --from-school edunex-core-test --to-school pomnsoe)

Apply, then the same command again shows nothing left to move. To undo, run
it with the schools swapped.

    ... --apply --out ~/nexa-evidence/m4-004
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import sys
import time
from typing import Any, Callable, Optional

CONTRACT = "nexa.lms-sync/1.0"
BATCH = 50


def plan(items: list[dict[str, Any]], get_event: Callable[[str], Optional[dict[str, Any]]], from_school: str,
         to_school: str, site: Optional[str] = None, now: Optional[dt.datetime] = None) -> dict[str, Any]:
    """The events to replay, grouped by site, and what is skipped and why."""
    now = now or dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y%m%d%H%M%S")
    by_site: dict[str, dict[str, Any]] = {}
    skipped: collections.Counter = collections.Counter()
    courses: collections.Counter = collections.Counter()
    read: list[tuple[dict[str, Any], Optional[dict[str, Any]]]] = []
    for it in items:
        e = get_event(it["last_event_id"]) if it.get("last_event_id") else None
        read.append((it, e.get("payload") if e and isinstance(e.get("payload"), dict) else None))
    movable: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for it, p in read:
        if site and it.get("site_id") != site:
            skipped["another site"] += 1
        elif it.get("state") != "active" or not it.get("last_event_id"):
            skipped["not active"] += 1
        elif p is None:
            skipped["last event not readable"] += 1
        elif p.get("school_id") != from_school:
            skipped[f"school is {p.get('school_id')}"] += 1
        elif (p.get("event") or {}).get("op") != "upsert" or \
                ((p.get("event") or {}).get("item") or {}).get("scope") == "national":
            skipped["not a school-scoped upsert"] += 1
        else:
            movable[it["item_key"]] = (it, p)
    # Identical content is stored once and shared by the items that have it.
    # Moving a shared version moves every item on it, so it is moved only when
    # all of them are being moved.
    users: dict[str, set] = collections.defaultdict(set)
    for it, _ in read:
        if it.get("version_id"):
            users[it["version_id"]].add(it["item_key"])
    for key, (it, p) in movable.items():
        if not users.get(it.get("version_id") or "", {key}) <= set(movable):
            skipped["content shared with an item that is not moved (left unchanged)"] += 1
            continue
        ev = p.get("event") or {}
        h = hashlib.sha256(f"{ev.get('event_id')}|{to_school}".encode()).hexdigest()[:16]
        replay = dict(ev, event_id=f"rescope-{stamp}-{h}", occurred_at=now.isoformat())
        s = dict(p.get("site") or {})
        s["school_id"] = to_school
        skey = s.get("site_id", "?")
        grp = by_site.setdefault(skey, {"site": s, "events": []})
        grp["events"].append(replay)
        courses[f"{skey} / {(ev.get('item') or {}).get('course_id')}"] += 1
    return {"from_school": from_school, "to_school": to_school, "sites": by_site,
            "count": sum(len(g["events"]) for g in by_site.values()), "courses": dict(courses),
            "skipped": dict(skipped)}


def batches(p: dict[str, Any], size: int = BATCH):
    for grp in p["sites"].values():
        evs = grp["events"]
        for i in range(0, len(evs), size):
            yield {"contract": CONTRACT, "site": grp["site"], "events": evs[i:i + size]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8103")
    ap.add_argument("--from-school", required=True)
    ap.add_argument("--to-school", required=True)
    ap.add_argument("--site", help="only this LMS site_id")
    ap.add_argument("--apply", action="store_true", help="send the events (default: dry run)")
    ap.add_argument("--out", default=os.path.expanduser("~/nexa-evidence"))
    a = ap.parse_args(argv)
    if a.from_school == a.to_school:
        print("--from-school and --to-school are the same")
        return 2
    import httpx

    from v1.identity.tokens import issue_token
    h = {"Authorization": "Bearer " + issue_token("ops:lms-rescope", ["platform_admin"], "user", 3600,
                                                  extra={"school_id": a.to_school})}
    c = httpx.Client(base_url=a.url.rstrip("/"), headers=h, timeout=600)
    r = c.get("/v1/knowledge/lms/items", params={"state": "active", **({"site_id": a.site} if a.site else {})})
    if r.status_code != 200:
        print(f"cannot list LMS items: HTTP {r.status_code} {r.text[:200]}")
        return 2
    items = r.json()["items"]

    def get_event(eid: str) -> Optional[dict[str, Any]]:
        g = c.get(f"/v1/knowledge/lms/events/{eid}")
        return g.json() if g.status_code == 200 else None
    p = plan(items, get_event, a.from_school, a.to_school, a.site)
    print(f"{len(items)} active LMS items; {p['count']} to move from {a.from_school} to {a.to_school}")
    for k, v in sorted(p["courses"].items()):
        print(f"  {v:>5}  {k}")
    for k, v in p["skipped"].items():
        print(f"  skipped {v}: {k}")
    os.makedirs(a.out, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    if not a.apply:
        path = os.path.join(a.out, f"nexa-lms-rescope-plan-{stamp}.json")
        with open(path, "w") as fh:
            json.dump({k: v for k, v in p.items() if k != "sites"} |
                      {"events": [e["event_id"] for g in p["sites"].values() for e in g["events"]]}, fh, indent=2)
        print(f"\nDry run: nothing was sent. Plan: {path}\nRun again with --apply to make the change.")
        return 0
    states: collections.Counter = collections.Counter()
    errors = []
    for b in batches(p):
        resp = c.post("/v1/knowledge/lms/events", params={"wait": "true"}, json=b)
        if resp.status_code != 200:
            errors.append(f"HTTP {resp.status_code}: {resp.text[:200]}")
            states["batch refused"] += len(b["events"])
            continue
        for res in resp.json()["results"]:
            states[res.get("state") or res.get("status")] += 1
            if res.get("state") not in ("applied",):
                errors.append(f"{res.get('item_key')}: {res.get('state') or res.get('status')} "
                              f"{res.get('error') or res.get('reason') or ''}"[:200])
        print(f"  sent {len(b['events'])}: {dict(states)}")
    left = plan(c.get("/v1/knowledge/lms/items", params={"state": "active"}).json()["items"], get_event,
                a.from_school, a.to_school, a.site)["count"]
    path = os.path.join(a.out, f"nexa-lms-rescope-{stamp}.json")
    with open(path, "w") as fh:
        json.dump({"from_school": a.from_school, "to_school": a.to_school, "moved": p["count"], "states": states,
                   "errors": errors, "still_with_from_school": left, "courses": p["courses"]}, fh, indent=2)
    print(f"\n{dict(states)}; still scoped to {a.from_school}: {left}")
    for e in errors[:20]:
        print(f"  {e}")
    print(f"report: {path}")
    return 0 if not errors and left == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
