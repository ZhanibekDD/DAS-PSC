from __future__ import annotations

import hashlib
import io
import json
import re
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from pypdf import PdfReader

from app.services.classifier import Classification, classify_content

MAX_CONTENT_FILE = 32 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
MAX_PDF_PAGES = 1000
MAX_PDF_EXTRACT_PAGES = 40
MAX_DOCX_XML = 12 * 1024 * 1024
MAX_DOCX_TOTAL_XML = 24 * 1024 * 1024


@dataclass(frozen=True)
class ContentAnalysis:
    status: str
    format: str
    text_chars: int = 0
    pages: int | None = None
    reason: str = ""
    classification: Classification | None = None

    def as_payload(self) -> dict:
        payload = asdict(self)
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload["digest"] = hashlib.sha256(canonical.encode()).hexdigest()
        payload["needs_ocr"] = self.status == "no_text" and self.format == "pdf"
        return payload


def _normalize_text(value: str) -> str:
    text = re.sub(r"\s+", " ", value.replace("\x00", " ")).strip()
    return text[:MAX_TEXT_CHARS]


def _finish(fmt: str, text: str, pages: int | None = None) -> ContentAnalysis:
    normalized = _normalize_text(text)
    if not normalized:
        reason = "PDF не содержит извлекаемого текстового слоя; нужен OCR" if fmt == "pdf" else "Текст в DOCX не найден"
        return ContentAnalysis("no_text", fmt, 0, pages, reason)
    content_class = classify_content(normalized)
    return ContentAnalysis(
        "text",
        fmt,
        len(normalized),
        pages,
        "Текстовый слой прочитан; исходный текст не сохраняется",
        content_class,
    )


def _pdf(data: bytes, deadline: float) -> ContentAnalysis:
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            return ContentAnalysis("encrypted", "pdf", reason="Зашифрованный PDF не анализируется")
        pages = len(reader.pages)
        if pages > MAX_PDF_PAGES:
            return ContentAnalysis(
                "too_large",
                "pdf",
                pages=pages,
                reason=f"PDF содержит больше {MAX_PDF_PAGES} страниц; содержательный разбор пропущен",
            )
        chunks: list[str] = []
        chars = 0
        extract_pages = min(pages, MAX_PDF_EXTRACT_PAGES)
        for index in range(extract_pages):
            if time.monotonic() > deadline:
                return ContentAnalysis("timeout", "pdf", pages=pages, reason="Не хватило времени на чтение PDF")
            value = reader.pages[index].extract_text() or ""
            if value:
                remaining = MAX_TEXT_CHARS - chars
                if remaining <= 0:
                    break
                value = value[:remaining]
                chunks.append(value)
                chars += len(value)
        result = _finish("pdf", "\n".join(chunks), pages)
        if result.status == "text" and pages > extract_pages:
            return ContentAnalysis(
                "text",
                "pdf",
                result.text_chars,
                pages,
                f"Прочитаны первые {extract_pages} из {pages} страниц; исходный текст не сохраняется",
                result.classification,
            )
        return result
    except Exception:
        return ContentAnalysis("error", "pdf", reason="PDF не удалось безопасно прочитать")


def _docx(data: bytes, deadline: float) -> ContentAnalysis:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [
                name for name in archive.namelist()
                if name == "word/document.xml"
                or name == "word/footnotes.xml"
                or name == "word/endnotes.xml"
                or name.startswith("word/header") and name.endswith(".xml")
                or name.startswith("word/footer") and name.endswith(".xml")
            ]
            if "word/document.xml" not in names:
                return ContentAnalysis("error", "docx", reason="DOCX не содержит word/document.xml")
            total = 0
            chunks: list[str] = []
            for name in sorted(set(names)):
                if time.monotonic() > deadline:
                    return ContentAnalysis("timeout", "docx", reason="Не хватило времени на чтение DOCX")
                info = archive.getinfo(name)
                if info.file_size > MAX_DOCX_XML:
                    return ContentAnalysis("too_large", "docx", reason="Слишком большой XML-раздел DOCX")
                total += info.file_size
                if total > MAX_DOCX_TOTAL_XML:
                    return ContentAnalysis("too_large", "docx", reason="Слишком большой объем XML внутри DOCX")
                raw = archive.read(info)
                if len(raw) > MAX_DOCX_XML:
                    return ContentAnalysis("too_large", "docx", reason="Размер XML внутри DOCX превышает лимит")
                root = ET.fromstring(raw)
                text = " ".join(value for value in root.itertext() if value)
                if text:
                    chunks.append(text)
            return _finish("docx", "\n".join(chunks))
    except (zipfile.BadZipFile, KeyError, ET.ParseError, DefusedXmlException, RuntimeError, NotImplementedError):
        return ContentAnalysis("error", "docx", reason="DOCX не удалось безопасно прочитать")


def analyze_content(path: str, data: bytes, deadline: float) -> dict:
    suffix = PurePosixPath(path.casefold()).suffix
    if suffix not in {".pdf", ".docx"}:
        return ContentAnalysis("unsupported", suffix.lstrip("."), reason="Формат пока не читается по содержанию").as_payload()
    if len(data) > MAX_CONTENT_FILE:
        return ContentAnalysis(
            "too_large",
            suffix.lstrip("."),
            reason=f"Файл больше {MAX_CONTENT_FILE // (1024 * 1024)} МиБ; содержательный разбор пропущен",
        ).as_payload()
    if time.monotonic() > deadline:
        return ContentAnalysis("timeout", suffix.lstrip("."), reason="Не хватило времени на содержательный разбор").as_payload()
    result = _pdf(data, deadline) if suffix == ".pdf" else _docx(data, deadline)
    return result.as_payload()
