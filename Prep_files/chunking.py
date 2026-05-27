from __future__ import annotations

import importlib
import re
from typing import Dict, List

"""Semantic chunking utilities and LangChain document conversion helpers."""


def split_into_sentences(text: str) -> List[str]:
    """Split text into sentences while preserving common abbreviations."""
    text = text.replace("Dr.", "Dr_ABBR").replace("Mr.", "Mr_ABBR").replace("Mrs.", "Mrs_ABBR")
    text = text.replace("Prof.", "Prof_ABBR").replace("vs.", "vs_ABBR").replace("e.g.", "e_g_ABBR")
    text = text.replace("i.e.", "i_e_ABBR").replace("etc.", "etc_ABBR")

    sentences = re.split(r"(?<=[.!?])\s+", text)
    return [
        s.replace("Dr_ABBR", "Dr.")
        .replace("Mr_ABBR", "Mr.")
        .replace("Mrs_ABBR", "Mrs.")
        .replace("Prof_ABBR", "Prof.")
        .replace("vs_ABBR", "vs.")
        .replace("e_g_ABBR", "e.g.")
        .replace("i_e_ABBR", "i.e.")
        .replace("etc_ABBR", "etc.")
        for s in sentences
    ]


def chunk_blocks(
    blocks: List[Dict],
    *,
    chunk_size_words: int = 120,
    chunk_overlap_words: int = 20,
) -> List[Dict]:
    """Chunk structured blocks without breaking their semantic boundaries."""
    chunks: List[Dict] = []
    block_idx = 0

    while block_idx < len(blocks):
        current_block = blocks[block_idx]
        element_type = current_block.get("metadata", {}).get("element_type", "paragraph")
        chunk_idx = 0

        if element_type == "heading":
            heading_text = current_block["text"]
            if len(heading_text.split()) <= chunk_size_words:
                combined_text = heading_text
                combined_metadata = dict(current_block["metadata"])
                following_blocks = 1

                while (
                    block_idx + following_blocks < len(blocks)
                    and blocks[block_idx + following_blocks].get("metadata", {}).get("element_type") == "paragraph"
                ):
                    next_block = blocks[block_idx + following_blocks]
                    candidate_text = combined_text + "\n" + next_block["text"]
                    if len(candidate_text.split()) > chunk_size_words:
                        break
                    combined_text = candidate_text
                    following_blocks += 1

                chunks.append(
                    {
                        "text": combined_text,
                        "metadata": {
                            **combined_metadata,
                            "chunk_index": chunk_idx,
                            "chunking_strategy": "heading_with_context",
                            "blocks_combined": following_blocks,
                        },
                    }
                )
                block_idx += following_blocks
                continue

            chunks.append(
                {
                    "text": heading_text,
                    "metadata": {
                        **current_block["metadata"],
                        "chunk_index": chunk_idx,
                        "chunking_strategy": "heading_standalone",
                    },
                }
            )
            block_idx += 1
            continue

        if element_type == "list_item":
            list_items = [current_block["text"]]
            list_metadata = dict(current_block["metadata"])
            consecutive_count = 1

            while (
                block_idx + consecutive_count < len(blocks)
                and blocks[block_idx + consecutive_count].get("metadata", {}).get("element_type") == "list_item"
            ):
                next_item = blocks[block_idx + consecutive_count]
                candidate_text = "\n".join(list_items + [next_item["text"]])
                if len(candidate_text.split()) > chunk_size_words:
                    break
                list_items.append(next_item["text"])
                consecutive_count += 1

            chunks.append(
                {
                    "text": "\n".join(list_items),
                    "metadata": {
                        **list_metadata,
                        "chunk_index": chunk_idx,
                        "chunking_strategy": "list_items_grouped",
                        "list_item_count": consecutive_count,
                    },
                }
            )
            block_idx += consecutive_count
            continue

        if element_type == "table":
            chunks.append(
                {
                    "text": current_block["text"],
                    "metadata": {
                        **current_block["metadata"],
                        "chunk_index": chunk_idx,
                        "chunking_strategy": "table_intact",
                    },
                }
            )
            block_idx += 1
            continue

        if element_type == "table_row":
            table_rows = [current_block["text"]]
            table_metadata = dict(current_block["metadata"])
            consecutive_count = 1

            while (
                block_idx + consecutive_count < len(blocks)
                and blocks[block_idx + consecutive_count].get("metadata", {}).get("element_type") == "table_row"
            ):
                next_row = blocks[block_idx + consecutive_count]
                candidate_text = "\n".join(table_rows + [next_row["text"]])
                if len(candidate_text.split()) > chunk_size_words:
                    break
                table_rows.append(next_row["text"])
                consecutive_count += 1

            chunks.append(
                {
                    "text": "\n".join(table_rows),
                    "metadata": {
                        **table_metadata,
                        "chunk_index": chunk_idx,
                        "chunking_strategy": "table_rows_grouped",
                        "row_count": consecutive_count,
                    },
                }
            )
            block_idx += consecutive_count
            continue

        paragraph_text = current_block["text"]
        sentences = split_into_sentences(paragraph_text)
        if not sentences:
            block_idx += 1
            continue

        sentence_buffer: List[str] = []
        buffer_word_count = 0
        chunk_idx = 0

        for sentence in sentences:
            sentence_words = len(sentence.split())
            if buffer_word_count + sentence_words <= chunk_size_words:
                sentence_buffer.append(sentence)
                buffer_word_count += sentence_words
            else:
                if sentence_buffer:
                    chunks.append(
                        {
                            "text": " ".join(sentence_buffer),
                            "metadata": {
                                **current_block["metadata"],
                                "chunk_index": chunk_idx,
                                "chunking_strategy": "sentence_boundary",
                                "sentence_count": len(sentence_buffer),
                            },
                        }
                    )
                    chunk_idx += 1

                sentence_buffer = [sentence]
                buffer_word_count = sentence_words

        if sentence_buffer:
            chunks.append(
                {
                    "text": " ".join(sentence_buffer),
                    "metadata": {
                        **current_block["metadata"],
                        "chunk_index": chunk_idx,
                        "chunking_strategy": "sentence_boundary",
                        "sentence_count": len(sentence_buffer),
                    },
                }
            )

        block_idx += 1

    return chunks


