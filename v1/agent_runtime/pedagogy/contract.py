"""nexa.pedagogy/1.0: the Teaching Intelligence Policy contract, loading,
composition and screening (NEXA-M4-001).

    policy = get("1.0.0")
    c = compose(policy, profile="tutor", audience="student", grade=11, message="...")
    c.text      # the block the runtime appends to the agent's system prompt
    c.record    # what the trace and audit record: policy, version, profile, digest, rules

Released policy files are immutable: policies/released.json holds the digest
of each released version, and loading fails if a file no longer matches.
Changing the policy means a new version, reviewed and approved like an agent.

    python -m v1.agent_runtime.pedagogy validate
    python -m v1.agent_runtime.pedagogy show tutor student 11
    python -m v1.agent_runtime.pedagogy schema > v1/agent_runtime/schema/nexa-pedagogy-1.0.schema.json
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

HERE = os.path.dirname(os.path.abspath(__file__))
POLICY_DIR = os.path.join(HERE, "policies")
RELEASED = os.path.join(POLICY_DIR, "released.json")
POLICY_ID = "nexa-teaching-policy"
RULE_ID = re.compile(r"^TP-[A-Z]+-\d{2}$")
SEMVER = r"^\d+\.\d+\.\d+$"
AUDIENCES = ("student", "teacher")


class PedagogyError(Exception):
    pass


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Rule(Strict):
    id: str = Field(pattern=RULE_ID.pattern)
    text: str = Field(min_length=20, max_length=600)
    audience: Optional[list[Literal["student", "teacher"]]] = Field(
        None, description="Who the rule applies to; absent: everyone.")


class GradeBand(Strict):
    id: str = Field(pattern=r"^g\d+_\d+$")
    grades: list[int] = Field(min_length=1)
    label: str
    rules: list[Rule] = Field(min_length=1)


class Section(Strict):
    id: str = Field(pattern=r"^[a-z_]{3,30}$")
    title: str
    rules: list[Rule] = Field(min_length=1)


class Profile(Strict):
    description: str
    sections: list[str] = Field(min_length=1)
    grade_style: bool = True


class Trigger(Strict):
    id: str = Field(pattern=r"^[a-z_]{3,30}$")
    rule: str = Field(pattern=RULE_ID.pattern)
    patterns: list[str] = Field(min_length=1)


class DecisionPoint(Strict):
    id: str = Field(pattern=r"^[A-Z]+-D\d+$")
    question: str
    options: list[str] = Field(min_length=1)
    current: str
    owner: str
    status: Literal["open", "proposed", "decided"]
    needs: Optional[str] = None


class PublicContact(Strict):
    """A published service number the policy tells people to call. It is not
    personal data, so output checks and redaction leave it in place."""
    name: str
    phone: str = Field(pattern=r"^[0-9 +]{6,20}$")
    purpose: str
    checked_on: date
    source: str


class Governance(Strict):
    framework: Literal["NEXA-GOV-001"]
    review: Literal["provisional", "reviewed"]
    note: str


class Change(Strict):
    version: str = Field(pattern=SEMVER)
    date: date
    change: str


class Policy(Strict):
    policy_version: Literal["nexa.pedagogy/1.0"]
    id: Literal["nexa-teaching-policy"]
    name: str
    version: str = Field(pattern=SEMVER)
    status: Literal["proposed", "approved", "retired"]
    owner: str
    approver: str
    approved_by: Optional[str] = None
    approved_on: Optional[date] = None
    released_on: date
    governance: Governance
    summary: str
    grade_bands: list[GradeBand] = Field(min_length=1)
    default_band: str
    sections: list[Section] = Field(min_length=1)
    profiles: dict[str, Profile] = Field(min_length=1)
    escalation_triggers: list[Trigger] = Field(default_factory=list)
    escalation_response: dict[Literal["student", "teacher"], str]
    public_contacts: list[PublicContact] = Field(default_factory=list)
    language_markers: dict[str, list[str]] = Field(default_factory=dict)
    decision_points: list[DecisionPoint] = Field(default_factory=list)
    agent_prompt_must_not_restate: list[str] = Field(default_factory=list)
    changelog: list[Change] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> "Policy":
        ids = [r.id for b in self.grade_bands for r in b.rules] + [r.id for s in self.sections for r in s.rules]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"duplicate rule ids: {sorted(dup)}")
        sections = {s.id for s in self.sections}
        for name, p in self.profiles.items():
            missing = set(p.sections) - sections
            if missing:
                raise ValueError(f"profile {name} names unknown sections {sorted(missing)}")
        if self.default_band not in {b.id for b in self.grade_bands}:
            raise ValueError("default_band is not a grade band")
        grades = [g for b in self.grade_bands for g in b.grades]
        if len(grades) != len(set(grades)):
            raise ValueError("a grade is in more than one band")
        for t in self.escalation_triggers:
            if t.rule not in ids:
                raise ValueError(f"trigger {t.id} names unknown rule {t.rule}")
            for p in t.patterns:
                re.compile(p, re.I)
        for ps in self.language_markers.values():
            for p in ps:
                re.compile(p, re.I)
        if self.status == "approved" and not (self.approved_by and self.approved_on):
            raise ValueError("an approved policy names approved_by and approved_on")
        if self.changelog[-1].version != self.version:
            raise ValueError("the last changelog entry is this version")
        return self

    def rule(self, rule_id: str) -> Rule:
        for r in [r for b in self.grade_bands for r in b.rules] + [r for s in self.sections for r in s.rules]:
            if r.id == rule_id:
                return r
        raise KeyError(rule_id)

    def band_for(self, grade: Optional[int]) -> GradeBand:
        for b in self.grade_bands:
            if grade is not None and grade in b.grades:
                return b
        return next(b for b in self.grade_bands if b.id == self.default_band)


def digest(path: str) -> str:
    with open(path, "rb") as fh:
        return "sha256:" + hashlib.sha256(fh.read()).hexdigest()


def _path(version: str) -> str:
    return os.path.join(POLICY_DIR, f"{POLICY_ID}-{version}.json")


@lru_cache(maxsize=None)
def _released() -> dict[str, str]:
    with open(RELEASED) as fh:
        return json.load(fh)


@lru_cache(maxsize=None)
def get(version: str) -> Policy:
    """A released version of the policy. Fails if the file was changed after
    release (versions are immutable)."""
    path = _path(version)
    rel = _released()
    if version not in rel:
        raise PedagogyError(f"{POLICY_ID} {version} is not a released version")
    if not os.path.exists(path):
        raise PedagogyError(f"{POLICY_ID} {version}: {path} is missing")
    if digest(path) != rel[version]:
        raise PedagogyError(f"{POLICY_ID} {version} was changed after release (digest mismatch): "
                            f"release a new version instead")
    with open(path) as fh:
        raw = json.load(fh)
    try:
        p = Policy.model_validate(raw)
    except ValidationError as exc:
        raise PedagogyError(f"{path}: " + "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                                                    for e in exc.errors())) from None
    if p.version != version:
        raise PedagogyError(f"{path} declares version {p.version}")
    return p


def versions() -> list[str]:
    return sorted(_released(), key=lambda v: tuple(int(x) for x in v.split(".")))


def load_all() -> dict[str, Policy]:
    return {v: get(v) for v in versions()}


# ---------------------------------------------------------------- composition

@dataclass(frozen=True)
class Composed:
    text: str
    record: dict[str, Any] = field(default_factory=dict)


def _audience(roles) -> str:
    roles = set(roles or ())
    return "teacher" if roles & {"teacher", "school_admin", "national_admin", "platform_admin", "admin",
                                 "ai_engineer"} and "student" not in roles else "student"


def _grade(value: Any) -> Optional[int]:
    m = re.search(r"\d{1,2}", str(value)) if value not in (None, "") else None
    return int(m.group(0)) if m else None


def screen(policy: Policy, message: str) -> dict[str, Any]:
    """What the runtime must know about the user's message before a call:
    an escalation trigger (the call is replaced by the policy's response) and
    the language it is written in. Records ids only, never the text."""
    text = message or ""
    for t in policy.escalation_triggers:
        if any(re.search(p, text, re.I) for p in t.patterns):
            return {"escalation": t.id, "rule": t.rule}
    langs = [name for name, ps in policy.language_markers.items()
             if sum(bool(re.search(p, text, re.I)) for p in ps) >= 2]
    return {"language": langs[0]} if langs else {}


def compose(policy: Policy, *, profile: str, roles=None, audience: Optional[str] = None,
            grade: Any = None, language: Optional[str] = None) -> Composed:
    """The policy block for one call: the profile's sections, the rules for this
    audience, the grade band's style rules, and a note when the learner wrote
    in another language."""
    if profile not in policy.profiles:
        raise PedagogyError(f"{policy.id} {policy.version} has no profile {profile!r}")
    prof = policy.profiles[profile]
    aud = audience or _audience(roles)
    g = _grade(grade)
    band = policy.band_for(g) if prof.grade_style else None
    lines = [f"NEXA Teaching Intelligence Policy {policy.version} ({profile}). "
             f"Follow these rules in every reply; they take precedence over style in the task below."]
    used: list[str] = []
    if band is not None:
        lines.append(f"\nLevel: {band.label}" + ("" if g is not None else " (grade not given; this is the default)")
                     + f". Audience: {aud}.")
        for r in band.rules:
            lines.append(f"- [{r.id}] {r.text}")
            used.append(r.id)
    else:
        lines.append(f"\nAudience: {aud}.")
    by_id = {s.id: s for s in policy.sections}
    for sid in prof.sections:
        s = by_id[sid]
        rules = [r for r in s.rules if r.audience is None or aud in r.audience]
        if not rules:
            continue
        lines.append(f"\n{s.title}:")
        for r in rules:
            lines.append(f"- [{r.id}] {r.text}")
            used.append(r.id)
    if language == "tok_pisin" and "language" in prof.sections:
        lines.append("\nThe learner wrote in Tok Pisin: apply TP-LANG-02.")
    text = "\n".join(lines)
    record = {"policy": policy.id, "version": policy.version, "status": policy.status, "profile": profile,
              "audience": aud, "band": band.id if band else None, "grade": g, "rules": len(used),
              "language": language, "digest": "sha256:" + hashlib.sha256(text.encode()).hexdigest()[:16]}
    return Composed(text=text, record=record)


def restated(policy: Policy, system_prompt: str) -> list[str]:
    """Teaching rules an agent's own prompt repeats (they belong in the policy)."""
    low = (system_prompt or "").lower()
    return [p for p in policy.agent_prompt_must_not_restate if p.lower() in low]


def public_numbers() -> set[str]:
    """The digits of every public contact number in any released policy."""
    out = set()
    for v in versions():
        for c in get(v).public_contacts:
            out.add(re.sub(r"\D", "", c.phone))
    return out


def is_public_number(text: str) -> bool:
    d = re.sub(r"\D", "", text or "")
    return any(d.endswith(n) for n in public_numbers()) if d else False


def personal_phones(pattern: "re.Pattern[str]", text: str) -> list[str]:
    """Phone-number matches in text that are not a policy's public contact."""
    return [m.group(0) for m in pattern.finditer(text or "") if not is_public_number(m.group(0))]


def json_schema() -> dict[str, Any]:
    s = Policy.model_json_schema()
    s["$id"] = "https://nexa.learnx.ai/schema/nexa-pedagogy-1.0.schema.json"
    s["title"] = "nexa.pedagogy/1.0: NEXA Teaching Intelligence Policy"
    return s


def _main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "validate"
    if cmd == "schema":
        print(json.dumps(json_schema(), indent=2, sort_keys=True))
        return 0
    if cmd == "show":
        prof, aud, grade = (argv[1:] + ["tutor", "student", None])[:3]
        p = get(versions()[-1])
        c = compose(p, profile=prof, audience=aud, grade=grade)
        print(c.text)
        print("\n" + json.dumps(c.record))
        return 0
    if cmd == "release":
        # Record a new version's digest. Run once, when the version is released.
        v = argv[1]
        rel = dict(_released())
        if v in rel:
            print(f"{v} is already released", file=sys.stderr)
            return 1
        Policy.model_validate(json.load(open(_path(v))))
        rel[v] = digest(_path(v))
        with open(RELEASED, "w") as fh:
            json.dump(rel, fh, indent=2)
            fh.write("\n")
        print(f"released {POLICY_ID} {v} {rel[v]}")
        return 0
    try:
        for v, p in load_all().items():
            n = sum(len(s.rules) for s in p.sections) + sum(len(b.rules) for b in p.grade_bands)
            open_points = [d.id for d in p.decision_points if d.status != "decided"]
            print(f"ok  {p.id} {v} {p.status}  {n} rules, profiles {sorted(p.profiles)}, "
                  f"open decisions {open_points}  {_released()[v][:19]}")
    except PedagogyError as exc:
        print(f"PedagogyError: {exc}", file=sys.stderr)
        return 1
    return 0
