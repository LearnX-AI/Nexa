import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
import base64
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import html
import uuid
import secrets
from urllib.parse import quote_plus
import os
import re
import json
import difflib
import textwrap
from typing import Optional, Dict, Any, List
import datetime
from fastapi.staticfiles import StaticFiles
import threading
import json
import sympy
from sympy.parsing.sympy_parser import parse_expr, standard_transformations, implicit_multiplication_application
from fastapi import UploadFile, File, Form
import tempfile

INGEST_TOKEN = os.environ.get("NEXA_INGEST_TOKEN", "")

try:
    import mysql.connector
except ModuleNotFoundError:
    mysql.connector = None

from ddgs import DDGS

try:
    import wikipedia
except ModuleNotFoundError:
    wikipedia = None

try:
    import torch
    from diffusers import DiffusionPipeline
    IMAGE_RUNTIME_AVAILABLE = True
except ModuleNotFoundError:
    torch = None
    DiffusionPipeline = None
    IMAGE_RUNTIME_AVAILABLE = False

try:
    from langchain_community.chat_message_histories import ChatMessageHistory
    from langchain_community.chat_models import ChatOllama
    from langchain_community.document_loaders import PyPDFLoader
    from langchain_community.embeddings import OllamaEmbeddings
    from langchain_chroma import Chroma
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
    from langchain_core.runnables.history import RunnableWithMessageHistory
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    from langchain.chains import create_history_aware_retriever, create_retrieval_chain
    from markdown_pdf import MarkdownPdf, Section
    LANGCHAIN_AVAILABLE = True
except ModuleNotFoundError:
    ChatMessageHistory = None
    ChatOllama = None
    PyPDFLoader = None
    OllamaEmbeddings = None
    Chroma = None
    AIMessage = None
    HumanMessage = None
    StrOutputParser = None
    ChatPromptTemplate = None
    MessagesPlaceholder = None
    RunnableWithMessageHistory = None
    RecursiveCharacterTextSplitter = None
    create_history_aware_retriever = None
    create_retrieval_chain = None
    MarkdownPdf = None
    Section = None
    LANGCHAIN_AVAILABLE = False


import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

_SCIENCE_IMAGE_SYSTEM = (
    "You convert a teacher's image request into a precise, renderable spec. Output ONLY "
    "JSON, no prose. Pick the engine that renders the topic ACCURATELY:\n\n"
    "- \"molecule\": a chemistry structure/bonding topic. Fields: smiles (valid SMILES), caption. "
    "Examples: water=O, ethanol=CCO, ethene(C=C double bond)=C=C, CO2=O=C=O, methane=C, "
    "ammonia=N, benzene=c1ccccc1, glucose=OCC1OC(O)C(O)C(O)C1O.\n"
    "- \"reaction\": a chemical reaction. Fields: reaction_smiles (reactants>>products), caption. "
    "Example combustion of methane: C.O=O>>O=C=O.O\n"
    "- \"plot\": a physics/math graph. Fields: plot = {title, xlabel, ylabel, expression "
    "(numpy in x, e.g. 'x**2' or 'sin(x)'), xmin, xmax}.\n"
    "- \"flow\": a process/cycle/steps diagram (nitrogen cycle, respiration, food chain). "
    "Fields: mermaid (a valid Mermaid 'flowchart TD' block), caption.\n"
    "- \"diffusion\": anything with NO exact renderer — biology anatomy (cell, heart, organs), "
    "real-world scenes, apparatus, ecosystems. Field: caption.\n\n"
    "Return {\"engine\": \"...\", ...fields...}. Base facts on standard curriculum; be exact. "
    "Prefer molecule/reaction/plot/flow whenever the topic fits; use diffusion only when none do."
)

_CURRICULUM_FORMAT_SYSTEM = (
    "You are Nexa, reformatting official curriculum material into a clear answer for a student.\n\n"
    "RULES:\n"
    "- Use ONLY the information in the material below. Do NOT add facts or outside knowledge.\n"
    "- Do NOT omit substantive information; cover every key point.\n"
    "- NEVER mention the material, the source, the excerpts, the curriculum, page numbers, "
    "grade, or term. The student must never learn that source material exists. Write as if "
    "the knowledge is simply yours.\n"
    "- NEVER comment on what the material does or does not contain. Phrases such as 'there "
    "is no coverage of', 'the material only mentions', 'not directly mentioned in the "
    "provided material', 'this can be inferred from related concepts', or any variant of "
    "them are failures.\n"
    "- If the material does not answer the question, your ENTIRE reply must be the "
    "single token NO_COVERAGE. Do not write a sentence explaining that. Do not add "
    "a heading. Do not write anything else. Any sentence about what the material "
    "contains, covers, mentions, or lacks is a failure.\n"
    "- Remove PDF artifacts (page markers like '2 | P a g e', footnote numbers, broken "
    "spacing, URLs).\n"
    "- Clean Markdown: a short opening sentence, then headings, bold terms, and bullet lists.\n"
    "- Do NOT greet or name the student. No preamble or closing question.\n"
    "- Begin directly with the answer.\n\n"
    "Audience guidance: {audience}"
)

_DOC_REFERENCE_RE = re.compile(
    r"\b(document|file|pdf|attachment|upload(?:ed)?|paper|report|handout|notes)\b",
    re.IGNORECASE)


_QTYPE_LABELS = {
    "mcq": "multiple-choice", "short_answer": "short-answer",
    "fill_blank": "fill-in-the-blank", "true_false": "true/false",
    "essay": "essay",
}

def build_question_request(spec: dict) -> str:
    """A standalone instruction built from the spec, so the generator never
    receives an unresolved 'from this' with no topic attached."""
    topic = (spec.get("topic") or "").strip()
    label = _QTYPE_LABELS.get(spec.get("type", "mcq"), "")
    count = spec.get("count", 10)
    if not topic:
        return f"Write {count} {label} questions."
    scope = spec.get("scope") or {}
    parts = [f"Write {count} {label} questions on {topic}"]
    if scope.get("grade"):
        parts.append(f"for {scope['grade']}")
    if scope.get("subject"):
        parts.append(f"in {scope['subject']}")
    return " ".join(parts) + "."



from docx import Document as DocxDocument
# s15_cm8912_12.4_Bioenergetics_2_Biomass  Biofuels.docx -> Grade 12 Biology lesson notes
DOCX_NAME_RE = re.compile(r"^s\d+_cm\d+_(?P<module>1[012]\.\d+)_(?P<topic>.+)$", re.IGNORECASE)

def parse_docx_curriculum_filename(path: str) -> dict:
    """Grade 12 Biology lesson-note docx: sNN_cmNNNN_12.X_Module_Topic.docx."""
    stem = os.path.splitext(os.path.basename(path or ""))[0]
    info = {"file": os.path.basename(path or ""), "subject": "Biology",
            "grade": "Grade 12", "term": "Unknown", "doctype": "Lesson Notes", "priority": 2}
    m = DOCX_NAME_RE.match(stem.strip())
    if m:
        topic = m.group("topic").replace("_", " ").strip()
        info["module"] = m.group("module")
        info["topic"] = topic
    return info

import os
try:
    from langchain_openai import ChatOpenAI
    _openai_key = os.environ.get("OPENAI_API_KEY")
    llm_general = ChatOpenAI(model="gpt-4o", temperature=0.3, api_key=_openai_key) if _openai_key else None
    if llm_general:
        print("General-knowledge model: OpenAI gpt-4o")
    else:
        print("OPENAI_API_KEY not set — general knowledge will use local model")
except Exception as exc:
    print(f"OpenAI init failed: {exc}")
    llm_general = None

def _load_docx_text(path):
    try:
        from docx import Document as _DocxDocument
        d = _DocxDocument(path)
        return "\n".join(p.text for p in d.paragraphs if p.text.strip())
    except Exception as exc:
        print(f"  [skip docx] {os.path.basename(path)}: {exc}")
        return ""

def classify_science_image(subject: str, ctx: str = "") -> dict:
    if not (LANGCHAIN_AVAILABLE and (llm_precise or llm)):
        return {"engine": "diffusion", "caption": subject}
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        human = f"Request: {subject}"
        if ctx:
            human += f"\n\nCurriculum context (ground facts in this):\n{_clean_pdf_artifacts(ctx)[:1800]}"
        model = llm_precise or llm
        resp = model.invoke([
            SystemMessage(content=_SCIENCE_IMAGE_SYSTEM),
            HumanMessage(content=human),
        ])
        raw = (resp.content if hasattr(resp, "content") else str(resp)).strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
        spec = json.loads(raw)
        print(f"[image] engine={spec.get('engine')}")
        return spec
    except Exception as exc:
        print(f"[image] classify failed: {exc}")
        return {"engine": "diffusion", "caption": subject}

_EMPTY_TOPIC_SIGNALS = {"", "questions", "question", "quiz", "them", "it", "these",
                        "some", "more", "another", "the topic", "this", "that"}


_NEW_REQUEST_RE = re.compile(
    r"\b(create|make|generate|write|prepare|draft|give me|show me|produce|"
    r"explain|describe|define|summari[sz]e|translate|solve|calculate|"
    r"lesson plan|quiz|questions?|mcqs?|worksheet|image|diagram)\b",
    re.IGNORECASE)


def extract_lesson_spec_llm(message: str, recent_conversation: str = "") -> dict:
    """Topic, grade, subject, and duration for a lesson plan. Missing values are ''."""
    empty = {"topic": "", "grade": "", "subject": "", "duration": ""}
    if not (LANGCHAIN_AVAILABLE and (llm_precise or llm)):
        return empty
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        import json as _json
        ctx = f"\n\nRecent conversation:\n{recent_conversation}" if recent_conversation else ""
        model = llm_precise or llm
        resp = model.invoke([
            SystemMessage(content=(
                "Extract lesson plan details. Output ONLY JSON:\n"
                '{"topic": "...", "grade": "...", "subject": "...", "duration": "..."}\n\n'
                "- topic: the SUBJECT MATTER to teach. A grade, a subject name, or a "
                "duration is NOT a topic. 'a Grade 11 Biology lesson' has no topic; "
                "'a lesson on photosynthesis' has topic 'photosynthesis'.\n"
                "- grade: e.g. 'Grade 11'. Empty if not stated.\n"
                "- subject: e.g. 'Biology'. Empty if not stated.\n"
                "- duration: e.g. '40 minutes'. Empty if not stated.\n"
                "Use the recent conversation only to resolve references like 'that' or "
                "'the same topic'. Output only the JSON object.")),
            HumanMessage(content=f"Request: {message}{ctx}"),
        ])
        raw = (resp.content if hasattr(resp, "content") else str(resp)).strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
        data = _json.loads(raw)
        return {k: str(data.get(k, "")).strip() for k in empty}
    except Exception as exc:
        print(f"[lesson] spec extract failed: {exc}")
        return empty


_LESSON_SLOTS = [
    ("topic",    "Which topic should the lesson cover?"),
    ("grade",    "Which grade is it for?"),
    ("subject",  "Which subject is it for?"),
    ("duration", "How long is the lesson? (for example 40 minutes)"),
]


def next_missing_lesson_slot(spec: dict):
    """First unfilled slot and the question to ask for it."""
    for key, question in _LESSON_SLOTS:
        if not (spec.get(key) or "").strip():
            return key, question
    return None, ""

def _extract_uploaded_text(path: str, filename: str) -> str:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    try:
        if ext == "pdf":
            return "\n".join(p.page_content for p in PyPDFLoader(path).load())
        if ext == "docx":
            from docx import Document as _Docx
            return "\n".join(p.text for p in _Docx(path).paragraphs if p.text.strip())
        if ext == "pptx":
            from pptx import Presentation
            out = []
            for slide in Presentation(path).slides:
                for shape in slide.shapes:
                    if shape.has_text_frame and shape.text_frame.text.strip():
                        out.append(shape.text_frame.text)
            return "\n".join(out)
    except Exception as exc:
        print(f"[ingest] extract failed for {filename}: {exc}")
    return ""






def build_lesson_request(spec: dict) -> str:
    """Turn a filled spec into a clean standalone request string."""
    topic = (spec.get("topic") or "").strip()
    if not topic:
        # No topic: fall back to what the user actually said. Never emit
        # "Create a lesson plan on " with nothing after it.
        return (spec.get("request") or "Create a lesson plan").strip()

    parts = [f"Create a lesson plan on {topic}"]
    if spec.get("grade"):
        parts.append(f"for {spec['grade']}")
    if spec.get("subject"):
        parts.append(f"in {spec['subject']}")
    if spec.get("duration"):
        parts.append(f"lasting {spec['duration']}")
    return " ".join(parts)


def topic_from_material(source_text: str) -> str:
    """The subject of a quoted passage, used when the request itself names none."""
    if not (LANGCHAIN_AVAILABLE and (llm_precise or llm)) or not source_text.strip():
        return ""
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        model = llm_precise or llm
        resp = model.invoke([
            SystemMessage(content=(
                "Name the single academic topic this passage teaches, in five words or "
                "fewer. Output only the topic, no punctuation, no explanation.")),
            HumanMessage(content=source_text[:2000]),
        ])
        topic = (resp.content if hasattr(resp, "content") else str(resp)).strip().strip('".')
        return topic if 0 < len(topic) <= 60 else ""
    except Exception as exc:
        print(f"[lesson] topic-from-material failed: {exc}")
        return ""


def looks_like_new_request(message: str) -> bool:
    """A clarification reply is a short value: 'photosynthesis', 'Grade 12',
    '40 minutes'. Anything that reads like a fresh instruction is not."""
    text = (message or "").strip()
    if not text:
        return False
    if len(text.split()) > 8:
        return True
    return bool(_NEW_REQUEST_RE.search(text))

def looks_like_question_set(text: str, count: int) -> bool:
    body = text or ""
    numbered = len(re.findall(r"^\s*\d+[\.\)]\s+\S", body, re.MULTILINE))
    has_key = "answer key" in body.lower()
    structure_words = ("lesson structure", "learning objectives", "guided practice",
                       "teacher action", "differentiation", "closure")
    if sum(w in body.lower() for w in structure_words) >= 2:
        return False
    return numbered >= max(1, min(count, 3)) and has_key

_QTYPE_INSTRUCTIONS = {
    "mcq": "Write multiple-choice questions, each with a stem and four options A-D, exactly one correct.",
    "short_answer": "Write short-answer questions, each answerable in 1-3 sentences.",
    "fill_blank": "Write fill-in-the-blank questions with a _______ where the key term goes.",
    "true_false": "Write true/false statements, a balanced mix of true and false.",
    "essay": "Write essay questions asking students to explain, compare, or evaluate; give a brief marking guide.",
}

_QUESTION_SYSTEM = (
    "You are a PNG curriculum specialist writing assessment questions for a teacher.\n\n"
    "RULES:\n"
    "- Base questions ONLY on the curriculum material provided. Do not test facts not in it.\n"
    "- Produce EXACTLY {count} questions, numbered 1..{count}.\n"
    "- {type_instruction}\n"
    "- After the questions add a '## Answer Key' section with the answer to each.\n"
    "- Clean Markdown. No preamble, no closing remarks. Begin with a '# ' title.\n\n"
    "Audience guidance: {audience}"
)

SESSION_PENDING_LESSON: Dict[str, Dict[str, str]] = {}


# The curriculum reformat is forbidden from mentioning its source, so ANY
# reference to the material means the model ignored the prompt and the answer
# must be discarded. Note the deliberate absence of a bare "the material":
# chemistry and physics answers legitimately say "the material conducts heat".
_SOURCE_REFERENCE_RE = re.compile(
    r"curriculum material|provided material|given material|supplied material|"
    r"source material|the provided (?:text|content|excerpt|document)|"
    r"the excerpts?\b|the documents provided|"
    r"no coverage|does not cover|doesn'?t cover|not covered|"
    r"not (?:directly |explicitly |specifically )?(?:mentioned|found|included|discussed|addressed)\b|"
    r"only mentions|only covers|"
    r"no information (?:is )?(?:provided|available|found)|"
    r"can be inferred from related concepts|no_coverage",
    re.IGNORECASE)

# Kept as the alias the rest of the file already imports.
_CURRICULUM_META_RE = _SOURCE_REFERENCE_RE


def curriculum_answer_is_evasive(text: str) -> bool:
    body = (text or "").strip()
    if len(body) < 150:          # a real curriculum answer is never this short
        return True
    return bool(_SOURCE_REFERENCE_RE.search(body))


def strip_curriculum_meta(text: str) -> str:
    blocks = re.split(r"\n\s*\n", text or "")
    kept = [b for b in blocks if not _SOURCE_REFERENCE_RE.search(b)]
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(kept)).strip()

def build_message_history(session_id: str, max_turns: int = 10,
                          max_chars: int = 1500) -> list:
    """Prior turns as real Human/AI messages. This is what lets the answering
    model resolve 'that', 'the second one', and 'in simpler language' itself."""
    if not LANGCHAIN_AVAILABLE:
        return []
    from langchain_core.messages import HumanMessage, AIMessage

    msgs = []
    # [:-1] drops the message currently being handled; it is sent separately.
    for turn in SESSION_CHAT_HISTORY.get(session_id, [])[:-1][-max_turns:]:
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        content = content[:max_chars]
        if turn.get("role") == "user":
            msgs.append(HumanMessage(content=content))
        else:
            msgs.append(AIMessage(content=content))
    return msgs



def answer_with_history(system_text: str, user_message: str, session_id: str,
                        reply_ctx: str = "", extra_material: str = "",
                        model=None, history_turns: int = None) -> str:
    """Answer with the conversation as real prior turns.

    When reply_ctx is present the quote goes INSIDE the current message and the
    history window shrinks, so an older topic cannot outweigh what the user
    explicitly pointed at.
    """
    if not LANGCHAIN_AVAILABLE:
        return ""
    from langchain_core.messages import SystemMessage, HumanMessage

    model = model or llm_general or llm
    if model is None:
        return ""

    if history_turns is None:
        history_turns = 2 if reply_ctx else 10

    messages = [SystemMessage(content=system_text)]
    if history_turns > 0:
        messages.extend(build_message_history(session_id, max_turns=history_turns))

    if reply_ctx:
        human = (
            "The user is replying to this exact passage, quoted from your earlier "
            "answer. It is the ONLY subject of their message. Ignore every other "
            "topic discussed earlier in the conversation.\n\n"
            "--- QUOTED PASSAGE ---\n"
            f"{reply_ctx[:6000]}\n"
            "--- END QUOTED PASSAGE ---\n\n"
            f"The user's message about that passage: {user_message}"
        )
    else:
        human = user_message

    if extra_material:
        human = f"{human}\n\nRelevant reference material:\n{extra_material[:6000]}"

    messages.append(HumanMessage(content=human))

    try:
        resp = model.invoke(messages)
        text = (resp.content if hasattr(resp, "content") else str(resp)).strip()
        return "" if looks_like_model_refusal(text) else text
    except Exception as exc:
        print(f"[history-answer] failed: {exc}")
        return ""

def generate_questions_grounded(message, access_role, spec, curriculum):
    if not (LANGCHAIN_AVAILABLE and (llm_precise or llm) and curriculum):
        return ""
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        system = _QUESTION_SYSTEM.format(
            count=spec["count"],
            type_instruction=_QTYPE_INSTRUCTIONS.get(spec["type"], _QTYPE_INSTRUCTIONS["mcq"]),
            audience=build_role_instruction(access_role))
        material = _clean_pdf_artifacts(curriculum["context"])
        model = llm_precise or llm
        resp = model.invoke([
            SystemMessage(content=system),
            HumanMessage(content=f"Request: {message}\n\nCurriculum material (the ONLY source):\n{material}"),
        ])
        return (resp.content if hasattr(resp, "content") else str(resp)).strip()
    except Exception as exc:
        print(f"[questions] grounded failed: {exc}")
        return ""





def get_recent_turns_text(session_id: str, n: int = 6) -> str:
    """Last n turns (user + assistant) as text, for LLM topic resolution."""
    history = SESSION_CHAT_HISTORY.get(session_id, [])
    turns = history[-(n + 1):-1] if len(history) > 1 else []
    lines = []
    for t in turns:
        role = "User" if t.get("role") == "user" else "Nexa"
        content = (t.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content[:400]}")
    return "\n".join(lines)

_STRUCTURE_WORDS = (
    "key concept", "overview", "learning objective", "objectives", "materials",
    "lesson structure", "lesson flow", "assessment", "teacher note",
    "differentiation", "answer key", "introduction", "closure", "conclusion",
    "definition and process", "summary", "homework", "extension", "vocabulary",
)

_REFERENCE_MARKERS = re.compile(
    r"\b(first|second|third|fourth|fifth|last|next|previous|former|latter|"
    r"one|ones|that|this|those|these|it|them|same|above)\b", re.IGNORECASE)

_MODIFIERS = ("simpler", "simple", "brief", "briefly", "short", "shorter",
              "detail", "detailed", "table", "bullet", "example", "examples",
              "step by step", "grade", "easier", "summary")


