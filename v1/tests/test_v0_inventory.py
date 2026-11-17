"""NEXA-M2-010: v0 knowledge inventory (read-only) and its decisions."""
import importlib.util
import os
import sqlite3

import pytest

spec = importlib.util.spec_from_file_location("v0_inventory", os.path.join("v1", "tools", "v0_inventory.py"))
inv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inv)


def make_chroma(path, docs):
    """A chroma.sqlite3 with the tables Chroma 0.4/0.5 uses (collections,
    segments, embeddings, embedding_metadata)."""
    os.makedirs(path, exist_ok=True)
    con = sqlite3.connect(os.path.join(path, "chroma.sqlite3"))
    con.executescript("""
      CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT, dimension INTEGER);
      CREATE TABLE segments (id TEXT PRIMARY KEY, type TEXT, scope TEXT, collection TEXT);
      CREATE TABLE embeddings (id INTEGER PRIMARY KEY, segment_id TEXT, embedding_id TEXT, seq_id BLOB);
      CREATE TABLE embedding_metadata (id INTEGER, key TEXT, string_value TEXT, int_value INTEGER,
                                       float_value REAL, bool_value INTEGER);
      INSERT INTO collections VALUES ('c1', 'curriculum_db', 768);
      INSERT INTO segments VALUES ('s1', 'sqlite', 'METADATA', 'c1');
    """)
    n = 0
    for name, md, chunks in docs:
        for _ in range(chunks):
            n += 1
            con.execute("INSERT INTO embeddings VALUES (?, 's1', ?, NULL)", (n, f"e{n}"))
            for k, v in {"file": name, "chroma:document": "chunk text", **md}.items():
                con.execute("INSERT INTO embedding_metadata (id, key, string_value) VALUES (?, ?, ?)", (n, k, v))
    con.commit()
    con.close()


@pytest.fixture
def v0(tmp_path):
    d = tmp_path / "Nexa"
    (d / "uploads").mkdir(parents=True)
    (d / "assets").mkdir()
    (d / "venv" / "lib").mkdir(parents=True)
    (d / "docs").mkdir()
    (d / "docs" / "design.pdf").write_bytes(b"%PDF v1 doc, not v0 knowledge")
    (d / "STEMBio-Term2-TeacherGuide-Gr11.pdf").write_bytes(b"%PDF teacher guide")
    (d / "STEMChem-Term1-Syllubus-Gr12.pdf").write_bytes(b"%PDF chemistry syllabus")
    (d / "copy of guide.pdf").write_bytes(b"%PDF teacher guide")
    (d / "gr12Ente3.pdf").write_bytes(b"%PDF engineering")
    (d / "user_memory.json").write_text("{}")
    (d / "uploads" / "homework.pdf").write_bytes(b"%PDF learner")
    (d / "assets" / "lesson_1.pdf").write_bytes(b"%PDF generated")
    (d / "venv" / "lib" / "x.txt").write_text("ignored")
    make_chroma(str(d / "chroma_store"), [
        ("STEMBio-Term2-TeacherGuide-Gr11.pdf", {"doctype": "Teacher Guide"}, 3),
        ("Birds of Paradise_Lesson_Note.docx", {"doctype": "LMS Material", "course": "11"}, 2),
        ("mystery.pdf", {"doctype": "Unknown"}, 1),
    ])
    return d


