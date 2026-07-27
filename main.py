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
from typing import Optional, Dict, Any, List
import datetime
from fastapi.staticfiles import StaticFiles
import threading

try:
    import mysql.connector
except ModuleNotFoundError:
    mysql.connector = None

try:
    from ddgs import DDGS
except ModuleNotFoundError:
    DDGS = None

try:
    import wikipedia
except ModuleNotFoundError:
    wikipedia = None

LANGCHAIN_AVAILABLE = True
try:
    from langchain_community.document_loaders import PyPDFLoader
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    from langchain_ollama import OllamaEmbeddings, ChatOllama
    from langchain_chroma import Chroma
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain.chains import create_history_aware_retriever, create_retrieval_chain
    from langchain_core.runnables.history import RunnableWithMessageHistory
    from langchain_community.chat_message_histories import ChatMessageHistory
except ModuleNotFoundError:
    LANGCHAIN_AVAILABLE = False

IMAGE_RUNTIME_AVAILABLE = True
try:
    from diffusers import DiffusionPipeline
    import torch
except ModuleNotFoundError:
    IMAGE_RUNTIME_AVAILABLE = False
    DiffusionPipeline = None
    torch = None

try:
    from markdown_pdf import MarkdownPdf, Section
except ModuleNotFoundError:
    MarkdownPdf = None
    Section = None

import textwrap


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
        catalog_offset = 0
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


class SharedChatResponse(BaseModel):
    share_token: str
    session_id: str
    created_at_utc: str
    messages: Any


# Server-side stack to mirror push/pop operations done by the UI.
chat_stack = []


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

def persist_chat_log(log_id: str, session_id: Optional[str], user_email: Optional[str], user_name: str, user_prompt: str, nexa_response: str, pdf_url: Optional[str] = None, stars: int = 0, timestamp: Optional[datetime.datetime] = None) -> bool:
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
                log_id,
                session_id,
                normalize_email_address(user_email),
                user_name,
                user_prompt,
                nexa_response,
                pdf_url,
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


def build_general_knowledge_answer(message: str) -> str:
    query = build_web_results_query(message)
    lowered = (message or "").lower()

    if "wikipedia" in lowered or "wiki" in lowered:
        return fetch_wikipedia_summary(query)

    if looks_like_web_query(message):
        return fetch_web_results(query)

    return ""

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

    "At the very end of every lesson plan, on its own line, append exactly:\n"
    "'---\\n*This content is generated by Nexa AI. Please review and adapt it to your classroom "
    "context, verify accuracy against your official curriculum, and use professional judgement "
    "before delivery.*'\n\n"

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