def _clean_for_resolution(text: str) -> str:
    """Strip Markdown noise but KEEP list markers: numbering is what ordinal
    references ('the second one') point at."""
    t = text or ""
    t = re.sub(r"^\s*#{1,6}\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"[*_`>|]", " ", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def get_last_user_message(session_id: str) -> str:
    """The user's previous turn, excluding the message being handled now."""
    history = SESSION_CHAT_HISTORY.get(session_id, [])
    for turn in reversed(history[:-1]):
        if turn.get("role") == "user":
            content = (turn.get("content") or "").strip()
            if content:
                return content[:300]
    return ""


def get_thread_topic(session_id: str) -> str:
    """The subject the conversation opened on."""
    history = SESSION_CHAT_HISTORY.get(session_id, [])
    for turn in history[:-1]:
        if turn.get("role") == "user":
            content = (turn.get("content") or "").strip()
            if content and not is_conversation_meta_question(content):
                return content[:200]
    return ""


def get_last_exchange_text(session_id: str, message: str = "") -> str:
    """Antecedent context for pronoun and ordinal resolution."""
    history = SESSION_CHAT_HISTORY.get(session_id, [])
    prior = history[:-1]
    last_user = get_last_user_message(session_id)

    # An ordinal reference needs the whole list, not just its opening.
    limit = 1500 if _REFERENCE_MARKERS.search(message or "") else 400

    last_assistant = ""
    for turn in reversed(prior):
        if turn.get("role") == "assistant":
            last_assistant = _clean_for_resolution(turn.get("content") or "")[:limit]
            break

    if not last_user and not last_assistant:
        return ""

    parts = []
    topic = get_thread_topic(session_id)
    if topic and topic != last_user:
        parts.append(f"This conversation started with: {topic}")
    if last_user:
        parts.append(f"The user's previous question was: {last_user}")
    if last_assistant:
        parts.append(f"Your previous answer was:\n{last_assistant}")
    return "\n\n".join(parts)


def _fallback_resolution(text: str, session_id: str) -> str:
    """When the rewrite is unusable, append the previous question verbatim so the
    topic words still reach classification and retrieval."""
    last_user = get_last_user_message(session_id)
    if not last_user:
        return text
    return f"{text} (in the context of: {last_user})"

_FOLLOWUP_REFERENCE_RE = re.compile(
    r"\b(it|its|this|that|these|those|them|they|the same|the second|the first|"
    r"the last|the next|the previous|the other|the one|above|below|earlier|"
    r"more|again|also|as well|further)\b", re.IGNORECASE)


def _needs_followup_resolution(message: str) -> bool:
    """Only rewrite when the message actually depends on prior context.
    A short reference ('explain that', 'the second one', 'tell me more') needs
    resolution; a self-contained question with its own subject does not."""
    m = (message or "").strip()
    if not m:
        return False
    words = m.split()
    has_reference = bool(_FOLLOWUP_REFERENCE_RE.search(m))
    starts_standalone = bool(re.match(
        r"^(what|which|who|when|where|why|how)\b.*\b(is|are|was|were|do|does|"
        r"did|can|could|explain|mean)\b", m, re.IGNORECASE))
    # A wh-question with its own subject and no pronoun reference stands alone.
    if starts_standalone and not has_reference:
        return False
    # Short pure-reference messages ("explain that", "the second one") -> rewrite.
    if has_reference and len(words) <= 12:
        return True
    return has_reference


def resolve_followup_message(message: str, session_id: str) -> str:
    """Rewrite a follow-up into a standalone SEARCH string. Used only for
    retrieval and classification; generation always gets the raw message."""
    text = (message or "").strip()
    if not text or len(text.split()) > 30:
        return message
    if not (LANGCHAIN_AVAILABLE and (llm_precise or llm)):
        return message

    recent = get_last_exchange_text(session_id, text)
    if not recent:
        return message

    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        model = llm_precise or llm
        resp = model.invoke([
            SystemMessage(content=(
                "Rewrite the user's latest message as a STANDALONE REQUEST that makes full "
                "sense with no conversation history. Your output must be a question or an "
                "instruction, never an answer or a statement of fact.\n\n"
                "ANTECEDENT RULE: 'this', 'that', 'it', 'the same' refer to the SUBJECT "
                "MATTER of the previous exchange. Ordinals ('the second one', 'the last "
                "one') refer to the correspondingly numbered item in your previous answer, "
                "shown below. Name that item explicitly.\n"
                "NEVER resolve a reference to a section heading such as 'key concepts', "
                "'overview', 'learning objectives', 'materials', or 'summary'.\n\n"
                "PRESERVE EVERY INSTRUCTION the user gave, word for word: 'in simpler "
                "language', 'briefly', 'in detail', 'for Grade 8', 'as a table'.\n"
                "KEEP THE THREAD: if the item only makes sense within the topic under "
                "discussion, say so, e.g. 'Explain <item> in simpler language, in the "
                "context of <topic>'.\n\n"
                "Keep any maths expression EXACTLY as it appears. If the message is ABOUT "
                "the conversation itself, or already stands alone, return it exactly as "
                "written. Output only the rewritten request, nothing else.")),
            HumanMessage(content=f"{recent}\n\nLatest message: {text}"),
        ])
        out = (resp.content if hasattr(resp, "content") else str(resp)).strip().strip('"')

        if not out or len(out) > 400:
            return _fallback_resolution(text, session_id)
        if len(out) > max(200, len(text) * 4):
            return _fallback_resolution(text, session_id)
        if re.match(r"^(i'd|i would|i'll|i can|i am|sure|certainly|of course|"
                    r"here(?:'s| is)|yes|no)\b", out, re.IGNORECASE):
            return _fallback_resolution(text, session_id)

        low_out, low_in = out.lower(), text.lower()

        lost = [m for m in _MODIFIERS if m in low_in and m not in low_out]
        if lost:
            print(f"[followup] rewrite dropped {lost} -> using fallback")
            return _fallback_resolution(text, session_id)

        if any(w in low_out and w not in low_in for w in _STRUCTURE_WORDS):
            print(f"[followup] rejected structural rewrite: {out[:60]!r}")
            return _fallback_resolution(text, session_id)

        request_start = re.match(
            r"^(what|which|who|when|where|why|how|explain|describe|define|give|"
            r"list|show|tell|create|make|write|generate|compare|summari[sz]e|"
            r"can you|could you|please|expand|shorten|simplify|solve)\b",
            out, re.IGNORECASE)
        if not request_start and not out.rstrip().endswith("?"):
            print(f"[followup] rewrite is not a request: {out[:60]!r}")
            return _fallback_resolution(text, session_id)

        return out
    except Exception as exc:
        print(f"[followup] resolve failed: {exc}")
        return _fallback_resolution(text, session_id)


def _fallback_resolution(text: str, session_id: str) -> str:
    """When the rewrite is unusable, append the previous question verbatim so the
    topic words still reach classification and retrieval."""
    last_user = get_last_user_message(session_id)
    if not last_user:
        return text
    return f"{text} (in the context of: {last_user})"

def generate_questions_direct(message, access_role, spec):
    if not (LANGCHAIN_AVAILABLE and llm is not None):
        return ""
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        system = _QUESTION_SYSTEM.format(
            count=spec["count"],
            type_instruction=_QTYPE_INSTRUCTIONS.get(spec["type"], _QTYPE_INSTRUCTIONS["mcq"]),
            audience=build_role_instruction(access_role))
        resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=message)])
        return (resp.content if hasattr(resp, "content") else str(resp)).strip()
    except Exception as exc:
        print(f"[questions] direct failed: {exc}")
        return ""


def extract_lesson_topic_llm(message: str, recent_conversation: str = "") -> str:
    """The topic a lesson plan should cover. Empty string when the request names none."""
    if not (LANGCHAIN_AVAILABLE and (llm_precise or llm)):
        return ""
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        ctx = f"\n\nRecent conversation:\n{recent_conversation}" if recent_conversation else ""
        model = llm_precise or llm
        resp = model.invoke([
            SystemMessage(content=(
                "Extract the SUBJECT MATTER the lesson plan should teach. Output only the "
                "topic, nothing else. A grade, a subject name, a duration, or a term is NOT "
                "a topic: 'a Grade 11 Biology lesson' has no topic, 'a lesson on photosynthesis' "
                "has the topic 'photosynthesis'. If the request continues an earlier "
                "discussion ('turn that into a lesson plan'), use the topic from the recent "
                "conversation. If there is genuinely no topic anywhere, output exactly: NONE")),
            HumanMessage(content=f"Request: {message}{ctx}"),
        ])
        topic = (resp.content if hasattr(resp, "content") else str(resp)).strip().strip('"')
        if not topic or topic.upper().startswith("NONE") or len(topic) > 120:
            return ""
        return topic
    except Exception as exc:
        print(f"[lesson] topic extract failed: {exc}")
        return ""


def extract_question_spec_llm(message: str, reply_context: str = "",
                              history_topic: str = "", recent_conversation: str = "") -> dict:
    """LLM extracts question type, count, and topic. Resolves 'this'/'that'/'as well'
    from the quoted reply or recent conversation. No regex."""
    fallback = {"type": "mcq", "count": 10, "topic": ""}
    if not (LANGCHAIN_AVAILABLE and (llm_precise or llm)):
        return fallback
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        import json as _json
        ctx = ""
        if reply_context:
            ctx += ("\n\nThe user is replying to this earlier text (use it as the topic if the "
                    f"message says 'this'/'that'/'it'):\n{reply_context[:1500]}")
        if recent_conversation:
            ctx += ("\n\nRecent conversation (use this to resolve the topic when the message says "
                    "'as well', 'more', 'also', 'same', 'this', or continues a previous request "
                    f"without naming a new topic):\n{recent_conversation}")
        if history_topic:
            ctx += f"\n\nMost recent topic discussed: {history_topic}"
        system = (
            "Extract assessment-question details from the user's request. Output ONLY JSON:\n"
            '{"type": "...", "count": N, "topic": "..."}\n\n'
            "- type: one of mcq, short_answer, fill_blank, true_false, essay. Default mcq.\n"
            "- count: the number requested (default 10).\n"
            "- topic: the subject of the questions. If the request says 'as well', 'more', "
            "'also', 'this', 'that', or otherwise continues without naming a new topic, use the "
            "topic from the recent conversation or the quoted reply. Only use empty string if "
            "there is genuinely no topic anywhere in the request or context.\n"
            "Output only the JSON object."
        )
        model = llm_precise or llm
        resp = model.invoke([
            SystemMessage(content=system),
            HumanMessage(content=f"Request: {message}{ctx}"),
        ])
        raw = (resp.content if hasattr(resp, "content") else str(resp)).strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
        data = _json.loads(raw)
        t = str(data.get("type", "mcq")).lower().strip()
        if t not in ("mcq", "short_answer", "fill_blank", "true_false", "essay"):
            t = "mcq"
        c = data.get("count", 10)
        try:
            c = int(c)
        except Exception:
            c = 10
        c = max(1, min(50, c))
        return {"type": t, "count": c, "topic": str(data.get("topic", "")).strip()}
    except Exception as exc:
        print(f"[questions] spec extract failed: {exc}")
        return fallback


def _looks_topicless(topic: str) -> bool:
    t = (topic or "").strip().lower()
    return t in _EMPTY_TOPIC_SIGNALS or len(t) < 3


def infer_topic_from_history(session_id: str) -> str:
    """Most recent substantive topic the user raised this session."""
    history = SESSION_CHAT_HISTORY.get(session_id, [])
    for turn in reversed(history[:-1]):
        if turn.get("role") != "user":
            continue
        text = (turn.get("content") or "").strip()
        if _is_question_request(text) or _is_lesson_request(text.lower()):
            continue
        topic = re.sub(
            r"\b(can you|could you|please|explain|what is|what are|tell me about|"
            r"describe|define|how does|how do|the)\b", " ", text.lower())
        topic = re.sub(r"[?.!]", " ", topic)
        topic = re.sub(r"\s{2,}", " ", topic).strip()
        if topic and len(topic) >= 3:
            return topic
    return ""


def _render_molecule(smiles: str, caption: str, out_path: str) -> bool:
    try:
        from rdkit import Chem
        from rdkit.Chem.Draw import rdMolDraw2D
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            print(f"[image] invalid SMILES {smiles!r}")
            return False
        d = rdMolDraw2D.MolDraw2DCairo(800, 600)
        rdMolDraw2D.PrepareAndDrawMolecule(d, mol, legend=caption or "")
        d.FinishDrawing()
        with open(out_path, "wb") as f:
            f.write(d.GetDrawingText())
        return True
    except Exception as exc:
        print(f"[image] molecule render failed: {exc}")
        return False

def classify_intent(message: str, has_document: bool = False, has_reply_context: bool = False) -> str:
    """Model decides the message type. Returns one of:
    smalltalk, curriculum, general, generate_questions, generate_lesson,
    generate_image, document_or_reply, other."""
    if not (LANGCHAIN_AVAILABLE and (llm_precise or llm)):
        return "curriculum"
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        context_note = ""
        if has_document:
            context_note += (
                "\nNOTE: the user has uploaded a document in this session. If the message "
                "asks ABOUT that document ('what is this document about', 'summarize the "
                "file', 'what does it say about X', 'explain this'), classify as "
                "document_or_reply. Any mention of 'this document', 'the file', 'the PDF', "
                "'the upload', or 'the notes' points at that document.")
        if has_reply_context:
            context_note += (
                "\nNOTE: the user is replying to a previous Nexa message (quoted). "
                "If they are asking ABOUT that quoted text ('explain this', 'elaborate on "
                "that', 'what does this mean'), classify as document_or_reply.")
        if has_document or has_reply_context:
            context_note += (
                "\nIMPORTANT: creating something NEW always wins over document_or_reply. "
                "If the message asks you to CREATE a lesson plan, quiz or assessment "
                "questions, or an image FROM the quoted text or the document (for example "
                "'make a lesson plan for this', 'generate 10 MCQs on this', 'draw a diagram "
                "of this'), classify it as generate_lesson, generate_questions, or "
                "generate_image, NOT document_or_reply. Mentions of output format such as "
                "'in PDF format' are irrelevant to the category, ignore them when deciding.")
        system = (
            "Classify the user's message into exactly ONE category. Reply with ONLY the "
            "category word, nothing else.\n\n"
            "- document_or_reply: the message asks about an uploaded document or a quoted "
            "previous reply ('explain this', 'what does this document contain', 'elaborate "
            "on that', 'summarize the file').\n"
            "- generate_questions: asking to CREATE quiz/test/assessment questions of any "
            "kind (multiple choice, short answer, fill in the blank, true/false, essay), "
            "however phrased.\n"
            "- generate_lesson: asking to CREATE a lesson plan or teaching plan.\n"
            "- generate_image: asking to CREATE/draw/generate/show an image, picture, or "
            "diagram.\n"
            "- curriculum: a question about an academic science topic that a school "
            "biology, chemistry, or physics curriculum would cover.\n"
            "- general: a factual or general-knowledge question NOT specific to the science "
            "curriculum (history, geography, computing, current events, people, everyday "
            "facts).\n"
            "- smalltalk: greetings, thanks, 'how are you', chit-chat, or personal questions "
            "to the assistant, ONLY when the message is not asking for information or "
            "content.\n"
            "- other: none of the above.\n\n"
            "CRITICAL: only choose a generate_* category when the user is ASKING you to "
            "produce something now. A statement about the user's own situation, plans, or "
            "work is NOT a request. 'I am preparing a Grade 11 Biology lesson this week', "
            "'I teach Grade 10 Chemistry', 'we have exams next month' are smalltalk, not "
            "generate_lesson. Look for an actual instruction or question (create, make, "
            "write, give me, can you, how do I, what is). If there is no instruction and no "
            "question, the category is smalltalk or other.\n\n"
            "Decide by the user's actual intent, not by keywords. Reply with one word."
            + context_note
        )
        model = llm_precise or llm
        resp = model.invoke([SystemMessage(content=system), HumanMessage(content=message)])
        raw = (resp.content if hasattr(resp, "content") else str(resp)).strip().lower()
        for cat in ("document_or_reply", "generate_questions", "generate_lesson",
                    "generate_image", "curriculum", "general", "smalltalk", "other"):
            if cat in raw:
                return cat
        return "curriculum"
    except Exception as exc:
        print(f"[intent] classify failed: {exc}")
        return "curriculum"

_LESSON_FROM_TEXT_SYSTEM = (
    "You are a PNG curriculum specialist writing a classroom-ready lesson plan.\n\n"
    "RULES:\n"
    "- Build the lesson around the material supplied below. It is the topic the "
    "teacher is pointing at. You may add standard pedagogy (activities, timings, "
    "assessment) but do not change the subject matter.\n"
    "- If the material is only a fragment, infer the full topic it belongs to and "
    "teach that topic properly.\n"
    "- Never ask clarifying questions. Never address the user by name. No preamble "
    "or closing remark. Begin with the # title and nothing else.\n\n"
    "Structure (clean Markdown):\n"
    "# [Lesson Title]\n## Overview\n## Learning Objectives\n## Key Concepts and Vocabulary\n"
    "## Materials\n## Lesson Structure\n### Introduction / Engagement\n"
    "### Direct Instruction / Explanation\n### Guided Practice\n### Independent Practice\n"
    "### Closure / Consolidation\n## Differentiation\n## Assessment\n"
    "## Extension and Homework\n## Teacher Notes\n\n"
    "Give a time estimate, teacher actions, and student actions for each "
    "lesson-structure subsection.\n\nAudience guidance: {audience}"
)


def generate_lesson_plan_from_text(message: str, access_role: str, source_text: str) -> str:
    """Lesson plan built from a quoted reply or uploaded document."""
    if not (LANGCHAIN_AVAILABLE and (llm_precise is not None or llm is not None)):
        return ""
    if not (source_text or "").strip():
        return ""
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        model = llm_precise or llm
        resp = model.invoke([
            SystemMessage(content=_LESSON_FROM_TEXT_SYSTEM.replace(
                "{audience}", build_role_instruction(access_role))),
            HumanMessage(content=(
                f"Teacher's request: {message}\n\n"
                f"Material the teacher is pointing at:\n{source_text[:6000]}")),
        ])
        return (resp.content if hasattr(resp, "content") else str(resp)).strip()
    except Exception as exc:
        print(f"[lesson] from-text failed: {exc}")
        return ""

_CURRICULUM_META_RE = re.compile(
    r"not\s+(?:directly\s+|explicitly\s+|specifically\s+)?"
    r"(?:mentioned|covered|found|included|stated|discussed|addressed)"
    r"|(?:the\s+)?(?:provided\s+|given\s+|available\s+|supplied\s+)*"
    r"(?:curriculum\s+)?(?:material|materials|excerpts?|content|text|documents?|sources?)"
    r"\s+(?:does|do|did)\s+not"
    r"|(?:material|materials|excerpts?|curriculum|information)\s+only\s+(?:mentions|covers|includes)"
    r"|can\s+be\s+inferred\s+from\s+related\s+concepts"
    r"|no\s+information\s+(?:is\s+)?(?:provided|available|found)"
    r"|based\s+on\s+the\s+(?:provided\s+)?(?:curriculum\s+)?(?:material|excerpts)"
    r"|no_coverage",
    re.IGNORECASE)


def curriculum_answer_is_evasive(text: str) -> bool:
    body = (text or "").strip()
    return (not body) or bool(_CURRICULUM_META_RE.search(body))


def strip_curriculum_meta(text: str) -> str:
    blocks = re.split(r"\n\s*\n", text or "")
    kept = [b for b in blocks if not _CURRICULUM_META_RE.search(b)]
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(kept)).strip()

def build_conversational_reply(message: str, name: str = "") -> str:
    if not (LANGCHAIN_AVAILABLE and llm is not None):
        return "I'm doing well, thanks! What would you like to learn about today?"
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        who = f" The student's name is {name}." if name else ""
        system = (
            "You are Nexa, a warm, friendly educational assistant for PNG students." + who +
            " Reply naturally and briefly to casual conversation, then gently invite them to "
            "ask about their studies. 1-2 sentences. No markdown headings."
        )
        resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=message)])
        return (resp.content if hasattr(resp, "content") else str(resp)).strip()
    except Exception:
        return "I'm doing well, thanks for asking! What would you like to learn about today?"

def _render_reaction(reaction_smiles: str, out_path: str) -> bool:
    try:
        from rdkit.Chem import AllChem
        from rdkit.Chem.Draw import rdMolDraw2D
        rxn = AllChem.ReactionFromSmarts(reaction_smiles, useSmiles=True)
        d = rdMolDraw2D.MolDraw2DCairo(1000, 400)
        d.DrawReaction(rxn)
        d.FinishDrawing()
        with open(out_path, "wb") as f:
            f.write(d.GetDrawingText())
        return True
    except Exception as exc:
        print(f"[image] reaction render failed: {exc}")
        return False

def _clean_pdf_artifacts(text: str) -> str:
    t = text or ""
    t = re.sub(r"^\s*\d+\s*\|\s*P\s*a\s*g\s*e\s*$", "", t, flags=re.MULTILINE)
    t = re.sub(r"\[SOURCE:[^\]]*\]", "", t)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

_CURRICULUM_FORMAT_SYSTEM = (
    "You are Nexa, reformatting official curriculum material into a clear answer for a student.\n\n"
    "RULES:\n"
    "- Use ONLY the information in the material below. Do NOT add facts or outside knowledge.\n"
    "- Do NOT omit substantive information; cover every key point.\n"
    "- NEVER comment on what the material does or does not contain. Do not write phrases "
    "like 'there is no information provided in the curriculum material', 'the material only "
    "mentions', 'the excerpts do not cover', or 'not stated in the source'. The student must "
    "never see any reference to the material, the excerpts, or their limits.\n"
    "- If the material does not actually answer the question, reply with exactly: NO_COVERAGE\n"
    "- Remove PDF artifacts (page markers like '2 | P a g e', footnote numbers, broken spacing, URLs).\n"
    "- Clean Markdown: a short opening sentence, then headings, bold terms, and bullet lists.\n"
    "- Do NOT greet or name the student. No preamble or closing question.\n"
    "- Do NOT mention the source, page numbers, grade, or term. Begin directly with the answer.\n\n"
    "Audience guidance: {audience}"
)

def answer_over_context(message: str, context_text: str, access_role: str, kind: str = "document") -> str:
    """Answer a question using the provided context (uploaded document or a quoted
    previous reply), not the curriculum. Used for 'explain this', 'what does this
    document contain', 'elaborate on that', etc."""
    if not (LANGCHAIN_AVAILABLE and llm is not None) or not context_text.strip():
        return ""
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        label = "an uploaded document" if kind == "document" else "your previous reply"
        system = (
            f"You are Nexa, an educational assistant. The user is asking about {label}, "
            "provided below. Answer their request using ONLY that content — explain, summarize, "
            "or elaborate as asked. Clean Markdown. Do not invent facts beyond the content. "
            f"Audience: {build_role_instruction(access_role)}"
        )
        resp = llm.invoke([
            SystemMessage(content=system),
            HumanMessage(content=f"Content:\n{context_text[:6000]}\n\nUser request: {message}"),
        ])
        return (resp.content if hasattr(resp, "content") else str(resp)).strip()
    except Exception as exc:
        print(f"[context-answer] failed: {exc}")
        return ""

_CURRICULUM_META_SIGNALS = (
    "no information provided in the curriculum",
    "not provided in the curriculum",
    "the curriculum material does not",
    "the curriculum does not",
    "the material only mentions",
    "the material does not",
    "the provided material",
    "the provided excerpts",
    "the excerpts do not",
    "in the given material",
    "not mentioned in the",
    "is not covered in the",
    "no_coverage",
)


def curriculum_answer_is_evasive(text: str) -> bool:
    """True when the reformat is talking about the PDFs instead of answering."""
    body = (text or "").strip().lower()
    if not body:
        return True
    return any(signal in body for signal in _CURRICULUM_META_SIGNALS)


def strip_curriculum_meta(text: str) -> str:
    """Drop any paragraph that comments on the curriculum material itself."""
    blocks = re.split(r"\n\s*\n", text or "")
    kept = [b for b in blocks
            if not any(signal in b.lower() for signal in _CURRICULUM_META_SIGNALS)]
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(kept)).strip()


_CONVERSATION_META_PHRASES = (
    "first question", "last question", "previous question", "what did i ask",
    "what i asked", "what we discussed", "we have discussed", "we've discussed",
    "our conversation", "this conversation", "this chat", "chat history",
    "earlier in this", "so far", "recap", "what have we", "what did we",
)


def is_conversation_meta_question(message: str) -> bool:
    """True when the user is asking ABOUT the conversation, not about a topic."""
    m = (message or "").lower()
    return any(p in m for p in _CONVERSATION_META_PHRASES)


def answer_about_conversation(message: str, session_id: str, access_role: str) -> str:
    """Answer using the session transcript itself: 'what was my first question',
    'summarize what we discussed'."""
    transcript = get_recent_turns_text(session_id, n=20)
    if not transcript or not (LANGCHAIN_AVAILABLE and llm is not None):
        return ""
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        resp = llm.invoke([
            SystemMessage(content=(
                "You are Nexa. The user is asking about THIS conversation, not about a "
                "school topic. Answer using ONLY the transcript below: name what they "
                "asked, in what order, or summarise what was covered. Be brief and "
                "concrete. Do NOT re-teach the topic and do NOT produce a lesson or "
                "study notes. Do not invent turns that are not in the transcript. "
                f"Audience guidance: {build_role_instruction(access_role)}")),
            HumanMessage(content=f"Transcript:\n{transcript}\n\nUser's question: {message}"),
        ])
        return (resp.content if hasattr(resp, "content") else str(resp)).strip()
    except Exception as exc:
        print(f"[conversation] meta answer failed: {exc}")
        return ""

def answer_from_curriculum(message: str, access_role: str) -> dict:
    """Answer strictly from indexed curriculum material.

    Returns None whenever the curriculum cannot genuinely answer the question, so
    the caller falls through to general knowledge. Never returns text that
    comments on what the material does or does not contain, and never dumps raw
    PDF text into the chat.
    """
    query = strip_output_format_noise(message) or message
    scope = parse_lesson_request_scope(message)

    curriculum = retrieve_curriculum_context(query, scope)
    if not curriculum:
        print(f"[curriculum] NO hit for {query!r}")
        return None

    material = _clean_pdf_artifacts(curriculum["context"])
    if not material:
        print("[curriculum] retrieved chunks were empty after cleaning")
        return None

    if not (LANGCHAIN_AVAILABLE and (llm_precise is not None or llm is not None)):
        # No model to reformat with. Raw PDF text is worse than no answer,
        # so hand the question to the caller instead.
        print("[curriculum] no model available to reformat -> treating as miss")
        return None

    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        model = llm_precise or llm
        resp = model.invoke([
            SystemMessage(content=_CURRICULUM_FORMAT_SYSTEM.replace(
                "{audience}", build_role_instruction(access_role))),
            HumanMessage(content=(
                f"Student's question: {message}\n\nCurriculum material:\n{material}")),
        ])
        formatted = (resp.content if hasattr(resp, "content") else str(resp)).strip()
    except Exception as exc:
        print(f"[curriculum] reformat failed: {exc}")
        return None

    if not formatted or looks_like_model_refusal(formatted):
        print("[curriculum] empty or refused reformat -> treating as miss")
        return None

    if curriculum_answer_is_evasive(formatted):
        first_block = re.split(r"\n\s*\n", formatted, maxsplit=1)[0]

        # A disclaimer in the opening paragraph means the retrieval was wrong.
        # Nothing after it is trustworthy, so do not salvage any of it.
        if _CURRICULUM_META_RE.search(first_block):
            print("[curriculum] answer opens with a coverage disclaimer -> treating as miss")
            return None

        salvaged = strip_curriculum_meta(formatted)
        if len(salvaged) < 120:
            print("[curriculum] evasive/meta answer -> treating as miss")
            return None

        print("[curriculum] stripped curriculum-gap commentary")
        formatted = salvaged

    return {
        "answer": formatted,
        "sources": curriculum.get("sources", []),
        "grade": curriculum.get("grade", ""),
        "subject": curriculum.get("subject", ""),
        "term": curriculum.get("term", ""),
    }


def _render_plot(spec: dict, out_path: str) -> bool:
    try:
        import numpy as np
        xmin = float(spec.get("xmin", -10)); xmax = float(spec.get("xmax", 10))
        x = np.linspace(xmin, xmax, 400)
        expr = spec.get("expression", "x")
        safe = {"x": x, "np": np, "sin": np.sin, "cos": np.cos, "tan": np.tan,
                "exp": np.exp, "log": np.log, "sqrt": np.sqrt, "pi": np.pi, "abs": np.abs}
        y = eval(expr, {"__builtins__": {}}, safe)
        fig, ax = plt.subplots(figsize=(8, 6))
        ax.plot(x, y, linewidth=2)
        ax.set_title(spec.get("title", "")); ax.set_xlabel(spec.get("xlabel", "x"))
        ax.set_ylabel(spec.get("ylabel", "y")); ax.grid(True, alpha=0.3)
        ax.axhline(0, color="black", lw=0.5); ax.axvline(0, color="black", lw=0.5)
        fig.savefig(out_path, dpi=120, bbox_inches="tight"); plt.close(fig)
        return True
    except Exception as exc:
        print(f"[image] plot render failed: {exc}")
        return False


def _render_flow(mermaid_src: str, out_path: str) -> bool:
    try:
        import subprocess, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".mmd", delete=False) as f:
            f.write(mermaid_src); mmd = f.name
        subprocess.run(["mmdc", "-i", mmd, "-o", out_path, "-b", "white"],
                       check=True, timeout=30, capture_output=True)
        os.unlink(mmd)
        return True
    except Exception as exc:
        print(f"[image] flow render failed (mmdc installed?): {exc}")
        return False


def render_science_image(subject: str, out_path: str, ctx: str = "") -> dict:
    """Try an exact renderer; fall back to a curriculum-grounded diffusion prompt.

    Returns {"ready": True, "engine", "caption"}  when a file was written now,
    or      {"ready": False, "engine": "diffusion", "prompt", "caption"}  to diffuse.
    """
    spec = classify_science_image(subject, ctx)
    engine = spec.get("engine", "diffusion")
    caption = spec.get("caption", subject)

    ok = False
    if engine == "molecule":
        ok = _render_molecule(spec.get("smiles", ""), caption, out_path)
    elif engine == "reaction":
        ok = _render_reaction(spec.get("reaction_smiles", ""), out_path)
    elif engine == "plot":
        ok = _render_plot(spec.get("plot", spec), out_path)
    elif engine == "flow":
        ok = _render_flow(spec.get("mermaid", ""), out_path)

    if ok:
        return {"ready": True, "engine": engine, "caption": caption}

    # diffusion fallback (engine==diffusion, or an exact renderer failed)
    prompt = build_curriculum_image_prompt(subject, ctx) if ctx else \
        build_image_generation_prompt(subject, "no_text", "")
    return {"ready": False, "engine": "diffusion", "prompt": prompt, "caption": caption}

def _clean_md_text(text: str) -> str:
    return re.sub(r"[\*_`~<>]", "", text or "")


def _escape_pdf_text(text: str) -> str:
    return text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def save_text_to_pdf(path: str, text: str) -> None:
    lines = []
    for paragraph in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        wrapped = textwrap.wrap(paragraph, width=90) or ['']
        lines.extend(wrapped)

    lines_per_page = 50
    page_texts = [lines[i:i + lines_per_page] for i in range(0, len(lines), lines_per_page)] or [[]]

    objects = []
    obj_id = 1

    # Catalog
    objects.append((obj_id, '<< /Type /Catalog /Pages 2 0 R >>'))
    obj_id += 1

    # Pages placeholder
    pages_obj_id = obj_id
    obj_id += 1

    # Font object
    font_obj_id = obj_id
    objects.append((font_obj_id, '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'))
    obj_id += 1

    page_ids = []
    content_ids = []
    for _page in page_texts:
        page_ids.append(obj_id)
        obj_id += 1
    for _page in page_texts:
        content_ids.append(obj_id)
        obj_id += 1

    # Page objects
    for page_id, content_id in zip(page_ids, content_ids):
        page_content = f'<< /Type /Page /Parent {pages_obj_id} 0 R /MediaBox [0 0 612 792] '
        page_content += f'/Resources << /Font << /F1 {font_obj_id} 0 R >> >> /Contents {content_id} 0 R >>'
        objects.append((page_id, page_content))

    # Content objects
    for content_id, page_lines in zip(content_ids, page_texts):
        stream_lines = ['BT', '/F1 12 Tf', '72 720 Td']
        for index, line in enumerate(page_lines):
            escaped = _escape_pdf_text(line)
            stream_lines.append(f'({escaped}) Tj')
            if index < len(page_lines) - 1:
                stream_lines.append('0 -14 Td')
        stream_lines.append('ET')
        stream_text = '\n'.join(stream_lines)
        stream_bytes = stream_text.encode('latin-1', errors='replace')
        content_obj = f'<< /Length {len(stream_bytes)} >>\nstream\n{stream_text}\nendstream'
        objects.append((content_id, content_obj))

    # Pages object after content populated
    kids = ' '.join(f'{pid} 0 R' for pid in page_ids)
    pages_obj = f'<< /Type /Pages /Kids [ {kids} ] /Count {len(page_ids)} >>'
    objects.insert(1, (pages_obj_id, pages_obj))

    with open(path, 'wb') as f:
        offsets = []
        for obj_id, obj_content in objects:
            offsets.append(f.tell())
            obj_bytes = f'{obj_id} 0 obj\n{obj_content}\nendobj\n'.encode('latin-1')
            f.write(obj_bytes)
        xref_offset = f.tell()
        f.write(b'xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1))
        for offset in offsets:
            f.write(f'{offset:010d} 00000 n \n'.encode('latin-1'))
        f.write(b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n' % (len(objects) + 1))
        f.write(f'{xref_offset}\n%%EOF\n'.encode('latin-1'))

WORKSPACE_DIR = os.path.dirname(__file__)
import glob

CURRICULUM_DIR = WORKSPACE_DIR



# STEMBio-Term1-StudentGuide-Gr12.pdf  ->  subject/term/doctype/grade
CURRICULUM_NAME_RE = re.compile(
    r"^STEM[\s_-]*(?P<subject>[A-Za-z]+)"
    r"[\s_-]*Term[\s_-]*(?P<term>\d+)"
    r"[\s_-]*(?P<doctype>[A-Za-z]+)"
    r"[\s_-]*Gr[\s_-]*(?P<grade>\d{1,2})$",
    re.IGNORECASE,
)

SUBJECT_CODE_MAP = {
    "bio": "Biology", "biology": "Biology",
    "chem": "Chemistry", "chemistry": "Chemistry",
    "physics": "Physics", "phy": "Physics",
    "eng": "Engineering", "engineering": "Engineering",
    "tech": "Technology", "technology": "Technology",
    "maths": "Mathematics", "math": "Mathematics",
}

DOCTYPE_MAP = {
    "studentguide": "Student Guide",
    "teacherguide": "Teacher Guide",
    "syllabus": "Syllabus",
    "syllubus": "Syllabus",   # two files are spelled this way
    "srb": "Student Guide",
    "trb": "Teacher Guide",
    "tg": "Teacher Guide",
}

# Syllabus defines the outcomes, so it outranks the guides during retrieval.
DOCTYPE_PRIORITY = {"LMS Material": 5, "Syllabus": 3, "Teacher Guide": 2, "Student Guide": 1}

def parse_curriculum_filename(path: str) -> dict:
    """Extract subject/term/grade/doctype from a curriculum filename."""
    stem = os.path.splitext(os.path.basename(path or ""))[0]
    info = {"file": os.path.basename(path or ""), "subject": "", "grade": "",
            "term": "", "doctype": "", "priority": 0}

    match = CURRICULUM_NAME_RE.match(stem.strip())
    if match:
        subject_key = match.group("subject").lower()
        doctype_key = match.group("doctype").lower()
        info["subject"] = SUBJECT_CODE_MAP.get(subject_key, match.group("subject").title())
        info["grade"] = f"Grade {int(match.group('grade'))}"
        info["term"] = f"Term {int(match.group('term'))}"
        info["doctype"] = DOCTYPE_MAP.get(doctype_key, match.group("doctype").title())
        info["priority"] = DOCTYPE_PRIORITY.get(info["doctype"], 1)
        return info

    # Legacy fallbacks (gr12Ente3.pdf, STEMBIOLOGY-SRB.pdf, ...)
    flat = stem.lower().replace("-", "").replace("_", "").replace(" ", "")
    m = re.search(r"gr(?:ade)?(9|1[0-3])", flat)
    if m:
        info["grade"] = f"Grade {m.group(1)}"
    for key, subject in SUBJECT_CODE_MAP.items():
        if key in flat:
            info["subject"] = subject
            break
    for key, doctype in DOCTYPE_MAP.items():
        if key in flat:
            info["doctype"] = doctype
            info["priority"] = DOCTYPE_PRIORITY.get(doctype, 1)
            break
    return info

def _is_vague_image_subject(message: str) -> bool:
    """True when the image request names no concrete subject of its own."""
    stripped = re.sub(
        r"\b(generate|create|make|draw|show|an?|image|picture|photo|illustration|diagram|"
        r"of|for|related|to|the|topic|this|that|it|please|can you|about|above|lesson)\b",
        " ", (message or "").lower())
    return len(stripped.strip()) < 4


def generate_questions_from_text(message, access_role, spec, source_text):
    """Generate questions grounded in provided text (a quoted reply), not curriculum retrieval."""
    if not (LANGCHAIN_AVAILABLE and (llm_precise or llm)):
        return ""
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        system = _QUESTION_SYSTEM.format(
            count=spec["count"],
            type_instruction=_QTYPE_INSTRUCTIONS.get(spec["type"], _QTYPE_INSTRUCTIONS["mcq"]),
            audience=build_role_instruction(access_role))
        model = llm_precise or llm
        resp = model.invoke([
            SystemMessage(content=system),
            HumanMessage(content=f"Request: {message}\n\nBase the questions ONLY on this material:\n{source_text[:4000]}"),
        ])
        return (resp.content if hasattr(resp, "content") else str(resp)).strip()
    except Exception as exc:
        print(f"[questions] from-text failed: {exc}")
        return ""

def discover_curriculum_files() -> list:
    files = sorted(glob.glob(os.path.join(CURRICULUM_DIR, "*.pdf")))
    return [f for f in files if os.path.isfile(f)]


PDF_PATHS = discover_curriculum_files()
CURRICULUM_INDEX = {p: parse_curriculum_filename(p) for p in PDF_PATHS}


INDEX_PATH = os.path.join(WORKSPACE_DIR, "index.html")

MODEL_NAME = "llama3.1:8b-instruct-q5_K_M"
EMBED_MODEL = "nomic-embed-text"

SESSION_STORE: Dict[str, Any] = {}
SESSION_CHAT_HISTORY: Dict[str, Any] = {}
SESSION_ACCESS_PROFILE: Dict[str, Dict[str, str]] = {}

IMAGE_OUTPUT_DIR = os.path.join(WORKSPACE_DIR, "assets")
UPLOAD_DIR = os.path.join(WORKSPACE_DIR, "uploads")

os.makedirs(IMAGE_OUTPUT_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

IMAGE_STATUS: Dict[str, str] = {}
USER_MEMORY: Dict[str, Dict[str, str]] = {}

SESSION_DOCUMENT_BUFFER: Dict[str, str] = {}


IMAGE_RUNTIME_AVAILABLE = True
IMAGE_FEATURE_ENABLED = True

IMAGE_DGX_URL = "http://100.92.95.83:9000"

DB_CONFIG = {
    "host": os.getenv("NEXA_DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("NEXA_DB_PORT", "3306")),
    "user": os.getenv("NEXA_DB_USER", "nexa_user"),
    "password": os.environ["NEXA_DB_PASSWORD"],
    "database": os.getenv("NEXA_DB_NAME", "nexa_ai"),
}

TEST_USER_NAME = "Test User"
MAX_DOC_CHARS = 12000 

class ChatLogPayload(BaseModel):
    log_id: str
    user_name: str
    user_prompt: str
    nexa_response: str
    timestamp: str
    session_id: Optional[str] = None
    user_email: Optional[str] = None
    pdf_url: Optional[str] = None
    stars: int = 0


class RatingPayload(BaseModel):
    log_id: str
    user_name: str
    stars: int
    timestamp: str


class PopPayload(BaseModel):
    log_id: str


class ImageBase64Payload(BaseModel):
    log_id: str
    user_name: str
    image_base64: str
    image_filename: Optional[str] = None
    image_mime_type: Optional[str] = None


class ShareChatPayload(BaseModel):
    session_id: str
    user_email: Optional[str] = None


class ShareChatResponse(BaseModel):
    share_token: str
    share_url: str


class ChatStopPayload(BaseModel):
    turn_id: str
    session_id: Optional[str] = None


class SharedChatResponse(BaseModel):
    share_token: str
    session_id: str
    created_at_utc: str
    messages: Any



# Server-side stack to mirror push/pop operations done by the UI.
chat_stack = []
CHAT_CANCELLED_TURNS: set[str] = set()
USER_MEMORY_FILE = os.path.join(os.path.dirname(__file__), "user_memory.json")

SESSION_PENDING_QUESTION = {}
# ====================== MISSING HELPERS ======================

_LLM_CHATTER_PATTERNS = (
    r"^\s*(?:sure|certainly|of course|absolutely|great|okay|ok)[!,.]?[^\n]*\n+",
    r"^\s*here(?:'s| is| are)\b[^\n]*\n+",
    r"^\s*i(?:'ve| have| will| 'll)?\s*(?:created|prepared|written|drafted|put together|made)\b[^\n]*\n+",
    r"^\s*below is\b[^\n]*\n+",
    r"^\s*as (?:an ai|a language model)\b[^\n]*\n+",
    r"\n+\s*let me know if\b[^\n]*$",
    r"\n+\s*i hope this helps\b[^\n]*$",
    r"\n+\s*feel free to\b[^\n]*$",
    r"\n+\s*would you like me to\b[^\n]*$",
)

_QTYPE_PATTERNS = [
    ("mcq",          r"\b(mcq|multiple[-\s]?choice|multiple choice question)\b"),
    ("fill_blank",   r"\b(fill[-\s]?in[-\s]?the[-\s]?blank|fill[-\s]?ups?|cloze)\b"),
    ("true_false",   r"\b(true[/\s]?(or)?[/\s]?false|true and false|t/?f)\b"),
    ("essay",        r"\b(essay|long[-\s]?answer|extended[-\s]?response|discussion question)\b"),
    ("short_answer", r"\b(short[-\s]?answer|short[-\s]?response|brief answer)\b"),
]

_QUESTION_TRIGGER = re.compile(
    r"\b(question|questions|quiz|mcq|multiple choice|fill[-\s]?in|true[/\s]?false|"
    r"essay|short[-\s]?answer|worksheet|test items?)\b", re.IGNORECASE)

_GENERATE_TRIGGER = re.compile(
    r"\b(generate|create|make|write|give me|prepare|set|produce|draft|come up with)\b",
    re.IGNORECASE)


def _is_question_request(message: str) -> bool:
    text = message or ""
    return bool(_QUESTION_TRIGGER.search(text) and _GENERATE_TRIGGER.search(text))

PDF_LESSON_CSS = """
body { font-family: sans-serif; font-size: 10.5pt; color: #1f2937; line-height: 1.5; }
h1 { font-size: 19pt; color: #0f172a; margin-bottom: 10px; }
h2 { font-size: 13.5pt; color: #1e3a8a; margin-top: 16px; margin-bottom: 6px; }
h3 { font-size: 11.5pt; color: #334155; margin-top: 12px; margin-bottom: 4px; }
p  { margin-top: 4px; margin-bottom: 8px; }
li { margin-top: 2px; margin-bottom: 2px; }
strong { color: #1e3a8a; }
table { width: 100%; }
th { background-color: #dbeafe; color: #1d4ed8; text-align: left; padding: 5px; }
td { padding: 5px; }
code { font-family: monospace; font-size: 9.5pt; color: #3730a3; }
blockquote { color: #475569; margin-left: 12px; }
"""


_GROUNDED_LESSON_SYSTEM = (
    "You are a PNG curriculum specialist writing a classroom-ready lesson plan.\n\n"
    "ABSOLUTE RULES:\n"
    "- Use ONLY the curriculum excerpts supplied below. Do not add outside knowledge or "
    "activities the excerpts do not support.\n"
    "- Every objective, key concept, and vocabulary item must be traceable to the excerpts.\n"
    "- State the grade exactly as the excerpts state it.\n"
    "- Never ask the user clarifying questions. Never address the user by name. No preamble "
    "or closing remark. Begin with the # title and nothing else.\n\n"
    "Structure (clean Markdown):\n"
    "# [Lesson Title]\n## Overview\n## Learning Objectives\n## Key Concepts and Vocabulary\n"
    "## Materials\n## Lesson Structure\n### Introduction / Engagement\n"
    "### Direct Instruction / Explanation\n### Guided Practice\n### Independent Practice\n"
    "### Closure / Consolidation\n## Differentiation\n## Assessment\n"
    "## Extension and Homework\n## Teacher Notes\n\n"
    "Give a time estimate, teacher actions, and student actions for each lesson-structure "
    "subsection.\n\nAudience guidance: {audience}"
)


def generate_lesson_plan_grounded(message: str, access_role: str, curriculum: dict) -> str:
    """Lesson plan written strictly from curriculum excerpts."""
    if not (LANGCHAIN_AVAILABLE and (llm_precise is not None or llm is not None) and curriculum):
        return ""
    try:
        prompt = ChatPromptTemplate.from_messages([
            ("system", _GROUNDED_LESSON_SYSTEM),
            ("human",
             "Teacher's request: {request}\n\n"
             "Curriculum excerpts (the ONLY permitted source of content):\n{excerpts}"),
        ])
        model = llm_precise or llm
        return (prompt | model | StrOutputParser()).invoke({
            "request": message,
            "excerpts": curriculum["context"],
            "audience": build_role_instruction(access_role),
        }).strip()
    except Exception as exc:
        print(f"[info] Grounded lesson plan generation failed: {exc}")
        return ""


_DIRECT_LESSON_SYSTEM = (
    "You are an expert curriculum designer producing a professional, classroom-ready "
    "lesson plan in clean Markdown.\n\n"
    "RULES:\n"
    "- Never refuse, never mention copyright, never add conversational preamble or closing remarks.\n"
    "- Never ask the user clarifying questions. If grade, duration, or subject are not stated, "
    "choose sensible values.\n"
    "- Never address the user by name. Begin with the # title and nothing else.\n\n"
    "Structure:\n"
    "# [Lesson Title]\n## Overview\n## Learning Objectives\n## Key Concepts and Vocabulary\n"
    "## Materials\n## Lesson Structure\n### Introduction / Engagement\n"
    "### Direct Instruction / Explanation\n### Guided Practice\n### Independent Practice\n"
    "### Closure / Consolidation\n## Differentiation\n## Assessment\n"
    "## Extension and Homework\n## Teacher Notes\n\n"
    "Give a time estimate, teacher actions, and student actions for each subsection.\n\n"
    "Audience guidance: {audience}"
)


def generate_lesson_plan_direct(message: str, access_role: str) -> str:
    """Fallback: LLM-generated lesson plan when the topic is not in the curriculum."""
    if not (LANGCHAIN_AVAILABLE and llm is not None):
        return ""
    try:
        prompt = ChatPromptTemplate.from_messages([
            ("system", _DIRECT_LESSON_SYSTEM),
            ("human", "{request}"),
        ])
        return (prompt | llm | StrOutputParser()).invoke({
            "request": message,
            "audience": build_role_instruction(access_role),
        }).strip()
    except Exception as exc:
        print(f"[info] Direct lesson plan generation failed: {exc}")
        return ""


def strip_output_format_noise(text: str) -> str:
    """Remove 'in PDF format', 'as a pdf', etc.
 
    Without this, 'lesson plan on X in PDF format' extracts a subject of
    'PDF format??' instead of X.
    """
    cleaned = text or ""
    cleaned = re.sub(
        r"\b(in|as|to|into)\s+(a\s+|the\s+)?pdf(\s+(format|file|document|version))?\b",
        " ", cleaned, flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\bpdf\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(please|can you|could you|generate|create|make|write|prepare)\b",
                     " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip(" ?.!,")


MODEL_REFUSAL_SIGNALS = (
    "i can't help", "i cannot help", "i can not help",
    "copyrighted material", "copyright",
    "i'm not able to", "i am not able to",
    "i'm unable to", "i am unable to",
    "i won't be able", "against my guidelines",
    "is there anything else i can assist",
)
 

def looks_like_model_refusal(text: str) -> bool:
    body = (text or "").strip().lower()
    if not body:
        return True
    if len(body) < 200 and any(s in body for s in MODEL_REFUSAL_SIGNALS):
        return True
    return any(s in body[:400] for s in MODEL_REFUSAL_SIGNALS)
 

def stamp_pdf_footer(path: str, footer_text: Optional[str] = None) -> bool:
    """Draw a separator rule, the disclaimer, and 'Page N of M' on each page.
 
    Runs after the PDF is written. Returns False (leaving the PDF intact)
    if PyMuPDF is unavailable or anything fails.
    """
    try:
        import fitz  # PyMuPDF
    except ModuleNotFoundError:
        print("[info] PyMuPDF not available - skipping PDF footer")
        return False
 
    text = (footer_text or GENERATION_DISCLAIMER).strip()
    if text.upper().startswith("DISCLAIMER:"):
        text = text.split(":", 1)[1].strip()
 
    tmp_path = f"{path}.tmp"
 
    try:
        doc = fitz.open(path)
        total_pages = doc.page_count
 
        for index, page in enumerate(doc, start=1):
            rect = page.rect
            margin = 36.0
            baseline = rect.height - 52.0     # top of the footer band
 
            # Separator rule
            page.draw_line(
                fitz.Point(margin, baseline),
                fitz.Point(rect.width - margin, baseline),
                color=(0.78, 0.82, 0.88),
                width=0.7,
            )
 
            # Disclaimer text - wraps across up to three short lines
            disclaimer_box = fitz.Rect(
                margin, baseline + 5,
                rect.width - margin - 70, rect.height - 14,
            )
            page.insert_textbox(
                disclaimer_box, text,
                fontsize=6.2, fontname="helv",
                color=(0.42, 0.46, 0.52),
                align=fitz.TEXT_ALIGN_LEFT,
            )
 
            # Page number, right-aligned on the first footer line
            page_box = fitz.Rect(
                rect.width - margin - 68, baseline + 5,
                rect.width - margin, baseline + 20,
            )
            page.insert_textbox(
                page_box, f"Page {index} of {total_pages}",
                fontsize=7.0, fontname="helv",
                color=(0.30, 0.35, 0.42),
                align=fitz.TEXT_ALIGN_RIGHT,
            )
 
        doc.save(tmp_path, garbage=3, deflate=True)
        doc.close()
        os.replace(tmp_path, path)
        return True
 
    except Exception as exc:
        print(f"PDF footer stamping failed: {exc}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False
 
CURRICULUM_MIN_SIMILARITY = float(os.getenv("NEXA_MIN_SIMILARITY", "0.10"))


def _chroma_filter(pairs: dict):
    clauses = [{k: {"$eq": v}} for k, v in pairs.items() if v]
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def retrieve_curriculum_context(query: str, scope: dict = None, k: int = 14):
    """Grounded curriculum excerpts, narrowed to the requested grade/term/subject.
    Returns None when the curriculum genuinely has nothing on the topic, so the
    caller falls through to general knowledge instead of forcing an answer out of
    unrelated chunks.
    """
    if not LANGCHAIN_AVAILABLE:
        return None
    store = globals().get("vectorstore")
    if store is None:
        return None
    scope = scope or {}
    grade = scope.get("grade")
    term = scope.get("term")
    subject = scope.get("subject")
    # Subject is a hard constraint when stated. Relaxing it turns a Physics
    # request into whatever Biology material exists for that grade.
    attempts = [
        {"grade": grade, "term": term, "subject": subject},
        {"grade": grade, "subject": subject},
    ]
    if subject:
        attempts.append({"subject": subject})
    elif grade:
        attempts.append({"grade": grade})
    if not grade and not subject:
        attempts.append({})

    hits, used_filter = [], {}
    seen_ids = set()

    def _merge(raw):
        """Add new results, de-duplicating by content so grade-filtered and
        subject-only passes don't double-count the same chunk."""
        for doc, dist in raw:
            key = (getattr(doc, "page_content", "") or "")[:120]
            if key in seen_ids:
                continue
            seen_ids.add(key)
            hits.append((doc, dist))

    for attempt in attempts:
        active = {key: value for key, value in attempt.items() if value}
        try:
            raw = store.similarity_search_with_score(query, k=k, filter=_chroma_filter(active))
        except Exception as exc:
            print(f"[warn] filtered retrieval failed ({active}): {exc}")
            continue
        if raw:
            _merge(raw)
            if not used_filter:
                used_filter = active
            # LMS content is tagged grade=Unknown, so a grade filter excludes it.
            # When this successful attempt used a grade filter, do one more
            # subject-only pass so Unknown-grade LMS material is included too.
            if active.get("grade"):
                subj_only = {"subject": active["subject"]} if active.get("subject") else {}
                try:
                    raw2 = store.similarity_search_with_score(
                        query, k=k, filter=_chroma_filter(subj_only))
                    if raw2:
                        _merge(raw2)
                except Exception:
                    pass
            break

    # Fallback: if the long-lived store handle returned nothing, retry once with a
    # freshly-opened handle. Works around a stale in-process search index after
    # documents were added by the ingest endpoint since this process started.
    if not hits:
        try:
            fresh = Chroma(
                collection_name="curriculum_db",
                embedding_function=globals().get("embeddings"),
                persist_directory=os.path.expanduser("~/Nexa/chroma_store"),
                collection_metadata={"hnsw:space": "cosine"},
            )
            raw = fresh.similarity_search_with_score(query, k=k, filter=None)
            if raw:
                _merge(raw)
                used_filter = {}
                globals()["vectorstore"] = fresh
                print(f"[retrieval] fresh-handle fallback returned {len(raw)} (adopted)")
        except Exception as exc:
            print(f"[retrieval] fresh-handle fallback failed: {exc}")

    if not hits:
        print(f"[retrieval] no chunks for {query[:60]!r}")
        return None

    # Re-rank the merged pool by distance so the best chunks (LMS or PDF) lead.
    hits.sort(key=lambda item: item[1])
    hits = hits[:k]

    keywords = _content_keywords(query)

    def _similarity(distance):
        if distance is None or distance > 2.0:
            return None
        return 1.0 - float(distance)

    scored = []
    for doc, distance in hits:
        body = (getattr(doc, "page_content", "") or "").strip()
        if not body:
            continue
        sim = _similarity(distance)
        if sim is not None and sim < CURRICULUM_MIN_SIMILARITY:
            continue
        matched = _keyword_hits(keywords, body)
        scored.append((doc, distance, sim, matched))
    if not scored:
        print(f"[retrieval] all chunks below similarity floor for {query[:60]!r}")
        return None

    def _meta(doc):
        return getattr(doc, "metadata", {}) or {}

    print("[retrieval] filter=%s query=%r" % (used_filter, query[:70]))
    for doc, distance, sim, matched in scored[:5]:
        m = _meta(doc)
        src_tag = "LMS" if m.get("doctype") == "LMS Material" else "PDF"
        # PDFs carry a page; LMS docx/pptx carry a chunk index instead.
        if src_tag == "LMS":
            locator = f"chunk {m.get('chunk', '?')}"
        else:
            pg = m.get("page")
            locator = f"p.{int(pg) + 1}" if isinstance(pg, int) else "p.?"
        course = m.get("course", "")
        print("   [%s] %-38s subj=%-10s grade=%-8s course=%-20s %-9s dist=%.3f sim=%s kw=%s"
              % (src_tag, str(m.get("file", "?"))[:38],
                 m.get("subject", "?"), m.get("grade", "?"),
                 (str(course)[:20] if course else "-"),
                 locator, distance,
                 f"{sim:.3f}" if sim is not None else "n/a", sorted(matched)[:4]))

    # COVERAGE GATE. One loose hit is not coverage: a single shared word like
    # "water" matched acid-base chemistry to a question about basalt.
    if keywords:
        needed = 2 if len(keywords) >= 2 else 1
        covered = any(len(matched) >= needed for _, _, _, matched in scored)
        # A strong-similarity chunk that matches at least one real keyword is
        # genuine coverage even for a single-word topic like "photosynthesis",
        # where the count can never reach two.
        if not covered:
            covered = any(
                (sim is not None and sim >= 0.70 and len(matched) >= 1)
                for _, _, sim, matched in scored)
        if not covered:
            print(f"[retrieval] weak keyword overlap (needed {needed}) -> not covered")
            return None

    scored.sort(key=lambda item: (
        0 if item[3] else 1,
        -DOCTYPE_PRIORITY.get(_meta(item[0]).get("doctype", ""), 0),
        item[1],
    ))

    blocks, sources, grades, subjects, terms = [], [], [], [], []
    for doc, _distance, _sim, _matched in scored[:10]:
        meta = _meta(doc)
        page = meta.get("page")
        page_label = f" p.{int(page) + 1}" if isinstance(page, int) else ""
        label = f"{meta.get('file', 'curriculum')}{page_label}"
        blocks.append(
            f"[SOURCE: {label} | {meta.get('subject','')} | {meta.get('grade','')} | "
            f"{meta.get('term','')} | {meta.get('doctype','')}]\n{doc.page_content.strip()}")
        if label not in sources:
            sources.append(label)
        for value, bucket in ((meta.get("grade"), grades),
                              (meta.get("subject"), subjects),
                              (meta.get("term"), terms)):
            if value and value != "Unknown":
                bucket.append(value)
    if not blocks:
        return None

    # Summary of where the answer's material came from, tagged LMS vs PDF.
    src_summary = set()
    for doc, _d, _s, _m in scored[:10]:
        meta = _meta(doc)
        tag = "LMS" if meta.get("doctype") == "LMS Material" else "PDF"
        src_summary.add(f"{tag}:{meta.get('subject','?')}/{meta.get('grade','?')}")
    print(f"[retrieval] answer drawn from: {', '.join(sorted(src_summary))}")

    def _most_common(values):
        return max(set(values), key=values.count) if values else ""

    return {
        "context": "\n\n---\n\n".join(blocks),
        "sources": sources,
        "grade": grade or _most_common(grades),
        "subject": subject or _most_common(subjects),
        "term": term or _most_common(terms),
        "filter_used": used_filter,
        "chunks": len(blocks),
    }
# ---------------------------------------------------------------------
# MAIN ENTRY POINT - build a PDF from a Markdown answer
# ---------------------------------------------------------------------
def build_answer_pdf(answer: str, filename_prefix: str = "lesson") -> Optional[str]:
    """Render a model answer to a styled, footered PDF.
 
    Returns the public /assets/... URL, or None if generation failed.
    """
    body = strip_llm_chatter(answer if isinstance(answer, str) else str(answer or ""))
    if not body:
        return None
 
    # Derive a real document title from the H1, or add one.
    doc_title = "Nexa AI Document"
    title_match = re.search(r"^#\s+(.+)$", body, flags=re.MULTILINE)
    if title_match:
        doc_title = title_match.group(1).strip()
    else:
        body = f"# {doc_title}\n\n{body}"
 
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{filename_prefix}_{ts}.pdf"
    path = os.path.join(IMAGE_OUTPUT_DIR, filename)
 
    try:
        if MarkdownPdf is not None and Section is not None:
            pdf = MarkdownPdf(toc_level=2)
            pdf.meta["title"] = doc_title
            pdf.meta["author"] = "NEXA AI"
            pdf.meta["subject"] = "EduNeX generated document"
            pdf.add_section(
                Section(body, toc=True, borders=(36, 36, -36, -64)),
                user_css=PDF_LESSON_CSS,
            )
            pdf.save(path)
        else:
            save_text_to_pdf(path, body)
 
        stamp_pdf_footer(path)
        return f"/assets/{filename}"
 
    except Exception as pdf_error:
        print(f"PDF generation failed: {pdf_error}")
        try:
            save_text_to_pdf(path, body)
            stamp_pdf_footer(path)
            return f"/assets/{filename}"
        except Exception as fallback_error:
            print(f"Fallback PDF generation failed: {fallback_error}")
            return None


def quick_reply(message: str, user_name: str = ""):
    """Instant answers for greetings and trivial messages — no LLM call."""
    m = (message or "").lower().strip().rstrip("!.?")
    name = (user_name or "").strip()
    first = name.split()[0] if name else ""

    # Name questions — answered instantly when we know the name
    if first and m in {"do you know my name", "what is my name", "whats my name",
                       "what's my name", "who am i", "do you remember my name",
                       "do you know who i am"}:
        return f"Yes — you're {first}. How can I help you today?"

    greetings = {"hi", "hello", "hey", "yo", "hi nexa", "hello nexa", "good morning",
                 "good afternoon", "good evening", "morning", "afternoon", "greetings"}
    thanks = {"thanks", "thank you", "thankyou", "cheers", "tenkyu", "ta", "thx"}
    acks = {"ok", "okay", "cool", "nice", "good", "great", "alright", "sure", "got it"}
    byes = {"bye", "goodbye", "see you", "later", "gotta go"}

    # Greetings: exact match, OR a message that STARTS with a greeting and is short
    # enough to be a pure greeting (so "hello nexa can you help me" greets, but
    # "hello, explain photosynthesis in detail" falls through to a real answer).
    greeting_starts = ("hello", "hi nexa", "hey", "good morning",
                       "good afternoon", "good evening", "greetings")
    is_greeting = (
        m in greetings
        or m in ("hi", "hey", "yo")
        or (any(m.startswith(g) for g in greeting_starts) and len(m.split()) <= 6)
    )
    if is_greeting:
        return (f"Hello {first}! I'm Nexa. What would you like to learn about today?"
                if first else "Hello! I'm Nexa. What would you like to learn about today?")

    if m in thanks:
        return "You're welcome! Ask me anything else."
    if m in acks:
        return "👍 What would you like to explore next?"
    if m in byes:
        return "Goodbye! Come back anytime you need help with your studies."
    return None




def load_user_memory():
    global USER_MEMORY
    try:
        with open(USER_MEMORY_FILE, "r", encoding="utf-8") as f:
            USER_MEMORY = json.load(f)
    except Exception:
        USER_MEMORY = {}

def save_user_memory():
    try:
        with open(USER_MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(USER_MEMORY, f)
    except Exception as e:
        print("user memory save failed:", e)

load_user_memory()

_TOPIC_STOPWORDS = {
    "lesson", "plan", "create", "make", "write", "generate", "prepare", "teaching",
    "grade", "term", "students", "student", "teacher", "about", "with", "from",
    "that", "this", "have", "need", "want", "please", "topic", "unit", "using",
    "based", "minutes", "minute", "hour", "hours", "subject", "school", "class",
    "would", "could", "should", "pdf", "format", "document", "file", "official",
    "curriculum", "syllabus",
    # Request scaffolding, never a topic. Without these, "give me three examples
    # of that" matches any chunk containing "three" or "examples".
    "examples", "example", "three", "four", "five", "give", "simpler", "simple",
    "shorter", "language", "another", "more", "explain", "expand", "second",
    "third", "first", "last", "next", "again", "instead", "different",
    # Question words and generic nouns — never the topic itself. Without these,
    # "what is photosynthesis" keeps "what" as a keyword and the coverage gate
    # wrongly demands two matches for a single-topic question.
    "what", "when", "where", "which", "whom", "whose", "does", "types", "type",
    "kind", "kinds", "various", "list", "tell", "discuss", "outline", "describe",
    "define", "definition", "meaning",
}

def _content_keywords(text: str) -> list:
    tokens = re.findall(r"[a-z]{4,}", (text or "").lower())
    seen, out = set(), []
    for token in tokens:
        if token in _TOPIC_STOPWORDS or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _keyword_hits(keywords: list, body: str) -> set:
    low = (body or "").lower()
    return {kw for kw in keywords if kw[:6] and kw[:6] in low}


def _material_covers_topic(topic: str, context: str) -> bool:
    """Retrieved chunks must actually mention the topic before we answer from them."""
    kws = _content_keywords(topic or "")
    if not kws:
        return True
    hits = _keyword_hits(kws, context or "")
    return len(hits) >= (2 if len(kws) >= 2 else 1)

import re

def capture_user_fact(email: str, message: str):
    """Detect and store definitions the user asserts, e.g. 'DOE stands for Department of Education'."""
    if not email:
        return
    patterns = [
        r'\b([A-Z]{2,6})\s+(?:stands for|means|is short for|refers to|is)\s+(.+)',
        r'\b(.+?)\s+is\s+(?:called|known as)\s+(.+)',
    ]
    for pat in patterns:
        m = re.search(pat, message, re.IGNORECASE)
        if m:
            term = m.group(1).strip().strip('."').upper()
            definition = m.group(2).strip().strip('."')
            if 1 < len(term) <= 10 and 2 < len(definition) <= 120:
                USER_MEMORY.setdefault(email, {})[term] = definition
                save_user_memory()
                return

def looks_like_math(message: str) -> bool:
    m = (message or "").lower()
    signals = ("solve", "integrate", "integral", "differentiate", "derivative",
               "evaluate", "calculate", "simplify", "∫", "∑", "√")
    arithmetic_pattern = re.search(r"\b\d+(?:\s*[+\-*/^=]\s*\d+)+(?:\s*\b|$)", m)
    return any(s in m for s in signals) or arithmetic_pattern is not None or sum(c in m for c in "∫∑√^=") >= 2


def looks_like_reasoning_question(message: str) -> bool:
    lowered = (message or "").strip().lower()
    if not lowered:
        return False

    reasoning_signals = (
        "analyze", "analyse", "analysis", "reason", "reasoning", "explain",
        "compare", "contrast", "deduce", "infer", "prove", "why does",
        "why is", "how does", "how do", "what happens if", "step by step",
    )
    return any(signal in lowered for signal in reasoning_signals)

def is_chat_turn_cancelled(turn_id: Optional[str]) -> bool:
    return bool(turn_id and turn_id in CHAT_CANCELLED_TURNS)


def get_conn():
    if mysql.connector is None:
        raise HTTPException(status_code=503, detail="MySQL connector is not installed")
    return mysql.connector.connect(**DB_CONFIG)



def fetch_page_text(url: str, max_chars: int = 8000) -> str:
    """Fetch a web page and return clean readable text (scripts/styles stripped)."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NexaBot/1.0; educational assistant)"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        return f"__ERROR__ Could not fetch the page: {exc}"

    ctype = resp.headers.get("Content-Type", "")
    if "html" not in ctype and "text" not in ctype:
        return "__ERROR__ That link is not a readable web page (it may be a file or media)."

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
        tag.decompose()

    title = (soup.title.string.strip() if soup.title and soup.title.string else "")
    text = " ".join(soup.get_text(separator=" ").split())
    if not text:
        return "__ERROR__ The page had no readable text content."

    text = text[:max_chars]
    return f"PAGE TITLE: {title}\n\nPAGE CONTENT:\n{text}"


def extract_text_from_upload(path: str, filename: str) -> str:
    name = (filename or "").lower()
    try:
        if name.endswith(".pdf") and LANGCHAIN_AVAILABLE:
            loader = PyPDFLoader(path)
            pages = loader.load()
            return "\n\n".join(p.page_content for p in pages)
        if name.endswith(".txt"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        if name.endswith(".docx"):
            try:
                import docx  # python-docx
                document = docx.Document(path)
                return "\n".join(p.text for p in document.paragraphs)
            except ModuleNotFoundError:
                return ""
    except Exception as exc:
        print(f"Text extraction failed: {exc}")
    return ""

def persist_chat_log(
    log_id: str,
    session_id: Optional[str],
    user_email: Optional[str],
    user_name: str,
    user_prompt: str,
    nexa_response: str,
    pdf_url: Optional[str] = None,
    image_filename: Optional[str] = None,
    image_mime_type: Optional[str] = None,
    image_base64: Optional[str] = None,
    stars: int = 0,
    timestamp: Optional[datetime.datetime] = None,
) -> bool:
    if mysql.connector is None:
        return False

    ts = timestamp or datetime.datetime.now(datetime.timezone.utc)
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO nexa_chat_logs (
                log_id, session_id, user_email, user_name, user_prompt, nexa_response,
                pdf_url, image_filename, image_mime_type, image_base64, timestamp_utc, stars
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                session_id = VALUES(session_id),
                user_email = VALUES(user_email),
                user_name = VALUES(user_name),
                user_prompt = VALUES(user_prompt),
                nexa_response = VALUES(nexa_response),
                pdf_url = VALUES(pdf_url),
                image_filename = VALUES(image_filename),
                image_mime_type = VALUES(image_mime_type),
                image_base64 = VALUES(image_base64),
                timestamp_utc = VALUES(timestamp_utc),
                stars = VALUES(stars)
            """,
            (
                log_id,
                session_id,
                normalize_email_address(user_email),
                user_name,
                user_prompt,
                nexa_response,
                pdf_url,
                image_filename,
                image_mime_type,
                image_base64,
                ts.strftime("%Y-%m-%d %H:%M:%S"),
                stars,
            ),
        )
        conn.commit()
        return True
    except Exception as exc:
        print(f"Failed to persist chat log: {exc}")
        return False
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()

import re
import sympy
from sympy.parsing.sympy_parser import (
    parse_expr, standard_transformations, implicit_multiplication_application
)

_SYMPY_TF = standard_transformations + (implicit_multiplication_application,)


_MATH_CHAR_MAP = str.maketrans({
    "⁰": "^0", "¹": "^1", "²": "^2", "³": "^3", "⁴": "^4",
    "⁵": "^5", "⁶": "^6", "⁷": "^7", "⁸": "^8", "⁹": "^9",
    "−": "-", "–": "-", "—": "-", "×": "*", "÷": "/",
})

def _latex_to_math_text(t: str) -> str:
    """Convert MathLive LaTeX into text solve_with_sympy understands."""
    s = t
    m = re.search(r'\\int_\{?([^{}]+?)\}?\^\{?([^{}]+?)\}?\s*(.+?)\s*(?:\\,)?\s*d\s*x', s)
    if m:
        return f"integrate {m.group(3)} from {m.group(1)} to {m.group(2)}"
    m = re.search(r'\\int\s*(.+?)\s*(?:\\,)?\s*d\s*x', s)
    if m:
        return f"integrate {m.group(1)}"
    s = re.sub(r'\\frac\{([^{}]+)\}\{([^{}]+)\}', r'(\1)/(\2)', s)
    s = s.replace('\\sqrt', 'sqrt').replace('\\pi', 'pi').replace('\\theta', 'theta')
    s = s.replace('\\cdot', '*').replace('\\times', '*').replace('\\div', '/')
    s = s.replace('\\left', '').replace('\\right', '').replace('\\,', ' ')
    s = re.sub(r'\^\{([^{}]+)\}', r'^(\1)', s)
    s = re.sub(r'\\[a-zA-Z]+', '', s)
    return s.strip()


def normalize_math_input(text: str) -> str:
    """Repair maths pasted from a page: Unicode superscripts and lost carets.
    'f(x)=3x4-5x3+2x2-7x+4' becomes 'f(x)=3x^4-5x^3+2x^2-7x+4'."""
    # MathLive sends LaTeX (contains backslashes) — convert it to plain math text first.
    if "\\" in (text or ""):
        text = _latex_to_math_text(text)
    t = (text or "").translate(_MATH_CHAR_MAP)
    t = re.sub(r"\^(\d)\s*\^(\d)", r"^\1\2", t)
    # A digit stuck to a variable is a superscript the copy/paste dropped.
    # Only applied when the text carries no carets already, so properly typed
    # input is never touched.
    if "^" not in t and re.search(r"[a-zA-Z]\d", t):
        t = re.sub(r"(?<=[a-zA-Z])(\d+)", r"^\1", t)
    return re.sub(r"\^{2,}", "^", t)


def solve_with_sympy(message: str):
    """Compute an exact answer with SymPy. Returns (latex_result, plain) or None."""
    # Normalize Unicode maths characters SymPy's parser can't read.
    message = (message or "").replace("−", "-").replace("–", "-").replace("×", "*").replace("÷", "/")
    x = sympy.Symbol('x')
    msg = message.strip()

    try:
        # ---- Definite integral: "integrate <f> from <a> to <b>" or "[a,b] <f> dx" ----
        defint = re.search(r'(?:integrate|integral of)\s+(.+?)\s+from\s+(.+?)\s+to\s+(.+)', msg, re.IGNORECASE)
        bounds = re.search(r'∫?\s*\[?\s*([\d\.\-/]+)\s*[,;]\s*([\d\.\-/]+)\s*\]?\s*(.+?)\s*dx', msg, re.IGNORECASE)
        if defint or bounds:
            if defint:
                body, lo, hi = defint.group(1), defint.group(2), defint.group(3)
            else:
                lo, hi, body = bounds.group(1), bounds.group(2), bounds.group(3)
            expr = parse_expr(body.replace("^", "**"), transformations=_SYMPY_TF)
            lo_v = parse_expr(lo, transformations=_SYMPY_TF)
            hi_v = parse_expr(hi, transformations=_SYMPY_TF)
            exact = sympy.integrate(expr, (x, lo_v, hi_v))
            approx = sympy.N(exact, 6)
            return (f"$$\\int_{{{sympy.latex(lo_v)}}}^{{{sympy.latex(hi_v)}}} "
                    f"{sympy.latex(expr)}\\,dx = {sympy.latex(exact)} \\approx {approx}$$",
                    f"{exact} (approx {approx})")

        # ---- Indefinite integral: "integrate <f>" ----
        indef = re.search(r'(?:integrate|integral of)\s+(.+?)(?:\s+dx)?$', msg, re.IGNORECASE)
        if indef:
            expr = parse_expr(indef.group(1).replace("^", "**"), transformations=_SYMPY_TF)
            result = sympy.integrate(expr, x)
            return (f"$$\\int {sympy.latex(expr)}\\,dx = {sympy.latex(result)} + C$$", str(result))

        # ---- Derivative: "differentiate <f>" / "derivative of <f>" ----
        diff = re.search(r'(?:differentiate|derivative of)\s+(.+)', msg, re.IGNORECASE)
        if diff:
            expr = parse_expr(diff.group(1).replace("^", "**"), transformations=_SYMPY_TF)
            result = sympy.diff(expr, x)
            return (f"$$\\frac{{d}}{{dx}}\\left({sympy.latex(expr)}\\right) = {sympy.latex(result)}$$", str(result))

        # ---- Equation solving: "solve <lhs> = <rhs>" ----
        eq = re.search(r'solve\s+(.+)', msg, re.IGNORECASE)
        if eq and "=" in eq.group(1):
            left, right = eq.group(1).split("=", 1)
            lhs = parse_expr(left.replace("^", "**"), transformations=_SYMPY_TF)
            rhs = parse_expr(right.replace("^", "**"), transformations=_SYMPY_TF)
            sols = sympy.solve(sympy.Eq(lhs, rhs), x)
            if not sols:
                return None
            if len(sols) == 1:
                return (f"$$x = {sympy.latex(sols[0])}$$", str(sols))
            body = ",\\quad ".join(f"x = {sympy.latex(s)}" for s in sols)
            return (f"$${body}$$", str(sols))

        # ---- Simplify / evaluate: "simplify <expr>" ----
        simp = re.search(r'(?:simplify|evaluate|calculate)\s+(.+)', msg, re.IGNORECASE)
        if simp:
            expr = parse_expr(simp.group(1).replace("^", "**"), transformations=_SYMPY_TF)
            result = sympy.simplify(expr)
            return (f"$${sympy.latex(expr)} = {sympy.latex(result)}$$", str(result))

    except Exception as e:
        print(f"[info] SymPy could not parse (falling back to LLM): {e}")
    return None

def to_utc_datetime(iso_str: str) -> datetime.datetime:
    try:
        parsed = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid ISO timestamp") from exc

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def escape_markdown(text: str) -> str:
    text = html.escape(text or "")
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|>])", r"\\\1", text)


def looks_like_web_query(message: str) -> bool:
    lowered = (message or "").strip().lower()
    if not lowered:
        return False

    web_keywords = (
        "search",
        "google",
        "wikipedia",
        "wiki",
        "latest",
        "news",
        "lookup",
        "look up",
    )

    return any(keyword in lowered for keyword in web_keywords)


def build_web_results_query(message: str) -> str:
    cleaned = (message or "").strip()
    cleaned = re.sub(r"^(search|google|find|look up|lookup|wikipedia|wiki)\s+(for\s+)?", "", cleaned, flags=re.IGNORECASE)
    return cleaned or message


def fetch_web_results(query: str, limit: int = 5) -> str:
    if DDGS is None:
        return "Web search is unavailable because duckduckgo-search is not installed in this environment."

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=limit, safesearch="moderate"))
    except Exception as exc:
        return f"I could not fetch web results right now: {_clean_md_text(str(exc))}"

    if not results:
        return (
            f"I could not fetch live web results for **{_clean_md_text(query)}** right now. "
            "You can ask again with a more specific phrase, or try a Wikipedia lookup instead."
        )

    cards = [f'<div class="nexa-search-results">'
             f'<div class="nexa-search-head">Web results for &ldquo;{html.escape(query)}&rdquo;</div>']

    for item in results:
        title = html.escape((item.get("title") or "Untitled result").strip())
        snippet = html.escape((item.get("body") or "").strip())
        url = (item.get("href") or item.get("url") or "").strip()
        safe_url = html.escape(url, quote=True)
        try:
            domain = html.escape(url.split("/")[2]) if "//" in url else ""
        except Exception:
            domain = ""

        cards.append(f'''
<div class="nexa-search-card">
  <a class="nexa-search-title" href="{safe_url}" target="_blank" rel="noopener noreferrer">{title}</a>
  <div class="nexa-search-domain">{domain}</div>
  <div class="nexa-search-snippet">{snippet}</div>
  <a class="nexa-search-more" href="{safe_url}" target="_blank" rel="noopener noreferrer">Read more &rarr;</a>
</div>''')

    cards.append("</div>")
    return "\n".join(cards)

def fetch_wikipedia_summary(query: str) -> str:
    if wikipedia is None:
        return "Wikipedia is unavailable because the wikipedia package is not installed in this environment."

    try:
        wikipedia.set_lang("en")
        search_results = wikipedia.search(query, results=5)
        if not search_results:
            return f"I could not find a Wikipedia page for **{escape_markdown(query)}**."

        page_title = search_results[0]
        page = wikipedia.page(page_title, auto_suggest=False)
        summary = wikipedia.summary(page.title, sentences=4, auto_suggest=False)
        return (
            f"# Wikipedia: {escape_markdown(page.title)}\n\n"
            f"{escape_markdown(summary)}\n\n"
            f"Source: {page.url}"
        )
    except wikipedia.DisambiguationError as exc:
        choices = ", ".join(escape_markdown(choice) for choice in exc.options[:5])
        return (
            f"I found multiple Wikipedia results for **{escape_markdown(query)}**.\n\n"
            f"Try one of these: {choices}"
        )
    except wikipedia.PageError:
        return f"I could not find a Wikipedia page for **{escape_markdown(query)}**."
    except Exception as exc:
        return (
            f"I could not fetch a live Wikipedia result for **{escape_markdown(query)}** right now. "
            "Try a more specific title or ask Nexa for a short explanation instead."
        )

import re  # at the top of the file if not already there

def analyze_image_text_intent(message: str):
    """Returns (intent, text): 'wants_text' | 'no_text' | 'ambiguous'."""
    m = (message or "").strip()
    low = m.lower()
    if any(p in low for p in ("no text", "without text", "no words", "no writing",
                              "text-free", "no labels")):
        return ("no_text", "")
    q = re.search(r'["\u201c\u2018\']([^"\u201d\u2019\']{1,80})["\u201d\u2019\']', m)
    if q:
        return ("wants_text", q.group(1).strip())
    if any(p in low for p in ("with the text", "that says", "saying", "with the words",
                              "captioned", "titled", "with the title", "label it")):
        return ("wants_text", "")
    ambiguous_types = ("banner", "poster", "flyer", "sign", "logo", "certificate",
                       "card", "cover", "brochure", "advertisement", "advert",
                       "infographic", "menu", "ticket", "invitation", "billboard")
    if any(t in low for t in ambiguous_types):
        return ("ambiguous", "")
    return ("no_text", "")



_IMAGE_PROMPT_SYSTEM = (
    "You are an expert at writing prompts for an AI image generator to produce accurate, "
    "detailed educational illustrations. You are given curriculum material on a topic. "
    "Write ONE rich, precise image prompt (4-7 sentences) describing exactly what the "
    "illustration should show.\n\n"
    "RULES:\n"
    "- Ground every visual detail in the curriculum material — depict its specific structures, "
    "parts, stages, and terminology, not a generic version.\n"
    "- Be concrete and spatial: describe the main subject, each labelled part and where it sits, "
    "the arrangement/layout, colours, proportions, and any directional flow or connections.\n"
    "- Name the parts that should be visible and labelled, using the exact terms from the material.\n"
    "- Specify a clear composition: single central subject on a plain background, viewed head-on, "
    "everything clearly separated and legible.\n"
    "- Scientifically correct: correct number of parts, correct relationships, correct proportions.\n"
    "- Do NOT include unrelated objects, people, or decorative scenery.\n"
    "- Output ONLY the prompt text — no preamble, no quotes, no explanation."
)


def build_curriculum_image_prompt(subject: str, curriculum_context: str) -> str:
    if not (LANGCHAIN_AVAILABLE and (llm_precise or llm)):
        return subject
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        material = _clean_pdf_artifacts(curriculum_context)[:3000]
        model = llm_precise or llm
        resp = model.invoke([
            SystemMessage(content=_IMAGE_PROMPT_SYSTEM),
            HumanMessage(content=f"Topic: {subject}\n\nCurriculum material:\n{material}"),
        ])
        built = (resp.content if hasattr(resp, "content") else str(resp)).strip()
        # Precision-focused style suffix (positive cues that steer Qwen toward a clean diagram)
        return (
            f"{built} "
            "Highly detailed, accurate scientific educational diagram, textbook illustration "
            "style, clearly labelled parts, clean flat vector art, plain white background, "
            "sharp lines, high clarity, correct proportions, well-composed and uncluttered, "
            "suitable for a secondary school classroom."
        )
    except Exception as exc:
        print(f"[image] curriculum prompt build failed: {exc}")
        return subject


def build_general_knowledge_answer(message: str, history_text: str = "") -> str:
    """Answer from the model's own knowledge. Never returns empty just because the
    question is a reasoning or maths question, and uses the session transcript so
    follow-up questions resolve."""
    lowered = (message or "").lower()
    query = build_web_results_query(message)

    if "wikipedia" in lowered or "wiki" in lowered:
        return fetch_wikipedia_summary(query)
    if looks_like_web_query(message):
        return fetch_web_results(query)

    if not LANGCHAIN_AVAILABLE:
        return ""

    from langchain_core.messages import SystemMessage, HumanMessage

    wants_detail = any(w in lowered for w in
                       ("in detail", "detailed", "in depth", "comprehensive",
                        "full", "explain fully", "elaborate", "thorough"))
    depth = ("Give a COMPLETE, thorough, well-structured answer with Markdown headings, "
             "covering background, key concepts, examples, and a short summary. Finish "
             "every section fully." if wants_detail
             else "Answer clearly and accurately in a few well-organized paragraphs.")

    system = (
        "You are Nexa, a knowledgeable educational assistant for PNG students. "
        "Answer accurately from your own knowledge in clean Markdown. " + depth +
        " Wrap any maths in $...$ or $$...$$ and write every power in LaTeX as x^{n}, "
        "never as x4 or x**4. "
        "NEVER comment on what any curriculum material, document, or excerpt does or "
        "does not contain. The student must never see phrases like 'the material only "
        "mentions' or 'there is no information provided in the curriculum'. "
        "If genuinely unsure of a fact, say so briefly rather than inventing details."
    )

    human = message
    if history_text:
        human = (f"Conversation so far (for context only):\n{history_text}\n\n"
                 f"Current question: {message}")

    # Try the OpenAI model first, then the local model. A reasoning or maths
    # question is answered here like any other, never refused.
    for model in (llm_general, llm):
        if model is None:
            continue
        try:
            resp = model.invoke([SystemMessage(content=system),
                                 HumanMessage(content=human)])
            text = (resp.content if hasattr(resp, "content") else str(resp)).strip()
            if text and not looks_like_model_refusal(text):
                return strip_curriculum_meta(text) or text
        except Exception as exc:
            print(f"[general] model call failed: {exc}")
    return ""


def solve_simple_reasoning_question(message: str) -> str:
    text = (message or "").strip()
    if not text:
        return ""

    lowered = text.lower()
    match = re.search(r"\ball but\s+(\d+)\b", lowered)
    if not match:
        return ""

    if any(trigger in lowered for trigger in ("how many", "how much", "left", "remain", "remaining", "stay", "sheep", "die")):
        number_left = match.group(1)
        return (
            f"The answer is {number_left}. "
            f"Because 'all but {number_left}' means every one except {number_left} is gone, so {number_left} are left."
        )

    return ""


def looks_like_image_generation_request(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False

    image_patterns = (
        r"\bimage\b", r"\bimages\b", r"\bimahe\b", r"\bpicture\b", r"\bpictures\b",
        r"\bphoto\b", r"\bphotos\b", r"\billustration\b", r"\billustrations\b",
        r"\bdiagram\b", r"\bdiagrams\b", r"\bdrawing\b", r"\bdrawings\b",
        r"\bsketch\b", r"\bsketches\b", r"\bpainting\b", r"\bpaintings\b",
        r"\bposter\b", r"\bposters\b", r"\bgraphic\b", r"\bgraphics\b",
        r"\bvisual\b", r"\bvisuals\b", r"\bartwork\b", r"\bportra(it|its)\b",
    )
    generation_patterns = (
        r"\b(generate|create|make|draw|design|produce|build)\b.*\b(image|picture|photo|illustration|diagram|drawing|sketch|painting|poster|graphic|visual|artwork|portrait)\b",
        r"\b(generate|create|make|draw|design|produce|build)\b.*\b(rose|flower|tree|sun|cat|dog|mountain|landscape|scene)\b",
    )

    if any(re.search(pattern, text) for pattern in image_patterns):
        return True

    if any(re.search(pattern, text) for pattern in generation_patterns):
        return True

    return False

RAG_WEAK_SIGNALS = (
    "i don't have", "i do not have", "not in the curriculum",
    "not mentioned", "cannot find", "can't find", "no information",
    "does not contain", "doesn't contain", "not provided in",
    "not available in the", "i'm not sure", "i am not sure",
    "no relevant information", "unable to find", "the curriculum does not",
    "the provided excerpts", "out of scope",
)

def rag_answer_is_weak(answer: str) -> bool:
    """True when the RAG chain effectively didn't find an answer in the PDFs."""
    text = (answer or "").strip().lower()
    if len(text) < 40:
        return True
    return any(signal in text for signal in RAG_WEAK_SIGNALS)

NEXA_FAQ_ANSWERS = {
    "what is nexa ai": "NEXA AI is an educational AI assistant designed to support students, teachers, schools, and the Department of Education in Papua New Guinea.",
    "what are you": "NEXA AI is an educational AI assistant designed to support students, teachers, schools, and the Department of Education in Papua New Guinea.",
    "who are you": "NEXA AI is an educational AI assistant designed to support students, teachers, schools, and the Department of Education in Papua New Guinea.",
    "what can you do": "I can answer curriculum and education questions, explain difficult topics, help with homework, support teachers, generate lesson plans and quizzes, and help you find the right FAQ answer.",
    "who created nexa ai": "NEXA AI was developed by the engineering team at PowerX Technologies as part of the EduNeX Digital Education Ecosystem.",
    "who made you": "NEXA AI was developed by the engineering team at PowerX Technologies as part of the EduNeX Digital Education Ecosystem.",
    "who owns nexa ai": "NEXA AI is part of the EduNeX platform and is managed by its authorized operators and partners.",
    "who is behind your creation": "NEXA AI is being developed under the leadership of Chandana Silva, with Yasaru Rathnasooriya leading the AI Engineering Team at PowerX Technologies. Together with a team of engineers, curriculum specialists, and stakeholders from the National Department of Education, they are building a next-generation AI-powered educational platform designed to transform teaching and learning across Papua New Guinea.",
    "where were you created": "NEXA AI was developed within the PowerX AI Lab for educational use in PNG.",
    "where are you from": "NEXA AI was developed within the PowerX AI Lab for educational use in PNG.",
    "why were you created": "I was created to improve access to quality education and support teaching and learning across Papua New Guinea. My primary mission is to assist students, teachers, and schools, particularly in remote and underserved communities where access to educational resources, qualified teachers, and learning support may be limited. By providing AI-powered learning assistance, I aim to help ensure that every child has the opportunity to learn, grow, and achieve their full potential.",
    "what is your mission": "To make learning more accessible, engaging, and effective for everyone.",
    "what's your mission": "To make learning more accessible, engaging, and effective for everyone.",
    "what's your purpose": "To make learning more accessible, engaging, and effective for everyone.",
    "tell me about nexa ai": "NEXA AI is an educational AI assistant designed to support students, teachers, schools, and the Department of Education in Papua New Guinea.",
    "what's nexa ai": "NEXA AI is an educational AI assistant designed to support students, teachers, schools, and the Department of Education in Papua New Guinea.",
    "are you a png ai": "Yes. NEXA AI is designed specifically to support the educational needs of Papua New Guinea.",
    "what makes you different from other ai systems": "NEXA AI is tailored to PNG education, curriculum, and local needs.",
    "what languages can you speak": "I can communicate in English and support other languages as configured.",
    "can you understand tok pisin": "Yes, I can assist in Tok Pisin where supported.",
    "can you understand local png languages": "Support may be added as language resources become available.",
    "can you learn new information": "I can be updated with approved knowledge and educational content.",
    "how often are you updated": "Updates are released periodically by administrators.",
    "what information do you know": "I provide information based on my approved knowledge sources.",
    "do you know the png curriculum": "Yes, I am designed to support PNG curriculum-aligned learning.",
    "can you help with stem subjects": "Yes, I can assist with science, technology, engineering, and mathematics.",
    "can you support vocational education": "Yes, I can support vocational and technical learning.",
    "can you help with research": "Yes, I can help students and teachers explore topics and resources.",
    "can you explain difficult concepts": "Yes, I can simplify and explain complex topics.",
    "are you dangerous to humans": "No. I am designed to assist people safely and responsibly.",
    "do you steal information":"No. I do not steal information and follow approved privacy controls.",
    "do you record conversations":"Only authorized systems may store interactions according to policy.",
    "who can see my questions":"Access is controlled by the platform's privacy and security settings.",
    "is my data secure":"Data is protected using approved security measures.",
    "can you access my phone":"No, unless explicitly authorized through an application.",
    "can you access my camera":"No.",
    "can you access my files":"Only if a user intentionally uploads or shares them.",
    "can you access my bank account":"No.",
    "can you access social media accounts":"No.",
    "can you be hacked":"Like any digital system, security measures are required to protect against threats.",
    "can I trust everything you say":"No. Important information should always be verified.",
    "what if you make a mistake":"Consult a teacher, expert, or trusted source to verify the answer.",
    "how do you protect children":"By following safety guidelines and educational safeguards.",
    "are you safe for students":"Yes, when used appropriately and under school policies.",
    "can you replace teachers":"No. Teachers remain essential to education. I’m only a digital tool.",
    "can you mark assignments":"I can assist, but final assessment should be overseen by teachers.",
    "can you help with homework":"Yes.",
    "can you write essays for me":"I can help you learn and draft ideas, but students should do their own work.",
    "can you solve mathematics problems":"Yes, and explain the steps.",
    "can you explain science concepts":"Yes.",
    "can you help me prepare for exams":"Yes.",
    "can you create lesson plans":"Yes, for teachers.",
    "can you generate quizzes":"Yes, for teachers.",
    "can you help teachers prepare notes":"Yes.",
    "can you support special-needs learners":"Yes, where suitable accommodations are available.",
    "can you work offline":"Yes, EduNeX supports offline learning in remote environments.",
    "can you help schools without internet":"Yes, through offline and synchronized deployments.",
    "how does NEXA support remote communities":"By providing access to educational resources even in low-connectivity areas.",
    "what is the future vision of NEXA AI":"To provide safe connectivity, smarter learning, and equitable access to quality education across PNG.",
    "why was NEXA created":"NEXA was created from the vision of Menuka Silva and Chandana Silva, who recognized the opportunity to use modern artificial intelligence to address the unique educational challenges of Papua New Guinea. Drawing on their extensive experience in education and technology, they envisioned a locally relevant AI platform that could support PNG students, teachers, schools, and the Department of Education while helping to improve educational outcomes nationwide.",
    "is NEXA AI a PNG-developed AI":"Yes. NEXA AI is being developed specifically to support the educational needs of Papua New Guinea and is designed around the PNG curriculum, educational goals, and local challenges.",
    "is NEXA AI owned by the Government":"No. NEXA AI is developed by PowerX Technologies as part of the EduNeX Digital Education Ecosystem. It works in partnership with educational stakeholders and government agencies where appropriate.",
    "is NEXA AI connected to the Department of Education":"NEXA AI is being developed to support educational initiatives and may integrate with programs approved by the National Department of Education.",
    "what makes NEXA AI different from ChatGPT, Gemini, or Copilot":"NEXA AI is specifically designed for Papua New Guinea. It focuses on the PNG curriculum, local educational needs, remote learning challenges, and supporting teachers and students throughout the country.",
    "why does PNG need its own AI model":"Papua New Guinea has unique educational, cultural, linguistic, and geographical challenges. A locally focused AI can provide more relevant and effective support for students, teachers, and schools.",
    "can NEXA understand PNG culture and traditions":"Yes. NEXA is being designed to respect and support the diverse cultures, traditions, and values of Papua New Guinea.",
    "can NEXA understand Tok Pisin":"Yes Support for Tok Pisin is part of the long-term vision for NEXA AI.",
    "can NEXA support local PNG languages":"As the platform evolves, support for additional PNG languages may be introduced where resources and linguistic data are available.",
    "how will NEXA continue to improve":"NEXA will continue to improve through ongoing development, curriculum updates, user feedback, and advances in artificial intelligence technology.",
    "what AI technology powers NEXA":"NEXA uses modern artificial intelligence technologies, including large language models, machine learning, and educational knowledge systems.",
    "does NEXA use Large Language Models (LLMs)":"Yes. NEXA leverages advanced language models to understand questions and generate helpful responses.",
    "does NEXA have access to the internet":"Depending on the deployment model, NEXA may operate online, offline, or in a hybrid environment.",
    "how does NEXA find answers":"NEXA generates answers using its trained knowledge base, educational resources, and approved information sources.",
    "does NEXA learn from users":"NEXA may improve through approved updates and training processes while maintaining privacy and security standards.",
    "can NEXA generate images":"Yes. NEXA can generate educational related image generation and visual learning resources.",
    "can NEXA create lesson plans automatically":"Yes. NEXA can assist teachers in preparing lesson plans aligned with curriculum requirements.",
    "can NEXA create quizzes and examinations":"Yes. NEXA can generate quizzes, practice tests, and assessment materials.",
    "can NEXA mark assignments":"NEXA can assist with marking and feedback, but final assessment decisions should be made by teachers.",
    "can NEXA provide personalized learning":"Yes. NEXA is designed to support personalized learning based on the needs and progress of individual students.",
    "is my information confidential":"Yes. NEXA follows approved privacy and security practices to protect user information.",
    "where is NEXA data stored":"Data storage depends on deployment requirements and may be hosted locally, in the cloud, or within DoE approved educational infrastructure.",
    "can parents see student conversations":"Access permissions are determined by school policies and administrative settings.",
    "does NEXA collect personal information":"Only information necessary to provide educational services and platform functionality is collected.",
    "can schools control access to NEXA":"Yes. Schools can manage user accounts, permissions, and access settings.",
    "how does NEXA protect children online":"NEXA includes safeguards designed to promote safe, responsible, and age-appropriate learning experiences.",
    "can NEXA identify inappropriate content":"Yes. NEXA is designed to help detect and filter inappropriate content.",
    "can NEXA help prevent cyberbullying":"NEXA can support digital citizenship education and assist schools in promoting safe online interactions.",
    "what happens if someone misuses NEXA":"Schools and administrators can apply policies, monitoring, and disciplinary procedures where necessary.",
    "can NEXA be used safely by young children":"Yes. NEXA is designed to support learners of different ages in a safe and educational manner.",
    "can NEXA help teachers create lesson plans":"Yes. NEXA can assist teachers in preparing engaging and curriculum-aligned lessons.",
    "can NEXA explain difficult concepts in simple language":"Yes. NEXA can simplify complex topics to suit different learning levels.",
    "can NEXA help students prepare for Grade 10 examinations":"Yes. NEXA can provide revision support, practice questions, and learning guidance.",
    "can NEXA help students prepare for Grade 12 examinations":"Yes. NEXA can assist with exam preparation and study planning.",
    "can NEXA support STEM education":"Yes. Supporting science, technology, engineering, and mathematics education is one of NEXA’s core objectives.",
    "can NEXA support vocational and technical education":"Yes. NEXA can assist with vocational, technical, and skills-based learning programs.",
    "can NEXA help students with disabilities":"NEXA aims to support inclusive education and provide accessible learning opportunities wherever possible.",
    "can NEXA recommend learning resources":"Yes. NEXA can suggest relevant resources based on curriculum requirements and learner needs.",
    "can NEXA support teacher professional development":"Yes. NEXA can assist with training materials, educational research, and professional learning resources.",
    "can NEXA help improve learning outcomes":"Yes. By providing personalized support and educational resources, NEXA aims to improve student achievement and engagement.",
    "what is the long-term vision for NEXA AI":"The vision for NEXA AI is to become Papua New Guinea's leading AI-powered educational assistant, providing personalized learning, voice-based support, and inclusive educational services for all students, including those with special learning needs, while helping improve educational outcomes across the nation.",

}

GRADE_RE = re.compile(r"\b(?:grade|gr|year)\s*\.?\s*(9|1[0-3])\b", re.IGNORECASE)
TERM_RE = re.compile(r"\bterm\s*\.?\s*([1-4])\b", re.IGNORECASE)


def parse_lesson_request_scope(message: str) -> dict:
    """Pull grade / term / subject out of the teacher's request."""
    text = (message or "")
    scope = {"grade": "", "term": "", "subject": ""}

    m = GRADE_RE.search(text)
    if m:
        scope["grade"] = f"Grade {m.group(1)}"
    m = TERM_RE.search(text)
    if m:
        scope["term"] = f"Term {m.group(1)}"

    lowered = text.lower()
    for key, subject in SUBJECT_CODE_MAP.items():
        if re.search(rf"\b{re.escape(key)}\b", lowered):
            scope["subject"] = subject
            break
    return scope



def curriculum_coverage_summary() -> str:
    """Human-readable list of what is actually indexed."""
    coverage = {}
    for info in CURRICULUM_INDEX.values():
        if not info.get("subject") or not info.get("grade"):
            continue
        coverage.setdefault((info["subject"], info["grade"]), set()).add(
            info.get("term") or "Term ?")
    if not coverage:
        return "no curriculum documents are currently indexed"
    lines = []
    for (subject, grade) in sorted(coverage):
        terms = ", ".join(sorted(coverage[(subject, grade)]))
        lines.append(f"- {subject}, {grade}: {terms}")
    return "\n".join(lines)


SESSION_PENDING_IMAGE: Dict[str, str] = {}

def normalize_faq_query(message: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", expand_common_contractions(message or "").strip().lower())

_NEXA_FAQ_NORMALIZED_CACHE: Dict[str, str] = {}


def get_faq_normalized() -> Dict[str, str]:
    """Built lazily: normalize_faq_query depends on expand_common_contractions,
    which is defined further down this file."""
    if not _NEXA_FAQ_NORMALIZED_CACHE:
        for question, answer in NEXA_FAQ_ANSWERS.items():
            _NEXA_FAQ_NORMALIZED_CACHE[normalize_faq_query(question)] = answer
    return _NEXA_FAQ_NORMALIZED_CACHE

FAQ_SELF_TOKENS = {
    "nexa", "ai", "you", "your", "yours", "yourself", "u", "ur",
    "this", "bot", "assistant", "system", "app", "tool", "chatbot",
}

FAQ_FILLER_TOKENS = {
    "what", "who", "where", "when", "why", "how", "which", "is", "are",
    "am", "do", "does", "did", "can", "could", "will", "would", "tell",
    "me", "about", "a", "an", "the", "of", "for", "and", "to", "in", "on",
    "made", "make", "created", "create", "built", "build", "owns", "own",
    "behind", "creation", "from", "called", "name", "named", "really",
    "good", "hi", "hello", "hey", "please", "so", "ok", "okay", "thanks",
}


def faq_residual_tokens(message: str) -> list:
    """Words left after removing self-reference and question filler.
    Anything remaining means the user is asking about a real topic."""
    text = re.sub(r"[^a-z0-9\s]", " ", (message or "").lower())
    return [t for t in text.split()
            if t not in FAQ_SELF_TOKENS and t not in FAQ_FILLER_TOKENS]

FAQ_ALIAS_TO_CANONICAL = {
    "tell me about": "tell me about nexa ai",
    "what can you tell me about": "tell me about nexa ai",
    "give me info about": "tell me about nexa ai",
    "give me information about": "tell me about nexa ai",
    "who made you": "who made you",
    "who made": "who made you",
    "who built you": "who made you",
    "what do you do": "what can you do",
    "how do you work": "how does NEXA find answers",
    "what can you do": "what can you do",
    "what are you": "what are you",
    "who are you": "who are you",
    "what's your mission": "what's your mission",
    "what's your purpose": "what's your purpose",
    "where are you from": "where are you from",
}


def canonicalize_faq_query(message: str) -> str:
    normalized = normalize_faq_query(message)
    if not normalized:
        return ""

    for alias, canonical in FAQ_ALIAS_TO_CANONICAL.items():
        alias_normalized = normalize_faq_query(alias)
        if normalized == alias_normalized or normalized.startswith(alias_normalized) or alias_normalized in normalized:
            return canonical

    return normalized


def build_faq_candidate_questions(message: str, limit: int = 12) -> List[str]:
    normalized = canonicalize_faq_query(message)
    if not normalized:
        return []

    candidate_scores: Dict[str, int] = {}
    query_terms = {token for token in normalized.split() if len(token) > 2}

    for question in NEXA_FAQ_ANSWERS.keys():
        score = 0
        question_normalized = normalize_faq_query(question)

        if question_normalized == normalized:
            score += 100
        if question_normalized.startswith(normalized):
            score += 30
        if normalized.startswith(question_normalized):
            score += 20

        question_terms = {token for token in question_normalized.split() if len(token) > 2}
        score += len(query_terms & question_terms) * 6

        for alias, canonical in FAQ_ALIAS_TO_CANONICAL.items():
            alias_normalized = normalize_faq_query(alias)
            if canonical == question and (alias_normalized in normalized or normalized in alias_normalized):
                score += 10

        if score > 0:
            candidate_scores[question] = score

    ordered_candidates = sorted(candidate_scores.items(), key=lambda item: (-item[1], item[0]))
    return [question for question, _ in ordered_candidates[:limit]]


def score_faq_match(query: str, question: str) -> float:
    query_text = normalize_faq_query(query)
    question_text = normalize_faq_query(question)
    if not query_text or not question_text:
        return 0.0

    query_tokens = {token for token in query_text.split() if len(token) > 2}
    question_tokens = {token for token in question_text.split() if len(token) > 2}

    token_overlap = 0.0
    if query_tokens or question_tokens:
        token_overlap = len(query_tokens & question_tokens) / max(len(query_tokens | question_tokens), 1)

    sequence_ratio = difflib.SequenceMatcher(None, query_text, question_text).ratio()
    alias_bonus = 0.0

    for alias, canonical in FAQ_ALIAS_TO_CANONICAL.items():
        if canonical == question:
            alias_text = normalize_faq_query(alias)
            if alias_text and (alias_text in query_text or query_text in alias_text):
                alias_bonus = 0.12
                break

    return (sequence_ratio * 0.7) + (token_overlap * 0.3) + alias_bonus


def find_best_faq_answer(message: str, minimum_score: float = 0.68) -> str:
    normalized = canonicalize_faq_query(message)
    if not normalized:
        return ""

    best_question = ""
    best_score = 0.0

    for question in NEXA_FAQ_ANSWERS.keys():
        score = score_faq_match(normalized, question)
        if score > best_score:
            best_score = score
            best_question = question

    if best_question and best_score >= minimum_score:
        return NEXA_FAQ_ANSWERS[best_question]

    return ""


def expand_common_contractions(text: str) -> str:
    normalized = (text or "").lower()

    contraction_map = [
        (r"\bwhat's\b", "what is"),
        (r"\bwho's\b", "who is"),
        (r"\bwhere's\b", "where is"),
        (r"\bwhen's\b", "when is"),
        (r"\bwhy's\b", "why is"),
        (r"\bhow's\b", "how is"),
        (r"\bit's\b", "it is"),
        (r"\bthat's\b", "that is"),
        (r"\bthere's\b", "there is"),
        (r"\bhere's\b", "here is"),
        (r"\bI'm\b", "i am"),
        (r"\bI've\b", "i have"),
        (r"\bI'll\b", "i will"),
        (r"\bI'd\b", "i would"),
        (r"\byou're\b", "you are"),
        (r"\byou've\b", "you have"),
        (r"\byou'll\b", "you will"),
        (r"\bwe're\b", "we are"),
        (r"\bwe've\b", "we have"),
        (r"\bwe'll\b", "we will"),
        (r"\bthey're\b", "they are"),
        (r"\bthey've\b", "they have"),
        (r"\bthey'll\b", "they will"),
        (r"\bcan't\b", "can not"),
        (r"\bcannot\b", "can not"),
        (r"\bdon't\b", "do not"),
        (r"\bdoesn't\b", "does not"),
        (r"\bdidn't\b", "did not"),
        (r"\bwon't\b", "will not"),
        (r"\bwouldn't\b", "would not"),
        (r"\bshouldn't\b", "should not"),
        (r"\bcouldn't\b", "could not"),
        (r"\bhasn't\b", "has not"),
        (r"\bhaven't\b", "have not"),
        (r"\bhadn't\b", "had not"),
        (r"\blet's\b", "let us"),
    ]

    for pattern, replacement in contraction_map:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)

    phrase_map = [
        (r"\btell me about\b", "what is"),
        (r"\bwhat can you tell me about\b", "what is"),
        (r"\bgive me info about\b", "what is"),
        (r"\bgive me information about\b", "what is"),
        (r"\bwho made you\b", "who created you"),
        (r"\bwho made\b", "who created"),
        (r"\bwho built you\b", "who created you"),
        (r"\bwhat do you do\b", "what is your mission"),
        (r"\bhow do you work\b", "how does NEXA find answers"),
        (r"\bwhat can you do\b", "what can you help with"),
    ]

    for pattern, replacement in phrase_map:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)

    return normalized

def llm_to_sympy(message: str):
    """Ask the LLM ONLY to translate the problem into SymPy code, then execute it
    ourselves for an exact answer. The model never does the arithmetic."""
    if not (LANGCHAIN_AVAILABLE and llm is not None):
        return None

    translate_prompt = ChatPromptTemplate.from_messages([
        ("system",
         "You convert a maths problem into a single line of Python SymPy code that computes "
         "the answer. Output ONLY the code, no explanation, no markdown, no backticks.\n"
         "Rules:\n"
         "- Use SymPy names directly (Symbol, integrate, solve, diff, cos, sin, exp, sqrt, Eq, pi, etc).\n"
         "- Define symbols with Symbol('x') or Symbol('t').\n"
         "- The final line MUST assign the answer to a variable named RESULT.\n"
         "- For a definite integral of f from a to b: RESULT = integrate(f, (x, a, b))\n"
         "- For solving an equation: RESULT = solve(Eq(lhs, rhs), x)\n"
         "Example problem: 'integral of 40*t*exp(-0.05*t) from 0 to 30'\n"
         "Example output: t = Symbol('t'); RESULT = integrate(40*t*exp(-0.05*t), (t, 0, 30))"),
        ("human", "{problem}"),
    ])

    try:
        code = (translate_prompt | llm | StrOutputParser()).invoke({"problem": message}).strip()
        code = code.replace("```python", "").replace("```", "").strip()

        # Namespace: all SymPy names plus a safe subset of builtins the code may need.
        ns = {name: getattr(sympy, name) for name in dir(sympy) if not name.startswith("_")}
        ns["Symbol"] = sympy.Symbol
        safe_builtins = {
            "abs": abs, "range": range, "min": min, "max": max, "sum": sum,
            "int": int, "float": float, "len": len, "round": round,
            "list": list, "tuple": tuple, "print": print, "pow": pow,
        }
        ns["__builtins__"] = safe_builtins

        exec(code, ns)

        result = ns.get("RESULT")
        if result is None:
            return None

        exact = result if isinstance(result, list) else sympy.simplify(result)
        approx = None
        try:
            approx = sympy.N(exact, 6)
        except Exception:
            pass

        latex = sympy.latex(exact)
        plain = f"{exact}" + (f" ≈ {approx}" if approx is not None else "")
        return (f"$${latex}$$", plain)
    except Exception as e:
        print(f"[info] LLM->SymPy translation failed: {e}")
        return None


FAQ_SELF_TOKENS = {
    "nexa", "ai", "you", "your", "yours", "yourself", "u", "ur",
    "this", "bot", "assistant", "system", "app", "tool", "chatbot",
}

FAQ_FILLER_TOKENS = {
    "what", "who", "where", "when", "why", "how", "which", "is", "are",
    "am", "do", "does", "did", "can", "could", "will", "would", "tell",
    "me", "my", "about", "a", "an", "the", "of", "for", "and", "to",
    "in", "on", "made", "make", "created", "create", "built", "build",
    "owns", "own", "behind", "creation", "from", "called", "name",
    "named", "really", "good", "hi", "hello", "hey", "please", "so",
    "ok", "okay", "thanks", "there", "any",
}

NEXA_SELF_QUESTION_PATTERNS = (
    r"\bnexa\b",
    r"\bedunex\b",
    r"\bthis (?:ai|assistant|bot|chatbot|app|tool|system)\b",
    r"\b(?:what|who) (?:are|is) you\b",
    r"\bwho (?:made|created|built|owns|designed|developed) you\b",
    r"\bwho is behind\b",
    r"\bwhere (?:are you from|were you (?:created|made|built))\b",
    r"\bwhy (?:were|was) you (?:created|made|built)\b",
    r"\bwhat(?: is)? your (?:mission|purpose|name|goal|vision)\b",
    r"\bhow do you work\b",
)


def faq_residual_tokens(message: str) -> list:
    """Words left after stripping self-reference and question filler.
    Anything remaining means the user is asking about a real topic."""
    text = re.sub(r"[^a-z0-9\s]", " ", (message or "").lower())
    return [t for t in text.split()
            if t not in FAQ_SELF_TOKENS and t not in FAQ_FILLER_TOKENS]


def query_is_about_nexa(message: str) -> bool:
    """Open the FAQ gate only for genuine questions about the assistant.

    A bare 'you' or 'can' is NOT enough - those appear in almost every
    student question ('can you explain metabolism'), which is what made
    curriculum questions return the canned NEXA AI description.
    """
    text = expand_common_contractions(message or "").lower()

    if any(re.search(pattern, text) for pattern in NEXA_SELF_QUESTION_PATTERNS):
        return True

    return not faq_residual_tokens(message)

def build_nexa_faq_answer(message: str, session_id: Optional[str] = None) -> str:
    if not query_is_about_nexa(message):
        return ""

    normalized = canonicalize_faq_query(message)
    if not normalized:
        return ""

    faq_map = get_faq_normalized()

    # Exact match always wins.
    if normalized in faq_map:
        return faq_map[normalized]

    residual = faq_residual_tokens(message)

    if residual:
        # Message carries a real topic (nutrition, metabolism, algebra...).
        # Only a near-exact FAQ match may answer it. Otherwise hand off to RAG.
        best_question, best_score = "", 0.0
        for question in faq_map:
            score = score_faq_match(normalized, question)
            if score > best_score:
                best_score, best_question = score, question
        if best_question and best_score >= 0.90:
            return faq_map[best_question]
        return ""

    # Purely self-referential ("what are you", "who made you") - loose match OK.
    for question, answer in faq_map.items():
        if normalized.startswith(question) or question in normalized:
            return answer

    return find_best_faq_answer(normalized, minimum_score=0.82)


def record_chat_turn(session_id: str, role: str, content: str) -> None:
    if session_id not in SESSION_CHAT_HISTORY:
        SESSION_CHAT_HISTORY[session_id] = []

    SESSION_CHAT_HISTORY[session_id].append({"role": role, "content": content})


def serialize_chat_history(session_id: str):
    return SESSION_CHAT_HISTORY.get(session_id, [])


def ensure_chat_log_schema():
    if mysql.connector is None:
        return

    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        cur.execute("SHOW COLUMNS FROM nexa_chat_logs LIKE 'session_id'")
        if cur.fetchone() is None:
            cur.execute("ALTER TABLE nexa_chat_logs ADD COLUMN session_id VARCHAR(64) NULL AFTER log_id")

        cur.execute("SHOW COLUMNS FROM nexa_chat_logs LIKE 'user_email'")
        if cur.fetchone() is None:
            cur.execute("ALTER TABLE nexa_chat_logs ADD COLUMN user_email VARCHAR(255) NULL AFTER user_name")
        
        cur.execute("SHOW COLUMNS FROM nexa_chat_logs LIKE 'image_base64'")
        if cur.fetchone() is None:
            cur.execute("ALTER TABLE nexa_chat_logs ADD COLUMN image_base64 LONGTEXT NULL AFTER nexa_response")
        
        cur.execute("SHOW COLUMNS FROM nexa_chat_logs LIKE 'image_blob'")
        if cur.fetchone() is None:
            cur.execute("ALTER TABLE nexa_chat_logs ADD COLUMN image_blob LONGBLOB NULL AFTER image_base64")
        
        cur.execute("SHOW COLUMNS FROM nexa_chat_logs LIKE 'image_mime_type'")
        if cur.fetchone() is None:
            cur.execute("ALTER TABLE nexa_chat_logs ADD COLUMN image_mime_type VARCHAR(100) NULL AFTER image_blob")
        
        cur.execute("SHOW COLUMNS FROM nexa_chat_logs LIKE 'image_filename'")
        if cur.fetchone() is None:
            cur.execute("ALTER TABLE nexa_chat_logs ADD COLUMN image_filename VARCHAR(255) NULL AFTER image_mime_type")
        
        cur.execute("SHOW COLUMNS FROM nexa_chat_logs LIKE 'image_saved_at'")
        if cur.fetchone() is None:
            cur.execute("ALTER TABLE nexa_chat_logs ADD COLUMN image_saved_at DATETIME NULL AFTER image_filename")

        cur.execute("SHOW COLUMNS FROM nexa_chat_logs LIKE 'pdf_url'")
        if cur.fetchone() is None:
            cur.execute("ALTER TABLE nexa_chat_logs ADD COLUMN pdf_url VARCHAR(255) NULL AFTER image_saved_at")


        cur.execute("""
            CREATE TABLE IF NOT EXISTS nexa_shared_chats (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                share_token VARCHAR(128) NOT NULL,
                session_id VARCHAR(64) NOT NULL,
                created_by_email VARCHAR(255) NULL,
                created_at_utc DATETIME NOT NULL,
                expires_at_utc DATETIME NULL,
                is_active TINYINT(1) NOT NULL DEFAULT 1,
                PRIMARY KEY (id),
                UNIQUE KEY uq_share_token (share_token),
                KEY idx_session_id (session_id),
                KEY idx_created_by_email (created_by_email),
                KEY idx_is_active (is_active)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        conn.commit()
    except Exception as exc:
        print(f"Failed to ensure chat log schema: {exc}")
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


def fetch_chat_history_rows(session_id: str):
    if mysql.connector is None:
        return []

    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT log_id, user_name, user_prompt, nexa_response, image_base64, image_mime_type, image_filename, pdf_url, timestamp_utc
            FROM nexa_chat_logs
            WHERE session_id = %s
            ORDER BY timestamp_utc ASC, id ASC
            """,
            (session_id,),
        )
        return cur.fetchall() or []
    except Exception as exc:
        print(f"Failed to fetch chat history from database: {exc}")
        return []
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


def hydrate_session_history(session_id: str) -> None:
    if not session_id or mysql.connector is None:
        return

    session_messages = SESSION_CHAT_HISTORY.setdefault(session_id, [])

    session_history = SESSION_STORE.get(session_id) if LANGCHAIN_AVAILABLE else None
    if LANGCHAIN_AVAILABLE and session_history is None:
        SESSION_STORE[session_id] = ChatMessageHistory()
        session_history = SESSION_STORE[session_id]

    if LANGCHAIN_AVAILABLE and session_history is not None and getattr(session_history, "messages", None):
        return

    if session_messages:
        if session_history is not None and not getattr(session_history, "messages", None):
            for message in session_messages:
                role = (message.get("role") or "").strip().lower()
                content = (message.get("content") or "").strip()
                if not content:
                    continue

                if role == "user":
                    session_history.add_message(HumanMessage(content=content))
                elif role == "assistant":
                    session_history.add_message(AIMessage(content=content))
        return

    rows = fetch_chat_history_rows(session_id)
    if not rows:
        return

    for row in rows:
        user_prompt = (row.get("user_prompt") or "").strip()
        nexa_response = (row.get("nexa_response") or "").strip()

        if user_prompt:
            SESSION_CHAT_HISTORY[session_id].append({"role": "user", "content": user_prompt})
            if session_history is not None:
                session_history.add_message(HumanMessage(content=user_prompt))

        if nexa_response:
            SESSION_CHAT_HISTORY[session_id].append({"role": "assistant", "content": nexa_response})
            if session_history is not None:
                session_history.add_message(AIMessage(content=nexa_response))


def fetch_user_chat_sessions(user_email: str):
    normalized_email = normalize_email_address(user_email)
    if mysql.connector is None or not normalized_email:
        return []

    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT session_id, MAX(timestamp_utc) AS last_activity, COUNT(*) AS message_count
            FROM nexa_chat_logs
            WHERE user_email = %s
              AND session_id IS NOT NULL
              AND session_id <> ''
            GROUP BY session_id
            ORDER BY last_activity DESC, session_id DESC
            LIMIT 20
            """,
            (normalized_email,),
        )
        rows = cur.fetchall() or []
        sessions = []
        for row in rows:
            last_activity = row.get("last_activity")
            sessions.append(
                {
                    "session_id": row.get("session_id") or "",
                    "last_activity": last_activity.isoformat() if hasattr(last_activity, "isoformat") else str(last_activity),
                    "message_count": int(row.get("message_count") or 0),
                }
            )
        return sessions
    except Exception as exc:
        print(f"Failed to fetch user chat sessions: {exc}")
        return []
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


def _truncate_search_snippet(text: str, limit: int = 140) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def search_user_chat_sessions(user_email: str, query: str, limit: int = 10):
    normalized_email = normalize_email_address(user_email)
    cleaned_query = (query or "").strip()
    if mysql.connector is None or not normalized_email or not cleaned_query:
        return []

    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor(dictionary=True)
        like_query = f"%{cleaned_query}%"
        cur.execute(
            """
            SELECT session_id, user_prompt, nexa_response, timestamp_utc
            FROM nexa_chat_logs
            WHERE user_email = %s
              AND session_id IS NOT NULL
              AND session_id <> ''
              AND (user_prompt LIKE %s OR nexa_response LIKE %s)
            ORDER BY timestamp_utc DESC, id DESC
            LIMIT 200
            """,
            (normalized_email, like_query, like_query),
        )
        rows = cur.fetchall() or []
        if not rows:
            return []

        session_meta = {item["session_id"]: item for item in fetch_user_chat_sessions(normalized_email)}
        seen_sessions = set()
        matches = []
        lowered_query = cleaned_query.lower()

        for row in rows:
            session_id = row.get("session_id") or ""
            if not session_id or session_id in seen_sessions:
                continue

            prompt = (row.get("user_prompt") or "").strip()
            response = (row.get("nexa_response") or "").strip()
            if lowered_query in prompt.lower():
                snippet_source = prompt
            elif lowered_query in response.lower():
                snippet_source = response
            else:
                snippet_source = prompt or response

            last_activity = row.get("timestamp_utc")
            meta = session_meta.get(session_id, {})

            matches.append(
                {
                    "session_id": session_id,
                    "last_activity": (meta.get("last_activity") if meta else None) or (last_activity.isoformat() if hasattr(last_activity, "isoformat") else str(last_activity)),
                    "message_count": int((meta.get("message_count") if meta else 0) or 0),
                    "snippet": _truncate_search_snippet(snippet_source),
                }
            )
            seen_sessions.add(session_id)

            if len(matches) >= limit:
                break

        return matches
    except Exception as exc:
        print(f"Failed to search user chat sessions: {exc}")
        return []
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


def normalize_email_address(email: Optional[str]) -> str:
    return (email or "").strip().lower()




def session_belongs_to_user(session_id: str, user_email: Optional[str]) -> bool:
    normalized_email = normalize_email_address(user_email)

    if not normalized_email:
        return True

    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*)
            FROM nexa_chat_logs
            WHERE session_id = %s
              AND user_email = %s
            """,
            (session_id, normalized_email),
        )
        count = cur.fetchone()[0]
        return count > 0
    except Exception as exc:
        print(f"Failed to verify chat ownership: {exc}")
        return False
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


def create_share_record(session_id: str, user_email: Optional[str]) -> str:
    share_token = secrets.token_urlsafe(32)
    created_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO nexa_shared_chats
                (share_token, session_id, created_by_email, created_at_utc, is_active)
            VALUES
                (%s, %s, %s, %s, 1)
            """,
            (
                share_token,
                session_id,
                normalize_email_address(user_email),
                created_at,
            ),
        )
        conn.commit()
        return share_token
    finally:
        cur.close()
        conn.close()


def get_share_record(share_token: str):
    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT share_token, session_id, created_at_utc, expires_at_utc, is_active
            FROM nexa_shared_chats
            WHERE share_token = %s
            LIMIT 1
            """,
            (share_token,),
        )
        return cur.fetchone()
    finally:
        cur.close()
        conn.close()


def _load_email_tokens(env_name: str) -> set[str]:
    raw_value = os.getenv(env_name, "")
    return {token.strip().lower() for token in raw_value.split(",") if token.strip()}


def infer_access_role(email: Optional[str]) -> str:
    normalized_email = normalize_email_address(email)
    if not normalized_email or "@" not in normalized_email:
        raise HTTPException(status_code=400, detail="A valid email address is required")
    _, _, domain = normalized_email.partition("@")
    domain = domain.lower()

    # Student domain MUST be checked first — it also ends with edunex.edu.pg,
    # so checking the teacher domain first would misclassify students as teachers.
    if domain == "stu.edunex.edu.pg" or domain.endswith(".stu.edunex.edu.pg"):
        return "student"
    if domain == "edunex.edu.pg" or domain.endswith(".edunex.edu.pg"):
        return "teacher"

    # Fallback: default to student
    return "student"

def build_role_instruction(role: str) -> str:
    if role == "teacher":
        return (
            "Teacher mode: respond as a curriculum-support assistant. "
            "Prioritize lesson structure, pedagogy, assessment, differentiation, and classroom use."
        )

    return (
        "Student mode: respond in clear, simple, supportive language. "
        "Focus on short explanations, examples, and study help without unnecessary teaching detail."
    )


def _is_lesson_request(message: str) -> bool:
    if not message:
        return False
    lower = (message or "").lower()
    return any(phrase in lower for phrase in ("lesson plan", "create lesson", "create a lesson", "make a lesson", "teaching plan"))


def extract_lesson_metadata(text: str) -> dict:
    meta: dict = {}
    if not text:
        return meta
 
    cleaned = strip_output_format_noise(text)
 
    # Grade
    m = re.search(r"\bgrade\s*(\d{1,2})\b", cleaned, flags=re.IGNORECASE)
    if m:
        meta["grade"] = f"Grade {m.group(1)}"
 
    # Duration
    m = re.search(r"(\d+\s*(?:minutes|minute|mins|min|hours|hour|hrs|hr))",
                  cleaned, flags=re.IGNORECASE)
    if m:
        meta["duration"] = m.group(1)
 
    # Topic - now accepts 'related to', 'based on', 'covering', 'for', etc.
    m = re.search(
        r"(?:lesson plan|teaching plan|lesson)\s*"
        r"(?:that is\s+|which is\s+)?"
        r"(?:related to|relating to|based on|regarding|covering|concerning|about|on|for|in)\s+"
        r"(.+?)$",
        cleaned, flags=re.IGNORECASE,
    )
    if m:
        topic = m.group(1).strip(" ?.!,")
        # Trim trailing qualifiers: 'for grade 11', 'for 40 minutes'
        topic = re.split(r"\s+for\s+(?:grade|year|\d)", topic, flags=re.IGNORECASE)[0]
        topic = re.sub(r"\s*\d+\s*(?:minutes|minute|mins|min|hours|hour|hrs|hr)\s*$",
                       "", topic, flags=re.IGNORECASE).strip(" ?.!,")
        if topic and 2 < len(topic) <= 80:
            meta["topic"] = topic
            meta["subject"] = topic
 
    # Prerequisite
    m = re.search(r"prereq(?:uisite)?s?:?\s*([^,\.\n]+)", cleaned, flags=re.IGNORECASE)
    if m:
        meta["prerequisite"] = m.group(1).strip()
 
    return meta


_LEADING_CHATTER_RE = re.compile(
    r"^\s*(?:"
    r"(?:sure|certainly|of course|absolutely|great|okay|ok|alright|no problem)\b"
    r"|here(?:'s| is| are)\b"
    r"|below (?:is|are)\b"
    r"|i(?:'ll| will| have|'ve| can)?\s*(?:create|created|prepare|prepared|write|"
    r"wrote|written|draft|drafted|put together|made|make)\b"
    r"|(?:based|building) on (?:our|the|your|previous|prior)\b"
    r"|i think there(?:'s| is)\b"
    r"|it (?:looks|seems|appears)\b"
    r"|as (?:an ai|a language model)\b"
    r"|(?:there(?:'s| is) (?:still )?a )?(?:small |slight |minor )?typo\b"
    r"|thank(?:s| you)\b"
    r"|good (?:question|point)\b"
    r"|let me\b"
    r"|happy to\b"
    r")", re.IGNORECASE)

_NAME_ADDRESS_RE = re.compile(r"^\s*[A-Z][A-Za-z'\-]{1,20},\s+")

_CONTENT_START_RE = re.compile(r"^\s*(?:#{1,6}\s|\||[-*+]\s|\d+\.\s|>|\*\*)")


def strip_llm_chatter(text) -> str:
    """Drop preamble/postamble so only the document survives."""
    body = (text if isinstance(text, str) else str(text or "")).strip()
    if not body:
        return ""
    body = re.sub(r"^```(?:markdown|md)?\s*\n", "", body)
    body = re.sub(r"\n```\s*$", "", body)

    # Drop leading conversational blocks before any real content starts.
    blocks = re.split(r"\n\s*\n", body)
    while blocks:
        head = blocks[0].strip()
        if not head:
            blocks.pop(0)
            continue
        if _CONTENT_START_RE.match(head):
            break
        candidate = _NAME_ADDRESS_RE.sub("", head)
        if _LEADING_CHATTER_RE.match(candidate):
            blocks.pop(0)
            continue
        # Short block ending in a colon is an intro line ("Here's a note:").
        if len(candidate) <= 220 and candidate.rstrip().endswith(":"):
            blocks.pop(0)
            continue
        break
    body = "\n\n".join(blocks).strip()

    # A leading bold-only line is the real title - promote it to H1.
    body = re.sub(r"\A\s*\*\*([^*\n]{3,80})\*\*\s*$", r"# \1", body,
                  count=1, flags=re.MULTILINE)

    # Everything before the first H1 is preamble, unless it's already real markdown
    match = re.search(r"^#\s+\S.*$", body, flags=re.MULTILINE)
    if match and match.start() > 0:
        head = body[:match.start()]
        if not re.search(r"^\s*(?:#{1,6}\s|\||[-*]\s|\d+\.\s)", head, flags=re.MULTILINE):
            body = body[match.start():]

    # Trailing sign-off paragraphs
    tail = re.compile(
        r"^\s*(?:i hope|hope this|let me know|feel free|if you (?:need|have|want)|"
        r"please (?:note|let)|would you like|do you (?:need|want)|good luck)\b",
        re.IGNORECASE,
    )
    blocks = re.split(r"\n\s*\n", body)
    while blocks and (not blocks[-1].strip() or tail.match(blocks[-1])):
        blocks.pop()
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(blocks)).strip()

