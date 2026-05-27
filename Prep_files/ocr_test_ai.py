from __future__ import annotations

import json
import sys

from ai_processing import ingest_pdf_with_ai, ai_pages_to_blocks
from chunking import chunk_blocks, validate_chunks, build_langchain_documents
from pipeline_metadata import normalize_blocks_with_metadata

"""
Entrypoint for AI-powered PDF page-by-page processing.

Usage:
    python ocr_test_ai.py <pdf_file> [--no-ai]

Example:
    python ocr_test_ai.py document.pdf
    python ocr_test_ai.py document.pdf --no-ai    # Skip AI processing
"""


def main() -> None:
    """Process a PDF page-by-page with optional AI enrichment."""
    if len(sys.argv) < 2:
        print("Usage: python ocr_test_ai.py <pdf_file> [--no-ai]")
        sys.exit(1)

    pdf_file = sys.argv[1]
    use_ai = "--no-ai" not in sys.argv

    print(f"Processing PDF: {pdf_file}")
    print(f"AI processing enabled: {use_ai}\n")

    # Step 1: Extract and process PDF pages with optional AI
    ai_result = ingest_pdf_with_ai(
        pdf_file,
        use_ai=use_ai,
        model_name="gpt-3.5-turbo",  # Set to your preferred LLM
    )

    total_pages = ai_result.get("total_pages", 0)
    print(f"Total pages in PDF: {total_pages}")

    # Step 2: Display page-by-page processing summary
    print("\n--- Page-by-Page Processing Summary ---")
    for page_dict in ai_result.get("pages", []):
        page_num = page_dict.get("page_number")
        ai_enabled = page_dict.get("ai_processing", False)
        ai_error = page_dict.get("ai_error")
        ai_summary = page_dict.get("ai_summary", "")

        print(f"\nPage {page_num}/{total_pages}:")
        if ai_enabled and not ai_error:
            print(f"  AI Summary: {ai_summary}")
            print(f"  AI Topics: {page_dict.get('ai_extracted_content', 'N/A')}")
            print(f"  Content Type: {page_dict.get('ai_metadata', {}).get('content_type', 'unknown')}")
        elif ai_enabled and ai_error:
            print(f"  AI Error: {ai_error}")
        else:
            print(f"  AI Processing: Disabled")

    # Step 3: Convert AI-processed pages to blocks
    blocks = ai_pages_to_blocks(ai_result, include_ai_metadata=True)
    print(f"\n\nConverted to {len(blocks)} blocks from {total_pages} pages")

    # Step 4: Normalize blocks with retrieval metadata
    normalized_blocks = normalize_blocks_with_metadata(
        blocks,
        source_file=pdf_file,
        subject="general",
        audience="both",
        grade_level_min=6,
        grade_level_max=12,
        language="en",
    )

    # Step 5: Semantic chunking
    chunks = chunk_blocks(normalized_blocks, chunk_size_words=120, chunk_overlap_words=20)

    # Step 6: Validation pass
    validated_chunks = validate_chunks(chunks, min_words=5, min_natural_language_ratio=0.6)

    # Step 7: Convert to LangChain documents
    documents = build_langchain_documents(validated_chunks)

    # Step 8: Output results
    with open("output_ai.txt", "w", encoding="utf-8") as f:
        f.write("# AI-Processed PDF Chunks\n\n")
        for i, chunk in enumerate(validated_chunks, start=1):
            line = f"{i}. {chunk['text']}\n"
            print(line, end="")
            f.write(line)
            f.write(json.dumps(chunk["metadata"], ensure_ascii=True) + "\n\n")

    print(f"\n\n--- Pipeline Summary ---")
    print(f"Total chunks created: {len(chunks)}")
    print(f"Valid chunks (post-validation): {len(validated_chunks)}")
    print(f"Low-quality chunks filtered: {len(chunks) - len(validated_chunks)}")
    print(f"LangChain documents prepared: {len(documents)}")
    print(f"\nResults written to: output_ai.txt")


if __name__ == "__main__":
    main()
