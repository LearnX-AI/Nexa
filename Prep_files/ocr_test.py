from __future__ import annotations

import json
import sys

from chunking import build_langchain_documents, chunk_blocks, validate_chunks
from ingestion import ingest_file
from pipeline_metadata import normalize_blocks_with_metadata
from retrieval_filters import student_filter, teacher_filter

"""Thin command-line entrypoint for the Nexaz OCR/RAG preprocessing pipeline."""


def main() -> None:
    """Run ingestion, normalization, chunking, validation, and metadata output for one file."""
    source_file = sys.argv[1] if len(sys.argv) > 1 else "test.png"
    ingested_blocks = ingest_file(source_file)

    normalized_blocks = normalize_blocks_with_metadata(
        ingested_blocks,
        source_file=source_file,
        subject="general",
        audience="both",
        grade_level_min=6,
        grade_level_max=12,
        language="en",
    )
    chunks = chunk_blocks(normalized_blocks, chunk_size_words=120, chunk_overlap_words=20)
    validated_chunks = validate_chunks(chunks, min_words=5, min_natural_language_ratio=0.6)
    documents = build_langchain_documents(validated_chunks)

    with open("output.txt", "w", encoding="utf-8") as f:
        f.write("# Validated chunks with metadata\n")
        for i, chunk in enumerate(validated_chunks, start=1):
            line = f"{i}. {chunk['text']}\n"
            print(line, end="")
            f.write(line)
            f.write(json.dumps(chunk["metadata"], ensure_ascii=True) + "\n\n")

    print(f"\nChunking & Validation Summary:")
    print(f"  Total chunks created: {len(chunks)}")
    print(f"  Chunks after validation: {len(validated_chunks)}")
    print(f"  Low-quality chunks filtered: {len(chunks) - len(validated_chunks)}")
    print(f"\nTeacher retrieval filter:", teacher_filter())
    print(f"Student retrieval filter (grade 8):", student_filter(8))
    print(f"Prepared {len(documents)} LangChain-style documents for embedding.")


if __name__ == "__main__":
    main()