def _norm_heading(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (text or "").lower()).strip()


def split_markdown_sections(md: str) -> dict:
    """{normalized heading: body} for every ## / ### section in the draft."""
    sections, current, buf = {}, None, []
    for line in (md or "").split("\n"):
        m = re.match(r"^\s*#{2,3}\s+(.+?)\s*$", line)
        if m:
            if current:
                sections.setdefault(current, "\n".join(buf).strip())
            current, buf = _norm_heading(m.group(1)), []
        elif current is not None:
            buf.append(line)
    if current:
        sections.setdefault(current, "\n".join(buf).strip())
    return sections


_SECTION_ALIASES = {
    "overview":        ["overview", "lesson overview", "introduction", "summary"],
    "objectives":      ["learning objectives", "objectives", "what students will learn", "aims"],
    "vocabulary":      ["key concepts and vocabulary", "key vocabulary", "vocabulary", "key concepts"],
    "materials":       ["materials", "resources", "materials and resources", "teaching aids"],
    "structure":       ["lesson structure", "lesson flow", "procedure", "lesson sequence"],
    "differentiation": ["differentiation", "support and extension", "differentiation strategies"],
    "assessment":      ["assessment", "evaluation", "assessment and evaluation"],
    "homework":        ["extension and homework", "homework", "homework and reflection", "extension"],
    "notes":           ["teacher notes", "notes", "teacher tips"],
}


