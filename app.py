from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import uuid, os, datetime, sys
from typing import Optional, Dict, Any

# ================= LANGCHAIN =================
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_ollama import OllamaEmbeddings, ChatOllama
from langchain_chroma import Chroma
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.output_parsers import StrOutputParser
from langchain.chains import create_history_aware_retriever, create_retrieval_chain
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_community.chat_message_histories import ChatMessageHistory

# ================= IMAGE + PDF =================
import torch
from diffusers import StableDiffusion3Pipeline
from markdown_pdf import MarkdownPdf, Section

# ================= CONFIG =================
PDF_PATHS = [
    "/home/admin/Nexa/G6_Science_Textbook_removed_compressed (1).pdf",
    "/home/admin/Nexa/gr12Ente3.pdf",
    "/home/admin/Nexa/gr13Phyte3.pdf",
]

MODEL_NAME = "llama3.1:8b-instruct-q5_K_M"
EMBED_MODEL = "nomic-embed-text"

SESSION_STORE: Dict[str, Any] = {}
ASSETS_DIR = "/home/admin/Nexa/assets"
os.makedirs(ASSETS_DIR, exist_ok=True)

# ================= LOAD DOCUMENTS =================
print("Loading PDFs...")
docs = []
for pdf in PDF_PATHS:
    loader = PyPDFLoader(pdf)
    docs.extend(loader.load())

splitter = RecursiveCharacterTextSplitter(chunk_size=1500, chunk_overlap=300)
splits = splitter.split_documents(docs)

embeddings = OllamaEmbeddings(model=EMBED_MODEL)

vectorstore = Chroma.from_documents(
    documents=splits,
    embedding=embeddings,
    collection_name="curriculum_db"
)

retriever = vectorstore.as_retriever(search_kwargs={"k": 6})

llm = ChatOllama(model=MODEL_NAME, temperature=0.4)

# ================= PROMPTS =================
contextualize_q_prompt = ChatPromptTemplate.from_messages([
    ("system", "Rephrase into standalone curriculum query."),
    MessagesPlaceholder("chat_history"),
    ("human", "{input}")
])

history_aware_retriever = create_history_aware_retriever(
    llm, retriever, contextualize_q_prompt
)

system_prompt = """
You are an expert educator.

Generate structured lesson plans in Markdown.

Required:
- Title
- Grade
- Subject
- Topic
- Objectives
- Activities
- Assessment
- Homework

Context:
{context}
"""

qa_prompt = ChatPromptTemplate.from_messages([
    ("system", system_prompt),
    MessagesPlaceholder("chat_history"),
    ("human", "{input}")
])

rag_chain = create_retrieval_chain(
    history_aware_retriever,
    qa_prompt | llm | StrOutputParser()
)

def get_session_history(session_id):
    if session_id not in SESSION_STORE:
        SESSION_STORE[session_id] = ChatMessageHistory()
    return SESSION_STORE[session_id]

chat_chain = RunnableWithMessageHistory(
    rag_chain,
    get_session_history,
    input_messages_key="input",
    history_messages_key="chat_history"
)

# ================= LOAD IMAGE MODEL =================
print("Loading Stable Diffusion...")
pipe = StableDiffusion3Pipeline.from_pretrained(
    "stabilityai/stable-diffusion-3.5-large",
    torch_dtype=torch.bfloat16,
    use_safetensors=True
).to("cuda")
print("Image model ready!")

# ================= CORE FUNCTION =================
def process_request(message: str, session_id: str):
    config = {"configurable": {"session_id": session_id}}
    response = chat_chain.invoke({"input": message}, config=config)
    answer = response.get("answer", "No response generated.")

    pdf_url = None
    image_url = None

    # ===== PDF GENERATION =====
    if any(k in message.lower() for k in ["pdf", "download pdf", "export pdf"]):
        filename = f"lesson_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        path = os.path.join(ASSETS_DIR, filename)

        pdf = MarkdownPdf()
        pdf.add_section(Section(answer))
        pdf.save(path)

        pdf_url = f"/assets/{filename}"

    # ===== IMAGE GENERATION =====
    if any(k in message.lower() for k in ["image", "diagram", "draw", "illustration"]):
        prompt = f"Educational diagram: {message}"

        image = pipe(
            prompt,
            num_inference_steps=28,
            guidance_scale=3.5
        ).images[0]

        filename = f"img_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        path = os.path.join(ASSETS_DIR, filename)
        image.save(path)

        image_url = f"/assets/{filename}"

    return answer, pdf_url, image_url

# ================= FASTAPI =================
app = FastAPI(title="Nexa Chat API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None

class ChatResponse(BaseModel):
    response: str
    session_id: str
    pdf_url: Optional[str] = None
    image_url: Optional[str] = None

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    session_id = request.session_id or str(uuid.uuid4())

    try:
        answer, pdf_url, image_url = process_request(
            request.message,
            session_id
        )

        return ChatResponse(
            response=answer,
            session_id=session_id,
            pdf_url=pdf_url,
            image_url=image_url
        )

    except Exception as e:
        print("ERROR:", e)
        raise HTTPException(status_code=500, detail="Internal Server Error")

# ================= CLI MODE =================
def run_cli():
    print("\n🔥 Nexa CLI Mode")
    print("Type 'exit' to quit\n")

    session_id = str(uuid.uuid4())

    while True:
        user_input = input("You: ")

        if user_input.lower() == "exit":
            break

        try:
            answer, pdf_url, image_url = process_request(user_input, session_id)

            print("\n🤖 Nexa:\n")
            print(answer)

            if pdf_url:
                print(f"\n📄 PDF saved at: {ASSETS_DIR}")

            if image_url:
                print(f"\n🖼 Image saved at: {ASSETS_DIR}")

            print("\n" + "-"*50 + "\n")

        except Exception as e:
            print("Error:", e)

# ================= ENTRY =================
if __name__ == "__main__":
    if "cli" in sys.argv:
        run_cli()
    else:
        import uvicorn
        uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
