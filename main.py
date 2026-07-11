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
    from langchain_community.vectorstores import Chroma
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

# ====================== CONFIG ======================
WORKSPACE_DIR = os.path.dirname(__file__)

PDF_PATHS = [
    os.path.join(WORKSPACE_DIR, "gr12Ente3.pdf"),
    os.path.join(WORKSPACE_DIR, "gr13Phyte3.pdf"),
    os.path.join(WORKSPACE_DIR, "Gr12te3.pdf"),

    # STEM Biology
    os.path.join(WORKSPACE_DIR, "STEMBIOLOGY-SRB.pdf"),
    os.path.join(WORKSPACE_DIR, "STEMBIOLOGY-TRB.pdf"),
    os.path.join(WORKSPACE_DIR, "STEMBIOLOGY-Syllabus.pdf"),
    os.path.join(WORKSPACE_DIR, "STEMBIOLOGYTG.pdf"),

    # STEM Chemistry
    os.path.join(WORKSPACE_DIR, "STEM-CHEMISTRY-SRB.pdf"),
    os.path.join(WORKSPACE_DIR, "STEM-CHEMISTRY-TRB.pdf"),
    os.path.join(WORKSPACE_DIR, "STEM-CHEMISTRY-Syllabus.pdf"),

    # STEM Engineering
    os.path.join(WORKSPACE_DIR, "STEM-ENGINEERING-SRB.pdf"),
    os.path.join(WORKSPACE_DIR, "STEM-ENGINEERING-TRB.pdf"),
    os.path.join(WORKSPACE_DIR, "STEM-ENGINEERING-Syllabus.pdf"),
    os.path.join(WORKSPACE_DIR, "STEM-ENGINEERING-TG.pdf"),

    # STEM Technology
    os.path.join(WORKSPACE_DIR, "STEM-TECHNOLOGY-SRB.pdf"),
    os.path.join(WORKSPACE_DIR, "STEM-TECHNOLOGY-TRB.pdf"),
    os.path.join(WORKSPACE_DIR, "STEM-TECHNOLOGY-Syllabus.pdf"),
    os.path.join(WORKSPACE_DIR, "STEM-TECHNOLOGY-TG.pdf"),
]

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

