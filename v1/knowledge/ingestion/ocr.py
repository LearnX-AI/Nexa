"""OCR through the tesseract engine (NEXA-M2-002).

Deterministic settings: fixed DPI, OEM 1 (LSTM), PSM 3 (automatic layout),
one language pack. The engine and settings are recorded on every extraction run
so a re-run can be compared with the one before it.

pytesseract is a thin wrapper around the tesseract binary; it is not a model SDK
and does not call any network service.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional

from .. import config

TESSERACT_CONFIG = "--oem 1 --psm 3"


@lru_cache(maxsize=1)
def engine_info() -> Optional[dict[str, Any]]:
    """Engine identity, or None when OCR is unavailable or disabled."""
    if not config.OCR_ENABLED:
        return None
    try:
        import pytesseract
        version = str(pytesseract.get_tesseract_version())
        langs = pytesseract.get_languages(config="")
    except Exception:
        return None
    if config.OCR_LANG not in langs:
        return None
    return {
        "engine": "tesseract",
        "version": version,
        "wrapper": f"pytesseract {pytesseract.__version__ if hasattr(pytesseract, '__version__') else 'unknown'}",
        "language": config.OCR_LANG,
        "dpi": config.OCR_DPI,
        "config": TESSERACT_CONFIG,
        "low_confidence_threshold": config.OCR_LOW_CONFIDENCE,
    }


def available() -> bool:
    return engine_info() is not None


def ocr_image(image) -> tuple[str, float]:
    """OCR a PIL image. Returns (text with line breaks, mean word confidence).

    Confidence is the character-weighted mean of tesseract word confidences,
    so a page of long, confidently read words is not dragged down by stray
    one-character marks.
    """
    import pytesseract

    if image.mode not in ("L", "RGB"):
        image = image.convert("RGB")
    data = pytesseract.image_to_data(
        image, lang=config.OCR_LANG, config=TESSERACT_CONFIG,
        output_type=pytesseract.Output.DICT, timeout=config.OCR_TIMEOUT_S,
    )
    lines: dict[tuple[int, int, int], list[str]] = {}
    order: list[tuple[int, int, int]] = []
    weighted, weight = 0.0, 0
    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        conf = float(data["conf"][i])
        if not word or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key not in lines:
            lines[key] = []
            order.append(key)
        lines[key].append(word)
        weighted += conf * len(word)
        weight += len(word)
    text_lines: list[str] = []
    last_block_par = None
    for key in order:
        if last_block_par is not None and key[:2] != last_block_par:
            text_lines.append("")  # paragraph break between tesseract paragraphs
        text_lines.append(" ".join(lines[key]))
        last_block_par = key[:2]
    mean = weighted / weight if weight else 0.0
    return "\n".join(text_lines).strip(), round(mean, 1)
