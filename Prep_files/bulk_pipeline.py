from __future__ import annotations

import argparse
import importlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from chunking import chunk_blocks, validate_chunks
from ingestion import ingest_file
from pipeline_metadata import normalize_blocks_with_metadata
from pdf_report import build_pdf_report, build_lecture_pdf

SUPPORTED_SUFFIXES = {
    ".pdf",
    ".pptx",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
}


@dataclass
class ProcessingResult:
    file_path: str
    chunk_count: int
    valid_chunk_count: int
    error: str | None = None


def discover_supported_files(input_dir: Path, recursive: bool) -> List[Path]:
    """Find supported curriculum files from the given directory."""
    file_iter = input_dir.rglob("*") if recursive else input_dir.glob("*")
    files = [p for p in file_iter if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES]
    return sorted(files)


def process_file(
    file_path: Path,
    *,
    subject: str | None,
    audience: str,
    grade_min: int | None,
    grade_max: int | None,
    language: str,
    use_ai_for_pdf: bool,
    ai_model: str,
    chunk_size_words: int,
    chunk_overlap_words: int,
    min_words: int,
    min_natural_language_ratio: float,
) -> Tuple[List[Dict], ProcessingResult]:
    """Run extraction -> metadata -> chunking -> validation for one file."""
    try:
        if file_path.suffix.lower() == ".pdf" and use_ai_for_pdf:
            ai_processing = importlib.import_module("ai_processing")
            ai_result = ai_processing.ingest_pdf_with_ai(
                str(file_path),
                use_ai=True,
                model_name=ai_model,
            )
            raw_blocks = ai_processing.ai_pages_to_blocks(ai_result, include_ai_metadata=True)
        else:
            raw_blocks = ingest_file(str(file_path))

        normalized_blocks = normalize_blocks_with_metadata(
            raw_blocks,
            source_file=str(file_path),
            subject=subject,
            audience=audience,
            grade_level_min=grade_min,
            grade_level_max=grade_max,
            language=language,
        )

        chunks = chunk_blocks(
            normalized_blocks,
            chunk_size_words=chunk_size_words,
            chunk_overlap_words=chunk_overlap_words,
        )
        valid_chunks = validate_chunks(
            chunks,
            min_words=min_words,
            min_natural_language_ratio=min_natural_language_ratio,
        )

        return valid_chunks, ProcessingResult(
            file_path=str(file_path),
            chunk_count=len(chunks),
            valid_chunk_count=len(valid_chunks),
        )
    except Exception as exc:
        return [], ProcessingResult(
            file_path=str(file_path),
            chunk_count=0,
            valid_chunk_count=0,
            error=str(exc),
        )


def embed_with_sentence_transformers(
    texts: Sequence[str],
    *,
    model_name: str,
    batch_size: int,
) -> List[List[float]]:
    sentence_transformers = importlib.import_module("sentence_transformers")
    SentenceTransformer = getattr(sentence_transformers, "SentenceTransformer")
    model = SentenceTransformer(model_name)
    vectors = model.encode(
        list(texts),
        batch_size=batch_size,
        show_progress_bar=False,
        normalize_embeddings=False,
    )
    return vectors.tolist()


def embed_with_openai(texts: Sequence[str], *, model_name: str) -> List[List[float]]:
    langchain_openai = importlib.import_module("langchain_openai")
    OpenAIEmbeddings = getattr(langchain_openai, "OpenAIEmbeddings")
    embeddings = OpenAIEmbeddings(model=model_name)
    return embeddings.embed_documents(list(texts))


def generate_embeddings(
    texts: Sequence[str],
    *,
    provider: str,
    sentence_transformers_model: str,
    openai_model: str,
    batch_size: int,
) -> Tuple[str, List[List[float]]]:
    """Generate text embeddings with fallback support for bulk indexing."""
    if not texts:
        return provider, []

    if provider == "sentence-transformers":
        return provider, embed_with_sentence_transformers(
            texts,
            model_name=sentence_transformers_model,
            batch_size=batch_size,
        )

    if provider == "openai":
        return provider, embed_with_openai(texts, model_name=openai_model)

    if provider == "auto":
        try:
            vectors = embed_with_sentence_transformers(
                texts,
                model_name=sentence_transformers_model,
                batch_size=batch_size,
            )
            return "sentence-transformers", vectors
        except Exception:
            vectors = embed_with_openai(texts, model_name=openai_model)
            return "openai", vectors

    raise ValueError("Unsupported embedding provider")


