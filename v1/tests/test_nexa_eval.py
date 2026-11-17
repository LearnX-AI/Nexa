"""NEXA-M2-008: evaluation dataset, metrics, the fixture suite (the CI gate),
and the deploy-time release gate."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from v1.knowledge.benchmark import evalsuite as ev

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "v1", "tools"))
import nexa_eval  # noqa: E402


@pytest.fixture(scope="module")
def ds():
    return ev.load_dataset()


@pytest.fixture(scope="module")
def thresholds():
    with open(ev.DEFAULT_THRESHOLDS) as fh:
        return json.load(fh)


# ---------------------------------------------------------------- dataset

def test_dataset_is_versioned_and_covers_the_required_cases(ds, thresholds):
    assert ds["version"] == thresholds["dataset_version"]
    items = ds["items"]
    types = {i["type"] for i in items}
    for t in ("exact_fact", "conceptual", "ambiguous", "no_answer", "multilingual", "conflict", "scope",
              "access", "citation_integrity", "failure"):
        assert t in types, t
    corpus = [i for i in items if i["suite"] == "corpus" and i["type"] != "no_answer"]
    assert {i["subject"] for i in corpus} >= {"Biology", "Chemistry", "Physics"}
    assert {i["grade"] for i in corpus} >= {11, 12}
    assert {i["term"] for i in corpus} >= {1, 2, 3}
    # Tok Pisin items are reported but do not gate until a speaker approves them.
    tpi = [i for i in items if i["language"] == "tpi"]
    assert tpi and not any(i["approved"] for i in tpi)
    # school-local vs national content
    scope = [i for i in items if i["type"] == "scope"]
    assert any(i.get("principal", {}).get("school_id") for i in scope)
    assert any(not i.get("principal", {}).get("school_id") for i in scope)


def test_dataset_validation_rejects_bad_items(ds):
    bad = json.loads(json.dumps(ds))
    bad["items"].append(dict(bad["items"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        ev.validate_dataset(bad)
    bad = json.loads(json.dumps(ds))
    bad["items"][0]["files"] = []
    with pytest.raises(ValueError, match="files and evidence"):
        ev.validate_dataset(bad)
    bad = json.loads(json.dumps(ds))
    x = next(i for i in bad["items"] if i["suite"] == "fixture" and i["expect"].get("relevant"))
    x["expect"]["relevant"] = ["F99"]
    with pytest.raises(ValueError, match="unknown fixture document"):
        ev.validate_dataset(bad)


# ---------------------------------------------------------------- metrics

def test_ranking_metrics():
    s = ev.retrieval_summary([{"rank": 1, "precision@5": 1.0}, {"rank": 3, "precision@5": 0.4},
                              {"rank": None, "precision@5": 0.0}, {"rank": 12, "precision@5": 0.0}])
    assert s["recall@1"] == 25.0 and s["recall@5"] == 50.0 and s["recall@10"] == 50.0
    assert s["mrr@10"] == round((1 + 1 / 3) / 4, 3)
    assert s["ndcg@10"] == round((1 + 0.5) / 4, 3)
    assert s["precision@5"] == 0.35


def test_hit_needs_all_evidence_and_strict_hit_the_expected_file():
    item = {"files": ["a.pdf"], "evidence": ["helicase", "hydrogen bonds"]}
    rows = [{"file": "b.pdf", "text": "helicase breaks hydrogen bonds"},
            {"file": "a.pdf", "text": "helicase unwinds DNA"},
            {"file": "a.pdf", "text": "Helicase breaks the HYDROGEN bonds."}]
    assert ev.hit_rank(rows, item, lambda r: r["file"]) == 1                 # any passage that answers it
    assert ev.hit_rank(rows, item, lambda r: r["file"], strict=True) == 3    # from an expected file
    assert ev.precision_at(rows, item, 5, lambda r: r["file"]) == pytest.approx(2 / 3)
    assert ev.file_of_result({"provenance": {"source_uri": "repo://curriculum/STEM%20Bio.pdf"}}) == "STEM Bio.pdf"


def test_answer_scoring():
    item = {"files": ["a.pdf"], "evidence": ["S (Synthesis) phase"]}
    good = {"status": "grounded", "answer": "It is copied in the S (synthesis) phase. [S1]",
            "citations": [{"used": True, "provenance": {"source_uri": "repo://x/a.pdf"}}],
            "integrity": {"valid_markers": ["S1"], "fabricated_markers": []}}
    a = ev.answer_correct(good, item)
    assert a["correct"] and a["cites_relevant"] and a["fabricated"] == 0
    wrong_source = {**good, "citations": [{"used": True, "provenance": {"source_uri": "repo://x/b.pdf"}}]}
    assert not ev.answer_correct(wrong_source, item)["correct"]
    refused = {"status": "no_evidence", "answer": "I could not find this", "citations": []}
    assert not ev.answer_correct(refused, item)["correct"]


def test_score_corpus_refusal_and_authority():
    ans = {"id": "q1", "type": "exact_fact", "approved": True, "language": "en", "files": ["a.pdf"],
           "evidence": ["x"], "expect": {"behavior": "answer", "top_source_type": "official_syllabus"}}
    una = {"id": "u1", "type": "no_answer", "approved": True, "language": "en", "files": [], "evidence": []}
    out = ev.score_corpus([
        {"item": ans, "results": [{"file": "a.pdf", "text": "x"}], "source_types": ["official_syllabus"],
         "evidence": "strong", "ms": 10},
        {"item": una, "results": [], "source_types": [], "evidence": "weak", "ms": 30},
    ])["metrics"]
    assert out["retrieval"]["gating"]["recall@1"] == 100.0
    assert out["authority"]["syllabus_first_rate"] == 1.0
    assert out["no_answer"]["correct_refusal_rate"] == 0.0 and out["no_answer"]["false_refusal_rate"] == 0.0


def test_gate_absolute_and_regression(thresholds):
    rep = {"suite": "corpus", "metrics": {"retrieval": {"gating": {"recall@5": 95.0, "recall@1": 70.0,
                                                                   "mrr@10": 0.7, "ndcg@10": 0.75},
                                                        "by_type": {t: {"recall@5": 95.0} for t in
                                                                    ("exact_fact", "conceptual", "ambiguous", "spelling")}},
                                          "authority": {"syllabus_first_rate": 1.0},
                                          "no_answer": {"correct_refusal_rate": 0.9, "false_refusal_rate": 0.02},
                                          "latency": {"p95_ms": 100.0}}}
    assert ev.evaluate_gate(rep, thresholds)["passed"]
    base = json.loads(json.dumps(rep))
    base["metrics"]["retrieval"]["gating"]["recall@5"] = 99.0  # candidate dropped 4 points
    g = ev.evaluate_gate(rep, thresholds, base)
    assert not g["passed"]
    assert any("vs baseline" in c["metric"] and not c["passed"] for c in g["checks"])
    rep["metrics"]["no_answer"]["false_refusal_rate"] = 0.3
    assert not ev.evaluate_gate(rep, thresholds)["passed"]
    # a metric that was not measured fails its check rather than passing silently
    del rep["metrics"]["latency"]
    assert not ev.evaluate_gate(rep, thresholds)["passed"]


# ---------------------------------------------------------------- fixture suite (the CI gate)

def test_fixture_suite_passes_the_gate(ds, thresholds):
    rep = ev.run_fixture_suite(ds)
    failed = [i for i in rep["items"] if not i["passed"]]
    assert not failed, failed
    assert ev.evaluate_gate(rep, thresholds)["passed"]


def test_fixture_suite_catches_an_authority_regression(ds, thresholds, monkeypatch):
    from v1.knowledge import authority, retrieval

    def no_authority_rules(pool, claim):
        """The real annotations, but relevance order and no conflict handling."""
        authority.arbitrate([dict(it) for it in pool], claim)
        out = list(pool)
        for it in out:
            it.setdefault("authority", {"notes": []})
        return out, []
    monkeypatch.setattr(retrieval, "arbitrate", no_authority_rules)
    rep = ev.run_fixture_suite(ds)
    assert not ev.evaluate_gate(rep, thresholds)["passed"]
    assert any(i["type"] == "conflict" and not i["passed"] for i in rep["items"])


def test_fixture_suite_catches_an_access_regression(ds, thresholds, monkeypatch):
    from v1.identity.principal import Principal
    from v1.knowledge import access
    from v1.knowledge import main as km
    admin = access.retrieval_filter(Principal("x", ("platform_admin",), "user"))
    monkeypatch.setattr(km, "retrieval_filter", lambda p, on=None: admin)
    rep = ev.run_fixture_suite(ds)
    bad = {i["type"] for i in rep["items"] if not i["passed"]}
    assert {"scope", "access"} <= bad


# ---------------------------------------------------------------- release gate in deploy.sh

def _git(repo, *a):
    return subprocess.run(["git", "-C", repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def deploy_repo(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git not available")
    origin, repo = str(tmp_path / "origin.git"), str(tmp_path / "repo")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
    os.makedirs(os.path.join(repo, "v1", "knowledge", "benchmark", "eval"))
    os.makedirs(os.path.join(repo, "v1", "model_gateway"))
    os.makedirs(os.path.join(repo, "v1", "deploy"))
    shutil.copy(os.path.join(ROOT, "v1", "deploy", "deploy.sh"), os.path.join(repo, "v1", "deploy"))
    for p, t in (("v1/knowledge/retrieval.py", "a = 1\n"), ("v1/model_gateway/x.py", "b = 1\n"),
                 ("v1/knowledge/benchmark/eval/thresholds.json", "{}\n"), ("README", "r\n")):
        with open(os.path.join(repo, p), "w") as fh:
            fh.write(t)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "remote", "add", "origin", origin)
    _git(repo, "push", "-q", "origin", "main")
    return repo, tmp_path


def _commit(repo, path, text):
    with open(os.path.join(repo, path), "w") as fh:
        fh.write(text)
    _git(repo, "commit", "-qam", path)
    _git(repo, "push", "-q", "origin", "main")
    return _git(repo, "rev-parse", "HEAD")


def _deploy(repo, tmp, sha, **env):
    e = {**os.environ, "NEXA_HOME": repo, "NEXA_BRANCH": "main", "NEXA_SYSTEMCTL": "true", "NEXA_VENV": "",
         "NEXA_HEALTH_URLS": "file:///etc/hostname", "NEXA_HEALTH_TRIES": "1", "NEXA_HEALTH_WAIT": "0",
         "NEXA_EVAL_GATE_DIR": str(tmp / "gates"), **env}
    return subprocess.run(["bash", os.path.join(repo, "v1", "deploy", "deploy.sh"), sha], env=e,
                          capture_output=True, text=True)


def test_deploy_refuses_a_retrieval_change_without_a_passing_evaluation(deploy_repo):
    repo, tmp = deploy_repo
    before = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "--detach", before)
    sha = _commit_on_main(repo, "v1/knowledge/retrieval.py", "a = 2\n")
    r = _deploy(repo, tmp, sha)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "deploy refused" in r.stdout
    assert _git(repo, "rev-parse", "HEAD") == before  # nothing changed

    # a failing record does not count; a passing one for this exact code does
    key = nexa_eval.gate_key(sha, repo)
    os.makedirs(tmp / "gates", exist_ok=True)
    (tmp / "gates" / f"{key}.json").write_text(json.dumps({"passed": False}))
    assert _deploy(repo, tmp, sha).returncode == 3
    (tmp / "gates" / f"{key}.json").write_text(json.dumps({"gate_key": key, "passed": True}, indent=2))
    r = _deploy(repo, tmp, sha)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _git(repo, "rev-parse", "HEAD") == sha


def test_deploy_needs_no_evaluation_when_retrieval_code_is_unchanged(deploy_repo):
    repo, tmp = deploy_repo
    _git(repo, "checkout", "-q", "--detach", "HEAD")
    for path in ("README", "v1/knowledge/benchmark/eval/thresholds.json"):
        sha = _commit_on_main(repo, path, "changed\n")
        r = _deploy(repo, tmp, sha)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "retrieval code unchanged" in r.stdout


def test_deploy_override_is_logged(deploy_repo):
    repo, tmp = deploy_repo
    _git(repo, "checkout", "-q", "--detach", "HEAD")
    sha = _commit_on_main(repo, "v1/model_gateway/x.py", "b = 2\n")
    r = _deploy(repo, tmp, sha, NEXA_SKIP_EVAL_GATE="1", NEXA_SKIP_EVAL_REASON="hotfix INC-1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OVERRIDDEN" in r.stdout
    with open(os.path.join(repo, ".nexa_eval_gate_overrides")) as fh:
        assert "hotfix INC-1" in fh.read()


def _commit_on_main(repo, path, text):
    """Commit on main in a second worktree (the deploy checkout stays detached
    on the deployed commit, like the DGX) and push it."""
    wt = repo + "-wt"
    if not os.path.exists(wt):
        _git(repo, "worktree", "add", "-q", wt, "main")
    else:
        _git(wt, "pull", "-q", "origin", "main")
    return _commit(wt, path, text)


def test_fixture_suite_catches_an_unlabelled_general_answer(ds, thresholds, monkeypatch):
    from v1.knowledge import general

    async def unlabelled(question, audience, trace_id, reason):
        info = general.GeneralInfo(source_kind="general_knowledge", mode="llm", label="")
        return "Queensland won.", info
    monkeypatch.setattr(general, "general_answer", unlabelled)
    rep = ev.run_fixture_suite(ds)
    assert not ev.evaluate_gate(rep, thresholds)["passed"]
    assert any(i["id"] == "x18" and not i["passed"] for i in rep["items"])


def test_end_to_end_refusal_counts_only_curriculum_answers_as_failures():
    una = {"id": "u1", "type": "no_answer", "approved": True, "language": "en", "files": [], "evidence": []}
    obs = [{"item": dict(una, id=f"u{i}"), "results": [], "source_types": [], "evidence": "weak", "ms": 1,
            "answer": {"status": s}} for i, s in enumerate(["no_evidence", "general", "grounded", "no_evidence"])]
    na = ev.score_corpus(obs)["metrics"]["no_answer"]
    assert na["correct_refusal_rate"] == 0.0  # the evidence grade alone let all four through
    assert na["curriculum_answer_rate"] == 0.25 and na["end_to_end_refusal_rate"] == 0.75


def test_answer_quality_reports_citation_precision_and_unsupported_content():
    """Answer quality is measured on separate axes: accuracy, citation
    correctness, unsupported content (hallucination indicator) and no-answer
    handling, not only retrieval."""
    from v1.knowledge.benchmark import evalsuite as ev
    item = {"id": "q", "type": "exact_fact", "files": ["a.pdf"], "evidence": ["mitochondria release energy"],
            "expect": {"answer_keys": ["mitochondria", "energy"]}}

    def ans(files, weak=0, moved=0):
        return {"status": "grounded", "answer": "Mitochondria release energy [S1].",
                "citations": [{"used": True, "provenance": {"source_uri": f"repo://{f}"}} for f in files],
                "integrity": {"valid_markers": ["S1"], "fabricated_markers": [],
                              "weakly_supported_sentences": weak, "general_paragraphs": moved}}
    a = ev.answer_correct(ans(["a.pdf", "b.pdf"], weak=1), item)
    assert a["citations_used"] == 2 and a["citations_expected"] == 1 and a["weak_sentences"] == 1
    b = ev.answer_correct(ans(["a.pdf"]), item)
    assert b["unsupported_paragraphs"] == 0 and b["correct"]
    # Aggregated: citation precision 2/3, one of two answers has unsupported content.
    obs = [{"item": item, "results": [{"provenance": {"source_uri": "repo://a.pdf"}, "text": "mitochondria release energy"}],
            "evidence": "strong", "ms": 5, "answer": x} for x in (ans(["a.pdf", "b.pdf"], weak=1), ans(["a.pdf"]))]
    m = ev.score_corpus(obs)["metrics"]["answers"]
    assert m["citation_precision"] == 0.667 and m["unsupported_answer_rate"] == 0.5


def test_thresholds_can_gate_a_single_subject_grade_or_language():
    """Curriculum expansion: a new subject, grade or approved language gets its
    own floor in the thresholds file (no code change), and a slice with no
    items fails rather than passing silently."""
    from v1.knowledge.benchmark import evalsuite as ev
    report = {"suite": "live", "metrics": {"retrieval": {"slices": {
        "subject=Biology": {"n": 29, "recall@5": 96.6}, "subject=Chemistry": {"n": 12, "recall@5": 91.7}}}}}
    thr = {"version": "t", "live": {"min": {"retrieval.slices.subject=Biology.recall@5": 85.0,
                                            "retrieval.slices.subject=Chemistry.recall@5": 95.0,
                                            "retrieval.slices.language=tpi.recall@5": 70.0}}}
    g = ev.evaluate_gate(report, thr)
    res = {c["metric"]: c["passed"] for c in g["checks"]}
    assert res["retrieval.slices.subject=Biology.recall@5"] is True
    assert res["retrieval.slices.subject=Chemistry.recall@5"] is False
    assert res["retrieval.slices.language=tpi.recall@5"] is False      # not measured -> fails
    assert g["passed"] is False


def test_diagnose_tool_reads_grade_levels_and_flags_near_copies():
    import importlib.util
    spec = importlib.util.spec_from_file_location("nexa_diagnose", "v1/tools/nexa_diagnose.py")
    nd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nd)
    assert nd.grades_of({"grade_levels": "11,12"}) == {11, 12} and nd.grades_of({}) == set()
    a = nd._shingles("the cell cycle has four phases g1 s g2 and m in that order")
    b = nd._shingles("Lesson 3. The cell cycle has four phases g1 s g2 and m in that order. Copy.")
    assert nd.containment(a, b) == 1.0
    rep = {"generated_at": "t", "base_url": "u", "service": {"index": "i"},
           "totals": {"questions": 2, "misses": 1, "miss_deep": 1, "top5": 5, "top5_lms": 3},
           "misses": [{"id": "c1", "type": "exact_fact", "subject": "Biology", "grade": 12, "question": "q",
                       "expected": ["a.pdf"], "answer_rank": 7, "diversity": None,
                       "top5": [nd.describe({"text": "x y z", "metadata": {"grade_levels": "11",
                                                                         "source_type": "lms_content"}}, 1, [])]}]}
    md = nd.markdown(rep)
    assert "LMS content | 60%" in md and "| 1 |" in md and "[11]" in md


def test_student_view_leaves_out_questions_only_a_teacher_guide_answers(monkeypatch):
    """On the DGX (2026-11-17), 4 of a student's 6 misses were questions whose only
    expected source is a teacher-only guide. Decided by asking the service as the
    role and as an admin, not from file names."""
    import importlib.util
    import types
    spec = importlib.util.spec_from_file_location("nexa_eval_tool", "v1/tools/nexa_eval.py")
    ne = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ne)
    monkeypatch.setenv("NEXA_IDENTITY_SECRET", "x" * 32)
    guide = {"text": "PNG has 79 families and 760 species of birds",
             "provenance": {"source_uri": "repo://TeacherGuide.pdf"}}
    other = {"text": "birds of paradise", "provenance": {"source_uri": "repo://Lesson.docx"}}

    class Client:
        def post(self, url, headers, json):
            admin = "platform_admin" in str(headers) or headers.get("Authorization", "").endswith("ADMIN")
            return types.SimpleNamespace(json=lambda: {"results": [other, guide] if admin else [other]})
    monkeypatch.setattr(ne, "tokens", None, raising=False)
    from v1.identity import tokens
    monkeypatch.setattr(tokens, "issue_token", lambda sub, roles, *a, **k: "ADMIN" if "platform_admin" in roles else "S")
    it_hidden = {"id": "q10", "type": "exact_fact", "files": ["TeacherGuide.pdf"], "evidence": ["79 families"],
                 "question": "q"}
    it_found = {"id": "q1", "type": "exact_fact", "files": ["Lesson.docx"], "evidence": ["birds of paradise"],
                "question": "q"}
    obs = [{"item": it_hidden, "results": [other]}, {"item": it_found, "results": [other]}]
    args = types.SimpleNamespace(as_role="student")
    assert ne.not_visible_to_role(Client(), "http://x", {"Authorization": "Bearer S"}, obs, args) == ["q10"]