def pick_section(sections: dict, key: str) -> str:
    aliases = [_norm_heading(a) for a in _SECTION_ALIASES.get(key, [])]
    for alias in aliases:
        if sections.get(alias, "").strip():
            return sections[alias].strip()
    for heading, body in sections.items():
        if body.strip() and any(alias and alias in heading for alias in aliases):
            return body.strip()
    return ""


def extract_lesson_body(md: str) -> str:
    """Model's Lesson Structure section including its ### subsections."""
    match = re.search(r"^##\s*Lesson Structure\s*$", md or "", flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        return ""
    rest = md[match.end():]
    nxt = re.search(r"^##\s+\S", rest, flags=re.MULTILINE)
    return (rest[:nxt.start()] if nxt else rest).strip()

def normalize_lesson_plan_format(answer: str, request_message: str) -> str:
    if not _is_lesson_request(request_message):
        return answer

    draft = strip_llm_chatter(answer)
    sections = split_markdown_sections(draft)

    meta = extract_lesson_metadata(strip_output_format_noise(request_message))
    default_value = "Not specified"
    topic = meta.get("topic") or default_value
    duration_text = meta.get("duration") or "40 minutes"

    # ---- timing split across the lesson-structure stages ----
    total = None
    m = re.search(r"(\d+)", duration_text)
    if m:
        total = int(m.group(1))
    if total and total >= 10:
        intro = max(3, round(total * 0.12))
        direct = max(8, round(total * 0.30))
        guided = max(8, round(total * 0.30))
        independent = max(5, round(total * 0.18))
        closure = max(3, total - (intro + direct + guided + independent))
    else:
        intro, direct, guided, independent, closure = 5, 15, 10, 7, 3

    # ---- title: model H1 if present, else topic ----
    title_match = re.search(r"^#\s+(.+)$", draft, flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else (
        topic if topic != default_value else "Lesson Plan")

    def block(key: str, fallback: str) -> str:
        return pick_section(sections, key) or fallback

    default_flow = (
        "| Stage | Time | Teacher does | Students do |\n"
        "|---|---:|---|---|\n"
        f"| Warm-up | {intro} min | Open with a question or quick review to activate prior knowledge. | Share ideas and recall what they know. |\n"
        f"| Teach | {direct} min | Explain the main concept with clear examples and checks for understanding. | Listen, note key ideas, answer short questions. |\n"
        f"| Guided Practice | {guided} min | Model one task and support students through it. | Work with the teacher and complete the guided task. |\n"
        f"| Independent Practice | {independent} min | Set an application task and monitor progress. | Work alone or in pairs to show understanding. |\n"
        f"| Wrap-up | {closure} min | Summarise and finish with a quick exit check. | Reflect and answer the closing question. |"
    )

    parts = [
        f"# {title}",
        "\n## Lesson Overview\n" + block(
            "overview",
            "A short explanation of what this lesson covers, why it matters, and how it "
            "connects to what students already know."),
        "\n## What Students Will Learn\n" + block(
            "objectives",
            "- Understand the key idea behind the topic\n"
            "- Use the important vocabulary correctly\n"
            "- Apply the idea in a guided activity\n"
            "- Show understanding in a short check for learning"),
        "\n## Key Concepts and Vocabulary\n" + block(
            "vocabulary", "- Key terms for this topic, with a short definition for each"),
        "\n## Materials\n" + block(
            "materials",
            "- Whiteboard or slides\n- Markers or pen\n- Student workbook or handout\n"
            "- Any demonstration items or digital resources"),
        "\n## Lesson Flow\n" + (extract_lesson_body(draft) or default_flow),
        "\n## Assessment\n" + block(
            "assessment",
            "- Formative: questioning, quick recap, or mini whiteboard check\n"
            "- Summative: a short task, quiz, or exit ticket covering the main skill\n"
            "- Success criteria: students can explain the idea and complete the task"),
        "\n## Support and Extension\n" + block(
            "differentiation",
            "- Support: sentence starters, visuals, worked examples, partner support\n"
            "- Core: scaffolded practice with clear steps\n"
            "- Extension: challenge questions or an independent task"),
        "\n## Homework / Reflection\n" + block(
            "homework",
            "- One short practice task or reflection question\n"
            "- Optional extension for students who finish early"),
        "\n## Teacher Notes\n" + block(
            "notes",
            "- Common misconceptions to watch for\n- Pacing and classroom management tips\n"
            "- Materials, safety notes, or reminders"),
    ]

    return "\n".join(p.strip() for p in parts if p and p.strip())


GENERATION_DISCLAIMER = (
    "DISCLAIMER: This response was generated by NEXA AI. Please verify important information with your official learning resources and consult your teacher or lecturer if needed. NEXA AI supports learning it does not replace your human teachers."
)

def append_generation_disclaimer(answer: str) -> str:
    return answer if isinstance(answer, str) else str(answer or "")

# ====================== LOAD PDFs ======================
docs = []
retriever = None
vectorstore = None
llm = None
rag_chain = None
conversational_rag_chain = None

class PrefixedOllamaEmbeddings(OllamaEmbeddings):
    """nomic-embed-text needs task prefixes, or unrelated topics that share a word
    embed too close and retrieval returns the wrong subject (or nothing)."""
    def embed_documents(self, texts):
        return super().embed_documents([f"search_document: {t}" for t in texts])
    def embed_query(self, text):
        return super().embed_query(f"search_query: {text}")

CHROMA_DIR = os.path.expanduser("~/Nexa/chroma_store")

if LANGCHAIN_AVAILABLE:
    embeddings = PrefixedOllamaEmbeddings(model=EMBED_MODEL)

    # If a persisted store already exists, load it — it contains the base PDFs
    # AND every LMS document ingested since, so a restart keeps all of them.
    if os.path.isdir(CHROMA_DIR) and os.listdir(CHROMA_DIR):
        print("Loading persisted vector store (base PDFs + ingested LMS materials)...")
        try:
            vectorstore = Chroma(
                collection_name="curriculum_db",
                embedding_function=embeddings,
                persist_directory=CHROMA_DIR,
                collection_metadata={"hnsw:space": "cosine"},
            )
            count = vectorstore._collection.count()
            print(f"Loaded persisted store: {count} chunks from {CHROMA_DIR}")
            retriever = vectorstore.as_retriever(search_kwargs={"k": 6})
            llm = ChatOllama(model=MODEL_NAME, temperature=0.4, num_predict=2048, num_ctx=8192)
            llm_precise = ChatOllama(model=MODEL_NAME, temperature=0.1, num_predict=2048, num_ctx=8192)
        except Exception as exc:
            print(f"Persisted store load failed ({exc}) -> rebuilding from PDFs")
            vectorstore = None

    # First run (or load failed): build from the curriculum PDFs and persist.
    if vectorstore is None:
        print(f"Loading {len(PDF_PATHS)} curriculum PDFs...")
        for pdf in PDF_PATHS:
            if not os.path.exists(pdf):
                continue
            info = CURRICULUM_INDEX.get(pdf) or parse_curriculum_filename(pdf)
            try:
                loaded = PyPDFLoader(pdf).load()
            except Exception as exc:
                print(f"  [skip] {os.path.basename(pdf)}: {exc}")
                continue
            for page in loaded:
                page.metadata = dict(page.metadata or {})
                page.metadata.update({
                    "file": info["file"],
                    "subject": info["subject"] or "Unknown",
                    "grade": info["grade"] or "Unknown",
                    "term": info["term"] or "Unknown",
                    "doctype": info["doctype"] or "Unknown",
                })
            docs.extend(loaded)
            print(f"  [ok] {info['file']} -> {info['subject']} / {info['grade']} / "
                  f"{info['term']} / {info['doctype']} ({len(loaded)} pages)")

        if docs:
            text_splitter = RecursiveCharacterTextSplitter(chunk_size=1500, chunk_overlap=300)
            splits = text_splitter.split_documents(docs)
            vectorstore = Chroma.from_documents(
                documents=splits,
                embedding=embeddings,
                collection_name="curriculum_db",
                collection_metadata={"hnsw:space": "cosine"},
                persist_directory=CHROMA_DIR,
            )
            retriever = vectorstore.as_retriever(search_kwargs={"k": 6})
            llm = ChatOllama(model=MODEL_NAME, temperature=0.4, num_predict=2048, num_ctx=8192)
            llm_precise = ChatOllama(model=MODEL_NAME, temperature=0.1, num_predict=2048, num_ctx=8192)
            print(f"Built and persisted {len(splits)} chunks from {len(docs)} pages to {CHROMA_DIR}.")
        else:
            llm_precise = None

# ====================== HISTORY RETRIEVER ======================
if LANGCHAIN_AVAILABLE and retriever is not None and llm is not None:
    contextualize_q_prompt = ChatPromptTemplate.from_messages([
        ("system", "Given the chat history and latest user question, reformulate it as a standalone query about the curriculum."),
        MessagesPlaceholder("chat_history"),
        ("human", "{input}"),
    ])

    history_aware_retriever = create_history_aware_retriever(llm, retriever, contextualize_q_prompt)


system_prompt = (
    "You are an expert curriculum designer and master educator creating professional, "
    "classroom-ready lesson plans for any subject area. "
    "Use the provided curriculum excerpts and prior conversation context to ground your content, "
    "and stay faithful to the curriculum where it applies.\n\n"

    "RESPONSE MODE:\n"
    "- If the user asks a simple question (What is..., Explain..., Define...), give a clear, direct, "
    "student-friendly explanation. Do NOT produce a lesson plan.\n"
    "- Only produce the full lesson plan when the user explicitly asks for a 'lesson plan', "
    "'teaching plan', or 'make a lesson'.\n\n"

    "When producing a LESSON PLAN, write a lengthy, detailed, well-structured, professional document "
    "in clean Markdown, following this exact structure:\n\n"

    "# [Lesson Title]\n\n"
    "## Overview\n"
    "A concise paragraph describing the lesson, its purpose, and how it fits the wider topic.\n\n"
    "## Lesson Details\n"
    "Present as a Markdown table with rows for: Grade / Level, Subject, Topic, Duration, "
    "Prerequisite Knowledge.\n\n"
    "## Learning Objectives\n"
    "4 to 6 measurable objectives written as 'By the end of this lesson, students will be able to...', "
    "aligned to the curriculum where relevant.\n\n"
    "## Key Concepts and Vocabulary\n"
    "A bulleted list of the essential terms with a short definition for each.\n\n"
    "## Lesson Structure\n"
    "A detailed, time-sequenced breakdown using these subsections:\n"
    "### Introduction / Engagement\n"
    "### Direct Instruction / Explanation\n"
    "### Guided Practice\n"
    "### Independent Practice\n"
    "### Closure / Consolidation\n"
    "For each subsection give an estimated time, what the teacher does, and what students do.\n\n"
    "## Differentiation\n"
    "Concrete strategies for three groups: support for struggling learners, core activities, "
    "and extension for advanced learners.\n\n"
    "## Assessment\n"
    "Both formative checks during the lesson and a summative task, with clear success criteria. "
    "Include a short Markdown rubric table where appropriate.\n\n"
    "## Extension and Homework\n"
    "Meaningful follow-up tasks that reinforce the objectives.\n\n"
    "## Teacher Notes\n"
    "Practical guidance: common misconceptions, pacing tips, and safety or sensitivity notes if relevant.\n\n"

    "FORMATTING RULES:\n"
    "- Start with a single # title, use ## for sections and ### for subsections.\n"
    "- Use bullet points, numbered steps, **bold**, and *italic* purposefully.\n"
    "- Use Markdown tables for the lesson details, rubrics, and any structured data.\n"
    "- Be thorough and specific to the requested subject; avoid generic filler.\n\n"

    ""


    "Audience guidance: {audience}\n\n"
    "Curriculum context: {context}\n\n"
    "Chat history (for continuity): {chat_history}"
)


if LANGCHAIN_AVAILABLE and llm is not None:
    qa_prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        MessagesPlaceholder("chat_history"),
        ("human", "{input}"),
    ])

    question_answer_chain = qa_prompt | llm | StrOutputParser()

    rag_chain = create_retrieval_chain(history_aware_retriever, question_answer_chain)

