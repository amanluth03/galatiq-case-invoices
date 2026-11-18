"""Tests for read_file: per-format reading, hashing, cleanup, scanned-PDF issue, unsupported types.
"""
import hashlib
from pathlib import Path

import pytest

from invoice_agent.utils.reader import UnsupportedFormatError, read_file

INVOICES = Path("data/invoices")


def test_reads_pdf_text_layer():
    """A real sample PDF yields its text (typo "2O26" preserved) with no issues."""
    result = read_file(INVOICES / "invoice_1012.pdf")
    assert "26-Jan-2O26" in result.raw_text
    assert "WidgetB" in result.raw_text
    assert result.format == "pdf"
    assert result.issues == []


def test_reads_json_verbatim():
    """JSON is returned as raw text, not parsed."""
    result = read_file(INVOICES / "invoice_1009.json")
    assert '"quantity": -5' in result.raw_text
    assert result.format == "json"


def test_content_hash_is_sha256_of_bytes():
    """content_hash equals sha256 of the file bytes."""
    path = INVOICES / "invoice_1009.json"
    assert read_file(path).content_hash == hashlib.sha256(path.read_bytes()).hexdigest()


def test_cleanup_normalizes_line_endings_and_trailing_space(tmp_path):
    """CRLF becomes LF and trailing spaces are stripped."""
    p = tmp_path / "x.txt"
    p.write_bytes(b"a  \r\nb\r\n")
    assert read_file(p).raw_text == "a\nb\n"


def test_pdf_without_text_layer_reports_issue(monkeypatch):
    """A PDF with no extractable text reports the scanned-PDF issue."""
    import pdfplumber.page

    # Simulate a scanned PDF by making text extraction return nothing.
    monkeypatch.setattr(pdfplumber.page.Page, "extract_text", lambda self, **kw: None)
    result = read_file(INVOICES / "invoice_1012.pdf")
    assert result.issues == ["no text layer — scanned PDF not supported"]


def test_unsupported_extension_raises(tmp_path):
    """.docx is rejected with UnsupportedFormatError."""
    p = tmp_path / "x.docx"
    p.write_text("hi")
    with pytest.raises(UnsupportedFormatError):
        read_file(p)