def test_every_v0_asset_gets_a_decision_and_a_reason(v0):
    files = inv.scan_files(str(v0))
    assert not any("venv" in f["path"] or "docs" in f["path"] for f in files)
    chroma = inv.read_chroma(str(v0 / "chroma_store"))
    assert chroma["chunks"] == 6 and chroma["dimension"] == 768 and len(chroma["documents"]) == 3
    import hashlib
    guide_sum = "sha256:" + hashlib.sha256(b"%PDF teacher guide").hexdigest()
    v1 = {guide_sum: {"version_id": "v1", "source_id": "png-ndoe:stem-biology-teacher-guide-gr11-t2",
                      "title": "t", "source_type": "teacher_guide", "file_name": "x"}}
    rows = {r["item"]: r for r in inv.decide(files, chroma, v1)}
    assert rows["STEMBio-Term2-TeacherGuide-Gr11.pdf"]["decision"] == "retained"
    assert rows["copy of guide.pdf"]["decision"] == "deprecated"
    syl = rows["STEMChem-Term1-Syllubus-Gr12.pdf"]
    assert syl["decision"] == "reprocess"
    assert syl["v1_mapping"]["source_type"] == "official_syllabus" and syl["v1_mapping"]["authority_tier"] == 1
    assert rows["gr12Ente3.pdf"]["decision"] == "review" and "grade 12" in rows["gr12Ente3.pdf"]["reason"]
    for p in ("user_memory.json", os.path.join("uploads", "homework.pdf"), os.path.join("assets", "lesson_1.pdf")):
        assert rows[p]["decision"] == "excluded"
    idx = [r for r in rows.values() if r["kind"] == "vector_index"][0]
    assert idx["decision"] == "deprecated" and idx["chunks"] == 6
    assert rows["chroma:Birds of Paradise_Lesson_Note.docx"]["decision"] == "resync"
    assert rows["chroma:mystery.pdf"]["decision"] == "review"
    assert "chroma:STEMBio-Term2-TeacherGuide-Gr11.pdf" not in rows     # covered by its file row
    assert all(r.get("reason") for r in rows.values())


def test_v1_mapping_is_v1_ingestions_own(v0):
    m = inv.v1_mapping("s04_cm8879_12.2_Genetics and Molecular Biology_3_DNA Replication.docx")
    assert m["source_type"] == "lms_content" and m["lms_item"] == "cm8879" and m["grade_levels"] == [12]
    assert inv.v1_mapping("STEMBio-Term2-TeacherGuide-Gr11.pdf")["access_classification"] == "teacher_only"
    assert inv.v1_mapping("gr12Ente3.pdf") is None


def test_report_lists_every_item(v0, tmp_path):
    files = inv.scan_files(str(v0))
    chroma = inv.read_chroma(str(v0 / "chroma_store"))
    rep = {"generated_at": "t", "v0_dir": str(v0), "v1_url": None, "files": files, "chroma": chroma,
           "items": inv.decide(files, chroma, None)}
    md = inv.markdown(rep)
    assert "| excluded | 3 |" in md and "6 chunks in `curriculum_db`" in md
    assert md.count("\n| ") >= len(rep["items"]) + 2


def test_unreadable_chroma_schema_is_reported_not_guessed(tmp_path):
    d = tmp_path / "chroma_store"
    d.mkdir()
    sqlite3.connect(str(d / "chroma.sqlite3")).execute("CREATE TABLE other (x)").connection.close()
    with pytest.raises(sqlite3.Error):
        inv.read_chroma(str(d))


def test_provenance_check_follows_a_citation_to_the_original_file():
    import hashlib
    spec2 = importlib.util.spec_from_file_location("provenance_check", os.path.join("v1", "tools", "provenance_check.py"))
    pc = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(pc)
    data = b"%PDF the syllabus bytes"
    ck = "sha256:" + hashlib.sha256(data).hexdigest()
    chunk = {"text": "Ornithology is a branch of zoology that deals with the study of birds.", "active": True,
             "source": {"version_id": "v1", "content_checksum": ck, "approved_by": "curriculum officer",
                        "status": "active"}}
    cit = {"marker": "S1", "quote": "Ornithology is a branch of zoology",
           "provenance": {"chunk_id": "c1", "version_id": "v1", "content_checksum": ck, "title": "Syllabus"}}
    ok = pc.check_citation(cit, lambda cid: chunk, lambda vid: data, {})
    assert all(ok[k] for k in pc.CHECKS) and "detail" not in ok
    bad = pc.check_citation({**cit, "quote": "A different passage entirely"}, lambda cid: chunk,
                            lambda vid: b"tampered", {})
    assert not bad["quote_matches"] and not bad["original_hash"] and bad["version_matches"]
    gone = pc.check_citation(cit, lambda cid: None, lambda vid: data, {})
    assert gone["detail"] == "chunk not found" and not any(gone[k] for k in pc.CHECKS)