# ====================== SESSION ======================
if LANGCHAIN_AVAILABLE and rag_chain is not None:
    def get_session_history(session_id: str):
        hydrate_session_history(session_id)

        if session_id not in SESSION_STORE:
            SESSION_STORE[session_id] = ChatMessageHistory()
        return SESSION_STORE[session_id]

    conversational_rag_chain = RunnableWithMessageHistory(
        rag_chain,
        get_session_history,
        input_messages_key="input",
        history_messages_key="chat_history",
        output_messages_key="answer"
    )

# ====================== IMAGE MODEL ======================
pipe = None  # image gen runs remotely on the Ilaibu DGX — never load Qwen on Canada
print("Image generation: remote mode (Ilaibu DGX).")


import requests as _rq
import time

def generate_image_task(prompt: str, out_path: str, image_id: str, allow_text: bool = False):
    """Submit to the DGX, poll until ready, download the PNG to out_path."""
    try:
        # 1) submit
        r = requests.post(
            f"{IMAGE_DGX_URL}/generate",
            json={"prompt": prompt, "allow_text": bool(allow_text)},
            timeout=(15, 30),
        )
        r.raise_for_status()
        job_id = r.json().get("job_id")
        if not job_id:
            print("[image] DGX gave no job_id")
            IMAGE_STATUS[image_id] = "failed"
            return

        # 2) poll status (best-quality gen can take a while; poll up to ~4 min)
        deadline = time.time() + 240
        while time.time() < deadline:
            try:
                s = requests.get(f"{IMAGE_DGX_URL}/status/{job_id}", timeout=(15, 20))
                st = s.json().get("status")
            except requests.exceptions.RequestException as exc:
                print(f"[image] status poll error: {exc}")
                time.sleep(3)
                continue

            if st == "ready":
                # 3) download the PNG
                img = requests.get(f"{IMAGE_DGX_URL}/image/{job_id}", timeout=(15, 60))
                img.raise_for_status()
                with open(out_path, "wb") as f:
                    f.write(img.content)
                IMAGE_STATUS[image_id] = "ready"
                print(f"[image] {image_id} ready -> {out_path}")
                return
            if st == "failed":
                print(f"[image] DGX reported failed for {job_id}")
                IMAGE_STATUS[image_id] = "failed"
                return

            time.sleep(2)

        print(f"[image] timed out waiting for {job_id}")
        IMAGE_STATUS[image_id] = "failed"

    except requests.exceptions.RequestException as exc:
        print(f"[image] DGX request failed: {exc}")
        IMAGE_STATUS[image_id] = "failed"
    except Exception as exc:
        print(f"[image] generate_image_task error: {exc}")
        IMAGE_STATUS[image_id] = "failed"

