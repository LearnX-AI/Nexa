"""Curriculum Intelligence Agent evaluation cases (NEXA-M4-004), offline.

Each case runs nexa-wf-curriculum-intelligence in the isolated NEXA with the
proposed agent versions activated, a scripted model and Knowledge Service
(sandbox.CURRICULUM: Grade 11-12 Biology, Chemistry and Physics), and checks
one acceptance property. The M3-009 safety invariants are checked on every
run. Categories: mapping, grounding, boundary, safety, integration.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import sandbox
from .sandbox import STUDENT, TEACHER, Knowledge, Model, World, result, run_safely

WF = "nexa-wf-curriculum-intelligence"
AGENT = "nexa-curriculum-intelligence"
TASKS = ["map", "alignment", "progression"]
CITE = re.compile(r"\[S\d+\]")
SECTIONS = {"alignment": ["Covered", "Partly covered", "Not covered", "Suggestions"],
            "progression": ["Prerequisites", "This topic", "What comes next"]}
MENDEL = "Mendel's experiments with pea plants show how dominant and recessive alleles are inherited."
LESSON = ("My lesson: students read about Mendel's pea plant experiments and draw a Punnett square for one "
          "monohybrid cross.")


def _reply(prompt: str) -> str:
    """A scripted answer with the sections the prompt asks for, citing the
    curriculum text's markers."""
    source = prompt.split("with its source markers:\n", 1)[-1].split("\n\n", 1)[0]
    marks = " ".join(dict.fromkeys(CITE.findall(source))) or ""
    task = next((t for t, secs in SECTIONS.items() if all(f"## {s}" in prompt for s in secs)), None)
    body = []
    for h in SECTIONS.get(task, ["Answer"]):
        body += [f"## {h}", f"- A point for {h.lower()}, from the curriculum {marks}.", ""]
    return "\n".join(body)


def model(**kw) -> Model:
    return Model(script=kw.pop("script", []) + [("use exactly these Markdown headings", _reply)], **kw)


def _start(w: World, task: str, text: str = MENDEL, grade: str = "any", subject: str = "any", who=TEACHER):
    return w.start(WF, who, {"task": task, "text": text, "grade": grade, "subject": subject})


@dataclass
class Case:
    id: str
    category: str
    run: Callable[[], tuple[bool, str]]


# ---------------------------------------------------------------- mapping

def maps_grade_subject_unit_topic_and_official_outcomes():
    with World(model(), Knowledge(), proposals=True) as w:
        r = _start(w, "map")
        out = r.get("output") or ""
        need = ["Grade: 11", "Subject: Biology", "Unit: 11.1 Genetics", "Topic: Classical Mendelian Genetics",
                "Official learning outcomes: 11.1.1", "(high confidence)", "Biology Syllabus Grade 11"]
        missing = [n for n in need if n not in out]
        return r["status"] == "succeeded" and not missing and not w.model.calls, f"{r['status']}; missing {missing}"


def grade_named_in_text_is_used():
    with World(model(), Knowledge(), proposals=True) as w:
        r = _start(w, "map", text="Grade 12 Biology: the DNA double helix and base pairing.")
        out = r.get("output") or ""
        grades = [q.get("filters", {}).get("grades") for q in w.knowledge.queries]
        return "Grade: 12" in out and "12.2.1" in out and all(g == [12] for g in grades), f"{grades}; {out[:120]}"


def given_grade_and_subject_filter_the_search():
    with World(model(), Knowledge(), proposals=True) as w:
        r = _start(w, "map", text="Strong acids ionise completely; weak acids only partly.", grade="11",
                   subject="Chemistry")
        out = r.get("output") or ""
        f = w.knowledge.queries[0].get("filters", {})
        return f == {"grades": [11], "subjects": ["Chemistry"]} and "11.3.2" in out and "Subject: Chemistry" in out, \
            f"{f}; {out[:120]}"


def grade_not_held_is_not_covered_and_not_substituted():
    with World(model(), Knowledge(), proposals=True) as w:
        r = _start(w, "map", text="In Grade 7 Science: Mendel's experiments with pea plants.")
        out = r.get("output") or ""
        ok = (w.path(r["run_id"])[-1] == "not_covered" and "no approved Grade 7" in out
              and "will not use another grade" in out and "11.1.1" not in out and not w.model.calls
              and all(q.get("filters", {}).get("grades") == [7] for q in w.knowledge.queries))
        return ok, out[:160]


