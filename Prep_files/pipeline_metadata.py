from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from text_processing import normalize_text

"""Attach retrieval-ready metadata to normalized content blocks."""


def normalize_blocks_with_metadata(
    raw_blocks: List[Dict],
    *,
    source_file: Optional[str] = None,
    source_type: Optional[str] = None,
    subject: Optional[str] = None,
    audience: str = "both",
    grade_level_min: Optional[int] = None,
    grade_level_max: Optional[int] = None,
    language: str = "en",
) -> List[Dict]:
    """Add source, audience, and grading metadata to each cleaned text block."""
    created_at = datetime.now(timezone.utc).isoformat()
    blocks: List[Dict] = []

    for i, block in enumerate(raw_blocks):
        raw_text = block.get("text", "")
        inherited_metadata = dict(block.get("metadata", {}))
        normalized_text = normalize_text(raw_text)
        if not normalized_text:
            continue

        metadata = {
            **inherited_metadata,
            "source_file": source_file or inherited_metadata.get("source_file"),
            "source_type": source_type or inherited_metadata.get("source_type"),
            "subject": subject,
            "audience": audience,
            "grade_level_min": grade_level_min,
            "grade_level_max": grade_level_max,
            "language": language,
            "block_index": i,
            "normalized_at": created_at,
            "normalization_method": "semantic_denoise_v1",
        }
        blocks.append({"text": normalized_text, "metadata": metadata})

    return blocks