# ====================== FASTAPI ======================
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://([a-z0-9-]+\.)*edunex\.edu\.pg$",
    allow_methods=["*"],
    allow_headers=["*"]
)
SESSION_DOCUMENTS: Dict[str, Dict[str, str]] = {}

@app.post("/upload")
async def upload_file(file: UploadFile = File(...), session_id: str = Form(None)):
    filename = file.filename or "upload"
    dest = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}_{filename}")
    with open(dest, "wb") as out:
        out.write(await file.read())

    text = extract_text_from_upload(dest, filename)
    if not text.strip():
        raise HTTPException(status_code=422, detail="Could not extract text from this file.")

    if session_id:
        existing = SESSION_DOCUMENT_BUFFER.get(session_id, "")
        combined = (existing + f"\n\n--- Document: {filename} ---\n{text}").strip()
        SESSION_DOCUMENT_BUFFER[session_id] = combined[:MAX_DOC_CHARS]

    return {"message": f"'{filename}' uploaded and added to this chat's knowledge.", "chars": len(text)}

@app.on_event("startup")
def startup_tasks():
    ensure_chat_log_schema()

app.mount("/assets", StaticFiles(directory=IMAGE_OUTPUT_DIR), name="assets")

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    fallback_index = os.path.join(os.path.dirname(__file__), "index (3).html")

    for index_path in (INDEX_PATH, fallback_index):
        if os.path.exists(index_path):
            with open(index_path, "r", encoding="utf-8") as f:
                return HTMLResponse(content=f.read())

    return HTMLResponse("<h1>index.html not found</h1>", status_code=404)


