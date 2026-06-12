from fastapi import FastAPI, HTTPException, UploadFile, File
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
from typing import Optional, Dict, Any, List
import datetime
from fastapi.staticfiles import StaticFiles
import threading

try:
    import mysql.connector
except ModuleNotFoundError:
    mysql.connector = None

try:
    from duckduckgo_search import DDGS
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

# ====================== CONFIG ======================
WORKSPACE_DIR = os.path.dirname(__file__)

PDF_PATHS = [
    os.path.join(WORKSPACE_DIR, "G6_Science_Textbook_removed_compressed (1).pdf"),
    os.path.join(WORKSPACE_DIR, "gr12Ente3.pdf"),
    os.path.join(WORKSPACE_DIR, "gr13Phyte3.pdf"),
    os.path.join(WORKSPACE_DIR, "Gr12te3.pdf")
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

DB_CONFIG = {
    "host": os.getenv("NEXA_DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("NEXA_DB_PORT", "3306")),
    "user": os.getenv("NEXA_DB_USER", "nexa_user"),
    "password": os.getenv("NEXA_DB_PASSWORD", "NexaPass123!"),
    "database": os.getenv("NEXA_DB_NAME", "nexa_ai"),
}

TEST_USER_NAME = "Test User"


class ChatLogPayload(BaseModel):
    log_id: str
    user_name: str
    user_prompt: str
    nexa_response: str
    timestamp: str
    session_id: Optional[str] = None
    user_email: Optional[str] = None
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
        return f"I could not fetch web results right now: {escape_markdown(str(exc))}"

    if not results:
        return (
            f"I could not fetch live web results for **{escape_markdown(query)}** right now. "
            "You can ask again with a more specific phrase, or try a Wikipedia lookup instead."
        )

    lines = [f"# Search Results for {escape_markdown(query)}", ""]
    for index, item in enumerate(results, start=1):
        title = escape_markdown(item.get("title") or "Untitled result")
        snippet = escape_markdown(item.get("body") or "No snippet available.")
        url = item.get("href") or item.get("url") or ""
        link = f"[{title}]({url})" if url else title
        lines.append(f"{index}. {link}")
        lines.append(f"   - {snippet}")

    lines.append("")
    lines.append("These are web search results. If you want, I can also open Wikipedia-style summaries for the same topic.")
    return "\n".join(lines)


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
            SELECT log_id, user_name, user_prompt, nexa_response, image_base64, image_mime_type, image_filename, timestamp_utc
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

    teacher_emails = _load_email_tokens("NEXA_TEACHER_EMAILS")
    student_emails = _load_email_tokens("NEXA_STUDENT_EMAILS")
    teacher_domains = _load_email_tokens("NEXA_TEACHER_EMAIL_DOMAINS")
    student_domains = _load_email_tokens("NEXA_STUDENT_EMAIL_DOMAINS")

    if normalized_email in teacher_emails or domain in teacher_domains:
        return "teacher"

    if normalized_email in student_emails or domain in student_domains:
        return "student"

    teacher_keywords = ("teacher", "staff", "faculty", "educator", "instructor", "lecturer", "tutor", "prof")
    student_keywords = ("student", "learner", "pupil", "scholar")

    if any(keyword in local_part for keyword in teacher_keywords):
        return "teacher"

    if any(keyword in local_part for keyword in student_keywords):
        return "student"

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
# ====================== YOUR SYSTEM PROMPT (UNCHANGED) ======================
system_prompt = ( "You are an expert educator and curriculum designer. " "Use ONLY the provided curriculum excerpts and previous conversation context " "to create high-quality, engaging lesson plans. " "Always stay faithful to the curriculum PDFs. " "\n\n" "- If the user asks a simple question (What is..., Explain..., Define..., etc.), give a **clear, direct, and student-friendly explanation**. Do NOT use >" "- Only use the full lesson plan structure when the user explicitly says 'lesson plan', 'create a lesson plan', 'teaching plan', or 'make a lesson'.\n\n" "Output **everything in clean Markdown format** so it can be easily converted to PDF:\n" "- Start with a single # Main Title\n" "- Use ## for major sections (Objectives, Materials, Activities, etc.)\n" "- Use ### for subsections\n" "- Use - or * for bullet points\n" "- Use 1. 2. 3. for numbered steps\n" "- Use **bold** and *italic* where appropriate\n" "- Use Markdown tables when showing rubrics, materials lists, or schedules\n" "\n" "Required structure:\n" "Grade\n" "Subject\n" "Topic\n" "Learning Objectives (aligned to curriculum)\n" "Duration\n" "Materials\n" "Step-by-step Activities\n" "Differentiation strategies\n" "Assessment methods\n" "Extensions / Homework\n\n" "Audience guidance: {audience}\n\n" "Curriculum context: {context}\n\n" "Chat history (for continuity): {chat_history}" )

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
            INSERT INTO nexa_chat_logs (log_id, session_id, user_email, user_name, user_prompt, nexa_response, timestamp_utc, stars)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                session_id = VALUES(session_id),
                user_email = VALUES(user_email),
                user_name = VALUES(user_name),
                user_prompt = VALUES(user_prompt),
                nexa_response = VALUES(nexa_response),
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

async function saveChatLog(logEntry) {{
    try {{
        await fetch('/api/chat-log', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(logEntry)
        }});
    }} catch (err) {{
        console.error('Shared log save failed', err);
    }}
}}

