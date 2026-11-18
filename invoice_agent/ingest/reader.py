import hashlib
import unicodedata
from pathlib import Path
from typing import Literal

import pdfplumber
from pydantic import BaseModel

NO_TEXT_ISSUE = "no text layer — scanned PDF not supported"
_FORMATS = {".pdf", ".txt", ".json", ".csv", ".xml"}


class UnsupportedFormatError(ValueError):
    pass


class ReadResult(BaseModel):
    raw_text: str
    format: Literal["pdf", "txt", "json", "csv", "xml"]
    source_path: str
    content_hash: str
    issues: list[str] = []


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n"))


def read_file(path: Path) -> ReadResult:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in _FORMATS:
        raise UnsupportedFormatError(f"unsupported file extension: {path.suffix!r}")
    fmt = suffix[1:]
    issues: list[str] = []
    if fmt == "pdf":
        with pdfplumber.open(path) as pdf:
            pages = [(page.extract_text(layout=True) or "") for page in pdf.pages]
        if not any(p.strip() for p in pages):
            issues.append(NO_TEXT_ISSUE)
        text = "\n".join(pages)
    else:
        text = path.read_text("utf-8")
    return ReadResult(
        raw_text=_clean(text),
        format=fmt,
        source_path=str(path),
        content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
        issues=issues,
    )