def is_chunk_valid(
    chunk: Dict,
    *,
    min_words: int = 5,
    min_natural_language_ratio: float = 0.6,
) -> bool:
    """
    Validate a chunk for embedding suitability.
    
    Filters out:
    - Chunks with fewer than min_words words
    - Chunks dominated by numbers/symbols (< min_natural_language_ratio natural language content)
    - Chunks with residual noise patterns (excessive URLs, emails, hex codes, etc.)
    
    Args:
        chunk: Dictionary with 'text' and 'metadata' keys
        min_words: Minimum word count threshold
        min_natural_language_ratio: Minimum ratio of alphabetic chars to total chars
    
    Returns:
        True if chunk is suitable for embedding, False otherwise
    """
    text = chunk.get("text", "").strip()
    
    # Check minimum length
    words = text.split()
    if len(words) < min_words:
        return False
    
    # Check natural language ratio (alphabetic characters vs total)
    alpha_chars = sum(1 for c in text if c.isalpha())
    total_chars = len(text)
    if total_chars == 0 or (alpha_chars / total_chars) < min_natural_language_ratio:
        return False
    
    # Check for residual noise patterns
    # URLs and email addresses
    if re.search(r"https?://|www\.|@[a-zA-Z0-9]", text):
        return False
    
    # Excessive hex codes or binary data
    if re.search(r"\b[0-9a-f]{8,}\b", text, re.IGNORECASE):
        hex_matches = re.findall(r"\b[0-9a-f]{8,}\b", text, re.IGNORECASE)
        if len(hex_matches) > 2:
            return False
    
    # Excessive special character sequences (corrupted data)
    special_char_sequences = len(re.findall(r"[^\w\s]{3,}", text))
    if special_char_sequences > 3:
        return False
    
    # All caps or all numbers (likely metadata/headers)
    if len(words) >= 3 and text.isupper() and text.replace(" ", "").isalpha():
        return False
    
    # Numeric-only chunks
    if all(w.replace(".", "").replace(",", "").isdigit() for w in words if w.strip()):
        return False
    
    return True


def validate_chunks(
    chunks: List[Dict],
    *,
    min_words: int = 5,
    min_natural_language_ratio: float = 0.6,
) -> List[Dict]:
    """
    Filter chunks to remove low-quality content before embedding.
    
    Args:
        chunks: List of chunk dictionaries with 'text' and 'metadata'
        min_words: Minimum word count threshold
        min_natural_language_ratio: Minimum ratio of alphabetic chars to total chars
    
    Returns:
        List of validated chunks suitable for embedding and indexing
    """
    validated = [
        chunk
        for chunk in chunks
        if is_chunk_valid(chunk, min_words=min_words, min_natural_language_ratio=min_natural_language_ratio)
    ]
    return validated


def build_langchain_documents(chunks: List[Dict]) -> List[Dict]:
    """Convert chunk dictionaries into LangChain Document objects when available."""
    try:
        document_module = importlib.import_module("langchain_core.documents")
        Document = getattr(document_module, "Document")
        return [Document(page_content=chunk["text"], metadata=chunk["metadata"]) for chunk in chunks]
    except ImportError:
        return [{"page_content": chunk["text"], "metadata": chunk["metadata"]} for chunk in chunks]
