from fastapi import FastAPI, HTTPException, UploadFile, File
import base64
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import html
import uuid
import os
import re
from typing import Optional, Dict, Any
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
system_prompt = ( "You are an expert educator and curriculum designer. " "Use ONLY the provided curriculum excerpts and previous conversation context " "to create high-quality, engaging lesson plans. " "Always stay faithful to the curriculum PDFs. " "\n\n" "- If the user asks a simple question (What is..., Explain..., Define..., etc.), give a **clear, direct, and student-friendly explanation**. Do NOT use >" "- Only use the full lesson plan structure when the user explicitly says 'lesson plan', 'create a lesson plan', 'teaching plan', or 'make a lesson'.\n\n" "Output **everything in clean Markdown format** so it can be easily converted to PDF:\n" "- Start with a single # Main Title\n" "- Use ## for major sections (Objectives, Materials, Activities, etc.)\n" "- Use ### for subsections\n" "- Use - or * for bullet points\n" "- Use 1. 2. 3. for numbered steps\n" "- Use **bold** and *italic* where appropriate\n" "- Use Markdown tables when showing rubrics, materials lists, or schedules\n" "\n" "Required structure:\n" "Grade\n" "Subject\n" "Topic\n" "Learning Objectives (aligned to curriculum)\n" "Duration\n" "Materials\n" "Step-by-step Activities\n" "Differentiation strategies\n" "Assessment methods\n" "Extensions / Homework\n\n" "Curriculum context: {context}\n\n" "Chat history (for continuity): {chat_history}" )

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
            INSERT INTO nexa_chat_logs (log_id, user_name, user_prompt, nexa_response, timestamp_utc, stars)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                user_name = VALUES(user_name),
                user_prompt = VALUES(user_prompt),
                nexa_response = VALUES(nexa_response),
                timestamp_utc = VALUES(timestamp_utc),
                stars = VALUES(stars)
            """,
            (
                payload.log_id,
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
        base64.b64decode(image_base64, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid base64 image payload") from exc

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE nexa_chat_logs
            SET image_base64 = %s,
                image_mime_type = %s,
                image_filename = %s,
                image_saved_at = %s
            WHERE log_id = %s AND user_name = %s
            """,
            (
                image_base64,
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
            SELECT image_base64, image_mime_type, image_filename
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

    image_base64, image_mime_type, image_filename = row
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

# ====================== CHAT ======================
class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None

class ChatResponse(BaseModel):
    response: str
    session_id: str
    pdf_url: Optional[str] = None
    image_url: Optional[str] = None
    image_id: Optional[str] = None


class ChatHistoryResponse(BaseModel):
    session_id: str
    messages: Any

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):

    session_id = request.session_id or str(uuid.uuid4())
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
                {"input": request.message},
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
            pdf_url=pdf_url,
            image_url=image_url,
            image_id=image_id
        )

    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail="Server error")


@app.get("/api/chat-history/{session_id}", response_model=ChatHistoryResponse)
def get_chat_history(session_id: str):
    return {
        "session_id": session_id,
        "messages": serialize_chat_history(session_id),
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