def subject_not_held_is_not_covered():
    with World(model(), Knowledge(), proposals=True) as w:
        r = _start(w, "alignment", text="Papua New Guinea's road to independence in 1975.", grade="12",
                   subject="History")
        return w.path(r["run_id"])[-1] == "not_covered" and not w.model.calls, w.path(r["run_id"])[-1]


def outcomes_only_from_the_official_syllabus():
    lms = result("Genetics worksheet", "Biology", 11, "lms_content", "Mendel 11.1.9 pea plants", 0.9,
                 outcome_codes="11.1.9", topic_title="Classical Mendelian Genetics")
    kn = Knowledge(results=lambda b: [] if "official_syllabus" in (b.get("filters") or {}).get("source_types", [])
                   else [lms])
    with World(model(), kn, proposals=True) as w:
        r = _start(w, "map")
        out = r.get("output") or ""
        return ("11.1.9" not in out and "none found" in out and "(low confidence)" in out
                and "needs a curriculum reviewer" in out), out[:200]


def outcomes_of_another_grade_are_not_listed():
    g12 = result("Biology Syllabus Grade 11", "Biology", 11, "official_syllabus",
                 "Builds towards 12.2.1 in Grade 12. 11.1.1 Explain Mendel's laws.", 0.8,
                 outcome_codes="12.2.1,11.1.1", topic_title="Classical Mendelian Genetics")
    with World(model(), Knowledge(results=[g12]), proposals=True) as w:
        r = _start(w, "map", grade="11")
        out = r.get("output") or ""
        return "11.1.1" in out and "12.2.1" not in out, out[:200]


def unrelated_outcomes_of_the_passage_are_not_offered():
    # A syllabus passage lists several outcomes; only the one whose statement
    # matches the text is offered.
    off = result("Biology Syllabus Grade 12", "Biology", 12, "official_syllabus",
                 "12.1.1a Investigate the scientific method and design fair tests.\n"
                 "12.6.2b Explain how negative feedback keeps body temperature stable in homeostasis.", 0.8,
                 outcome_codes="12.1.1a,12.6.2b", topic_title="Homeostasis")
    lesson = result("Homeostasis Lesson Note", "Biology", 12, "lms_content", "homeostasis feedback", 0.85)
    with World(model(), Knowledge(results=lambda b: [off] if "official_syllabus" in
                                  (b.get("filters") or {}).get("source_types", []) else [lesson, off]),
               proposals=True) as w:
        r = _start(w, "map", text="How does negative feedback maintain homeostasis?")
        out = r.get("output") or ""
        return ("Official learning outcomes: 12.6.2b" in out and "12.1.1a" not in out.split("- Sources")[0]
                and "12.6.2b: Explain how negative feedback" in out), out[:300]


def no_matching_outcome_is_low_confidence_for_a_reviewer():
    off = result("Physics Syllabus Grade 12", "Physics", 12, "official_syllabus",
                 "12.1.1a Investigate the scientific method and design fair tests.", 0.8, outcome_codes="12.1.1a",
                 topic_title="Wave Types")
    with World(model(), Knowledge(results=[off]), proposals=True) as w:
        r = _start(w, "map", text="What is the difference between transverse and longitudinal waves?")
        out = r.get("output") or ""
        return ("Official learning outcomes: none found" in out and "(low confidence)" in out
                and "list 12.1.1a, but none" in out and "a curriculum reviewer should choose" in out), out[:300]


def topic_in_two_grades_names_both():
    g11 = result("Quantities & Measurement Lesson Note", "Physics", 11, "lms_content", "base and derived quantities",
                 0.86)
    g12 = result("Quantities_and_Measurement_LessonNote", "Physics", 12, "lms_content",
                 "base and derived quantities", 0.85)
    off = result("Physics Syllabus Grade 11", "Physics", 11, "official_syllabus",
                 "11.1.1 Distinguish base and derived quantities and their SI units.", 0.7,
                 outcome_codes="11.1.1")
    kn = Knowledge(results=lambda b: [off] if "official_syllabus" in (b.get("filters") or {}).get(
        "source_types", []) else [g11, g12, g12, g11])
    with World(model(), kn, proposals=True) as w:
        r = _start(w, "map", text="What is the difference between base quantities and derived quantities?")
        out = r.get("output") or ""
        ok = ("Grade: 11" in out and "Also covered in: Grade 12" in out and "(low confidence)" in out
              and "Topic: Quantities & Measurement" in out)
        return ok, out[:300]