def generate_image_task(prompt, path, image_id):

    try:
        if pipe is None or torch is None:
            IMAGE_STATUS[image_id] = "failed"
            return

        print(f"Starting generation for {image_id}")

        image = pipe(
            prompt=prompt,
            negative_prompt="blurry, low quality",
            width=1024,
            height=1024,
            num_inference_steps=50,
            true_cfg_scale=5.0,
            generator=torch.Generator(device="cuda").manual_seed(42)
        ).images[0]

        image.save(path)

        print(f"Image saved: {path}")

        torch.cuda.empty_cache()

        # ✅ VERY IMPORTANT
        IMAGE_STATUS[image_id] = "ready"

        print(f"Image status updated: {image_id} -> ready")

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

            if image_base64 and log_id and user_name:
                assistant_message["image_url"] = f"/api/chat-image/{log_id}?user_name={quote_plus(user_name)}"

            if image_mime_type:
                assistant_message["image_mime_type"] = image_mime_type

            if image_filename:
                assistant_message["image_filename"] = image_filename

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
    safe_token = html.escape(share_token)

    return HTMLResponse(f'''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Shared Nexa Chat</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
</head>
<body class="bg-slate-100 min-h-screen">
    <main class="max-w-5xl mx-auto px-4 py-8">
        <div class="bg-white rounded-3xl shadow-xl border border-slate-200 overflow-hidden">
            <div class="px-6 py-5 border-b border-slate-200 bg-slate-50 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
                <div>
                    <h1 class="text-2xl font-bold text-slate-900">Shared Nexa Chat</h1>
                    <p class="text-sm text-slate-500 mt-1">Read and continue this conversation from any browser.</p>
                </div>
                <span class="text-xs bg-amber-100 text-amber-800 px-3 py-2 rounded-full font-semibold">External access</span>
            </div>

            <div class="p-6 space-y-6">
                <div id="chat-messages" class="space-y-5"></div>
                <div id="chat-empty" class="text-slate-500">Loading shared chat...</div>
            </div>

            <div class="px-6 py-5 border-t border-slate-200 bg-slate-50">
                <form id="shared-chat-form" class="flex flex-col gap-3 sm:flex-row">
                    <label class="sr-only" for="shared-message">Your message</label>
                    <textarea id="shared-message" rows="2" class="min-h-[90px] w-full rounded-3xl border border-slate-300 px-4 py-3 text-sm text-slate-900 focus:border-sky-500 focus:ring-sky-200" placeholder="Type your question or continue the shared chat..."></textarea>
                    <button id="shared-send-btn" type="submit" class="inline-flex items-center justify-center rounded-3xl bg-sky-600 px-5 py-3 text-sm font-semibold text-white transition hover:bg-sky-700">Send</button>
                </form>
                <p id="shared-status" class="mt-3 text-xs text-slate-500">Your messages will be added to the shared chat session.</p>
            </div>
        </div>
    </main>

<script>
const SHARE_TOKEN = "{safe_token}";
let SHARED_SESSION_ID = null;
let isSubmitting = false;

function escapeHtml(str) {{
    return String(str || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}}

function appendSharedMessage(text, role) {{
    const messages = document.getElementById("chat-messages");
    const isUser = role === "user";
    const bubbleClass = isUser ? 'bg-slate-900 text-white rounded-2xl rounded-br-md' : 'bg-sky-50 text-slate-800 rounded-2xl rounded-bl-md';
    const content = isUser
        ? `<div class="whitespace-pre-wrap break-words">${{escapeHtml(text)}}</div>`
        : `<div class="prose max-w-none">${{marked.parse(text || "")}}</div>`;

    const wrapper = document.createElement('div');
    wrapper.className = isUser ? 'flex justify-end' : 'flex justify-start';
    wrapper.innerHTML = '<div class="max-w-[92%] ' + bubbleClass + ' px-5 py-4">' + content + '</div>';

    messages.appendChild(wrapper);
    messages.scrollTop = messages.scrollHeight;
}}

function renderSharedMessages(messages) {{
    const messagesContainer = document.getElementById("chat-messages");
    const emptyNotice = document.getElementById("chat-empty");

    messagesContainer.innerHTML = "";
    emptyNotice.classList.add('hidden');

    if (!messages.length) {{
        emptyNotice.textContent = 'No messages found in this shared chat yet. Start the conversation below.';
        emptyNotice.classList.remove('hidden');
        return;
    }}

    messages.forEach(message => {{
        appendSharedMessage(message.content || '', message.role === 'assistant' ? 'assistant' : 'user');
    }});
}}

async function loadSharedChat() {{
    const status = document.getElementById('shared-status');
    status.textContent = 'Loading shared chat...';

    try {{
        const res = await fetch(`/api/shared-chat/${{encodeURIComponent(SHARE_TOKEN)}}`);
        if (!res.ok) {{
            document.getElementById('chat-messages').innerHTML = '';
            document.getElementById('chat-empty').textContent = 'This shared chat link is unavailable or expired.';
            document.getElementById('chat-empty').classList.remove('hidden');
            status.textContent = '';
            return;
        }}

        const data = await res.json();
        SHARED_SESSION_ID = data.session_id;
        renderSharedMessages(Array.isArray(data.messages) ? data.messages : []);
        status.textContent = 'Continue the conversation from this shared chat session.';
    }} catch (err) {{
        document.getElementById('chat-empty').textContent = 'Failed to load shared chat. Please refresh the page.';
        document.getElementById('chat-empty').classList.remove('hidden');
        console.error('Shared chat load failed', err);
        status.textContent = 'Unable to load shared chat right now.';
    }}
}}

async function sendSharedMessage(event) {{
    event.preventDefault();
    if (isSubmitting) return;

    const textarea = document.getElementById('shared-message');
    const message = textarea.value.trim();
    if (!message) return;
    if (!SHARED_SESSION_ID) {{
        document.getElementById('shared-status').textContent = 'Unable to send message until shared chat has loaded.';
        return;
    }}

    isSubmitting = true;
    const sendBtn = document.getElementById('shared-send-btn');
    sendBtn.disabled = true;
    sendBtn.textContent = 'Sending...';
    appendSharedMessage(message, 'user');
    textarea.value = '';
    document.getElementById('shared-status').textContent = 'Sending message...';

    try {{
        const response = await fetch('/chat', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ message, session_id: SHARED_SESSION_ID }})
        }});

        const data = await response.json();
        if (!response.ok) {{
            throw new Error(data.detail || `Chat API returned ${{response.status}}`);
        }}

        const assistantResponse = data.response || 'No response received.';
        if (data.log_id) {{
            console.log('Shared chat stored with log_id:', data.log_id);
        }}
        appendSharedMessage(assistantResponse, 'assistant');
        document.getElementById('shared-status').textContent = 'Message sent. You can continue the chat below.';
    }} catch (err) {{
        console.error('Shared chat send failed', err);
        document.getElementById('shared-status').textContent = 'Failed to send message. Please try again.';
    }} finally {{
        isSubmitting = false;
        sendBtn.disabled = false;
        sendBtn.textContent = 'Send';
    }}
}}

document.getElementById('shared-chat-form').addEventListener('submit', sendSharedMessage);
loadSharedChat();
</script>
</body>
</html>
    ''')