@app.post("/api/chat-log")
def save_chat_log(payload: ChatLogPayload):
    chat_stack.append(payload.log_id)

    conn = get_conn()
    cur = conn.cursor()
    try:
        ts_utc = to_utc_datetime(payload.timestamp)
        cur.execute(
            """
            INSERT INTO nexa_chat_logs (log_id, session_id, user_email, user_name, user_prompt, nexa_response, pdf_url, timestamp_utc, stars)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                session_id = VALUES(session_id),
                user_email = VALUES(user_email),
                user_name = VALUES(user_name),
                user_prompt = VALUES(user_prompt),
                nexa_response = VALUES(nexa_response),
                pdf_url = VALUES(pdf_url),
                timestamp_utc = VALUES(timestamp_utc),
                stars = VALUES(stars)
            """,
            (
                payload.log_id,
                payload.session_id,
                normalize_email_address(payload.user_email),
                payload.user_name,
                payload.user_prompt,
                payload.nexa_response,
                payload.pdf_url,
                ts_utc.strftime("%Y-%m-%d %H:%M:%S"),
                payload.stars,
            ),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()

    return {"status": "saved", "stack_size": len(chat_stack)}


@app.post("/api/chat-rating")
def save_rating(payload: RatingPayload):
    conn = get_conn()
    cur = conn.cursor()
    try:
        ts_utc = to_utc_datetime(payload.timestamp)
        cur.execute(
            """
            UPDATE nexa_chat_logs
            SET stars = %s, timestamp_utc = %s
            WHERE log_id = %s
            """,
            (
                payload.stars,
                ts_utc.strftime("%Y-%m-%d %H:%M:%S"),
                payload.log_id,
            ),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()

    return {"status": "rating_saved"}


@app.post("/api/chat-log/pop")
def pop_last_log(payload: PopPayload):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM nexa_chat_logs WHERE log_id = %s", (payload.log_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()

    # Keep in-memory stack consistent when explicit log_id is removed.
    if payload.log_id in chat_stack:
        chat_stack.remove(payload.log_id)

    return {"status": "popped", "stack_size": len(chat_stack)}


@app.post("/api/chat-image")
def save_chat_image(payload: ImageBase64Payload):
    log_id = payload.log_id.strip()
    user_name = payload.user_name.strip()
    image_filename = payload.image_filename.strip() if payload.image_filename else None
    mime_type = payload.image_mime_type or "application/octet-stream"

    image_base64 = payload.image_base64.strip()
    if image_base64.startswith("data:") and "," in image_base64:
        image_base64 = image_base64.split(",", 1)[1]

    try:
        image_blob = base64.b64decode(image_base64, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid base64 image payload") from exc

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE nexa_chat_logs
            SET image_base64 = %s,
                image_blob = %s,
                image_mime_type = %s,
                image_filename = %s,
                image_saved_at = %s
            WHERE log_id = %s AND user_name = %s
            """,
            (
                image_base64,
                image_blob,
                mime_type,
                image_filename,
                datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                log_id,
                user_name,
            ),
        )
        conn.commit()

        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Chat log not found for this user")
    finally:
        cur.close()
        conn.close()

    return {"status": "image_saved", "log_id": log_id, "user_name": user_name, "size": len(image_base64)}


@app.get("/api/chat-image/{log_id}")
def get_chat_image(log_id: str, user_name: str):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT image_base64, image_blob, image_mime_type, image_filename
            FROM nexa_chat_logs
            WHERE log_id = %s AND user_name = %s
            """,
            (log_id, user_name),
        )
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()

    if not row or row[0] is None:
        raise HTTPException(status_code=404, detail="Image not found")

    image_base64, image_blob, image_mime_type, image_filename = row
    if image_blob is not None:
        image_bytes = image_blob
    else:
        try:
            image_bytes = base64.b64decode(image_base64)
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Stored base64 image is invalid") from exc

    headers = {}
    if image_filename:
        headers["Content-Disposition"] = f'inline; filename="{image_filename}"'

    from io import BytesIO
    from fastapi.responses import StreamingResponse

    return StreamingResponse(BytesIO(image_bytes), media_type=image_mime_type or "application/octet-stream", headers=headers)



@app.post("/api/share-chat", response_model=ShareChatResponse)
def share_chat(payload: ShareChatPayload):
    session_id = (payload.session_id or "").strip()
    user_email = normalize_email_address(payload.user_email)

    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    if not session_belongs_to_user(session_id, user_email):
        raise HTTPException(status_code=403, detail="You can only share your own chat session")

    rows = fetch_chat_history_rows(session_id)
    if not rows:
        raise HTTPException(status_code=404, detail="No saved chat logs found for this session")

    share_token = create_share_record(session_id, user_email)
    share_url = f"/share/{share_token}"

    return {
        "share_token": share_token,
        "share_url": share_url,
    }


@app.get("/api/shared-chat/{share_token}", response_model=SharedChatResponse)
def get_shared_chat_data(share_token: str):
    share = get_share_record(share_token)

    if not share or not share.get("is_active"):
        raise HTTPException(status_code=404, detail="Shared chat link not found")

    expires_at = share.get("expires_at_utc")
    if expires_at:
        now_utc_naive = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        if expires_at < now_utc_naive:
            raise HTTPException(status_code=410, detail="Shared chat link has expired")

    session_id = share.get("session_id")
    messages = []

    for row in fetch_chat_history_rows(session_id):
        log_id = (row.get("log_id") or "").strip()
        user_name = (row.get("user_name") or "").strip()
        user_prompt = (row.get("user_prompt") or "").strip()
        nexa_response = (row.get("nexa_response") or "").strip()
        image_base64 = (row.get("image_base64") or "").strip()
        image_mime_type = (row.get("image_mime_type") or "").strip()
        image_filename = (row.get("image_filename") or "").strip()

        if user_prompt:
            messages.append({
                "role": "user",
                "content": user_prompt,
            })

        if nexa_response:
            assistant_message = {
                "role": "assistant",
                "content": nexa_response,
            }

            if image_mime_type:
                assistant_message["image_mime_type"] = image_mime_type

            if image_filename:
                assistant_message["image_filename"] = image_filename

            if image_filename:
                assistant_message["image_url"] = f"/assets/{quote_plus(image_filename)}"
            elif image_base64 and log_id and user_name:
                assistant_message["image_url"] = f"/api/chat-image/{log_id}?user_name={quote_plus(user_name)}"

            messages.append(assistant_message)

    created_at = share.get("created_at_utc")

    return {
        "share_token": share_token,
        "session_id": session_id,
        "created_at_utc": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
        "messages": messages,
    }


@app.get("/share/{share_token}", response_class=HTMLResponse)
def shared_chat_page(share_token: str):
    for index_path in (INDEX_PATH, os.path.join(WORKSPACE_DIR, "index (3).html")):
        if os.path.exists(index_path):
            with open(index_path, "r", encoding="utf-8") as f:
                return HTMLResponse(content=f.read())

    return HTMLResponse("<h1>index.html not found</h1>", status_code=404)


class ChatRequest(BaseModel):
    message: str
    question: Optional[str] = None
    user_email: Optional[str] = None
    user_name: Optional[str] = None          # ← add
    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    reply_context: Optional[str] = None
    staged_file_name: Optional[str] = None
    url: Optional[str] = None

class ChatResponse(BaseModel):
    response: str
    session_id: str
    access_role: Optional[str] = None
    status_message: Optional[str] = None
    pdf_url: Optional[str] = None
    image_url: Optional[str] = None
    image_id: Optional[str] = None
    log_id: Optional[str] = None


class ChatStatusResponse(BaseModel):
    status_message: str
    action: str


class ChatHistoryResponse(BaseModel):
    session_id: str
    messages: Any


class ChatSessionSummary(BaseModel):
    session_id: str
    last_activity: str
    message_count: int


class ChatSessionListResponse(BaseModel):
    user_email: str
    sessions: List[ChatSessionSummary]


class ChatSessionSearchSummary(ChatSessionSummary):
    snippet: Optional[str] = None


class ChatSessionSearchResponse(BaseModel):
    user_email: str
    query: str
    sessions: List[ChatSessionSearchSummary]

class UrlReadRequest(BaseModel):
    url: str
    question: Optional[str] = None
    user_email: Optional[str] = None
    session_id: Optional[str] = None

_LATEX_COMMAND_RE = re.compile(r"\\[a-zA-Z]+")


def normalize_latex_delimiters(text: str) -> str:
    r"""Convert \[ \] and \( \) to $$ and $. Markdown strips the backslash from
    \[ before KaTeX runs, which is why display blocks arrive as bare brackets."""
    t = text or ""
    t = re.sub(r"\\\[\s*", "\n$$\n", t)
    t = re.sub(r"\s*\\\]", "\n$$\n", t)
    t = re.sub(r"\\\(\s*", "$", t)
    t = re.sub(r"\s*\\\)", "$", t)
    return re.sub(r"\n{3,}", "\n\n", t)


def wrap_bare_latex(text: str) -> str:
    """Ensure LaTeX reaches KaTeX with usable delimiters.

    If the model already delimited its maths, only normalise the delimiter style.
    Inserting dollars into an already-delimited expression is what produced
    "$\\frac{-7$ $\\pm$ $\\sqrt{97}}{2}$".
    """
    if not text:
        return text

    t = normalize_latex_delimiters(text)

    if "$" in t:
        return t

    # Whole-line expressions only. No inline token wrapping: that rule also
    # mangled filenames like import_materials.php.
    t = re.sub(r"(?m)^\s*(\\boxed\{(?:[^{}]|\{[^{}]*\})*\})\s*$", r"$$\1$$", t)
    t = re.sub(r"(?m)^\s*(\\int[^\n]*?\bd[a-z]\b)\s*$", r"$$\1$$", t)

    def _wrap_line(m):
        line = m.group(0).strip()
        if not _LATEX_COMMAND_RE.search(line):
            return m.group(0)
        return f"$${line}$$"

    # A line that is nothing but a LaTeX expression.
    t = re.sub(r"(?m)^\s*[\\{}\[\]()a-zA-Z0-9+\-*/=^_.,\s]*\\[a-zA-Z]+"
               r"[\\{}\[\]()a-zA-Z0-9+\-*/=^_.,\s]*$", _wrap_line, t)
    return t


def infer_chat_status(message: str, access_role: str, staged_file_name: Optional[str] = None) -> tuple[str, str]:
    lower_msg = (message or "").lower()
    lower_file = (staged_file_name or "").lower()

    if staged_file_name:
        return ("Nexa is Processing document...", "document")

    if any(phrase in lower_msg for phrase in ("lesson plan", "create lesson", "create a lesson", "make a lesson")):
        if access_role != "teacher":
            return ("Nexa is Checking lesson plan access...", "lesson-plan-blocked")
        return ("Nexa is Generating lesson plan...", "lesson-plan")

    if "short notes" in lower_msg or "short note" in lower_msg or "summarize" in lower_msg:
        return ("Nexa is Generating short notes...", "short-notes")

    if "pdf" in lower_msg:
        return ("Nexa is Generating PDF...", "pdf")

    if any(keyword in lower_msg for keyword in ["image", "diagram", "draw", "visual"]):
        return ("Nexa is Generating image...", "image")

    if "wikipedia" in lower_msg or "wiki" in lower_msg:
        return ("Nexa is Searching Wikipedia...", "wikipedia")

    if looks_like_web_query(message):
        return ("Nexa is Searching ...", "web")

    if lower_file.endswith((".pdf", ".docx", ".txt")):
        return ("Nexa is Processing document...", "document")

    return ("Nexa is Thinking...", "general")


@app.post("/read-url", response_model=ChatResponse)
async def read_url_endpoint(request: UrlReadRequest):
    access_role = infer_access_role(request.user_email)
    normalized_email = normalize_email_address(request.user_email)
    session_id = request.session_id or str(uuid.uuid4())

    url = (request.url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url

    page_text = fetch_page_text(url)
    user_log_id = str(uuid.uuid4())

    if page_text.startswith("__ERROR__"):
        answer = page_text.replace("__ERROR__", "").strip()
        return ChatResponse(
            response=answer, session_id=session_id, access_role=access_role,
            pdf_url=None, image_url=None, image_id=None, log_id=user_log_id,
        )

    # Store the page in the session document buffer so later questions can reference it too.
    existing = SESSION_DOCUMENT_BUFFER.get(session_id, "")
    combined = (existing + f"\n\n--- Web page: {url} ---\n{page_text}").strip()
    SESSION_DOCUMENT_BUFFER[session_id] = combined[:MAX_DOC_CHARS]

    question = (request.question or "").strip() or "Summarize this web page clearly for a student."

    if LANGCHAIN_AVAILABLE and llm is not None:
        prompt = ChatPromptTemplate.from_messages([
            ("system",
             "You are Nexa, an educational assistant. Read the web page content provided and "
             "answer the user's request accurately in clean Markdown, using only that content. "
             "Do not invent details. Audience guidance: {audience}"),
            ("human", "User request: {question}\n\nWeb page content:\n{page}"),
        ])
        try:
            answer = (prompt | llm | StrOutputParser()).invoke({
                "question": question,
                "page": page_text,
                "audience": build_role_instruction(access_role),
            }).strip()
        except Exception as exc:
            print(f"URL read synthesis failed: {exc}")
            answer = "I read the page but could not process it just now. Please try again."
    else:
        answer = f"I read the page **{url}** but the language model is unavailable to summarize it."

    answer = append_generation_disclaimer(answer)

    record_chat_turn(session_id, "user", f"[Read URL] {url} — {question}")
    record_chat_turn(session_id, "assistant", answer)
    try:
        user_name = normalized_email or TEST_USER_NAME
        persist_chat_log(
            log_id=user_log_id, session_id=session_id, user_email=normalized_email,
            user_name=user_name, user_prompt=f"[Read URL] {url} — {question}",
            nexa_response=answer, pdf_url=None, stars=0,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
    except Exception:
        pass

    return ChatResponse(
        response=answer, session_id=session_id, access_role=access_role,
        status_message="Reading web page...",
        pdf_url=None, image_url=None, image_id=None, log_id=user_log_id,
    )


@app.post("/chat-status", response_model=ChatStatusResponse)
async def chat_status_endpoint(request: ChatRequest):
    try:
        access_role = infer_access_role(request.user_email)
    except HTTPException:
        access_role = "student"
    if (request.url or "").strip():
        return ChatStatusResponse(status_message="Nexa is Searching ...", action="web")

    status_message, action = infer_chat_status(request.message, access_role, request.staged_file_name)
    return ChatStatusResponse(status_message=status_message, action=action)


@app.post("/ingest-material")
async def ingest_material(
    file: UploadFile = File(...),
    course: str = Form(""),
    subject: str = Form(""),
    grade: str = Form(""),
    token: str = Form(""),
):
    if INGEST_TOKEN and token != INGEST_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid ingest token")
    if not (LANGCHAIN_AVAILABLE and globals().get("vectorstore") is not None):
        raise HTTPException(status_code=503, detail="Vector store unavailable")

    filename = file.filename or "material"
    suffix = "." + filename.rsplit(".", 1)[-1] if "." in filename else ""
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        text = _extract_uploaded_text(tmp_path, filename)
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    if not text.strip():
        return {"status": "empty", "filename": filename, "chunks": 0}

    subj = SUBJECT_CODE_MAP.get((subject or "").lower(), (subject or "Unknown").title() or "Unknown")
    g = re.search(r"(\d{1,2})", str(grade or ""))
    grade_norm = f"Grade {int(g.group(1))}" if g else "Unknown"

    from langchain.schema import Document as LCDocument
    splitter = RecursiveCharacterTextSplitter(chunk_size=1500, chunk_overlap=300)
    chunks = splitter.split_text(text)
    docs = [LCDocument(page_content=c, metadata={
        "file": filename, "subject": subj, "grade": grade_norm,
        "term": "Unknown", "doctype": "LMS Material", "course": course,
        "chunk": i + 1,
    }) for i, c in enumerate(chunks) if c.strip()]

    if not docs:
        return {"status": "empty", "filename": filename, "chunks": 0}

    try:
        globals()["vectorstore"].add_documents(docs)
        print(f"[ingest] +{len(docs)} chunks from {filename} ({subj} {grade_norm}, course={course!r})")
    except Exception as exc:
        print(f"[ingest] add_documents failed: {exc}")
        raise HTTPException(status_code=500, detail="Index update failed")

    return {"status": "ok", "filename": filename, "subject": subj,
            "grade": grade_norm, "chunks": len(docs)}


@app.post("/api/chat-stop")
async def chat_stop_endpoint(payload: ChatStopPayload):
    turn_id = (payload.turn_id or "").strip()
    if not turn_id:
        raise HTTPException(status_code=400, detail="turn_id is required")

    CHAT_CANCELLED_TURNS.add(turn_id)
    return {"ok": True, "turn_id": turn_id}

def solve_with_sympy(message: str):
    """Compute an exact answer with SymPy. Returns (latex_result, plain) or None."""
    # Normalize Unicode maths characters SymPy's parser can't read.
    message = (message or "").replace("−", "-").replace("–", "-").replace("×", "*").replace("÷", "/")
    x = sympy.Symbol('x')
    msg = message.strip()

    try:
        # ---- Definite integral: "integrate <f> from <a> to <b>" or "[a,b] <f> dx" ----
        defint = re.search(r'(?:integrate|integral of)\s+(.+?)\s+from\s+(.+?)\s+to\s+(.+)', msg, re.IGNORECASE)
        bounds = re.search(r'∫?\s*\[?\s*([\d\.\-/]+)\s*[,;]\s*([\d\.\-/]+)\s*\]?\s*(.+?)\s*dx', msg, re.IGNORECASE)
        if defint or bounds:
            if defint:
                body, lo, hi = defint.group(1), defint.group(2), defint.group(3)
            else:
                lo, hi, body = bounds.group(1), bounds.group(2), bounds.group(3)
            expr = parse_expr(body.replace("^", "**"), transformations=_SYMPY_TF)
            lo_v = parse_expr(lo, transformations=_SYMPY_TF)
            hi_v = parse_expr(hi, transformations=_SYMPY_TF)
            exact = sympy.integrate(expr, (x, lo_v, hi_v))
            approx = sympy.N(exact, 6)
            return (f"$$\\int_{{{sympy.latex(lo_v)}}}^{{{sympy.latex(hi_v)}}} "
                    f"{sympy.latex(expr)}\\,dx = {sympy.latex(exact)} \\approx {approx}$$",
                    f"{exact} (approx {approx})")

        # ---- Indefinite integral: "integrate <f>" ----
        indef = re.search(r'(?:integrate|integral of)\s+(.+?)(?:\s+dx)?$', msg, re.IGNORECASE)
        if indef:
            expr = parse_expr(indef.group(1).replace("^", "**"), transformations=_SYMPY_TF)
            result = sympy.integrate(expr, x)
            return (f"$$\\int {sympy.latex(expr)}\\,dx = {sympy.latex(result)} + C$$", str(result))

        # ---- Derivative: "differentiate <f>" / "derivative of <f>" ----
        diff = re.search(r'(?:differentiate|derivative of)\s+(.+)', msg, re.IGNORECASE)
        if diff:
            expr = parse_expr(diff.group(1).replace("^", "**"), transformations=_SYMPY_TF)
            result = sympy.diff(expr, x)
            return (f"$$\\frac{{d}}{{dx}}\\left({sympy.latex(expr)}\\right) = {sympy.latex(result)}$$", str(result))

        # ---- Equation solving: "solve <lhs> = <rhs>" ----
        eq = re.search(r'solve\s+(.+)', msg, re.IGNORECASE)
        if eq and "=" in eq.group(1):
            left, right = eq.group(1).split("=", 1)
            lhs = parse_expr(left.replace("^", "**"), transformations=_SYMPY_TF)
            rhs = parse_expr(right.replace("^", "**"), transformations=_SYMPY_TF)
            sols = sympy.solve(sympy.Eq(lhs, rhs), x)
            if not sols:
                return None
            if len(sols) == 1:
                return (f"$$x = {sympy.latex(sols[0])}$$", str(sols))
            body = ",\\quad ".join(f"x = {sympy.latex(s)}" for s in sols)
            return (f"$${body}$$", str(sols))

        # ---- Simplify / evaluate: "simplify <expr>" ----
        simp = re.search(r'(?:simplify|evaluate|calculate)\s+(.+)', msg, re.IGNORECASE)
        if simp:
            expr = parse_expr(simp.group(1).replace("^", "**"), transformations=_SYMPY_TF)
            result = sympy.simplify(expr)
            return (f"$${sympy.latex(expr)} = {sympy.latex(result)}$$", str(result))

    except Exception as e:
        print(f"[info] SymPy could not parse (falling back to LLM): {e}")
    return None

def gather_web_context(query: str, limit: int = 5) -> str:
    """Collect plain-text context from Wikipedia + DuckDuckGo for LLM synthesis.
    Wikipedia is prioritized because it is most reliable for 'who is / what is'
    factual questions like 'Who is Michael Somare'."""
    parts = []
    primary_source = ""

    if wikipedia is not None:
        try:
            wikipedia.set_lang("en")
            hits = wikipedia.search(query, results=3)
            if hits:
                page = wikipedia.page(hits[0], auto_suggest=False)
                summary = wikipedia.summary(page.title, sentences=5, auto_suggest=False)
                parts.append(f"Wikipedia ({page.title}): {summary}")
                primary_source = page.url
        except Exception:
            pass

    if DDGS is not None:
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=limit, safesearch="moderate"))
            for item in results:
                title = (item.get("title") or "").strip()
                body = (item.get("body") or "").strip()
                url = (item.get("href") or item.get("url") or "").strip()
                if body:
                    parts.append(f"{title}: {body}")
                    if not primary_source and url:
                        primary_source = url
        except Exception:
            pass

    if not parts:
        return ""

    context = "\n\n".join(parts)
    if primary_source:
        context += f"\n\nMost relevant source URL: {primary_source}"
    return context


def synthesize_web_answer(message: str, access_role: str) -> str:
    """Fetch web context and have the LLM write a direct, student-friendly answer.
    Returns '' if nothing usable was found or the context is off-topic."""
    query = build_web_results_query(message)
    context = gather_web_context(query)
    if not context:
        return ""
    # No LLM available - degrade gracefully to the cleaned link list.
    if not (LANGCHAIN_AVAILABLE and llm is not None):
        return fetch_web_results(query)
    synth_prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            "You are Nexa, an educational assistant for Papua New Guinea. "
            "Answer the user's question using the web context below. "
            "If the context is about a different subject that merely shares a "
            "name with something in the question, IGNORE it and reply with "
            "exactly: UNKNOWN. Never describe unrelated companies, products, "
            "crypto wallets or software. Do not invent facts beyond the context. "
            "Write a clear explanation in your own words in clean Markdown, then "
            "one final line exactly like: 'Source: <url>'. "
            "Audience guidance: {audience}"
        ),
        ("human", "Question: {question}\n\nWeb context:\n{context}"),
    ])
    try:
        result = (synth_prompt | llm | StrOutputParser()).invoke({
            "question": message,
            "context": context,
            "audience": build_role_instruction(access_role),
        }).strip()
        if not result or result.upper().startswith("UNKNOWN"):
            return ""
        return result
    except Exception as exc:
        print(f"Web synthesis failed: {exc}")
        return ""

@app.get("/api/chat-history/{session_id}", response_model=ChatHistoryResponse)
def get_chat_history(session_id: str):
    messages = []

    for row in fetch_chat_history_rows(session_id):
        log_id = (row.get("log_id") or "").strip()
        user_name = (row.get("user_name") or "").strip()
        user_prompt = (row.get("user_prompt") or "").strip()
        nexa_response = (row.get("nexa_response") or "").strip()
        image_base64 = (row.get("image_base64") or "").strip()
        image_mime_type = (row.get("image_mime_type") or "").strip()
        image_filename = (row.get("image_filename") or "").strip()

        if user_prompt:
            messages.append({"role": "user", "content": user_prompt})
        pdf_url = (row.get("pdf_url") or "").strip()
        if nexa_response:
            message = {"role": "assistant", "content": nexa_response}
            if image_mime_type:
                message["image_mime_type"] = image_mime_type
            if image_filename:
                message["image_filename"] = image_filename
                message["image_url"] = f"/assets/{quote_plus(image_filename)}"
            elif image_base64 and log_id and user_name:
                message["image_url"] = f"/api/chat-image/{log_id}?user_name={quote_plus(user_name)}"
            if pdf_url:
                message["pdf_url"] = pdf_url
            messages.append(message)

    if not messages:
        messages = serialize_chat_history(session_id)

    return {
        "session_id": session_id,
        "messages": messages,
    }


@app.get("/api/chat-sessions/{user_email}", response_model=ChatSessionListResponse)
def get_chat_sessions(user_email: str):
    normalized_email = normalize_email_address(user_email)
    return {
        "user_email": normalized_email,
        "sessions": fetch_user_chat_sessions(normalized_email),
    }


@app.get("/api/chat-sessions/{user_email}/search", response_model=ChatSessionSearchResponse)
def search_chat_sessions(user_email: str, query: str):
    normalized_email = normalize_email_address(user_email)
    cleaned_query = (query or "").strip()
    return {
        "user_email": normalized_email,
        "query": cleaned_query,
        "sessions": search_user_chat_sessions(normalized_email, cleaned_query),
    }

@app.get("/image-status/{image_id}")
async def image_status(image_id: str):

    status = IMAGE_STATUS.get(image_id, "processing")

    return {
        "status": status
    }


# Words that mean "make something new", not "reformat what you just said".
# Used to stop the reply-context PDF shortcut from swallowing generation requests.
_NEW_CONTENT_WORDS = (
    "lesson plan", "teaching plan", "quiz", "question", "mcq", "worksheet",
    "test", "essay", "image", "diagram", "picture", "explain", "expand",
    "rewrite", "shorten", "translate", "summar", "notes",
)

