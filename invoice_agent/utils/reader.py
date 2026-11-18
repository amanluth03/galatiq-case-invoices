"""read_file stage: turn a PDF/TXT/JSON/CSV/XML file into raw text plus a content hash.
No parsing or content edits here; extract (LLM) interprets the text next.
"""
import hashlib
import unicodedata
from pathlib import Path
from typing import Literal

import pdfplumber
from pydantic import BaseModel

# Scanned PDFs have no text layer; OCR is out of scope, so we report an issue instead of guessing.
NO_TEXT_ISSUE = "no text layer — scanned PDF not supported"
_FORMATS = {".pdf", ".txt", ".json", ".csv", ".xml"}


class UnsupportedFormatError(ValueError):
    """Raised for file extensions the pipeline does not handle."""
    pass


class ReadResult(BaseModel):
    """File text and metadata handed to extract."""
    raw_text: str
    format: Literal["pdf", "txt", "json", "csv", "xml"]
    source_path: str
    # sha256 of the file bytes: used to spot exact duplicate submissions.
    content_hash: str
    issues: list[str] = []


def _clean(text: str) -> str:
    """Light cleanup only (Unicode, line endings, trailing spaces); never changes content like "2O26"."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n"))


def read_file(path: Path) -> ReadResult:
    """Read an invoice file of a supported format into a ReadResult."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in _FORMATS:
        raise UnsupportedFormatError(f"unsupported file extension: {path.suffix!r}")
    fmt = suffix[1:]
    issues: list[str] = []
    if fmt == "pdf":
        with pdfplumber.open(path) as pdf:
            # layout=True keeps table columns aligned so line items stay readable.
            pages = [(page.extract_text(layout=True) or "") for page in pdf.pages]
        if not any(p.strip() for p in pages):
            issues.append(NO_TEXT_ISSUE)
        text = "\n".join(pages)
    else:
        # Structured formats (JSON/CSV/XML) stay raw text on purpose: they contain bad values and odd layouts the LLM must interpret.
        text = path.read_text("utf-8")
    return ReadResult(
        raw_text=_clean(text),
        format=fmt,
        source_path=str(path),
        content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
        issues=issues,
    )
