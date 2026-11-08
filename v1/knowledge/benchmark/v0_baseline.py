"""The v0 chunking strategy, reproduced for the M2-003 benchmark.

v0 (main.py, NEXA-M0-004 section 4):
- PDFs: PyPDFLoader, one document per page, metadata file/subject/grade/term/
  doctype/page (main.py:3970-3986).
- DOCX: _load_docx_text joins non-empty paragraphs; tables are not read
  (main.py:178-185).
- Chunking: RecursiveCharacterTextSplitter(chunk_size=1500, chunk_overlap=300),
  separators ["\\n\\n", "\\n", " ", ""], applied per page document
  (main.py:3992).

The splitter below is a dependency-free port of
langchain_text_splitters.RecursiveCharacterTextSplitter with those defaults
(keep_separator=True, strip_whitespace=True, length_function=len). The tests
check it against the real library when that library is installed.
"""
from __future__ import annotations

import io
import re
from typing import Any, Optional

V0_CHUNK_SIZE = 1500
V0_CHUNK_OVERLAP = 300
_SEPARATORS = ["\n\n", "\n", " ", ""]


class RecursiveSplitter:
    def __init__(self, chunk_size: int = V0_CHUNK_SIZE, chunk_overlap: int = V0_CHUNK_OVERLAP,
                 separators: Optional[list[str]] = None) -> None:
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.separators = separators or list(_SEPARATORS)

    @staticmethod
    def _split_with_regex(text: str, separator: str) -> list[str]:
        if separator:
            parts = re.split(f"({separator})", text)
            splits = [parts[i] + parts[i + 1] for i in range(1, len(parts), 2)]
            if len(parts) % 2 == 0:
                splits += parts[-1:]
            splits = [parts[0]] + splits
        else:
            splits = list(text)
        return [s for s in splits if s != ""]

    def _join(self, docs: list[str], separator: str) -> Optional[str]:
        text = separator.join(docs).strip()
        return text or None

    def _merge(self, splits: list[str], separator: str) -> list[str]:
        sep_len = len(separator)
        docs: list[str] = []
        current: list[str] = []
        total = 0
        for d in splits:
            n = len(d)
            if total + n + (sep_len if current else 0) > self.chunk_size:
                if current:
                    doc = self._join(current, separator)
                    if doc is not None:
                        docs.append(doc)
                    while total > self.chunk_overlap or (
                            total + n + (sep_len if current else 0) > self.chunk_size and total > 0):
                        total -= len(current[0]) + (sep_len if len(current) > 1 else 0)
                        current = current[1:]
            current.append(d)
            total += n + (sep_len if len(current) > 1 else 0)
        doc = self._join(current, separator)
        if doc is not None:
            docs.append(doc)
        return docs

    def _split(self, text: str, separators: list[str]) -> list[str]:
        final: list[str] = []
        separator = separators[-1]
        new_separators: list[str] = []
        for i, s in enumerate(separators):
            if s == "":
                separator = s
                break
            if re.search(re.escape(s), text):
                separator = s
                new_separators = separators[i + 1:]
                break
        splits = self._split_with_regex(text, re.escape(separator))
        good: list[str] = []
        for s in splits:
            if len(s) < self.chunk_size:
                good.append(s)
            else:
                if good:
                    final.extend(self._merge(good, ""))
                    good = []
                if not new_separators:
                    final.append(s)
                else:
                    final.extend(self._split(s, new_separators))
        if good:
            final.extend(self._merge(good, ""))
        return final

    def split_text(self, text: str) -> list[str]:
        return self._split(text, self.separators)


def v0_load_pdf_pages(data: bytes) -> list[str]:
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(data))
    return [(p.extract_text() or "") for p in reader.pages]


def v0_load_docx_text(data: bytes) -> str:
    import docx
    d = docx.Document(io.BytesIO(data))
    return "\n".join(p.text for p in d.paragraphs if p.text.strip())


def v0_chunks(file_name: str, data: bytes, base_meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Chunk one file the way v0 did. Returns [{text, metadata}]."""
    sp = RecursiveSplitter()
    out: list[dict[str, Any]] = []
    if file_name.lower().endswith(".pdf"):
        for i, page in enumerate(v0_load_pdf_pages(data)):
            for t in sp.split_text(page):
                out.append({"text": t, "metadata": {**base_meta, "page": i}})
    elif file_name.lower().endswith(".docx"):
        for t in sp.split_text(v0_load_docx_text(data)):
            out.append({"text": t, "metadata": dict(base_meta)})
    return out
