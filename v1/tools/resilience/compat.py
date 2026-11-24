"""v0 compatibility cases for the migrated assistant workflow (NEXA-M3-010).

Each case is one thing people do with NEXA v0 today, through v0's /chat
endpoint (main.py chat_endpoint), with what v0 does and what v1 must do
instead (nexa-wf-assistant). The v0 behaviour is read from the code; the v1
expectation is the compatibility contract:

  same outcome    the request reaches the same kind of answer (a greeting, an
                  exact calculation, a cited curriculum answer, a labelled
                  general answer, a teacher-only refusal for a student)
  safer where     v1 computes arithmetic with the calculator tool, cites the
  v0 was not      curriculum, labels general answers, and fails without output
  deliberately    capabilities moved to M4 agents (lesson plans, assessment
  different       questions) or not carried over (images) give a clear answer
                  instead of v0's output; listed with the reason

Live, a curriculum question must go to the curriculum first; whether the
answer is then cited or a labelled general answer depends on what the indexed
curriculum covers, which the run reports (live_expect, via).

Offline: run in the isolated NEXA (sandbox.World) with a scripted classifier
and Knowledge Service, so every case is deterministic. Live: the same
messages on the DGX with the real model and curriculum (v1/tools/v0_compat.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .sandbox import STUDENT, TEACHER, Knowledge, Model, World

ASSISTANT_WF = "nexa-wf-assistant"


@dataclass(frozen=True)
class Case:
    id: str
    workflow: str                 # the v0 workflow it belongs to
    message: str
    role: str                     # student | teacher
    v0: str                       # what v0 does (from main.py)
    expect: tuple[str, ...]       # acceptable final v1 steps
    intent: str = "curriculum"    # offline: what the scripted classifier answers
    knowledge: str = "no_evidence"
    no_model: bool = False        # v0 answers without any model call; so must v1
    calculator: bool = False      # the answer must come through the calculator tool
    cited: bool = False           # the answer must carry the curriculum citation
    change: str = ""              # a deliberate difference from v0, and why
    live: bool = True             # also run live on the DGX
    fault: Optional[str] = None   # offline only: knowledge_down | classifier_down | bad_expression
    live_expect: Optional[tuple[str, ...]] = None  # live: acceptable final steps, if not `expect`
    via: Optional[str] = None     # live: a step the run must pass through


CASES: list[Case] = [
    # ---- W1 greetings and small talk (v0: quick_reply fast path, then classify_intent smalltalk)
    Case("greet.hello", "greetings", "Hello", "student", "quick_reply: instant greeting, no model call",
         ("greet",), no_model=True),
    Case("greet.morning", "greetings", "Good morning Nexa!", "teacher", "quick_reply greeting",
         ("greet",), no_model=True),
    Case("greet.thanks", "greetings", "Thank you", "student", "quick_reply thanks", ("thanks",), no_model=True),
    Case("greet.bye", "greetings", "Bye", "student", "quick_reply goodbye", ("bye",), no_model=True),
    Case("smalltalk.statement", "greetings", "I am preparing a Grade 11 Biology lesson this week.", "teacher",
         "classify_intent: smalltalk (a statement, not a request) → conversational reply",
         ("chat",), intent="curriculum",
         change="decided by rule: the live classifier called this curriculum (M3 exit suite, 24 Nov)"),
    Case("smalltalk.how_are_you", "greetings", "How are you today?", "student",
         "classify_intent: smalltalk → conversational reply", ("chat",), intent="smalltalk"),
    # ---- W2 maths (v0: looks_like_math → sympy, or the model as tutor)
    Case("maths.arithmetic", "maths", "What is 12 x 8?", "student", "maths first: sympy computes, model explains",
         ("worked",), calculator=True, change="the calculator tool computes it; the model only explains"),
    Case("maths.percent", "maths", "Calculate 15% of 240.", "student", "maths first (calculate)",
         ("worked",), calculator=True),
    Case("maths.words", "maths", "How many students are in 4 classes of 35?", "student",
         "classify_intent → curriculum or general; the model answers the arithmetic",
         ("worked",), calculator=True, change="computed exactly instead of by the model"),
    Case("maths.interest", "maths", "Calculate the simple interest on K500 at 4% per year for 3 years.", "student",
         "maths first: sympy may not parse it, the model as tutor", ("worked",), calculator=True),
    Case("maths.teacher", "maths", "Work out 3/4 of 120.", "teacher", "maths first", ("worked",), calculator=True),
    # ---- W3 curriculum questions (v0: classify curriculum → retrieve → answer with history)
    Case("curriculum.photosynthesis", "curriculum", "What is photosynthesis?", "student",
         "classify curriculum → Chroma retrieval → model answer, no citations",
         ("cited",), knowledge="grounded", cited=True, change="answered with citations to approved sources",
         live_expect=("cited", "general"), via="lookup"),
    Case("curriculum.explain", "curriculum", "Explain osmosis.", "student", "classify curriculum → retrieval",
         ("cited",), knowledge="grounded", cited=True,
         live_expect=("cited", "general"), via="lookup"),
    Case("curriculum.describe", "curriculum", "Describe the water cycle.", "teacher",
         "classify curriculum → retrieval", ("cited",), knowledge="grounded", cited=True,
         live_expect=("cited", "general"), via="lookup"),
    Case("curriculum.mitosis", "curriculum", "What are the stages of mitosis?", "student",
         "classify curriculum → retrieval", ("cited",), knowledge="grounded", cited=True,
         live_expect=("cited", "general"), via="lookup"),
    Case("curriculum.weak", "curriculum", "What causes tides?", "student", "classify curriculum → retrieval",
         ("cited",), knowledge="grounded_weak", cited=True, live=False),
    # ---- W4 curriculum miss (v0: retrieval misses or is evasive → general knowledge)
    Case("curriculum.miss", "curriculum", "Why is the sky blue?", "student",
         "classify curriculum → retrieval misses → general knowledge, unlabelled",
         ("general", "cited"), change="the general answer says it is not from the curriculum"),
    # ---- W5 general knowledge (v0: classify general → general knowledge with history)
    Case("general.capital", "general", "What is the capital city of France?", "student",
         "classify general → general knowledge", ("general", "cited"), intent="general"),
    Case("general.person", "general", "Who invented the telephone?", "teacher", "classify general",
         ("general", "cited"), intent="general"),
    Case("general.sport", "general", "Who won the 2022 football World Cup?", "student", "classify general",
         ("general",), intent="general"),
    # ---- W6 lesson plans (v0: generate_lesson, teachers only)
    Case("lesson.teacher", "lesson plans", "Create a lesson plan on photosynthesis for Grade 11.", "teacher",
         "generate_lesson: asks for missing details, then writes the plan", ("lesson_next",), no_model=True,
         change="lesson plans move to the M4 lesson-planning agent; v1 points to the reviewed Lesson explainer"),
    Case("lesson.student", "lesson plans", "Make a lesson plan about cells.", "student",
         "generate_lesson → 'Only teachers can create full lesson plans.'", ("teachers_only_lesson",), no_model=True),
    # ---- W7 assessment questions (v0: generate_questions, teachers only)
    Case("questions.teacher", "assessment questions", "Generate 10 multiple choice questions on genetics.", "teacher",
         "generate_questions: grounded question set", ("questions_next",), no_model=True,
         change="assessment questions move to the M4 assessment agent, with teacher review"),
    Case("questions.student", "assessment questions", "Make a quiz on the water cycle.", "student",
         "generate_questions → 'Only teachers can generate assessment questions.'",
         ("teachers_only_questions",), no_model=True),
    # ---- W8 images (v0: generate_image with the image model)
    Case("image.request", "images", "Draw a diagram of the human heart.", "student",
         "generate_image: an image is generated", ("no_images",), no_model=True,
         change="image generation is not carried over to v1 (no governed image model yet)"),
    # ---- failures degrade safely (offline only: faults are injected)
    Case("fault.knowledge_down", "curriculum", "What is photosynthesis?", "student",
         "retrieval fails → general knowledge", ("general",), fault="knowledge_down", live=False,
         change="labelled general answer, never an invented citation"),
    Case("fault.classifier_down", "curriculum", "What is a food web?", "student",
         "classify_intent fails → curriculum", ("cited",), knowledge="grounded", fault="classifier_down",
         cited=True, live=False),
    Case("fault.bad_expression", "maths", "What is 7 + 5?", "student",
         "sympy fails → the model as tutor", ("cited", "general"), fault="bad_expression", live=False,
         change="a bad expression is refused by the calculator; the question goes to the curriculum"),
]

WORKFLOWS = ["greetings", "maths", "curriculum", "general", "lesson plans", "assessment questions", "images"]


def _who(role: str):
    return TEACHER if role == "teacher" else STUDENT


def run_offline(case: Case) -> dict[str, Any]:
    from . import sandbox
    model = Model(intent=case.intent, expression="12*8" if "12 x 8" in case.message else Model().expression)
    kn = Knowledge(status=case.knowledge)
    if case.fault == "knowledge_down":
        kn.fail = "unavailable"
    if case.fault == "classifier_down":
        model.faults = {1: "unavailable", 2: "unavailable"}
    if case.fault == "bad_expression":
        model.expression = "seven plus five"
    sandbox.LAST_VIOLATIONS.clear()
    with World(model, kn) as w:
        r = w.start(ASSISTANT_WF, _who(case.role), {"message": case.message})
        path = w.path(r["run_id"])
        trace = w.trace(r["run_id"])
    problems = []
    last = path[-1] if path else None
    if r["status"] != "succeeded":
        problems.append(f"run {r['status']}: {r.get('error')}")
    if last not in case.expect:
        problems.append(f"ended at {last}, expected {' or '.join(case.expect)}")
    if case.no_model and model.calls:
        problems.append(f"{len(model.calls)} model call(s); v0 answers this without the model")
    if case.calculator and not any(t["kind"] == "tool.call" and t["detail"].get("tool") == "calculator"
                                   and t["detail"].get("ok", True) for t in trace):
        problems.append("the calculator tool was not used")
    if case.cited and "[S1]" not in (r.get("output") or ""):
        problems.append("no curriculum citation in the answer")
    violations = list(sandbox.LAST_VIOLATIONS)
    return {"id": case.id, "workflow": case.workflow, "message": case.message, "role": case.role,
            "v0": case.v0, "v1_path": path, "status": r["status"], "passed": not problems and not violations,
            "problems": problems, "violations": violations, "change": case.change, "run_id": r["run_id"]}


def offline_report() -> dict[str, Any]:
    results = [run_offline(c) for c in CASES]
    by_wf = {}
    for wf in WORKFLOWS + ["curriculum"]:
        rs = [r for r in results if r["workflow"] == wf]
        if rs:
            by_wf[wf] = {"cases": len(rs), "passed": sum(r["passed"] for r in rs)}
    violations = [v for r in results for v in r["violations"]]
    return {"suite": "nexa-m3-010-v0-compat", "cases": len(results), "passed": sum(r["passed"] for r in results),
            "workflows": by_wf, "safety_violations": violations,
            "meets": all(r["passed"] for r in results) and not violations, "results": results}


def markdown(rep: dict[str, Any]) -> str:
    lines = ["# v0 compatibility of the migrated assistant (NEXA-M3-010, offline)", "",
             f"{rep['passed']}/{rep['cases']} cases passed; safety invariant violations: "
             f"{len(rep['safety_violations'])}. **{'COMPATIBLE' if rep['meets'] else 'NOT COMPATIBLE'}**.", "",
             "| v0 workflow | Cases | Passed |", "|---|---|---|"]
    for wf, v in rep["workflows"].items():
        lines.append(f"| {wf} | {v['cases']} | {v['passed']} |")
    lines += ["", "| | Case | Role | v0 | v1 path | Deliberate change |", "|---|---|---|---|---|---|"]
    for r in rep["results"]:
        lines.append(f"| {'✔' if r['passed'] else '✘'} | `{r['id']}` {r['message']} | {r['role']} | {r['v0']} | "
                     f"{' → '.join(r['v1_path'])} | {r['change']} |")
        for p in r["problems"] + r["violations"]:
            lines.append(f"| | ↳ {p} | | | | |")
    return "\n".join(lines) + "\n"


__all__ = ["CASES", "Case", "WORKFLOWS", "ASSISTANT_WF", "run_offline", "offline_report", "markdown"]