def test_a_document_only_in_the_v0_index_is_retained_when_v1_holds_the_same_text(v0):
    """DGX, 2026-11-17: 527 documents exist only inside the v0 Chroma store (LMS
    uploads). Those v1 already holds through the LMS sync are retained; only the
    rest need to come from the LMS again."""
    files = inv.scan_files(str(v0))
    chroma = inv.read_chroma(str(v0 / "chroma_store"))
    birds = [d for d in chroma["documents"] if d["name"].startswith("Birds")][0]
    assert birds["samples"] == ["chunk text"]
    v0_text = ("Papua New Guinea has a rich biodiversity of birds. The country is home to more than 79 families "
               "and about 760 species of birds, many found nowhere else.")
    v1_text = "Lesson 2. " + v0_text + " Teacher note: ask students which birds live near their village."
    assert inv.overlap(v0_text, v1_text) == 1.0 and inv.overlap(v0_text, "an unrelated passage on DNA") == 0.0
    matches = {"Birds of Paradise_Lesson_Note.docx": {"overlap": 1.0, "title": "Birds of Paradise_Lesson_Note",
                                                      "source_id": "lms:edunex:cm1", "source_type": "lms_content"},
               "mystery.pdf": {"overlap": 0.2, "title": "Something else", "source_id": "x"}}
    rows = {r["item"]: r for r in inv.decide(files, chroma, None, matches)}
    assert rows["chroma:Birds of Paradise_Lesson_Note.docx"]["decision"] == "retained"
    assert "lms:edunex:cm1" in rows["chroma:Birds of Paradise_Lesson_Note.docx"]["reason"]
    assert rows["chroma:mystery.pdf"]["decision"] == "review"
    assert rows["chroma:mystery.pdf"]["closest_in_v1"].startswith("20% word match")


@pytest.mark.parametrize("v0_name,v1_title,same", [
    ("04_DNA_Transistor.docx", "DNA Transistor_Lesson Note", True),
    ("11.6_Ornithology_2_Birds_Behaviour.pptx", "Birds Behaviour_PPT", True),
    ("Topic1_Common_Acids_and_Bases.pptx", "Common Acids and Bases", True),
    ("Properties_and_Structure_of_Matter.pptx", "Chemical Bonding", False),
    ("12.5_Battery Technology_2_Paper Batteries.pptx", "PPT", False),     # v1 title says nothing: not assumed
])
def test_title_match_for_older_revisions(v0_name, v1_title, same):
    assert inv.same_title(v0_name, v1_title) is same


def test_older_revisions_tests_and_generated_files_are_not_sent_for_resync(v0):
    """DGX, 2026-11-17: of 78 v0-only documents v1 did not hold verbatim, most
    were older revisions of lessons v1 holds from the LMS, plus test uploads and
    files v0 generated itself."""
    files = inv.scan_files(str(v0))
    chroma = {"found": True, "path": "p", "collection": "curriculum_db", "chunks": 9, "documents": [
        {"name": "04_DNA_Transistor.docx", "chunks": 10, "v0_doctype": "LMS Material", "course": "Physics"},
        {"name": "Test_PPT.pptx", "chunks": 8, "v0_doctype": "LMS Material", "course": "Biology"},
        {"name": "lesson_20260724_092800.pdf", "chunks": 5, "v0_doctype": "LMS Material", "course": "Biology"},
        {"name": "Ocean Currents.pdf", "chunks": 16, "v0_doctype": "LMS Material", "course": "Geography"},
        {"name": "12.5_Battery Technology_2_Paper Batteries.pptx", "chunks": 10, "v0_doctype": "LMS Material",
         "course": "Biology"}]}
    matches = {"04_DNA_Transistor.docx": {"overlap": 0.59, "title": "DNA Transistor_Lesson Note", "source_id": "lms:a"},
               "Ocean Currents.pdf": {"overlap": 0.0},
               "12.5_Battery Technology_2_Paper Batteries.pptx": {"overlap": 0.54, "title": "PPT", "source_id": "lms:b"}}
    rows = {r["item"]: r for r in inv.decide(files, chroma, None, matches)}
    assert rows["chroma:04_DNA_Transistor.docx"]["decision"] == "deprecated"
    assert "superseded" in rows["chroma:04_DNA_Transistor.docx"]["reason"]
    assert rows["chroma:Test_PPT.pptx"]["decision"] == "deprecated"
    assert rows["chroma:lesson_20260724_092800.pdf"]["decision"] == "excluded"
    assert rows["chroma:Ocean Currents.pdf"]["decision"] == "resync"
    assert rows["chroma:12.5_Battery Technology_2_Paper Batteries.pptx"]["decision"] == "resync"