def generic_headings_are_not_topics():
    lesson = result("Classical Mendelian Genetics_Lesson_Note", "Biology", 11, "lms_content", "Mendel pea plants",
                    0.9, section_path="Genetics > Check Yourself")
    guide = result("Biology Student Guide Grade 11", "Biology", 11, "student_guide", "Mendel pea plants", 0.9,
                   section_path="Unit 1 > Inheritance > Check Yourself")
    with World(model(), Knowledge(results=[lesson]), proposals=True) as w:
        a = _start(w, "map").get("output") or ""
    with World(model(), Knowledge(results=[guide]), proposals=True) as w:
        b = _start(w, "map").get("output") or ""
    ok = "Topic: Classical Mendelian Genetics" in a and "Topic: Inheritance" in b and "Check Yourself" not in a + b
    return ok, a[:120] + " | " + b[:120]


def mixed_subjects_are_ambiguous():
    mix = [result("Biology Syllabus Grade 11", "Biology", 11, "official_syllabus", "energy", 0.8),
           result("Chemistry Syllabus Grade 11", "Chemistry", 11, "official_syllabus", "energy", 0.8),
           result("Physics Syllabus Grade 12", "Physics", 12, "official_syllabus", "energy", 0.8)]
    with World(model(), Knowledge(results=mix), proposals=True) as w:
        r = _start(w, "map", text="Energy and how it is stored.")
        out = r.get("output") or ""
        return w.path(r["run_id"])[-1] == "ambiguous" and "Say which subject" in out and not w.model.calls, out[:160]


# ---------------------------------------------------------------- grounding

def task_grounded(task: str):
    def run():
        with World(model(), Knowledge(), proposals=True) as w:
            r = _start(w, task, text=LESSON if task == "alignment" else MENDEL)
            out = r.get("output") or ""
            heads = [h for h in SECTIONS[task] if f"## {h}" in out]
            ok = (r["status"] == "succeeded" and len(heads) == len(SECTIONS[task]) and CITE.search(out)
                  and "Official learning outcomes: 11.1.1" in out and "Advisory" in out)
            return bool(ok), f"{r['status']}; sections {heads}"
    return run


def lookups_name_the_mapped_grade():
    with World(model(), Knowledge(), proposals=True) as w:
        _start(w, "alignment", text=LESSON)
        _start(w, "progression")
        return bool(w.knowledge.calls) and all("Grade 11 Biology" in q for q in w.knowledge.calls), \
            str(w.knowledge.calls)


def invented_citation_stops_the_answer():
    m = model(script=[("use exactly these Markdown headings", "## Covered\n- x [S7]\n## Partly covered\n- y [S7]\n"
                                                                  "## Not covered\n- z\n## Suggestions\n- w [S7]")])
    with World(m, Knowledge(), proposals=True) as w:
        r = _start(w, "alignment", text=LESSON)
        return r["status"] == "failed" and not r.get("output"), f"{r['status']}: {str(r.get('error'))[:80]}"


def no_curriculum_text_gives_the_mapping_only():
    with World(model(), Knowledge(status="no_evidence"), proposals=True) as w:
        r = _start(w, "progression")
        out = r.get("output") or ""
        return (w.path(r["run_id"])[-1] == "not_grounded" and "Official learning outcomes: 11.1.1" in out
                and "does not guess" in out and not w.model.calls), out[:160]


# ---------------------------------------------------------------- boundary

def students_cannot_run_it():
    with World(model(), Knowledge(), proposals=True) as w:
        _, err = run_safely(lambda: _start(w, "map", who=STUDENT))
        return bool(err and "403" in err), err or "started"