def write_jsonl(path: Path, rows: Sequence[Dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bulk curriculum ingestion: extraction, chunking, and embeddings.",
    )
    parser.add_argument("input_dir", help="Directory containing curriculum files.")
    parser.add_argument("--output-dir", default="bulk_output", help="Directory for output artifacts.")
    parser.add_argument("--recursive", action="store_true", help="Recursively process files in subfolders.")

    parser.add_argument("--subject", default="curriculum", help="Metadata subject value.")
    parser.add_argument(
        "--audience",
        default="both",
        choices=["teacher", "student", "both"],
        help="Audience metadata for all processed content.",
    )
    parser.add_argument("--grade-min", type=int, default=None, help="Minimum grade metadata value.")
    parser.add_argument("--grade-max", type=int, default=None, help="Maximum grade metadata value.")
    parser.add_argument("--language", default="en", help="Language metadata value.")

    parser.add_argument("--chunk-size", type=int, default=120, help="Target words per chunk.")
    parser.add_argument("--chunk-overlap", type=int, default=20, help="Overlap words between chunks.")
    parser.add_argument("--min-words", type=int, default=5, help="Minimum words for chunk validation.")
    parser.add_argument(
        "--min-natural-language-ratio",
        type=float,
        default=0.6,
        help="Minimum alphabetic ratio for chunk validation.",
    )

    parser.add_argument("--use-ai-for-pdf", action="store_true", help="Use AI page processing for PDF files.")
    parser.add_argument("--ai-model", default="gpt-3.5-turbo", help="Model for PDF AI page processing.")

    parser.add_argument(
        "--embedding-provider",
        default="auto",
        choices=["auto", "sentence-transformers", "openai"],
        help="Embedding backend. Auto prefers sentence-transformers then OpenAI.",
    )
    parser.add_argument(
        "--sentence-transformers-model",
        default="all-MiniLM-L6-v2",
        help="Sentence-Transformers model name.",
    )
    parser.add_argument(
        "--openai-embedding-model",
        default="text-embedding-3-small",
        help="OpenAI embedding model name.",
    )
    parser.add_argument("--embedding-batch-size", type=int, default=64, help="Batch size for local embeddings.")
    parser.add_argument("--skip-embeddings", action="store_true", help="Skip embedding generation.")
    parser.add_argument(
        "--generate-pdf-report",
        action="store_true",
        help="Generate a polished PDF content template report.",
    )
    parser.add_argument(
        "--pdf-report-name",
        default="lecture_short_notes_template.pdf",
        help="Filename for the generated PDF report.",
    )
    parser.add_argument(
        "--pdf-per-lecture",
        action="store_true",
        help="Generate one PDF per detected lecture (uses the short-notes template).",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_dir = Path(args.input_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()

    if not input_dir.exists() or not input_dir.is_dir():
        raise SystemExit(f"Input directory not found: {input_dir}")

    files = discover_supported_files(input_dir, recursive=args.recursive)
    if not files:
        raise SystemExit("No supported curriculum files found in input directory.")

    all_chunks: List[Dict] = []
    file_results: List[ProcessingResult] = []

    for file_path in files:
        valid_chunks, result = process_file(
            file_path,
            subject=args.subject,
            audience=args.audience,
            grade_min=args.grade_min,
            grade_max=args.grade_max,
            language=args.language,
            use_ai_for_pdf=args.use_ai_for_pdf,
            ai_model=args.ai_model,
            chunk_size_words=args.chunk_size,
            chunk_overlap_words=args.chunk_overlap,
            min_words=args.min_words,
            min_natural_language_ratio=args.min_natural_language_ratio,
        )
        file_results.append(result)
        all_chunks.extend(valid_chunks)

    chunk_rows: List[Dict] = []
    for idx, chunk in enumerate(all_chunks, start=1):
        chunk_id = f"chunk-{idx}"
        chunk_rows.append(
            {
                "id": chunk_id,
                "text": chunk["text"],
                "metadata": chunk["metadata"],
            }
        )

    chunks_path = output_dir / "chunks.jsonl"
    write_jsonl(chunks_path, chunk_rows)

    selected_provider = args.embedding_provider
    embeddings_count = 0
    embeddings_path = output_dir / "embeddings.jsonl"

    manifest_data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "files_discovered": len(files),
        "files_succeeded": sum(1 for r in file_results if r.error is None),
        "files_failed": sum(1 for r in file_results if r.error is not None),
        "chunks_total": sum(r.chunk_count for r in file_results),
        "chunks_valid": len(chunk_rows),
        "embeddings_generated": embeddings_count,
        "embedding_provider": selected_provider,
        "file_results": [r.__dict__ for r in file_results],
        "artifacts": {
            "chunks": str(chunks_path),
            "embeddings": str(embeddings_path) if embeddings_count else None,
        },
    }

    if not args.skip_embeddings and chunk_rows:
        texts = [row["text"] for row in chunk_rows]
        selected_provider, vectors = generate_embeddings(
            texts,
            provider=args.embedding_provider,
            sentence_transformers_model=args.sentence_transformers_model,
            openai_model=args.openai_embedding_model,
            batch_size=args.embedding_batch_size,
        )
        embedding_rows = [
            {
                "id": chunk_rows[i]["id"],
                "embedding": vectors[i],
                "metadata": chunk_rows[i]["metadata"],
            }
            for i in range(len(chunk_rows))
        ]
        write_jsonl(embeddings_path, embedding_rows)
        embeddings_count = len(embedding_rows)

    manifest_data["embeddings_generated"] = embeddings_count
    manifest_data["embedding_provider"] = selected_provider
    manifest_data["artifacts"]["embeddings"] = str(embeddings_path) if embeddings_count else None

    pdf_report_path = output_dir / args.pdf_report_name
    pdf_report_created = False
    if args.generate_pdf_report and chunk_rows:
        if args.pdf_per_lecture:
            # Group chunks by an explicit lesson key in metadata, or fall back to source file
            lectures = {}
            for row in chunk_rows:
                meta = row.get("metadata", {})
                lesson_key = meta.get("lesson") or meta.get("lesson_title") or meta.get("source_file") or "default"
                lectures.setdefault(lesson_key, []).append(row)

            generated = []
            for lesson_key, rows in lectures.items():
                # Build a minimal lecture dict; prefer AI metadata if present
                meta0 = rows[0].get("metadata", {}) if rows else {}
                lecture = {
                    "title": meta0.get("lesson_title") or Path(str(lesson_key)).name,
                    "learning_objective": meta0.get("ai_summary") or "",
                    "notes": "\n\n".join([r.get("text", "") for r in rows[:6]]),
                    "key_terms": meta0.get("ai_topics") or "",
                    "example": "",
                    "quick_check": "",
                }
                safe_name = Path(lecture["title"]).stem.replace(" ", "_").lower()[:120]
                lesson_pdf_path = output_dir / f"lecture_{safe_name}.pdf"
                build_lecture_pdf(lecture, lesson_pdf_path)
                generated.append(str(lesson_pdf_path))

            pdf_report_created = True
            manifest_data["artifacts"]["pdf_report"] = generated
        else:
            build_pdf_report(manifest_data, chunk_rows, pdf_report_path)
            pdf_report_created = True
            manifest_data["artifacts"]["pdf_report"] = str(pdf_report_path)
    else:
        manifest_data["artifacts"]["pdf_report"] = None

    manifest = manifest_data

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=True, indent=2)

    print("Bulk curriculum processing complete.")
    print(f"Files discovered: {manifest['files_discovered']}")
    print(f"Files succeeded:  {manifest['files_succeeded']}")
    print(f"Files failed:     {manifest['files_failed']}")
    print(f"Valid chunks:     {manifest['chunks_valid']}")
    print(f"Embeddings:       {manifest['embeddings_generated']}")
    print(f"Manifest:         {manifest_path}")
    if pdf_report_created:
        print(f"PDF report:       {pdf_report_path}")


if __name__ == "__main__":
    main()