DB_CONFIG = {
    "host": os.getenv("NEXA_DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("NEXA_DB_PORT", "3306")),
    "user": os.getenv("NEXA_DB_USER", "nexa_user"),
    "password": os.getenv("NEXA_DB_PASSWORD", "NexaPass123!"),
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


def wrap_bare_latex(text: str) -> str:
    """Ensure LaTeX the model emitted without $ delimiters gets wrapped so KaTeX renders it.
    Applied to math answers before they are sent to the frontend."""
    if not text:
        return text

    # \boxed{...} (may contain one level of nested braces) -> $$...$$ if not already wrapped
    text = re.sub(
        r'(?<!\$)(\\boxed\{(?:[^{}]|\{[^{}]*\})*\})(?!\$)',
        r'$$\1$$',
        text,
    )

    # \int ... dt / dx  -> $$...$$
    text = re.sub(
        r'(?<!\$)(\\int[^\n]*?\bd[a-z]\b)(?!\$)',
        r'$$\1$$',
        text,
    )

    # Inline tokens like V_{total}, e^{-0.2t}, 120t^2, \frac{a}{b} -> $...$
    text = re.sub(
        r'(?<!\$)([A-Za-z0-9]*(?:\\[a-zA-Z]+|[_^]\{[^}]*\}|[_^][A-Za-z0-9])[A-Za-z0-9{}^_\\\-\.]*)(?!\$)',
        r'$\1$',
        text,
    )
    return text

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
    return any(s in m for s in signals) or sum(c in m for c in "∫∑√^=") >= 2

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
        "who is",
        "what is",
        "define",
        "latest",
        "news",
        "find",
        "lookup",
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


def build_image_generation_prompt(message: str, intent: str, text: str) -> str:
    if intent == "wants_text":
        if text:
            return (f'{message}. Clean professional design. IMPORTANT: display the exact text '
                    f'"{text}", spelled correctly, sharp and clearly readable. No other text, '
                    f'no random letters. High resolution.')
        return (f"{message}. Clean professional design with the requested wording spelled "
                f"correctly and clearly readable. No random or extra text. High resolution.")
    return (f"{message}. Clean, high-quality illustration with NO text, NO words, NO letters, "
            f"no captions, no labels, no watermark. High resolution.")

def build_general_knowledge_answer(message: str) -> str:
    query = build_web_results_query(message)
    lowered = (message or "").lower()

    if "wikipedia" in lowered or "wiki" in lowered:
        return fetch_wikipedia_summary(query)

    if looks_like_web_query(message):
        return fetch_web_results(query)

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

SESSION_PENDING_IMAGE: Dict[str, str] = {}

def normalize_faq_query(message: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", expand_common_contractions(message or "").strip().lower())


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


def query_is_about_nexa(message: str) -> bool:
    """FAQ entries are all about the assistant itself. Only treat a message as a
    potential FAQ hit if it references Nexa or the assistant directly. This stops
    curriculum questions (Newton's laws, photosynthesis, etc.) from being captured."""
    text = expand_common_contractions(message or "").lower()
    self_reference_signals = (
        "nexa", "you", "your", "yourself", "who made", "who created",
        "who built", "who owns", "who is behind", "what are you",
        "tell me about", "this ai", "this assistant", "this bot",
    )
    return any(signal in text for signal in self_reference_signals)


def build_nexa_faq_answer(message: str, session_id: Optional[str] = None) -> str:
    # Gate: skip FAQ entirely for questions that are not about Nexa.
    if not query_is_about_nexa(message):
        return ""

    normalized = canonicalize_faq_query(message)
    if not normalized:
        return ""

    # Exact / substring matches against the FAQ keys.
    for question, answer in NEXA_FAQ_ANSWERS.items():
        if normalized == question:
            return answer
        if normalized.startswith(question):
            return answer
        if question in normalized:
            return answer
        if normalized in question:
            return answer

    # Stricter fuzzy match (was 0.68 — too loose, caused false hits).
    fuzzy_answer = find_best_faq_answer(normalized, minimum_score=0.82)
    if fuzzy_answer:
        return fuzzy_answer

    # NOTE: the old LLM fallback was removed. A small local model frequently
    # returned a wrong FAQ instead of NO_MATCH, which is exactly what made
    # "explain three laws of Newton" return the image-generation answer.
    return ""

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

    local_part, _, domain = normalized_email.partition("@")
    # Prefer the domain-based EduNex account rules:
    # - teacher accounts use the education domain marker (e.g. yasaru.rathnasooriya@education.edu.pg)
    # - student accounts use the edunex domain marker (e.g. yasaru.rathnasooriya@edunex.edu.pg)
    # Keep the older local-part markers as a fallback so existing accounts still work.
    if domain.startswith("education.") or ".education" in local_part:
        return "teacher"

    if domain.startswith("edunex.") or ".edunex" in local_part:
        return "student"

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

    # Grade
    m = re.search(r"grade\s*(\d{1,2})", text, flags=re.IGNORECASE)
    if m:
        meta["grade"] = f"Grade {m.group(1)}"

    # Duration
    m = re.search(r"(\d+\s*(?:minutes|minute|mins|min|hours|hour|hrs|hr))", text, flags=re.IGNORECASE)
    if m:
        meta["duration"] = m.group(1)

    # Topic / Subject
    m = re.search(r"(?:lesson plan|create a lesson|make a lesson)\s*(?:on|about)\s+([^,\.\n]+)", text, flags=re.IGNORECASE)
    if m:
        topic = m.group(1).strip()
        topic = re.split(r"\s+for\b", topic, flags=re.IGNORECASE)[0].strip()
        meta["topic"] = topic
        if len(topic.split()) <= 3:
            meta["subject"] = topic
    else:
        m = re.search(r"in\s+([^,\.\n]+)\s*(?:for|grade|$)", text, flags=re.IGNORECASE)
        if m:
            meta["subject"] = m.group(1).strip()

    # Prerequisite
    m = re.search(r"prereq(?:uisite)?s?:?\s*([^,\.\n]+)", text, flags=re.IGNORECASE)
    if m:
        meta["prerequisite"] = m.group(1).strip()

    return meta


def normalize_lesson_plan_format(answer: str, request_message: str) -> str:
    """Normalize lesson plan responses into one friendly, consistent template."""
    if not _is_lesson_request(request_message):
        return answer

    if not isinstance(answer, str):
        answer = str(answer or "")

    # Note: generation disclaimer removed from lesson plan output

    meta = extract_lesson_metadata(request_message)
    default_value = "Not specified"
    grade = meta.get("grade") or default_value
    subject = meta.get("subject") or default_value
    topic = meta.get("topic") or default_value
    duration_text = meta.get("duration") or "40 minutes"
    prereq = meta.get("prerequisite") or default_value

    def cell(value: object) -> str:
        return str(value if value is not None else "").replace("|", r"\|").replace("\n", " ").replace("\r", " ").strip() or default_value

    # Determine total minutes if possible
    total_minutes = None
    m = re.search(r"(\d+)", duration_text)
    if m:
        try:
            total_minutes = int(m.group(1))
        except Exception:
            total_minutes = None

    # Fallback timings (minutes) distribution
    if total_minutes and total_minutes >= 10:
        intro = max(3, round(total_minutes * 0.12))
        direct = max(8, round(total_minutes * 0.30))
        guided = max(8, round(total_minutes * 0.30))
        independent = max(5, round(total_minutes * 0.18))
        closure = total_minutes - (intro + direct + guided + independent)
        if closure < 3:
            # adjust
            closure = 3
    else:
        intro = 5
        direct = 15
        guided = 10
        independent = 7
        closure = 3

    title = f"# {cell(topic) if topic != default_value else 'Lesson Plan'}"

    template_parts = []
    template_parts.append(title)
    template_parts.append("\nA friendly, classroom-ready lesson plan designed to be easy to scan and teach from.\n")

    # Quick snapshot table
    template_parts.append("\n## Quick Snapshot\n")
    template_parts.append("| Item | Details |\n")
    template_parts.append("|---|---|\n")
    template_parts.append(f"| Grade / Level | {cell(grade)} |\n")
    template_parts.append(f"| Subject | {cell(subject)} |\n")
    template_parts.append(f"| Topic | {cell(topic)} |\n")
    template_parts.append(f"| Duration | {cell(duration_text)} |\n")
    template_parts.append(f"| Prerequisite Knowledge | {cell(prereq)} |\n")

    template_parts.append("\n## Lesson Overview\nA short explanation of what the lesson is about, why it matters, and how it connects to what students already know.\n")
    template_parts.append("\n## What Students Will Learn\n- Understand the key idea behind the topic\n- Use important vocabulary correctly\n- Apply the idea in a guided activity or example\n- Show understanding in a short check for learning\n")

    template_parts.append("\n## Materials\n- Whiteboard or slides\n- Markers or pen\n- Student workbook, handout, or notebook\n- Any demonstration items or digital resources\n")

    template_parts.append("\n## Lesson Flow\n")
    template_parts.append("| Stage | Time | Teacher does | Students do |\n")
    template_parts.append("|---|---:|---|---|\n")
    template_parts.append(f"| Warm-up | {intro} min | Open with a question, image, or quick review to activate prior knowledge. | Share ideas, predict the topic, or recall what they already know. |\n")
    template_parts.append(f"| Teach | {direct} min | Explain the main concept with clear examples and simple checks for understanding. | Listen, note key ideas, and answer short questions. |\n")
    template_parts.append(f"| Guided Practice | {guided} min | Model one task and support students while they try it together. | Work with the teacher, ask questions, and complete the guided task. |\n")
    template_parts.append(f"| Independent Practice | {independent} min | Give an application task and monitor progress. | Work independently or in pairs to show understanding. |\n")
    template_parts.append(f"| Wrap-up | {closure} min | Summarize the lesson and end with a quick exit check. | Reflect on learning and complete the closing question. |\n")

    template_parts.append("\n## Assessment\n")
    template_parts.append("- Formative: questioning, quick recap, or mini whiteboard check\n")
    template_parts.append("- Summative: a short task, quiz, worksheet, or exit ticket that shows the main skill\n")
    template_parts.append("- Success criteria: students can explain the idea, use the vocabulary, and complete the task with support\n")

    template_parts.append("\n## Support and Extension\n- Support: sentence starters, visuals, worked examples, or partner support\n- Core: scaffolded practice with clear steps\n- Extension: challenge questions, deeper reasoning, or an independent task\n")

    template_parts.append("\n## Homework / Reflection\n- One short practice task or reflection question to check understanding\n- Optional extension activity for students who finish early\n")

    template_parts.append("\n## Teacher Notes\n- Common misconceptions to watch for\n- Pacing or classroom management tips\n- Any materials, safety notes, or reminders\n")

    if answer.strip():
        template_parts.append("\n## Original Draft\n<details>\n<summary>View the model draft used to create this lesson plan</summary>\n\n")
        template_parts.append(answer.strip() + "\n")
        template_parts.append("\n</details>\n")

    # Note: removed generation disclaimer per UI requirement

    out = "\n".join(p.strip() for p in template_parts if p is not None)
    return out


# append_generation_disclaimer removed — disclaimer injection disabled by project policy.

# ====================== LOAD PDFs ======================
docs = []
retriever = None
llm = None
rag_chain = None
conversational_rag_chain = None

if LANGCHAIN_AVAILABLE:
    print("Loading PDFs...")
    for pdf in PDF_PATHS:
        if os.path.exists(pdf):
            loader = PyPDFLoader(pdf)
            docs.extend(loader.load())

    if docs:
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=1500, chunk_overlap=300)
        splits = text_splitter.split_documents(docs)

        embeddings = OllamaEmbeddings(model=EMBED_MODEL)
        vectorstore = Chroma.from_documents(
            documents=splits,
            embedding=embeddings,
            collection_name="curriculum_db"
        )

        retriever = vectorstore.as_retriever(search_kwargs={"k": 6})

        llm = ChatOllama(model=MODEL_NAME, temperature=0.4)

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
pipe = None
if IMAGE_RUNTIME_AVAILABLE:
    print("Loading Qwen Image...")
    try:
        pipe = DiffusionPipeline.from_pretrained(
            "Qwen/Qwen-Image-2512",
            torch_dtype=torch.bfloat16
        ).to("cuda")
        print("Image model loaded")
    except Exception as exc:
        print("Image model unavailable:", exc)
        pipe = None

def generate_image_task(prompt, path, image_id, allow_text=False):
    try:
        current_pipe = pipe                       # use the model loaded at startup
        if current_pipe is None or torch is None:
            IMAGE_STATUS[image_id] = "failed"
            return

        if allow_text:
            negative_prompt = "blurry, low quality, distorted, duplicate text, overlapping text, watermark"
        else:
            negative_prompt = (
                "text, words, letters, captions, labels, writing, numbers, typography, "
                "watermark, signature, gibberish text, random letters, signage, "
                "blurry, low quality, distorted"
            )

        image = current_pipe(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=1024, height=1024,
            num_inference_steps=50,
            true_cfg_scale=5.0,
            generator=torch.Generator(device="cuda").manual_seed(42)
        ).images[0]
        image.save(path)
        IMAGE_STATUS[image_id] = "ready"
    except Exception as e:
        print("IMAGE THREAD ERROR:", e)
        IMAGE_STATUS[image_id] = "failed"

# ====================== FASTAPI ======================
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    reply_context: Optional[str] = None
    staged_file_name: Optional[str] = None
    url: Optional[str] = None   # NEW: web page to read

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

    # disclaimer removed — return model answer as-is
    answer = answer

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
    Returns '' if nothing usable was found."""
    query = build_web_results_query(message)
    context = gather_web_context(query)
    if not context:
        return ""

    # No LLM available — degrade gracefully to the cleaned link list.
    if not (LANGCHAIN_AVAILABLE and llm is not None):
        return fetch_web_results(query)

    synth_prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            "You are Nexa, an educational assistant for Papua New Guinea. "
            "Answer the user's question directly and accurately using ONLY the web "
            "context provided below. Write a clear explanation in your own words in "
            "clean Markdown. Do NOT invent facts beyond the context. If the topic is "
            "relevant to Papua New Guinea, mention that relevance. "
            "Finish with one line exactly like: 'Source: <url>'. "
            "Audience guidance: {audience}"
        ),
        ("human", "Question: {question}\n\nWeb context:\n{context}"),
    ])

    try:
        return (synth_prompt | llm | StrOutputParser()).invoke({
            "question": message,
            "context": context,
            "audience": build_role_instruction(access_role),
        }).strip()
    except Exception as exc:
        print(f"Web synthesis failed: {exc}")
        return fetch_web_results(query)

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

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):

    try:
        access_role = infer_access_role(request.user_email)
    except HTTPException:
        access_role = "student"
    normalized_email = normalize_email_address(request.user_email)
    session_id = request.session_id or str(uuid.uuid4())
    SESSION_ACCESS_PROFILE[session_id] = {"email": normalized_email, "role": access_role}

    if is_chat_turn_cancelled(request.turn_id):
        return ChatResponse(
            response="", session_id=session_id, access_role=access_role,
            status_message="Nexa is Thinking...", pdf_url=None,
            image_url=None, image_id=None, log_id=None,
        )

    hydrate_session_history(session_id)
    record_chat_turn(session_id, "user", request.message)

    capture_user_fact(normalized_email, request.message)

    page_url = (request.url or "").strip()
    page_question = (request.question or "").strip() or (request.message or "").strip()

    # ================= URL READING =================
    if page_url:
        page_text = fetch_page_text(page_url)
        if page_text.startswith("__ERROR__"):
            answer = page_text.replace("__ERROR__", "").strip()
            return ChatResponse(
                response=answer, session_id=session_id, access_role=access_role,
                status_message="Nexa is Searching ...", pdf_url=None,
                image_url=None, image_id=None, log_id=str(uuid.uuid4()),
            )

        existing = SESSION_DOCUMENT_BUFFER.get(session_id, "")
        combined = (existing + f"\n\n--- Web page: {page_url} ---\n{page_text}").strip()
        SESSION_DOCUMENT_BUFFER[session_id] = combined[:MAX_DOC_CHARS]

        question = page_question or "Summarize this web page clearly for a student."

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
                    "question": question, "page": page_text,
                    "audience": build_role_instruction(access_role),
                }).strip()
            except Exception as exc:
                print(f"URL read synthesis failed: {exc}")
                answer = "I read the page but could not process it just now. Please try again."
        else:
            answer = f"I read the page **{page_url}** but the language model is unavailable to summarize it."

        record_chat_turn(session_id, "assistant", answer)
        try:
            user_name = SESSION_ACCESS_PROFILE.get(session_id, {}).get('email') or TEST_USER_NAME
            persist_chat_log(
                log_id=str(uuid.uuid4()), session_id=session_id, user_email=normalized_email,
                user_name=user_name, user_prompt=f"[Read URL] {page_url} — {question}",
                nexa_response=answer, pdf_url=None, stars=0,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
        except Exception:
            pass

        return ChatResponse(
            response=answer, session_id=session_id, access_role=access_role,
            status_message="Nexa is Searching ...", pdf_url=None,
            image_url=None, image_id=None, log_id=str(uuid.uuid4()),
        )

    user_log_id = str(uuid.uuid4())
    try:
        user_name = SESSION_ACCESS_PROFILE.get(session_id, {}).get('email') or TEST_USER_NAME
        persist_chat_log(
            log_id=user_log_id, session_id=session_id, user_email=normalized_email,
            user_name=user_name, user_prompt=(request.message or "").strip(),
            nexa_response="", pdf_url=None, stars=0,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
    except Exception:
        pass

    lower_msg = (request.message or "").lower()
    status_message, _ = infer_chat_status(request.message, access_role, request.staged_file_name)
    if any(phrase in lower_msg for phrase in ("lesson plan", "create lesson", "create a lesson", "make a lesson")) and access_role != "teacher":
        answer = "Only teachers can create full lesson plans. Please sign in with a teacher EduNex account."
        record_chat_turn(session_id, "assistant", answer)
        try:
            user_name = SESSION_ACCESS_PROFILE.get(session_id, {}).get('email') or TEST_USER_NAME
            persist_chat_log(
                log_id=user_log_id, session_id=session_id, user_email=normalized_email,
                user_name=user_name, user_prompt=(request.message or "").strip(),
                nexa_response=(answer or "").strip(), pdf_url=None, stars=0,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
        except Exception:
            pass
        return ChatResponse(
            response=answer, session_id=session_id, access_role=access_role,
            status_message=status_message, pdf_url=None,
            image_url=None, image_id=None, log_id=user_log_id,
        )

    try:
        config = {"configurable": {"session_id": session_id}}

        if is_chat_turn_cancelled(request.turn_id):
            return ChatResponse(
                response="", session_id=session_id, access_role=access_role,
                status_message=status_message, pdf_url=None,
                image_url=None, image_id=None, log_id=None,
            )

        pending_image_request = SESSION_PENDING_IMAGE.get(session_id)

        if pending_image_request:
            answer = ""
        else:
            reasoning_answer = solve_simple_reasoning_question(request.message)
            if reasoning_answer:
                answer = reasoning_answer

            elif looks_like_math(request.message):
                if LANGCHAIN_AVAILABLE and llm is not None:
                    try:
                        math_prompt = ChatPromptTemplate.from_messages([
                            ("system",
                             "You are a precise mathematics tutor. Solve the problem ONE step at a time. "
                             "Do NOT produce a lesson plan or teaching objectives.\n"
                             "FORMATTING — follow EXACTLY, this is critical:\n"
                             "- Start with '## Problem' restating the question.\n"
                             "- Then '## Solution'. Number each step '### Step 1', '### Step 2', one action per step.\n"
                             "- Under each step: a short sentence, then the maths on its own line.\n"
                             "- CRITICAL: EVERY piece of mathematics MUST be wrapped in dollar signs. "
                             "Inline maths in single dollars like $x = 5$ or $\\cos(x)$. Displayed equations "
                             "in double dollars on their own line like $$\\int_0^5 x^2\\,dx$$.\n"
                             "- NEVER write a bare LaTeX command (\\cos, \\frac, \\int) outside dollar signs.\n"
                             "- NEVER write maths as plain ASCII like x^2 or e^(-t); always LaTeX inside dollars.\n"
                             "- End with '## Final Answer' and the result in $$...$$.\n"
                             "Audience: {audience}"),
                            ("human", "{problem}"),
                        ])
                        answer = (math_prompt | llm | StrOutputParser()).invoke({
                            "problem": request.message,
                            "audience": build_role_instruction(access_role),
                        }).strip()
                    except Exception as exc:
                        print(f"Math solve failed: {exc}")
                        answer = None
                else:
                    answer = None
            else:
                answer = None

            if answer is None:
                faq_answer = build_nexa_faq_answer(request.message, session_id=session_id)
                if faq_answer:
                    answer = faq_answer
                else:
                    explicit_web = build_general_knowledge_answer(request.message)
                    if explicit_web:
                        answer = explicit_web
                    elif conversational_rag_chain is None:
                        answer = synthesize_web_answer(request.message, access_role) or (
                            "Chat is available, but the curriculum model dependencies are not installed in this workspace."
                        )
                    else:
                        doc_context = SESSION_DOCUMENT_BUFFER.get(session_id, "")
                        augmented_input = request.message

                        user_facts = USER_MEMORY.get(normalized_email, {})
                        if user_facts:
                            facts_str = "; ".join(f"{k} = {v}" for k, v in user_facts.items())
                            augmented_input = (
                                f"Known facts for this user (use when relevant): {facts_str}\n\n"
                                f"{augmented_input}"
                            )

                        if request.reply_context:
                            augmented_input = (
                                f"The user is replying to your previous message: \"{request.reply_context}\"\n\n"
                                f"Their follow-up: {augmented_input}"
                            )

                        if doc_context:
                            augmented_input = (
                                f"Use the following document the user uploaded in this session when relevant:\n"
                                f"{doc_context}\n\n"
                                f"User question: {augmented_input}"
                            )

                        try:
                            result = conversational_rag_chain.invoke(
                                {"input": augmented_input, "audience": build_role_instruction(access_role)},
                                config=config
                            )
                            rag_answer = (result.get("answer") or "").strip()
                        except Exception as model_error:
                            print(f"RAG/LLM call failed: {model_error}")
                            rag_answer = ""

                        if rag_answer_is_weak(rag_answer) and not doc_context:
                            web_answer = synthesize_web_answer(request.message, access_role)
                            answer = web_answer or rag_answer or (
                                "I'm having trouble reaching the language model right now (it may be low on memory). "
                                "Please try again in a moment."
                            )
                        else:
                            answer = rag_answer or (
                                "I'm having trouble reaching the language model right now (it may be low on memory). "
                                "Please try again in a moment."
                            )

        try:
            if _is_lesson_request(lower_msg) and access_role == "teacher":
                answer = normalize_lesson_plan_format(answer, request.message)
        except Exception:
            pass

        pdf_url = None
        image_url = None
        image_id = None

        # ================= PDF GENERATION =================
        if "pdf" in request.message.lower():
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"lesson_{ts}.pdf"
            path = os.path.join(IMAGE_OUTPUT_DIR, filename)
            safe_answer = answer if isinstance(answer, str) else str(answer or "")
            stripped = safe_answer.lstrip()
            if not stripped.startswith("# "):
                safe_answer = "# Nexa AI Document\n\n" + safe_answer
            try:
                if MarkdownPdf is not None and Section is not None:
                    pdf = MarkdownPdf(toc_level=0)
                    pdf.add_section(Section(safe_answer))
                    pdf.save(path)
                    pdf_url = f"/assets/{filename}"
                else:
                    save_text_to_pdf(path, safe_answer)
                    pdf_url = f"/assets/{filename}"
            except Exception as pdf_error:
                print(f"PDF generation failed: {pdf_error}")
                try:
                    save_text_to_pdf(path, safe_answer)
                    pdf_url = f"/assets/{filename}"
                except Exception as fallback_error:
                    print(f"Fallback PDF generation failed: {fallback_error}")
                    pdf_url = None

        # ================= IMAGE GENERATION =================
        is_image_request = looks_like_image_generation_request(request.message) or bool(pending_image_request)

        if is_image_request:

            if not IMAGE_RUNTIME_AVAILABLE:
                SESSION_PENDING_IMAGE.pop(session_id, None)
                answer = "Your image request was received, but image generation is unavailable in this workspace."
                record_chat_turn(session_id, "assistant", answer)
                try:
                    user_name = SESSION_ACCESS_PROFILE.get(session_id, {}).get('email') or TEST_USER_NAME
                    persist_chat_log(
                        log_id=user_log_id, session_id=session_id, user_email=normalized_email,
                        user_name=user_name, user_prompt=(request.message or "").strip(),
                        nexa_response=(answer or "").strip(), pdf_url=pdf_url, stars=0,
                        timestamp=datetime.datetime.now(datetime.timezone.utc),
                    )
                except Exception:
                    pass
                return ChatResponse(
                    response=answer, session_id=session_id, access_role=access_role,
                    pdf_url=pdf_url, image_url=None, image_id=None, log_id=user_log_id,
                )

            if pending_image_request:
                SESSION_PENDING_IMAGE.pop(session_id, None)
                message_for_image = pending_image_request
                reply = request.message.lower().strip()
                if reply in ("no", "none", "nope") or any(
                    w in reply for w in ("no text", "without", "no words", "don't", "dont")
                ):
                    intent, text = "no_text", ""
                else:
                    q = re.search(r'["\u201c\u2018\']([^"\u201d\u2019\']{1,80})["\u201d\u2019\']', request.message)
                    if q:
                        intent, text = "wants_text", q.group(1).strip()
                    else:
                        intent, text = "wants_text", request.message.strip()
            else:
                intent, text = analyze_image_text_intent(request.message)
                message_for_image = request.message

                if intent == "ambiguous":
                    SESSION_PENDING_IMAGE[session_id] = request.message
                    answer = (
                        "I can create that for you. Quick question first: should the image "
                        "include any text or wording?\n\n"
                        "- If **yes**, reply with the exact words to show (for example: \"Welcome On Board\").\n"
                        "- If **no**, reply \"no text\" and I'll keep it clean with no writing."
                    )
                    record_chat_turn(session_id, "assistant", answer)
                    try:
                        user_name = SESSION_ACCESS_PROFILE.get(session_id, {}).get('email') or TEST_USER_NAME
                        persist_chat_log(
                            log_id=user_log_id, session_id=session_id, user_email=normalized_email,
                            user_name=user_name, user_prompt=(request.message or "").strip(),
                            nexa_response=answer, pdf_url=pdf_url, stars=0,
                            timestamp=datetime.datetime.now(datetime.timezone.utc),
                        )
                    except Exception:
                        pass
                    return ChatResponse(
                        response=answer, session_id=session_id, access_role=access_role,
                        pdf_url=pdf_url, image_url=None, image_id=None, log_id=user_log_id,
                    )

            final_prompt = build_image_generation_prompt(message_for_image, intent, text)
            answer = "Generating image..."

            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"image_{ts}.png"
            path = os.path.join(IMAGE_OUTPUT_DIR, filename)
            image_id = ts

            try:
                user_name = SESSION_ACCESS_PROFILE.get(session_id, {}).get('email') or TEST_USER_NAME
                persist_chat_log(
                    log_id=user_log_id, session_id=session_id, user_email=normalized_email,
                    user_name=user_name, user_prompt=(request.message or "").strip(),
                    nexa_response="Generating image...", pdf_url=pdf_url,
                    image_filename=filename, image_mime_type="image/png", image_base64=None,
                    stars=0, timestamp=datetime.datetime.now(datetime.timezone.utc),
                )
            except Exception:
                pass

            threading.Thread(
                target=generate_image_task,
                args=(final_prompt, path, image_id, intent == "wants_text")
            ).start()
            image_url = f"/assets/{filename}"

        record_chat_turn(session_id, "assistant", answer)

        try:
            user_name = SESSION_ACCESS_PROFILE.get(session_id, {}).get('email') or TEST_USER_NAME
            persist_chat_log(
                log_id=user_log_id, session_id=session_id, user_email=normalized_email,
                user_name=user_name, user_prompt=(request.message or "").strip(),
                nexa_response=(answer or "").strip(), pdf_url=pdf_url, stars=0,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
        except Exception:
            pass

        return ChatResponse(
            response=answer, session_id=session_id, access_role=access_role,
            status_message=status_message, pdf_url=pdf_url,
            image_url=image_url, image_id=image_id, log_id=user_log_id,
        )

    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail="Server error")


@app.get("/health")
def health():
    
    return {"status": "ok"}

# ====================== RUN ======================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
