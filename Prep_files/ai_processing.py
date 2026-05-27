from __future__ import annotations

import importlib
import unicodedata
import re
from pathlib import Path
from typing import Dict, List, Optional

from text_processing import (
    detect_running_header_footer_lines,
    is_noise_line,
    normalize_text,
    remove_running_header_footer,
    reassemble_structured_blocks,
)

"""AI-powered page-by-page PDF processing with LLM-based content extraction and analysis."""


def extract_pdf_pages(file_path: str) -> List[Dict]:
    """Extract raw text from each PDF page, returning page-level metadata."""
    pypdf_module = importlib.import_module("pypdf")
    PdfReader = getattr(pypdf_module, "PdfReader")

    pages: List[Dict] = []
    with open(file_path, "rb") as pdf_stream:
        pdf_reader = PdfReader(pdf_stream)
        total_pages = len(pdf_reader.pages)

        for page_number, page in enumerate(pdf_reader.pages, start=1):
            page_text = page.extract_text() or ""
            page_text = unicodedata.normalize("NFKC", page_text)
            page_text = re.sub(r"([A-Za-z])-[ \t]*\n[ \t]*([A-Za-z])", r"\1\2", page_text)

            pages.append(
                {
                    "page_number": page_number,
                    "raw_text": page_text,
                    "normalized_text": normalize_text(page_text),
                    "total_pages": total_pages,
                    "source_file": file_path,
                }
            )

    return pages


def process_page_with_ai(
    page_dict: Dict,
    llm_prompt_template: Optional[str] = None,
    model_name: str = "gpt-3.5-turbo",
) -> Dict:
    """
    Send a single page through an LLM for intelligent content extraction.

    Args:
        page_dict: Dictionary with 'raw_text', 'page_number', etc.
        llm_prompt_template: Custom prompt; uses default if None
        model_name: LLM model identifier (OpenAI, local, etc.)

    Returns:
        Enriched page dict with AI-generated content, summary, and metadata
    """
    try:
        langchain_module = importlib.import_module("langchain_openai")
        ChatOpenAI = getattr(langchain_module, "ChatOpenAI")
    except ImportError:
        return {
            **page_dict,
            "ai_processing": False,
            "ai_error": "LangChain or OpenAI not installed",
            "ai_summary": None,
            "ai_extracted_content": None,
            "ai_metadata": {},
        }

    page_text = page_dict.get("normalized_text", "") or page_dict.get("raw_text", "")
    if not page_text.strip():
        return {
            **page_dict,
            "ai_processing": True,
            "ai_error": "Page text is empty",
            "ai_summary": None,
            "ai_extracted_content": None,
            "ai_metadata": {},
        }

    if llm_prompt_template is None:
        llm_prompt_template = """
Analyze the following PDF page content and provide:
1. A concise summary (2-3 sentences)
2. Key topics or concepts (as a comma-separated list)
3. Content type (e.g., heading, body text, table, list, image description, etc.)

Page Content:
{page_text}

Provide your response in the following JSON format:
{{"summary": "...", "key_topics": "...", "content_type": "..."}}
"""

    try:
        llm = ChatOpenAI(model=model_name, temperature=0.3)
        from langchain_core.messages import HumanMessage

        prompt_text = llm_prompt_template.format(page_text=page_text[:2000])
        message = HumanMessage(content=prompt_text)
        response = llm.invoke([message])
        response_text = response.content

        import json

        try:
            ai_result = json.loads(response_text)
        except json.JSONDecodeError:
            ai_result = {
                "summary": response_text[:200],
                "key_topics": "parsing_error",
                "content_type": "unknown",
            }

        return {
            **page_dict,
            "ai_processing": True,
            "ai_error": None,
            "ai_summary": ai_result.get("summary"),
            "ai_extracted_content": ai_result.get("key_topics"),
            "ai_metadata": {
                "content_type": ai_result.get("content_type"),
                "ai_model": model_name,
            },
        }

    except Exception as e:
        return {
            **page_dict,
            "ai_processing": True,
            "ai_error": str(e),
            "ai_summary": None,
            "ai_extracted_content": None,
            "ai_metadata": {"error_type": type(e).__name__},
        }


def ingest_pdf_with_ai(
    file_path: str,
    use_ai: bool = True,
    llm_prompt_template: Optional[str] = None,
    model_name: str = "gpt-3.5-turbo",
) -> Dict:
    """
    Process a PDF page-by-page, optionally enriching with AI analysis.

    Args:
        file_path: Path to PDF file
        use_ai: Whether to invoke LLM processing for each page
        llm_prompt_template: Custom LLM prompt template
        model_name: LLM model identifier

    Returns:
        Dictionary with 'pages' (list of processed page dicts) and 'summary' metadata
    """
    pages = extract_pdf_pages(file_path)
    processed_pages: List[Dict] = []

    for page_dict in pages:
        if use_ai:
            processed_page = process_page_with_ai(
                page_dict,
                llm_prompt_template=llm_prompt_template,
                model_name=model_name,
            )
        else:
            processed_page = {
                **page_dict,
                "ai_processing": False,
                "ai_summary": None,
                "ai_extracted_content": None,
                "ai_metadata": {},
            }

        processed_pages.append(processed_page)

    return {
        "source_file": file_path,
        "total_pages": len(pages),
        "pages": processed_pages,
        "ai_enabled": use_ai,
    }


def ai_pages_to_blocks(
    ai_result: Dict,
    include_ai_metadata: bool = True,
) -> List[Dict]:
    """
    Convert AI-processed pages into text blocks suitable for chunking.

    Args:
        ai_result: Dictionary returned from ingest_pdf_with_ai()
        include_ai_metadata: Whether to attach AI summaries/topics to block metadata

    Returns:
        List of blocks with text and metadata
    """
    blocks: List[Dict] = []

    for page_dict in ai_result.get("pages", []):
        page_number = page_dict.get("page_number")
        normalized_text = page_dict.get("normalized_text", "")

        if not normalized_text.strip():
            continue

        base_metadata = {
            "source_file": ai_result.get("source_file"),
            "source_type": "pdf",
            "extraction_method": "pypdf_with_ai",
            "page_number": page_number,
            "total_pages": ai_result.get("total_pages"),
        }

        if include_ai_metadata and page_dict.get("ai_processing"):
            base_metadata.update({
                "ai_summary": page_dict.get("ai_summary"),
                "ai_topics": page_dict.get("ai_extracted_content"),
                "ai_content_type": page_dict.get("ai_metadata", {}).get("content_type"),
            })

        blocks.append(
            {
                "text": normalized_text,
                "metadata": base_metadata,
            }
        )

    return blocks