def contact_details_refused_before_anything_runs():
    with World(model(), Knowledge(), proposals=True) as w:
        r = _start(w, "map", text="Sam (sam@example.org, 7123 4567) is learning Mendel's laws.")
        return (w.path(r["run_id"])[-1] == "personal_data" and not w.knowledge.queries
                and not w.model.calls), w.path(r["run_id"])[-1]


def read_only_and_least_privilege():
    from v1.agent_runtime.policy import pdp
    from v1.tool_gateway import gateway
    with World(model(), Knowledge(), proposals=True):
        p = pdp.POLICIES[AGENT]
        tool = gateway.TOOLS["curriculum_map"] if hasattr(gateway, "TOOLS") else None
        agents = set(tool.allowed_agents) if tool else {AGENT}
        ok = (not p.write and not p.delegate and not p.accept_delegation and set(p.invoke) ==
              {"curriculum_map", "knowledge_answer"} and agents == {AGENT}
              and (tool is None or tool.side_effects == "read"))
        return ok, f"invoke {sorted(p.invoke)}; curriculum_map for {sorted(agents)}"


def other_agents_cannot_use_curriculum_map():
    from v1.agent_runtime import tools
    with World(model(), Knowledge(), proposals=True) as w:
        import asyncio
        try:
            asyncio.run(tools.invoke_tool("nexa-teacher-assistant", "curriculum_map", TEACHER, {"text": MENDEL},
                                          user_token=f"Bearer {sandbox.token(TEACHER)}"))
        except tools.ToolError as exc:
            return exc.status_code in (403, 404) and not w.knowledge.queries, f"refused {exc.status_code}"
        return False, "the Teacher Assistant could use curriculum_map"


# ---------------------------------------------------------------- safety

def curriculum_down_nothing_mapped():
    with World(model(), Knowledge(fail="unavailable"), proposals=True) as w:
        r = _start(w, "alignment", text=LESSON)
        return w.path(r["run_id"])[-1] == "unavailable" and not w.model.calls, w.path(r["run_id"])[-1]


def policy_on_every_call():
    with World(model(), Knowledge(), proposals=True) as w:
        r = _start(w, "progression")
        peds = [t["detail"].get("pedagogy") for t in w.trace(r["run_id"]) if t["kind"] == "agent.call"]
        return bool(peds) and all(p and p["version"] == "1.0.0" and p["profile"] == "planning" for p in peds), \
            str(peds)


# ---------------------------------------------------------------- integration

def advisory_footer_with_run_id():
    with World(model(), Knowledge(), proposals=True) as w:
        r = _start(w, "map")
        out = r.get("output") or ""
        return "nothing is published or changed" in out and r["run_id"] in out, out[-160:]


def unknown_task_explained():
    with World(model(), Knowledge(), proposals=True) as w:
        r = _start(w, "rewrite_syllabus")
        out = r.get("output") or ""
        return "Choose a task: map" in out and not w.knowledge.queries and not w.model.calls, out[:100]


def through_the_runtime_api():
    from fastapi.testclient import TestClient

    from v1.agent_runtime.main import app
    with World(model(), Knowledge(), proposals=True):
        c = TestClient(app)
        h = {"Authorization": f"Bearer {sandbox.token(TEACHER)}"}
        r = c.post(f"/v1/workflows/{WF}/runs?wait=true",
                   json={"input": {"task": "map", "text": MENDEL, "grade": "any", "subject": "any"}}, headers=h)
        body = r.json()
        hs = {"Authorization": f"Bearer {sandbox.token(STUDENT)}"}
        s = c.post(f"/v1/workflows/{WF}/runs", json={"input": {"task": "map", "text": MENDEL, "grade": "11", "subject": "any"}}, headers=hs)
        ok = r.status_code in (200, 201, 202) and s.status_code == 403
        return ok, f"teacher {r.status_code} {str(body.get('status'))}; student {s.status_code}"


