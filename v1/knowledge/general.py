"""General-knowledge answers when the curriculum has none (NEXA-M2-006 extension).

When the approved curriculum holds no evidence for a question, NEXA can still
help, but it must never pass a general answer off as curriculum. A general
answer therefore:

- has its own status, ``general``, and ``source_kind`` ``general_knowledge``
  or ``web``, never ``grounded``;
- starts with a fixed label saying it is not from the approved curriculum
  materials, so a client that shows only the answer text still shows it;
- carries no curriculum citations ([S1]...). A web answer cites the pages it
  used as [W1]..[Wn]; any other marker is removed and reported;
- is told not to invent syllabus codes, outcomes or curriculum references.

Modes (NEXA_GENERAL_ANSWERS): ``off`` keeps the strict refusal; ``llm`` asks
the general model through the Model Gateway (``nexa-general``, falling back
to the answer model when that is unavailable); ``web`` searches the web first
and answers from the results with [W] citations, falling back to ``llm`` when
the search fails or finds nothing. A request can opt out (``general: false``)
but cannot turn general answers on when the deployment has them off, and only
the roles in NEXA_GENERAL_ROLES get them.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

from . import config

LABEL_LLM = ("Not from your approved curriculum materials: this is a general explanation. "
             "Check it with your teacher or textbook.")
LABEL_WEB = ("Not from your approved curriculum materials: this answer comes from web sources listed below. "
             "Check it with your teacher or textbook.")

GENERAL_PROMPT = (
    "You are NEXA, an educational assistant for Papua New Guinea (PNG) secondary school students in the "
    "National Department of Education STEM programme (Grades 11 and 12, National Schools of Excellence). The "
    "approved curriculum materials do not cover this question, so answer from general knowledge: accurately, "
    "clearly, in simple English, at a level suitable for a PNG secondary student. "
    "Make the answer specific to Papua New Guinea wherever the topic allows: use PNG examples, places, plants, "
    "animals, foods, industries, health issues and situations (for example kaukau, taro, sago, coconut, cocoa "
    "and coffee growing; birds of paradise and tree kangaroos; the Sepik and Fly rivers; the reefs of Milne Bay "
    "and Kimbe Bay; the volcanoes of Rabaul and Manam; malaria, tuberculosis and clean water; mining, LNG, "
    "fisheries and forestry; village gardens and town life in Port Moresby, Lae, Mt Hagen and Goroka), give "
    "money in kina and use metric units, and relate the idea to students' daily life in PNG. Where a Tok Pisin "
    "word helps understanding, you may add it in brackets. Keep the science internationally correct, and never "
    "invent PNG statistics, names or events: if you are not sure of a PNG fact, say so. "
    "Do not claim the answer comes from the PNG curriculum, a syllabus or a textbook, and never invent syllabus "
    "codes, learning outcomes, page numbers or curriculum references. If the question asks what the PNG syllabus "
    "contains, say that NEXA does not hold that part of the syllabus yet and describe the topic only in general "
    "terms. If the request is harmful, or not appropriate for a school setting, decline briefly and suggest "
    "asking a teacher."
)
WEB_PROMPT = (
    GENERAL_PROMPT + " Use the numbered web results provided. After each sentence that uses a result, cite it "
    "as [W1], [W2] and so on. Use only the markers given; never invent a source. If the results do not help, "
    "answer from general knowledge without a marker and say the web results did not cover it."
)


class WebSource(BaseModel):
    marker: str
    title: str
    url: str
    snippet: str = ""
    used: bool = False


class GeneralInfo(BaseModel):
    source_kind: str = Field(..., description="general_knowledge or web")
    mode: str
    label: str
    model: Optional[str] = None
    web_provider: Optional[str] = None
    web_sources: list[WebSource] = Field(default_factory=list)
    fabricated_markers: list[str] = Field(default_factory=list)
    reason: str = ""


def enabled_for(roles: tuple[str, ...] | list[str], requested: Optional[bool]) -> bool:
    if config.GENERAL_ANSWERS == "off" or requested is False:
        return False
    allowed = config.GENERAL_ROLES
    return "*" in allowed or bool(set(roles) & allowed)


# ---------------------------------------------------------------- web search

async def _search_wikipedia(q: str, n: int) -> list[dict[str, str]]:
    base = config.WEB_SEARCH_URL or "https://en.wikipedia.org"
    async with httpx.AsyncClient(timeout=config.WEB_SEARCH_TIMEOUT,
                                 headers={"User-Agent": "NEXA-education/1.0"}) as c:
        r = await c.get(f"{base}/w/api.php", params={
            "action": "query", "format": "json", "generator": "search", "gsrsearch": q, "gsrlimit": n,
            "prop": "extracts|info", "exintro": 1, "explaintext": 1, "exchars": 1200, "inprop": "url"})
        r.raise_for_status()
        pages = sorted((r.json().get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    return [{"title": p.get("title", ""), "url": p.get("fullurl", ""), "snippet": p.get("extract", "")}
            for p in pages if p.get("extract")]


async def _search_tavily(q: str, n: int) -> list[dict[str, str]]:
    async with httpx.AsyncClient(timeout=config.WEB_SEARCH_TIMEOUT) as c:
        r = await c.post(config.WEB_SEARCH_URL or "https://api.tavily.com/search",
                         json={"query": q, "max_results": n, "search_depth": "basic"},
                         headers={"Authorization": f"Bearer {config.WEB_SEARCH_API_KEY}"})
        r.raise_for_status()
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
            for x in r.json().get("results", [])]


async def _search_searxng(q: str, n: int) -> list[dict[str, str]]:
    async with httpx.AsyncClient(timeout=config.WEB_SEARCH_TIMEOUT) as c:
        r = await c.get(f"{config.WEB_SEARCH_URL.rstrip('/')}/search",
                        params={"q": q, "format": "json", "safesearch": 2})
        r.raise_for_status()
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
            for x in r.json().get("results", [])[:n]]


PROVIDERS = {"wikipedia": _search_wikipedia, "tavily": _search_tavily, "searxng": _search_searxng}


async def _web_search(q: str) -> list[dict[str, str]]:
    fn = PROVIDERS.get(config.WEB_SEARCH_PROVIDER)
    if fn is None:
        raise RuntimeError(f"unknown web search provider {config.WEB_SEARCH_PROVIDER!r}")
    return [x for x in await fn(q, config.WEB_SEARCH_RESULTS) if x.get("url")]


# Replaced in tests.
web_search = _web_search


# ---------------------------------------------------------------- generation

async def _general_chat(messages: list[dict[str, str]], trace_id: Optional[str]) -> tuple[str, str]:
    """The general model first, then the answer model (a local model keeps
    working when the external one has no key or is down)."""
    if not config.GATEWAY_TOKEN:
        raise RuntimeError("NEXA_KNOWLEDGE_GATEWAY_TOKEN not set")
    headers = {"Authorization": f"Bearer {config.GATEWAY_TOKEN}"}
    if trace_id:
        headers["X-NEXA-Trace-Id"] = trace_id
    last: Optional[Exception] = None
    for model in dict.fromkeys([config.GENERAL_MODEL, config.ANSWER_MODEL]):
        try:
            async with httpx.AsyncClient(timeout=config.ANSWER_TIMEOUT) as client:
                resp = await client.post(f"{config.GATEWAY_URL}/v1/chat/completions", headers=headers, json={
                    "model": model, "messages": messages, "temperature": config.GENERAL_TEMPERATURE,
                    "max_tokens": config.ANSWER_MAX_TOKENS})
            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"], data.get("model") or model
            last = RuntimeError(f"gateway {resp.status_code} for {model}")
        except httpx.HTTPError as exc:
            last = exc
    raise RuntimeError(str(last))


# Replaced in tests.
chat = _general_chat


def _clean(text: str, n_web: int) -> tuple[str, list[str], list[str]]:
    """Keep [W1]..[Wn]; remove curriculum markers ([S1]) and any other
    bracketed reference, which a general answer must not carry."""
    used, bad = [], []

    def sub(m: re.Match) -> str:
        tok = m.group(1).strip()
        w = re.fullmatch(r"[Ww](\d+)", tok)
        if w and 1 <= int(w.group(1)) <= n_web:
            used.append(f"W{int(w.group(1))}")
            return f"[W{int(w.group(1))}]"
        bad.append(tok)
        return ""
    text = re.sub(r"\[((?:[SsWw]\s*\d+)|\d+)\]", sub, text)
    text = re.sub(r"[ \t]+([.,;:])", r"\1", re.sub(r"[ \t]{2,}", " ", text)).strip()
    return text, sorted(set(used), key=lambda x: int(x[1:])), sorted(set(bad))


async def general_answer(question: str, audience: str, trace_id: Optional[str], reason: str
                         ) -> tuple[str, GeneralInfo]:
    """Returns (labelled answer text, info). Raises when no model answers."""
    mode = config.GENERAL_ANSWERS
    results: list[dict[str, str]] = []
    note = ""
    if mode == "web":
        try:
            results = await web_search(question)
        except Exception as exc:  # noqa: BLE001 - fall back to the model alone
            note = f"web search unavailable ({type(exc).__name__}); answered from general knowledge"
        if not results and not note:
            note = "web search found nothing; answered from general knowledge"
    aud = "Write for a teacher." if audience == "teacher" else "Write for a student: clear and step by step."
    if results:
        listing = "\n\n".join(f"[W{i}] {r['title']} ({r['url']})\n{(r.get('snippet') or '')[:1200]}"
                              for i, r in enumerate(results, 1))
        msgs = [{"role": "system", "content": WEB_PROMPT + " " + aud},
                {"role": "user", "content": f"Web results:\n{listing}\n\nQuestion: {question}"}]
    else:
        msgs = [{"role": "system", "content": GENERAL_PROMPT + " " + aud},
                {"role": "user", "content": question}]
    raw, model = await chat(msgs, trace_id)
    text, used, bad = _clean(raw, len(results))
    web = [WebSource(marker=f"W{i}", title=r["title"], url=r["url"], snippet=(r.get("snippet") or "")[:300],
                     used=f"W{i}" in used) for i, r in enumerate(results, 1)]
    kind = "web" if results else "general_knowledge"
    label = LABEL_WEB if results else LABEL_LLM
    info = GeneralInfo(source_kind=kind, mode=mode, label=label, model=model,
                       web_provider=config.WEB_SEARCH_PROVIDER if mode == "web" else None,
                       web_sources=web, fabricated_markers=bad, reason="; ".join(x for x in (reason, note) if x))
    return f"{label}\n\n{text}", info


def payload(info: GeneralInfo) -> dict[str, Any]:
    return info.model_dump(mode="json")
