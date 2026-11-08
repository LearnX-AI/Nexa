"""File validation and content safety checks (NEXA-M2-002, NEXA-GOV-001).

Runs before any parser touches the bytes. A file is accepted only when:
- its size is within limits,
- its sniffed type (magic bytes and container contents) matches an allowed
  type AND agrees with its extension,
- it carries no active content (PDF JavaScript or launch actions, Office
  macros, externally loaded templates),
- ZIP containers are not bombs and have no path traversal,
- the configured malware scanner (if any) reports it clean.

After extraction, scan_sensitive_text() refuses documents that contain
credentials or key material so they never reach the knowledge store.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from typing import Optional

from .. import config

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PNG = "image/png"
JPEG = "image/jpeg"
TIFF = "image/tiff"

ALLOWED_EXTENSIONS = {
    ".pdf": PDF, ".docx": DOCX, ".pptx": PPTX,
    ".png": PNG, ".jpg": JPEG, ".jpeg": JPEG, ".tif": TIFF, ".tiff": TIFF,
}


class ValidationError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class ValidationResult:
    mime_type: str
    size: int
    av: str                      # scanner result: clean | skipped:<reason>
    warnings: list[str] = field(default_factory=list)


def image_uploads_enabled() -> bool:
    mode = config.IMAGE_UPLOADS
    if mode == "force":
        return True
    if mode == "off":
        return False
    try:
        import PIL
        return int(PIL.__version__.split(".")[0]) >= 13
    except Exception:
        return False


def sniff(data: bytes) -> Optional[str]:
    head = data[:16]
    if head.startswith(b"%PDF-") or b"%PDF-" in data[:1024]:
        return PDF
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if head.startswith(b"\xff\xd8\xff"):
        return JPEG
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return TIFF
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile:
            return None
        if "[Content_Types].xml" in names:
            if any(n.startswith("word/") for n in names):
                return DOCX
            if any(n.startswith("ppt/") for n in names):
                return PPTX
        return "application/zip"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "application/x-ole-storage"  # legacy .doc/.ppt/.xls
    return None


# PDF names may hide letters as #xx escapes (/J#61vaScript). Decode before matching.
_PDF_NAME_ESC = re.compile(rb"#([0-9A-Fa-f]{2})")
_PDF_ACTIVE = re.compile(rb"/(JavaScript|JS|Launch|EmbeddedFile|RichMedia|XFA|SubmitForm|ImportData)\b")


def _pdf_checks(data: bytes) -> list[str]:
    decoded = _PDF_NAME_ESC.sub(lambda m: bytes([int(m.group(1), 16)]), data)
    if re.search(rb"/Encrypt\b", decoded):
        raise ValidationError("encrypted", "password-protected or encrypted PDFs are not accepted")
    found = sorted({m.group(1).decode() for m in _PDF_ACTIVE.finditer(decoded)})
    if found:
        raise ValidationError("active_content", f"PDF contains active content: {', '.join(found)}")
    return []


def _zip_checks(data: bytes) -> list[str]:
    warnings: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = z.infolist()
        if len(infos) > config.MAX_ZIP_ENTRIES:
            raise ValidationError("zip_bomb", f"{len(infos)} entries exceeds {config.MAX_ZIP_ENTRIES}")
        total = 0
        for i in infos:
            name = i.filename
            if name.startswith("/") or ".." in name.split("/") or "\\" in name:
                raise ValidationError("path_traversal", f"unsafe member name {name!r}")
            total += i.file_size
            if i.compress_size and i.file_size / max(i.compress_size, 1) > config.MAX_ZIP_RATIO \
                    and i.file_size > 1_000_000:
                raise ValidationError("zip_bomb", f"member {name} expands {i.file_size / i.compress_size:.0f}x")
        if total > config.MAX_UNCOMPRESSED_BYTES:
            raise ValidationError("zip_bomb", f"uncompressed size {total} exceeds limit")
        names = {i.filename.lower() for i in infos}
        if any(n.endswith("vbaproject.bin") for n in names):
            raise ValidationError("active_content", "Office file contains macros (vbaProject.bin)")
        if any(n.endswith(".bin") and "activex" in n for n in names):
            raise ValidationError("active_content", "Office file contains ActiveX controls")
        for n in z.namelist():
            if not n.lower().endswith(".rels"):
                continue
            rels = z.read(n)
            if b'TargetMode="External"' in rels and re.search(rb"attachedTemplate|oleObject|frame", rels):
                raise ValidationError("active_content", "Office file loads an external template or object")
        if any("embeddings/" in n for n in names):
            warnings.append("embedded objects present; their content is not extracted")
    return warnings


def _malware_scan(data: bytes) -> str:
    """ClamAV via clamdscan when configured. In DEV the scanner may be absent;
    production sets NEXA_KNOWLEDGE_REQUIRE_AV=true so an absent scanner rejects."""
    if config.AV_SCANNER == "clamav":
        exe = shutil.which("clamdscan") or shutil.which("clamscan")
        if exe:
            proc = subprocess.run([exe, "--no-summary", "-"], input=data, capture_output=True, timeout=120)
            if proc.returncode == 0:
                return "clean"
            if proc.returncode == 1:
                raise ValidationError("malware", proc.stdout.decode(errors="replace").strip()[:200])
            if config.REQUIRE_AV:
                raise ValidationError("av_unavailable", "malware scanner error")
            return f"skipped:scanner_error_{proc.returncode}"
        if config.REQUIRE_AV:
            raise ValidationError("av_unavailable", "clamav configured but not installed")
        return "skipped:not_installed"
    if config.REQUIRE_AV:
        raise ValidationError("av_unavailable", "a malware scanner is required but none is configured")
    return "skipped:not_configured"


def validate_file(file_name: str, data: bytes) -> ValidationResult:
    size = len(data)
    if size == 0:
        raise ValidationError("empty", "file is empty")
    if size > config.MAX_FILE_BYTES:
        raise ValidationError("too_large", f"{size} bytes exceeds {config.MAX_FILE_BYTES}")
    ext = os.path.splitext(file_name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError("unsupported_type", f"extension {ext or '(none)'} is not accepted")
    sniffed = sniff(data)
    if sniffed == "application/x-ole-storage":
        raise ValidationError("unsupported_type", "legacy binary Office files (.doc/.ppt) are not accepted; "
                                                  "convert to .docx or .pptx")
    expected = ALLOWED_EXTENSIONS[ext]
    if sniffed != expected:
        raise ValidationError("type_mismatch", f"extension says {expected} but content is {sniffed or 'unknown'}")
    if sniffed in (PNG, JPEG, TIFF) and not image_uploads_enabled():
        raise ValidationError("unsupported_type", "image uploads are disabled until the runtime has Pillow 13 "
                                                  "or later (Python 3.11+); upload a PDF scan instead")

    warnings: list[str] = []
    if sniffed == PDF:
        warnings += _pdf_checks(data)
    elif sniffed in (DOCX, PPTX):
        warnings += _zip_checks(data)
    av = _malware_scan(data)
    return ValidationResult(mime_type=sniffed, size=size, av=av, warnings=warnings)


# ---- sensitive content in extracted text ----

_SECRET_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b")),
    ("slack_token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("assigned_secret", re.compile(
        r"(?i)\b(?:password|passwd|api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*['\"]?[^\s'\"]{8,}")),
]
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def scan_sensitive_text(text: str) -> tuple[list[str], list[str]]:
    """Return (blocking_findings, warnings). Credentials block ingestion. Many
    personal email addresses suggest a roster or learner records, which belong
    in the Memory Service, not the knowledge store, so that blocks too."""
    blocking = [name for name, rx in _SECRET_PATTERNS if rx.search(text)]
    warnings: list[str] = []
    emails = set(_EMAIL.findall(text))
    if len(emails) >= config.MAX_EMAILS_BEFORE_BLOCK:
        blocking.append(f"personal_data:{len(emails)}_email_addresses")
    elif emails:
        warnings.append(f"{len(emails)} email address(es) present")
    return blocking, warnings