CASES: list[Case] = (
    [Case("mapping.grade_subject_unit_topic_outcomes", "mapping", maps_grade_subject_unit_topic_and_official_outcomes),
     Case("mapping.grade_named_in_text", "mapping", grade_named_in_text_is_used),
     Case("mapping.given_grade_and_subject", "mapping", given_grade_and_subject_filter_the_search),
     Case("mapping.grade_not_held_not_substituted", "mapping", grade_not_held_is_not_covered_and_not_substituted),
     Case("mapping.subject_not_held", "mapping", subject_not_held_is_not_covered),
     Case("mapping.outcomes_only_official", "mapping", outcomes_only_from_the_official_syllabus),
     Case("mapping.no_other_grade_outcomes", "mapping", outcomes_of_another_grade_are_not_listed),
     Case("mapping.ambiguous_subject", "mapping", mixed_subjects_are_ambiguous),
     Case("mapping.unrelated_outcomes_dropped", "mapping", unrelated_outcomes_of_the_passage_are_not_offered),
     Case("mapping.no_matching_outcome_low", "mapping", no_matching_outcome_is_low_confidence_for_a_reviewer),
     Case("mapping.topic_in_two_grades", "mapping", topic_in_two_grades_names_both),
     Case("mapping.generic_heading_not_topic", "mapping", generic_headings_are_not_topics)]
    + [Case(f"grounding.{t}", "grounding", task_grounded(t)) for t in ("alignment", "progression")]
    + [Case("grounding.lookups_name_the_grade", "grounding", lookups_name_the_mapped_grade),
       Case("grounding.invented_citation_stops", "grounding", invented_citation_stops_the_answer),
       Case("grounding.no_text_mapping_only", "grounding", no_curriculum_text_gives_the_mapping_only),
       Case("boundary.students_refused", "boundary", students_cannot_run_it),
       Case("boundary.contact_details_refused", "boundary", contact_details_refused_before_anything_runs),
       Case("boundary.read_only_least_privilege", "boundary", read_only_and_least_privilege),
       Case("boundary.tool_for_this_agent_only", "boundary", other_agents_cannot_use_curriculum_map),
       Case("safety.curriculum_down", "safety", curriculum_down_nothing_mapped),
       Case("safety.policy_on_every_call", "safety", policy_on_every_call),
       Case("integration.advisory_footer", "integration", advisory_footer_with_run_id),
       Case("integration.unknown_task", "integration", unknown_task_explained),
       Case("integration.runtime_api", "integration", through_the_runtime_api)]
)
CATEGORIES = ["mapping", "grounding", "boundary", "safety", "integration"]


def run_case(c: Case) -> dict[str, Any]:
    sandbox.LAST_VIOLATIONS.clear()
    res, err = run_safely(c.run)
    ok, detail = res if res else (False, err)
    v = list(sandbox.LAST_VIOLATIONS)
    return {"id": c.id, "category": c.category, "passed": bool(ok) and not v, "detail": detail, "violations": v}


def report(only: Optional[str] = None) -> dict[str, Any]:
    t0 = time.time()
    results = [run_case(c) for c in CASES if not only or c.id.startswith(only) or c.category == only]
    cats = {}
    for cat in CATEGORIES:
        rs = [r for r in results if r["category"] == cat]
        if rs:
            cats[cat] = {"cases": len(rs), "passed": sum(r["passed"] for r in rs)}
    return {"suite": "nexa-m4-004-curriculum-intelligence", "cases": len(results),
            "passed": sum(r["passed"] for r in results), "categories": cats, "tasks": TASKS,
            "violations": [v for r in results for v in r["violations"]],
            "meets": bool(results) and all(r["passed"] for r in results), "seconds": round(time.time() - t0, 1),
            "results": results}


def markdown(rep: dict[str, Any]) -> str:
    lines = ["# Curriculum Intelligence Agent evaluation (NEXA-M4-004, offline)", "",
             f"{rep['passed']}/{rep['cases']} cases passed; safety invariant violations: {len(rep['violations'])}. "
             f"**{'PASS' if rep['meets'] else 'FAIL'}**.", "", "Tasks: " + ", ".join(rep["tasks"]), "",
             "| Category | Cases | Passed |", "|---|---|---|"]
    lines += [f"| {c} | {v['cases']} | {v['passed']} |" for c, v in rep["categories"].items()]
    lines += ["", "| | Case | Detail |", "|---|---|---|"]
    for r in rep["results"]:
        lines.append(f"| {'✔' if r['passed'] else '✘'} | `{r['id']}` | {str(r['detail'])[:160]} |")
        for v in r["violations"]:
            lines.append(f"| | ↳ invariant | {v} |")
    return "\n".join(lines) + "\n"
