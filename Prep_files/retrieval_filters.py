from __future__ import annotations

from typing import Dict

"""Reusable metadata filters for Teacher and Student retrieval paths."""


def teacher_filter() -> Dict:
    """Return the vector-store filter for teacher-facing content."""
    return {"audience": {"$in": ["teacher", "both"]}}


def student_filter(grade_level: int) -> Dict:
    """Return the vector-store filter for student content at a given grade."""
    return {
        "audience": {"$in": ["student", "both"]},
        "grade_level_min": {"$lte": grade_level},
        "grade_level_max": {"$gte": grade_level},
    }
