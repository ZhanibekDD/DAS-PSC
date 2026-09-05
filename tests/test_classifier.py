from app.services.classifier import classify_path, extract_project_code, extract_revision


def test_project_drawing_classification():
    result = classify_path("Проект/ABC-001-ГЧ-010_2.pdf")
    assert result.category == "Проект"
    assert result.confidence >= 0.8


def test_certificate_classification():
    result = classify_path("Сертификат/Труба - сертификат качества №123.pdf")
    assert result.category == "Сертификаты и паспорта"


def test_photo_classification():
    result = classify_path("Фото от 01.01.2026/image.jpg")
    assert result.category == "Фото объекта"


def test_service_file():
    assert classify_path("Thumbs.db").category == "Служебный файл"


def test_extract_metadata():
    path = "Проект/ABC-001-ГЧ-010_2.pdf"
    assert extract_project_code(path) is not None
    assert extract_revision(path) == "2"