async function saveSharedLogResponse(logId, userPrompt, assistantResponse) {{
    const timestamp = new Date().toISOString();
    await saveChatLog({{
        log_id: logId,
        session_id: SHARED_SESSION_ID,
        user_name: 'External Visitor',
        user_prompt: userPrompt,
        nexa_response: assistantResponse,
        timestamp,
        stars: 0
    }});
}}

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

    const logId = `ext-${{Date.now()}}-${{Math.random().toString(36).slice(2,9)}}`;
    await saveChatLog({{
        log_id: logId,
        session_id: SHARED_SESSION_ID,
        user_name: 'External Visitor',
        user_prompt: message,
        nexa_response: '',
        timestamp: new Date().toISOString(),
        stars: 0
    }});

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
        appendSharedMessage(assistantResponse, 'assistant');
        await saveSharedLogResponse(logId, message, assistantResponse);
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


# ====================== CHAT ======================
class ChatRequest(BaseModel):
    message: str
    user_email: Optional[str] = None
    session_id: Optional[str] = None

class ChatResponse(BaseModel):
    response: str
    session_id: str
    access_role: Optional[str] = None
    pdf_url: Optional[str] = None
    image_url: Optional[str] = None
    image_id: Optional[str] = None


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

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):

    access_role = infer_access_role(request.user_email)
    normalized_email = normalize_email_address(request.user_email)
    session_id = request.session_id or str(uuid.uuid4())
    SESSION_ACCESS_PROFILE[session_id] = {"email": normalized_email, "role": access_role}
    record_chat_turn(session_id, "user", request.message)

    try:
        config = {"configurable": {"session_id": session_id}}

        web_answer = build_general_knowledge_answer(request.message)
        if web_answer:
            answer = web_answer
        elif conversational_rag_chain is None:
            answer = "Chat is available, but the curriculum model dependencies are not installed in this workspace."
        else:
            result = conversational_rag_chain.invoke(
                {"input": request.message, "audience": build_role_instruction(access_role)},
                config=config
            )

            answer = result.get("answer") or "No response"

        pdf_url = None
        image_url = None
        image_id = None

        # ================= PDF GENERATION (UNCHANGED) =================
        if "pdf" in request.message.lower():
            if MarkdownPdf is not None and Section is not None:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"lesson_{ts}.pdf"
                path = os.path.join(IMAGE_OUTPUT_DIR, filename)

                pdf = MarkdownPdf()
                pdf.add_section(Section(answer))
                pdf.save(path)

                pdf_url = f"/assets/{filename}"

        # ================= IMAGE GENERATION (FIXED + SAFE) =================
        if any(k in request.message.lower() for k in ["image", "diagram", "draw", "visual"]):

            if pipe is None:
                answer = "Your image request was received, but image generation is unavailable in this workspace."
                record_chat_turn(session_id, "assistant", answer)
                return ChatResponse(
                    response=answer,
                    session_id=session_id,
                    access_role=access_role,
                    pdf_url=pdf_url,
                    image_url=None,
                    image_id=None
                )

            answer = "Your image is generating..."

            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

            filename = f"image_{ts}.png"
            path = os.path.join(IMAGE_OUTPUT_DIR, filename)

            image_id = ts  # ✅ IMPORTANT for frontend polling

            prompt = f"{request.message}. educational diagram, clean labels, high quality, textbook style"

            # 🔥 NON-BLOCKING BACKGROUND THREAD (UNCHANGED LOGIC)
            threading.Thread(
                target=generate_image_task,
                args=(prompt, path, image_id)
            ).start()

            image_url = f"/assets/{filename}"

        record_chat_turn(session_id, "assistant", answer)

        return ChatResponse(
            response=answer,
            session_id=session_id,
            access_role=access_role,
            pdf_url=pdf_url,
            image_url=image_url,
            image_id=image_id
        )

    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail="Server error")


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
        if nexa_response:
            message = {"role": "assistant", "content": nexa_response}
            if image_base64 and log_id and user_name:
                message["image_url"] = f"/api/chat-image/{log_id}?user_name={quote_plus(user_name)}"
                if image_mime_type:
                    message["image_mime_type"] = image_mime_type
                if image_filename:
                    message["image_filename"] = image_filename
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