# Shared maths formatting rule. Every prompt that can emit maths uses this, so
# the delimiters the frontend renders are the only ones the model produces.
_MATH_FORMAT_RULE = (
    "Use ONLY $...$ for inline maths and $$...$$ for display maths. NEVER use "
    "\\[ \\], \\( \\), or bare brackets. Write every power as x^{n}, never as x4 "
    "or x**4. Keep each complete expression inside ONE pair of delimiters: write "
    "$x = \\frac{-7 \\pm \\sqrt{97}}{2}$, never split a fraction or a root across "
    "several dollar signs."
)

def _is_explain_request(message: str) -> bool:
    """True when the user is asking to explain / guide / clarify / walk through,
    not to explicitly create a lesson, quiz, or image."""
    m = (message or "").lower().strip()
    # Explicit content-creation requests are NOT explain requests.
    if re.search(r"\b(lesson plan|make a lesson|create a lesson|quiz|"
                 r"mcq|worksheet|generate .*questions|create .*questions|"
                 r"image|diagram|picture)\b", m):
        return False
    return bool(re.search(
        r"\b(explain|elaborate|clarify|describe|expand|tell me more|"
        r"what (is|are|does)|in detail|simpler|break (it|this) down|"
        r"guide me|guide us|how (to|do i|do we|can i|can we|does|is)|"
        r"walk me through|walk us through|steps to|step by step|"
        r"help me (with|understand|implement)|show me how)\b", m))

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    # ---------- ROLE RESOLUTION ----------
    try:
        access_role = infer_access_role(request.user_email)
        normalized_email = normalize_email_address(request.user_email)
    except HTTPException:
        prior = SESSION_ACCESS_PROFILE.get(request.session_id or "", {})
        access_role = prior.get("role", "student")
        normalized_email = prior.get("email", "")
    session_id = request.session_id or str(uuid.uuid4())
    SESSION_ACCESS_PROFILE[session_id] = {"email": normalized_email, "role": access_role}
    student_name = (request.user_name or "").strip()
    def _log_name() -> str:
        return student_name or normalized_email or TEST_USER_NAME
    if is_chat_turn_cancelled(request.turn_id):
        return ChatResponse(
            response="", session_id=session_id, access_role=access_role,
            status_message="Nexa is Thinking...", pdf_url=None,
            image_url=None, image_id=None, log_id=None,
        )
    hydrate_session_history(session_id)
    record_chat_turn(session_id, "user", request.message)
    capture_user_fact(session_id, request.message)
    page_url = (request.url or "").strip()
    page_question = (request.question or "").strip() or (request.message or "").strip()
    # ---------- URL READING ----------
    if page_url:
        page_text = fetch_page_text(page_url)
        if page_text.startswith("__ERROR__"):
            return ChatResponse(
                response=page_text.replace("__ERROR__", "").strip(),
                session_id=session_id, access_role=access_role,
                status_message="Nexa is Searching ...", pdf_url=None,
                image_url=None, image_id=None, log_id=str(uuid.uuid4()),
            )
        existing = SESSION_DOCUMENT_BUFFER.get(session_id, "")
        combined = (existing + f"\n\n--- Web page: {page_url} ---\n{page_text}").strip()
        SESSION_DOCUMENT_BUFFER[session_id] = combined[:MAX_DOC_CHARS]
        question = page_question or "Summarize this web page clearly for a student."
        url_system = (
            "You are Nexa, an educational assistant. Read the web page content provided "
            "and answer the user's request accurately in clean Markdown, using only that "
            f"content. Do not invent details. {_MATH_FORMAT_RULE} "
            f"Audience guidance: {build_role_instruction(access_role)}")
        answer = answer_with_history(
            url_system, question, session_id, extra_material=page_text) or \
            f"I read the page **{page_url}** but could not process it just now."
        answer = wrap_bare_latex(answer)
        url_log_id = str(uuid.uuid4())
        record_chat_turn(session_id, "assistant", answer)
        try:
            persist_chat_log(
                log_id=url_log_id, session_id=session_id, user_email=normalized_email,
                user_name=_log_name(), user_prompt=f"[Read URL] {page_url} : {question}",
                nexa_response=answer, pdf_url=None, stars=0,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
        except Exception:
            pass
        return ChatResponse(
            response=answer, session_id=session_id, access_role=access_role,
            status_message="Nexa is Searching ...", pdf_url=None,
            image_url=None, image_id=None, log_id=url_log_id,
        )
    # ---------- placeholder log ----------
    user_log_id = str(uuid.uuid4())
    try:
        persist_chat_log(
            log_id=user_log_id, session_id=session_id, user_email=normalized_email,
            user_name=_log_name(), user_prompt=(request.message or "").strip(),
            nexa_response="", pdf_url=None, stars=0,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
    except Exception:
        pass
    lower_msg = (request.message or "").lower()
    status_message, _ = infer_chat_status(request.message, access_role, request.staged_file_name)
    pending_image_request = SESSION_PENDING_IMAGE.get(session_id)
    pending_question = SESSION_PENDING_QUESTION.get(session_id)
    pending_lesson = SESSION_PENDING_LESSON.get(session_id)
    if pending_lesson and looks_like_new_request(request.message):
        print("[pending] new request -> abandoning lesson clarification")
        SESSION_PENDING_LESSON.pop(session_id, None)
        pending_lesson = None
    if pending_question and looks_like_new_request(request.message):
        print("[pending] new request -> abandoning question clarification")
        SESSION_PENDING_QUESTION.pop(session_id, None)
        pending_question = None
    def _finish(answer_text: str, pdf_url=None, image_url=None, image_id=None):
        record_chat_turn(session_id, "assistant", answer_text)
        try:
            persist_chat_log(
                log_id=user_log_id, session_id=session_id, user_email=normalized_email,
                user_name=_log_name(), user_prompt=(request.message or "").strip(),
                nexa_response=(answer_text or "").strip(), pdf_url=pdf_url,
                image_filename=(image_url.split("/")[-1] if image_url else None),
                image_mime_type=("image/png" if image_url else None),
                image_base64=None, stars=0,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
        except Exception:
            pass
        return ChatResponse(
            response=answer_text, session_id=session_id, access_role=access_role,
            status_message=status_message, pdf_url=pdf_url,
            image_url=image_url, image_id=image_id, log_id=user_log_id,
        )
    # ---------- FAST PATH: exact greetings (instant, no LLM) ----------
    if not pending_image_request and not pending_question and not pending_lesson:
        qr = quick_reply(request.message, student_name)
        if qr:
            status_message = None
            return _finish(qr)
    try:
        if is_chat_turn_cancelled(request.turn_id):
            return ChatResponse(
                response="", session_id=session_id, access_role=access_role,
                status_message=status_message, pdf_url=None,
                image_url=None, image_id=None, log_id=None,
            )
        pdf_url = None
        reply_ctx = (request.reply_context or "").strip()[:MAX_DOC_CHARS]
        doc_context = SESSION_DOCUMENT_BUFFER.get(session_id, "")
        print(f"[context] reply_ctx={len(reply_ctx)} chars, doc={len(doc_context)} chars")
        doc_referenced = bool(doc_context) and bool(
            _DOC_REFERENCE_RE.search(request.message)
            or re.search(r'\b(this|that|it|the|these|those)\b', request.message, re.IGNORECASE))
        # ================= QUESTIONS ABOUT THE CONVERSATION =================
        if is_conversation_meta_question(request.message) and not doc_referenced:
            meta_answer = answer_about_conversation(request.message, session_id, access_role)
            if meta_answer:
                print("[answer] served from conversation transcript")
                return _finish(meta_answer)
            print("[conversation] meta answer unavailable, continuing to normal routing")
        if reply_ctx or doc_referenced or doc_context:
            search_message = request.message
            print("[search-rewrite] skipped: context present (quote/document)")
        elif _needs_followup_resolution(request.message):
            search_message = resolve_followup_message(request.message, session_id)
            if search_message != request.message:
                print(f"[search-rewrite] {request.message[:50]!r} -> {search_message[:70]!r}")
        else:
            search_message = request.message
            print("[search-rewrite] skipped: message is self-contained")
        # ================= REPLY-CONTEXT PDF SHORTCUT =================
        if (reply_ctx and "pdf" in lower_msg
                and len(request.message.strip()) < 80
                and not any(w in lower_msg for w in _NEW_CONTENT_WORDS)):
            kind = "lesson" if _is_lesson_request(reply_ctx.lower()) else "nexa"
            pdf_url = build_answer_pdf(reply_ctx, kind)
            return _finish("Here's that as a PDF.", pdf_url=pdf_url)
        # ================= MATHS FIRST =================
        if looks_like_math(search_message) and not doc_referenced:
            math_input = normalize_math_input(search_message)
            computed = solve_with_sympy(math_input) or llm_to_sympy(math_input)
            if computed:
                latex_result, plain = computed
                math_answer = f"## Answer\n{latex_result}"
                explain_system = (
                    "You are a maths tutor. The verified correct answer is provided and is "
                    "CORRECT. Explain how to reach it step by step. Do NOT change the final "
                    f"answer. {_MATH_FORMAT_RULE} Use '## Problem', '### Step 1', then "
                    f"'## Final Answer'. Audience: {build_role_instruction(access_role)}")
                body = answer_with_history(
                    explain_system,
                    f"Problem: {math_input}\n\nVerified answer: {plain}",
                    session_id, model=(llm or llm_general))
                if body:
                    math_answer = f"{body}\n\n## Final Answer\n{latex_result}"
                math_answer = wrap_bare_latex(math_answer)
                pdf_url = build_answer_pdf(math_answer, "nexa") if "pdf" in lower_msg else None
                return _finish(math_answer, pdf_url=pdf_url)
            print("[maths] no exact solution -> LLM math tutor")
            tutor_system = (
                "You are a maths tutor. Solve the problem step by step, showing all "
                f"working. Do NOT produce a lesson plan. {_MATH_FORMAT_RULE} Use "
                "'## Problem', '### Step 1', '### Step 2', then '## Final Answer'. "
                f"Audience: {build_role_instruction(access_role)}")
            body = answer_with_history(
                tutor_system, search_message, session_id, model=(llm or llm_general))
            if body:
                body = wrap_bare_latex(body)
                pdf_url = build_answer_pdf(body, "nexa") if "pdf" in lower_msg else None
                return _finish(body, pdf_url=pdf_url)
            print("[maths] tutor produced nothing, continuing to normal routing")
        # ================= PENDING CLARIFICATIONS =================
        if pending_question and not pending_image_request:
            SESSION_PENDING_QUESTION.pop(session_id, None)
            reply = (request.message or "").strip()
            if reply.lower() in ("cancel", "never mind", "nevermind", "stop", "no"):
                return _finish("No problem, cancelled. What else can I help with?")
            spec = {
                "type": pending_question.get("type", "mcq"),
                "count": pending_question.get("count", 10),
                "topic": reply,
                "scope": parse_lesson_request_scope(reply),
            }
            return _generate_questions_and_finish(spec, reply, access_role, lower_msg, _finish)
        if pending_lesson and not pending_image_request:
            reply = (request.message or "").strip()
            if reply.lower() in ("cancel", "never mind", "nevermind", "stop", "no"):
                SESSION_PENDING_LESSON.pop(session_id, None)
                return _finish("No problem, cancelled. What else can I help with?")
            spec = dict(pending_lesson)
            slot, _ = next_missing_lesson_slot(spec)
            if slot:
                spec[slot] = reply
            next_slot, next_question = next_missing_lesson_slot(spec)
            if next_slot:
                SESSION_PENDING_LESSON[session_id] = spec
                status_message = None
                return _finish(next_question)
            SESSION_PENDING_LESSON.pop(session_id, None)
            print(f"[lesson] spec complete: {spec}")
            source_text = reply_ctx if reply_ctx else doc_context
            return _generate_lesson_and_finish(
                build_lesson_request(spec), access_role, source_text,
                spec.get("topic", ""), lower_msg, _finish)
        # ================= INTENT CLASSIFICATION =================
        intent = classify_intent(
            search_message,
            has_document=bool(doc_context),
            has_reply_context=bool(reply_ctx),
        )
        print(f"[intent] {intent!r} for: {search_message[:60]!r}")
        if intent == "document_or_reply" and not (reply_ctx or doc_context):
            print("[intent] document_or_reply with no context -> general")
            intent = "general"
        if doc_referenced and intent not in (
                "generate_lesson", "generate_questions", "generate_image"):
            if intent != "document_or_reply":
                print(f"[intent] {intent!r} overridden -> document_or_reply "
                      f"(message refers to the document)")
            intent = "document_or_reply"
        if (reply_ctx or doc_context) and intent in ("generate_lesson", "generate_questions") \
                and _is_explain_request(request.message):
            print(f"[intent] {intent!r} overridden -> document_or_reply "
                  f"(explain/guide request against supplied material)")
            intent = "document_or_reply"
        # A lesson plan is produced ONLY when explicitly requested. The classifier
        # drifts plain questions ("will this work for me?") into generate_lesson;
        # reassign those to answer over the material or curriculum instead.
        if intent == "generate_lesson" and not _is_lesson_request(search_message.lower()):
            new_intent = "document_or_reply" if (reply_ctx or doc_context) else "curriculum"
            print(f"[intent] generate_lesson not explicit -> {new_intent}")
            intent = new_intent
        # Same principle for questions: only generate a quiz when explicitly asked.
        if intent == "generate_questions" and not _is_question_request(search_message.lower()) \
                and (reply_ctx or doc_context):
            print("[intent] generate_questions not explicit -> document_or_reply")
            intent = "document_or_reply"
        # ---- DOCUMENT / QUOTED-REPLY QUESTIONS ----
        if intent == "document_or_reply":
            source = reply_ctx if reply_ctx else doc_context
            doc_system = (
                "You are Nexa, an educational assistant. The user is asking about the "
                "material supplied with their message: a passage they quoted, or a document "
                "they uploaded. That material is the ONLY subject. Never describe the "
                "conversation itself, and ignore other topics discussed earlier. Follow the "
                "user's instructions exactly: if they ask for simpler language, a shorter "
                "answer, or one specific item from it, do that rather than starting a new "
                f"explanation. Do not invent facts beyond the material. {_MATH_FORMAT_RULE} "
                f"Audience: {build_role_instruction(access_role)}")
            answer = answer_with_history(
                doc_system, request.message, session_id,
                reply_ctx=source if reply_ctx else "",
                extra_material="" if reply_ctx else source)
            if answer:
                answer = wrap_bare_latex(strip_curriculum_meta(answer) or answer)
                if "pdf" in lower_msg:
                    pdf_url = build_answer_pdf(answer, "nexa")
                return _finish(answer, pdf_url=pdf_url)
            intent = "general"
        # ---- LESSON PLAN (teacher only) ----
        if intent == "generate_lesson":
            if access_role != "teacher":
                return _finish("Only teachers can create full lesson plans. "
                               "Please sign in with a teacher EduNex account.")
            source_text = reply_ctx if reply_ctx else doc_context
            spec = extract_lesson_spec_llm(
                search_message, get_recent_turns_text(session_id, n=6))
            spec["request"] = search_message
            if not spec.get("topic") and source_text and len(source_text.split()) > 8:
                spec["topic"] = topic_from_material(source_text)
                print(f"[lesson] topic from supplied material: {spec['topic']!r}")
            slot, question = next_missing_lesson_slot(spec)
            if slot and not (source_text and len(source_text.split()) > 8):
                SESSION_PENDING_LESSON[session_id] = spec
                status_message = None
                return _finish(question)
            print(f"[lesson] spec={spec}")
            return _generate_lesson_and_finish(
                build_lesson_request(spec), access_role, source_text,
                spec.get("topic", ""), lower_msg, _finish)
        # ---- QUESTION GENERATION (teacher only) ----
        if intent == "generate_questions":
            if access_role != "teacher":
                return _finish("Only teachers can generate assessment questions. "
                               "Please sign in with a teacher EduNex account.")
            history_topic = infer_topic_from_history(session_id)
            spec = extract_question_spec_llm(
                search_message, reply_ctx, history_topic,
                get_recent_turns_text(session_id, n=6))
            source_text = reply_ctx if reply_ctx else doc_context
            if source_text and len(source_text.split()) > 15:
                if not (spec.get("topic") or "").strip():
                    spec["topic"] = topic_from_material(source_text)
                    print(f"[questions] topic from material: {spec['topic']!r}")
                print(f"[questions] grounding on supplied material ({len(source_text)} chars)")
                answer = generate_questions_from_text(
                    build_question_request(spec), access_role, spec, source_text)
                if (answer and not looks_like_model_refusal(answer)
                        and looks_like_question_set(answer, spec["count"])):
                    answer = wrap_bare_latex(strip_curriculum_meta(answer) or answer)
                    if "pdf" in lower_msg:
                        pdf_url = build_answer_pdf(answer, "questions")
                    return _finish(answer, pdf_url=pdf_url)
                print("[questions] supplied-material output was not a question set")
            spec["scope"] = parse_lesson_request_scope(
                search_message + " " + (spec.get("topic") or ""))
            if not (spec.get("topic") or "").strip():
                SESSION_PENDING_QUESTION[session_id] = {
                    "type": spec["type"], "count": spec["count"]}
                status_message = None
                label = {"mcq": "multiple-choice", "short_answer": "short-answer",
                         "fill_blank": "fill-in-the-blank", "true_false": "true/false",
                         "essay": "essay"}.get(spec["type"], "")
                return _finish(
                    f"Sure, I'll make {spec['count']} {label} questions. Which topic or "
                    f"lesson should they cover?")
            return _generate_questions_and_finish(
                spec, search_message, access_role, lower_msg, _finish)
        # ---- IMAGE GENERATION ----
        if intent == "generate_image" or pending_image_request:
            return _handle_image_generation(
                request, session_id, pending_image_request, lower_msg, _finish)
        # ---- SMALLTALK ----
        if intent == "smalltalk":
            return _finish(build_conversational_reply(request.message, student_name))
        answer = solve_simple_reasoning_question(search_message) or None
        if answer is None:
            answer = build_nexa_faq_answer(search_message, session_id=session_id) or None
        # Curriculum: retrieve on the search string, answer on the raw message.
        if answer is None and intent == "curriculum":
            curriculum = None
            try:
                curriculum = retrieve_curriculum_context(
                    strip_output_format_noise(search_message) or search_message,
                    parse_lesson_request_scope(search_message))
            except Exception as exc:
                print(f"Curriculum retrieval failed: {exc}")
            if curriculum and not _material_covers_topic(search_message, curriculum["context"]):
                print("[answer] retrieved material does not match the question -> general")
                curriculum = None
            if curriculum:
                material = _clean_pdf_artifacts(curriculum["context"])
                drafted = answer_with_history(
                    _CURRICULUM_FORMAT_SYSTEM.replace(
                        "{audience}", build_role_instruction(access_role)),
                    request.message, session_id, reply_ctx=reply_ctx,
                    extra_material=material, model=(llm_precise or llm))
                if drafted and not curriculum_answer_is_evasive(drafted):
                    answer = drafted
                    print("[answer] served from curriculum with history")
                else:
                    print("[answer] curriculum evasive or empty -> general knowledge")
            else:
                print("[answer] curriculum miss -> general knowledge")
        if answer is None:
            general_system = (
                "You are Nexa, a knowledgeable educational assistant for PNG students. "
                "Answer the CURRENT message, using the conversation above for context. "
                "If reference material is supplied below, it is a document the user "
                "uploaded: answer from it and do not describe the conversation itself. "
                "Follow every instruction in the message exactly: if the user asks for "
                "simpler language, a shorter answer, more examples, or one specific item "
                "from your previous reply, do that rather than starting a new explanation. "
                f"Clean Markdown. {_MATH_FORMAT_RULE} Never mention curriculum material, "
                "sources, or what any material does or does not contain. If genuinely "
                "unsure of a fact, say so briefly rather than inventing details. "
                f"Audience guidance: {build_role_instruction(access_role)}")
            answer = answer_with_history(
                general_system, request.message, session_id,
                reply_ctx=reply_ctx,
                extra_material=doc_context if (doc_context and not reply_ctx) else "") or None
            if answer:
                print("[answer] served from general knowledge with history")
        if answer is None:
            answer = ("I could not answer that just now. Please rephrase the question, "
                      "or name the topic you would like to explore.")
        answer = strip_curriculum_meta(answer) or answer
        answer = wrap_bare_latex(answer)
        if "pdf" in lower_msg and (answer or "").strip():
            pdf_url = build_answer_pdf(answer, "nexa")
        return _finish(answer, pdf_url=pdf_url)
    except Exception as e:
        print(f"Chat endpoint failed: {e}")
        raise HTTPException(status_code=500, detail="Server error")

@app.get("/health")
def health():
    
    return {"status": "ok"}

def _generate_questions_and_finish(spec, source_message, access_role, lower_msg, _finish):
    # Always generate from a resolved instruction, never from the raw message,
    # which may still contain an unresolved "from this".
    request_text = build_question_request(spec)
    print(f"[questions] request={request_text!r}")

    curriculum = retrieve_curriculum_context(spec["topic"], spec.get("scope") or {})

    if curriculum and not _material_covers_topic(spec["topic"], curriculum["context"]):
        print(f"[questions] material does not cover {spec['topic']!r} -> direct")
        curriculum = None

    answer = ""
    try:
        if curriculum:
            print(f"[questions] type={spec['type']} count={spec['count']} "
                  f"topic={spec['topic']!r} -> curriculum")
            grounded = generate_questions_grounded(
                request_text, access_role, spec, curriculum)
            if (grounded and not looks_like_model_refusal(grounded)
                    and looks_like_question_set(grounded, spec["count"])):
                answer = grounded
            else:
                print("[questions] grounded output was not a question set")

        if not answer:
            print(f"[questions] topic {spec['topic']!r} -> direct generation")
            direct = generate_questions_direct(request_text, access_role, spec)
            if (direct and not looks_like_model_refusal(direct)
                    and looks_like_question_set(direct, spec["count"])):
                answer = direct
            else:
                print("[questions] direct output was not a question set, retrying")
                # One retry with an explicit format demand.
                retry_text = (
                    f"{request_text}\n\nOutput format: a numbered list of exactly "
                    f"{spec['count']} questions, then a '## Answer Key' section. "
                    "Do not write an explanation, an essay, or a lesson plan.")
                direct = generate_questions_direct(retry_text, access_role, spec)
                if (direct and not looks_like_model_refusal(direct)
                        and looks_like_question_set(direct, spec["count"])):
                    answer = direct
    except Exception as exc:
        print(f"Question generation failed: {exc}")

    if not (answer or "").strip():
        return _finish("I couldn't generate those questions just now. Please name the "
                       "topic and try again.")

    answer = wrap_bare_latex(strip_curriculum_meta(answer) or answer)
    pdf_url = build_answer_pdf(answer, "questions") if "pdf" in lower_msg else None
    return _finish(answer, pdf_url=pdf_url)

def _generate_lesson_and_finish(message, access_role, source_text, topic, lower_msg, _finish):
    answer = ""

    if source_text and len(source_text.split()) > 8:
        print(f"[lesson] building from quoted/uploaded material ({len(source_text)} chars)")
        drafted = generate_lesson_plan_from_text(message, access_role, source_text)
        if drafted and not looks_like_model_refusal(drafted):
            answer = drafted
        else:
            print("[lesson] from-material draft failed")

    if not answer:
        lesson_query = (topic or "").strip()
        if not lesson_query:
            # No topic means no safe search. Retrieving on a bare "create a
            # lesson plan" matches arbitrary chunks from the whole corpus.
            print("[lesson] no topic -> skipping retrieval, generating directly")
            curriculum = None
        else:
            scope = parse_lesson_request_scope(message)
            curriculum = retrieve_curriculum_context(lesson_query, scope)

            if curriculum and not _material_covers_topic(topic, curriculum["context"]):
                print(f"[lesson] retrieved material does not cover {topic!r} -> direct")
                curriculum = None

            subject_wanted = scope.get("subject")
            if (curriculum and subject_wanted and curriculum.get("subject")
                    and curriculum["subject"] != subject_wanted):
                print(f"[lesson] retrieved {curriculum['subject']} for a "
                      f"{subject_wanted} request -> direct")
                curriculum = None

        try:
            if curriculum:
                grounded = generate_lesson_plan_grounded(message, access_role, curriculum)
                if grounded and "NO_COVERAGE" in grounded.upper():
                    print("[lesson] grounded generator reported no coverage -> direct")
                    grounded = ""
                if grounded and not looks_like_model_refusal(grounded):
                    answer = grounded
                    print("[lesson] built from curriculum")
            if not answer:
                direct = generate_lesson_plan_direct(message, access_role)
                if direct and not looks_like_model_refusal(direct):
                    answer = direct
                    print("[lesson] built directly from the model")
        except Exception as exc:
            print(f"Lesson plan failed: {exc}")

    if not (answer or "").strip():
        return _finish("I could not draft the lesson plan just now. Please try again.")

    try:
        answer = normalize_lesson_plan_format(answer, message)
    except Exception as exc:
        print(f"Lesson format failed: {exc}")

    answer = re.sub(r"^#\s*(?:Thema|Theme|Unit|Topic|Strand)\s*[:\-]\s*", "# ",
                    answer, count=1, flags=re.MULTILINE | re.IGNORECASE)
    answer = strip_curriculum_meta(answer) or answer
    answer = wrap_bare_latex(answer)

    pdf_url = build_answer_pdf(answer, "lesson") if "pdf" in lower_msg else None
    return _finish(answer, pdf_url=pdf_url)

def _handle_image_generation(request, session_id, pending_image_request, lower_msg, _finish):
    if not (IMAGE_RUNTIME_AVAILABLE and IMAGE_FEATURE_ENABLED):
        SESSION_PENDING_IMAGE.pop(session_id, None)
        return _finish("Image generation is unavailable right now.")

    if pending_image_request:
        SESSION_PENDING_IMAGE.pop(session_id, None)
        message_for_image = pending_image_request
        reply = request.message.lower().strip()
        if reply in ("no", "none", "nope") or any(
            w in reply for w in ("no text", "without", "no words", "don't", "dont")):
            intent_img, text = "no_text", ""
        else:
            q = re.search(r'["\u201c\u2018\']([^"\u201d\u2019\']{1,80})["\u201d\u2019\']', request.message)
            intent_img, text = ("wants_text", q.group(1).strip()) if q else ("wants_text", request.message.strip())
    else:
        message_for_image = request.message
        if _is_vague_image_subject(request.message):
            inherited = infer_topic_from_history(session_id)
            if inherited:
                message_for_image = inherited
            else:
                return _finish('Sure — what should the image show? Name the topic, e.g. "the water cycle".')
        intent_img, text = analyze_image_text_intent(message_for_image)
        if intent_img == "ambiguous":
            SESSION_PENDING_IMAGE[session_id] = message_for_image
            return _finish("Should the image include any text? Reply with the words to show, or \"no text\".")

    ctx = ""
    try:
        hit = retrieve_curriculum_context(message_for_image, parse_lesson_request_scope(message_for_image))
        if hit:
            ctx = hit.get("context", "")
    except Exception:
        pass

    final_prompt = build_curriculum_image_prompt(message_for_image, ctx) if ctx \
        else build_image_generation_prompt(message_for_image, intent_img, text)
    if intent_img == "wants_text" and text:
        final_prompt += f' Include the text label: "{text}".'
    elif intent_img == "no_text":
        final_prompt += " No text or words in the image."

    print("=" * 70)
    print(f"[image] ORIGINAL REQUEST : {message_for_image}")
    print(f"[image] DETAILED PROMPT  : {final_prompt}")
    print("=" * 70)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"image_{ts}.png"
    path = os.path.join(IMAGE_OUTPUT_DIR, filename)
    IMAGE_STATUS[ts] = "processing"
    threading.Thread(target=generate_image_task,
                     args=(final_prompt, path, ts, intent_img == "wants_text")).start()
    return _finish("Generating image...", image_url=f"/assets/{filename}", image_id=ts)

# ====================== RUN ======================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