class ChatRequest(BaseModel):
    message: str
    user_email: Optional[str] = None
    session_id: Optional[str] = None
    reply_context: Optional[str] = None
    url: Optional[str] = None   # NEW: web page to read

class ChatResponse(BaseModel):
    response: str
    session_id: str
    access_role: Optional[str] = None
    pdf_url: Optional[str] = None
    image_url: Optional[str] = None
    image_id: Optional[str] = None
    log_id: Optional[str] = None


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
        pdf_url=None, image_url=None, image_id=None, log_id=user_log_id,
    )

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):

    access_role = infer_access_role(request.user_email)
    normalized_email = normalize_email_address(request.user_email)
    session_id = request.session_id or str(uuid.uuid4())
    SESSION_ACCESS_PROFILE[session_id] = {"email": normalized_email, "role": access_role}

    hydrate_session_history(session_id)
    record_chat_turn(session_id, "user", request.message)

    # Create a single log_id for this user -> assistant turn so frontend can attach images/files.
    user_log_id = str(uuid.uuid4())
    try:
        user_name = SESSION_ACCESS_PROFILE.get(session_id, {}).get('email') or TEST_USER_NAME
        persist_chat_log(
            log_id=user_log_id,
            session_id=session_id,
            user_email=normalized_email,
            user_name=user_name,
            user_prompt=(request.message or "").strip(),
            nexa_response="",
            pdf_url=None,
            stars=0,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
    except Exception:
        # If persistence fails (e.g., DB not available), continue without blocking the chat flow.
        pass

    # Block lesson-plan generation for non-teachers
    lower_msg = (request.message or "").lower()
    if any(phrase in lower_msg for phrase in ("lesson plan", "create lesson", "create a lesson", "make a lesson")) and access_role != "teacher":
        answer = "Only teachers can create full lesson plans. Please sign in with a teacher EduNex account."
        record_chat_turn(session_id, "assistant", answer)
        try:
            user_name = SESSION_ACCESS_PROFILE.get(session_id, {}).get('email') or TEST_USER_NAME
            persist_chat_log(
                log_id=user_log_id,
                session_id=session_id,
                user_email=normalized_email,
                user_name=user_name,
                user_prompt=(request.message or "").strip(),
                nexa_response=(answer or "").strip(),
                pdf_url=None,
                stars=0,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
        except Exception:
            pass
        return ChatResponse(
            response=answer,
            session_id=session_id,
            access_role=access_role,
            pdf_url=None,
            image_url=None,
            image_id=None,
            log_id=user_log_id,
        )

    try:
        config = {"configurable": {"session_id": session_id}}

        # ============ ANSWER RESOLUTION: FAQ -> explicit web -> RAG -> web fallback ============
        faq_answer = build_nexa_faq_answer(request.message, session_id=session_id)
        if faq_answer:
            answer = faq_answer
        else:
            # Explicit "search the web" / "wikipedia ..." requests still go straight to web.
            explicit_web = build_general_knowledge_answer(request.message)
            if explicit_web:
                answer = explicit_web
            elif conversational_rag_chain is None:
                # No RAG available — try web synthesis before giving up.
                answer = synthesize_web_answer(request.message, access_role) or (
                    "Chat is available, but the curriculum model dependencies are not installed in this workspace."
                )
            else:
                # Build the augmented question: inject reply context and any uploaded document.
                doc_context = SESSION_DOCUMENT_BUFFER.get(session_id, "")
                augmented_input = request.message

                if request.reply_context:
                    augmented_input = (
                        f"The user is replying to your previous message: \"{request.reply_context}\"\n\n"
                        f"Their follow-up: {request.message}"
                    )

                if doc_context:
                    augmented_input = (
                        f"Use the following document the user uploaded in this session when relevant:\n"
                        f"{doc_context}\n\n"
                        f"User question: {augmented_input}"
                    )

                # 1) Try the curriculum RAG chain first (guarded against Ollama load failures).
                try:
                    result = conversational_rag_chain.invoke(
                        {"input": augmented_input, "audience": build_role_instruction(access_role)},
                        config=config
                    )
                    rag_answer = (result.get("answer") or "").strip()
                except Exception as model_error:
                    print(f"RAG/LLM call failed: {model_error}")
                    rag_answer = ""  # fall through to web fallback / friendly message below

                # 2) If the PDFs didn't cover it (and there's no uploaded doc driving the
                #    answer), fall back to a web-synthesized answer.
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

        # Guarantee the Nexa disclaimer on lesson plans even if the model omits it.
        disclaimer = (
            "\n\n---\n*This content is generated by Nexa AI. Please review and adapt it to your "
            "classroom context, verify accuracy against your official curriculum, and use "
            "professional judgement before delivery.*"
        )
        if any(p in lower_msg for p in ("lesson plan", "teaching plan", "make a lesson")) \
           and isinstance(answer, str) and "generated by Nexa AI" not in answer:
            answer = answer + disclaimer

        pdf_url = None
        image_url = None
        image_id = None

        # ================= PDF GENERATION =================
        if "pdf" in request.message.lower():
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"lesson_{ts}.pdf"
            path = os.path.join(IMAGE_OUTPUT_DIR, filename)

            safe_answer = answer if isinstance(answer, str) else str(answer or "")

            # markdown-pdf builds a TOC that requires the doc to start at H1.
            # Ensure there is a leading H1 so it never throws "hierarchy level must be 1".
            stripped = safe_answer.lstrip()
            if not stripped.startswith("# "):
                safe_answer = "# Nexa AI Document\n\n" + safe_answer

            try:
                if MarkdownPdf is not None and Section is not None:
                    # toc_level=0 disables the table of contents and its hierarchy check.
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
        if any(k in request.message.lower() for k in ["image", "diagram", "draw", "visual"]):

            if not IMAGE_RUNTIME_AVAILABLE:
                answer = "Your image request was received, but image generation is unavailable in this workspace."
                record_chat_turn(session_id, "assistant", answer)
                try:
                    user_name = SESSION_ACCESS_PROFILE.get(session_id, {}).get('email') or TEST_USER_NAME
                    persist_chat_log(
                        log_id=user_log_id,
                        session_id=session_id,
                        user_email=normalized_email,
                        user_name=user_name,
                        user_prompt=(request.message or "").strip(),
                        nexa_response=(answer or "").strip(),
                        pdf_url=pdf_url,
                        stars=0,
                        timestamp=datetime.datetime.now(datetime.timezone.utc),
                    )
                except Exception:
                    pass
                return ChatResponse(
                    response=answer,
                    session_id=session_id,
                    access_role=access_role,
                    pdf_url=pdf_url,
                    image_url=None,
                    image_id=None,
                    log_id=user_log_id,
                )

            answer = "Your image is generating..."

            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"image_{ts}.png"
            path = os.path.join(IMAGE_OUTPUT_DIR, filename)

            image_id = ts  # IMPORTANT for frontend polling

            prompt = f"{request.message}. educational diagram, clean labels, high quality, textbook style"

            # NON-BLOCKING BACKGROUND THREAD
            threading.Thread(
                target=generate_image_task,
                args=(prompt, path, image_id)
            ).start()

            image_url = f"/assets/{filename}"

        record_chat_turn(session_id, "assistant", answer)

        # Server-side persistence: update the earlier user log entry with the assistant response
        try:
            user_name = SESSION_ACCESS_PROFILE.get(session_id, {}).get('email') or TEST_USER_NAME
            persist_chat_log(
                log_id=user_log_id,
                session_id=session_id,
                user_email=normalized_email,
                user_name=user_name,
                user_prompt=(request.message or "").strip(),
                nexa_response=(answer or "").strip(),
                pdf_url=pdf_url,
                stars=0,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
        except Exception:
            pass

        return ChatResponse(
            response=answer,
            session_id=session_id,
            access_role=access_role,
            pdf_url=pdf_url,
            image_url=image_url,
            image_id=image_id,
            log_id=user_log_id,
        )

    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail="Server error")

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
            if image_base64 and log_id and user_name:
                message["image_url"] = f"/api/chat-image/{log_id}?user_name={quote_plus(user_name)}"
                if image_mime_type:
                    message["image_mime_type"] = image_mime_type
                if image_filename:
                    message["image_filename"] = image_filename
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


@app.get("/health")
def health():
    
    return {"status": "ok"}

# ====================== RUN ======================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
