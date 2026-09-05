import io
import time
import zipfile

from pypdf import PdfWriter

from app.services.classifier import UNKNOWN, classify_path, combine_classifications
from app.services.content_analyzer import analyze_content
from app.services.zip_analyzer import analyze_zip
from test_zip_analyzer import make_zip


def make_docx(text: str) -> bytes:
    out = io.BytesIO()
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>' + text + '</w:t></w:r></w:p></w:body></w:document>'
    ).encode()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)
    return out.getvalue()


def make_blank_pdf() -> bytes:
    out = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(out)
    return out.getvalue()


def test_docx_text_is_read_without_persisting_raw_text():
    result = analyze_content(
        "unknown.docx",
        make_docx("СЕРТИФИКАТ КАЧЕСТВА на трубу. Изготовитель: synthetic."),
        time.monotonic() + 10,
    )
    assert result["status"] == "text"
    assert result["text_chars"] > 20
    assert result["classification"]["category"] == "Сертификаты и паспорта"
    assert result["classification"]["confidence"] >= 0.90
    assert "СЕРТИФИКАТ КАЧЕСТВА" not in str(result)


def test_blank_pdf_is_marked_for_ocr():
    result = analyze_content("scan.pdf", make_blank_pdf(), time.monotonic() + 10)
    assert result["status"] == "no_text"
    assert result["format"] == "pdf"
    assert result["needs_ocr"] is True
    assert result["classification"] is None


def test_content_can_upgrade_a_weak_filename_but_conflict_stays_manual():
    content = analyze_content(
        "mystery.docx",
        make_docx("ПАСПОРТ ОБОРУДОВАНИЯ. Паспорт изделия для synthetic valve."),
        time.monotonic() + 10,
    )["classification"]
    from app.services.classifier import Classification

    content_result = Classification(content["category"], content["confidence"], content["reason"])
    weak = classify_path("СТРМ-ННП.24126-ННП-001-АС01-ГЧ-001_1.pdf")
    combined = combine_classifications(weak, content_result)
    assert combined.category == "Сертификаты и паспорта"
    assert combined.confidence >= 0.90

    project_content = analyze_content(
        "mystery.docx",
        make_docx("РАБОЧАЯ ДОКУМЕНТАЦИЯ. Рабочие чертежи synthetic section."),
        time.monotonic() + 10,
    )["classification"]
    project_result = Classification(
        project_content["category"], project_content["confidence"], project_content["reason"]
    )
    strong_path = classify_path("Сертификаты/паспорт трубы.pdf")
    conflict = combine_classifications(strong_path, project_result)
    assert conflict.category == UNKNOWN
    assert conflict.confidence < 0.70


def test_zip_analyzer_uses_docx_content_for_unknown_filename():
    result = analyze_zip(make_zip({"Документы/unknown.docx": make_docx("СЕРТИФИКАТ СООТВЕТСТВИЯ synthetic") }))
    row = result["files"][0]
    assert row["content_analysis"]["status"] == "text"
    assert row["classification"]["category"] == "Сертификаты и паспорта"
    assert result["content_text_files"] == 1
    assert result["content_needs_ocr"] == 0
