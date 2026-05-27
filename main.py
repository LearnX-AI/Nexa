from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uuid
import os
from typing import Optional, Dict, Any
import datetime
from fastapi.staticfiles import StaticFiles
import threading

# LangChain
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_ollama import OllamaEmbeddings, ChatOllama
from langchain_chroma import Chroma
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.output_parsers import StrOutputParser
from langchain.chains import create_history_aware_retriever, create_retrieval_chain
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_community.chat_message_histories import ChatMessageHistory

# Image
from diffusers import DiffusionPipeline
import torch

# PDF
from markdown_pdf import MarkdownPdf, Section

# ====================== CONFIG ======================
PDF_PATHS = [
    "/home/admin/Nexa/G6_Science_Textbook_removed_compressed (1).pdf",
    "/home/admin/Nexa/gr12Ente3.pdf",
    "/home/admin/Nexa/gr13Phyte3.pdf",
    "/home/admin/Nexa/Gr12te3.pdf"
]

INDEX_PATH="/home/admin/Nexa/index.html"

MODEL_NAME = "llama3.1:8b-instruct-q5_K_M"
EMBED_MODEL = "nomic-embed-text"

SESSION_STORE: Dict[str, Any] = {}

IMAGE_OUTPUT_DIR = "/home/admin/Nexa/assets"
UPLOAD_DIR = "/home/admin/Nexa/uploads"

os.makedirs(IMAGE_OUTPUT_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

IMAGE_STATUS: Dict[str, str] = {}

# ====================== LOAD PDFs ======================
print("Loading PDFs...")
docs = []
for pdf in PDF_PATHS:
    loader = PyPDFLoader(pdf)
    docs.extend(loader.load())

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
contextualize_q_prompt = ChatPromptTemplate.from_messages([
    ("system", "Given the chat history and latest user question, reformulate it as a standalone query about the curriculum."),
    MessagesPlaceholder("chat_history"),
    ("human", "{input}"),
])

history_aware_retriever = create_history_aware_retriever(llm, retriever, contextualize_q_prompt)

# ====================== YOUR SYSTEM PROMPT (UNCHANGED) ======================
system_prompt = ( "You are an expert educator and curriculum designer. " "Use ONLY the provided curriculum excerpts and previous conversation context " "to create high-quality, engaging lesson plans. " "Always stay faithful to the curriculum PDFs. " "\n\n" "- If the user asks a simple question (What is..., Explain..., Define..., etc.), give a **clear, direct, and student-friendly explanation**. Do NOT use >" "- Only use the full lesson plan structure when the user explicitly says 'lesson plan', 'create a lesson plan', 'teaching plan', or 'make a lesson'.\n\n" "Output **everything in clean Markdown format** so it can be easily converted to PDF:\n" "- Start with a single # Main Title\n" "- Use ## for major sections (Objectives, Materials, Activities, etc.)\n" "- Use ### for subsections\n" "- Use - or * for bullet points\n" "- Use 1. 2. 3. for numbered steps\n" "- Use **bold** and *italic* where appropriate\n" "- Use Markdown tables when showing rubrics, materials lists, or schedules\n" "\n" "Required structure:\n" "Grade\n" "Subject\n" "Topic\n" "Learning Objectives (aligned to curriculum)\n" "Duration\n" "Materials\n" "Step-by-step Activities\n" "Differentiation strategies\n" "Assessment methods\n" "Extensions / Homework\n\n" "Curriculum context: {context}\n\n" "Chat history (for continuity): {chat_history}" )

qa_prompt = ChatPromptTemplate.from_messages([
    ("system", system_prompt),
    MessagesPlaceholder("chat_history"),
    ("human", "{input}"),
])

question_answer_chain = qa_prompt | llm | StrOutputParser()

rag_chain = create_retrieval_chain(history_aware_retriever, question_answer_chain)

# ====================== SESSION ======================
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
print("Loading Qwen Image...")
pipe = DiffusionPipeline.from_pretrained(
    "Qwen/Qwen-Image-2512",
    torch_dtype=torch.bfloat16
).to("cuda")
print("Image model loaded")

def generate_image_task(prompt, path, image_id):

    try:
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
    if not os.path.exists(INDEX_PATH):
        return HTMLResponse("<h1>index.html not found</h1>", status_code=404)

    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

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

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):

    session_id = request.session_id or str(uuid.uuid4())

    try:
        config = {"configurable": {"session_id": session_id}}

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

            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"lesson_{ts}.pdf"
            path = os.path.join(IMAGE_OUTPUT_DIR, filename)

            pdf = MarkdownPdf()
            pdf.add_section(Section(answer))
            pdf.save(path)

            pdf_url = f"/assets/{filename}"

        # ================= IMAGE GENERATION (FIXED + SAFE) =================
        if any(k in request.message.lower() for k in ["image", "diagram", "draw", "visual"]):

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

@app.get("/image-status/{image_id}")
async def image_status(image_id: str):

    status = IMAGE_STATUS.get(image_id, "processing")

    return {
        "status": status
    }

# ====================== RUN ======================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
