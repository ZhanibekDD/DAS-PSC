import pytest
from app.services.classifier import classify_path, extract_project_code, extract_revision


@pytest.mark.parametrize('path,category', [
    ('Проект/ABC-001-ГЧ-010_2.pdf', 'Проект'),
    ('Сертификат/Сертификат трубы.pdf', 'Сертификаты и паспорта'),
    ('Сертификат/scan.jpg', 'Сертификаты и паспорта'),
    ('Фото/image.jpg', 'Фото объекта'), ('image.jpg', 'Фото объекта'),
    ('Thumbs.db', 'Служебный файл'),
    ('Ограждения.bak', 'Служебный файл'),
    ('999.unknown', 'На ручную проверку'),
    ('Геодезическая съёмка/схема.pdf', 'Геодезическая съемка'),
    ('Согласование изменение объекта.txt', 'Согласование изменение объекта'),
    ('Раздел ПД №5-ПОС (2).pdf', 'Проект'),
    ('Ограждения Швеллер 10 -Модель.pdf', 'Проект'),
])
def test_classification(path, category):
    assert classify_path(path).category == category


def test_realistic_project_hints_are_conservative():
    pos = classify_path('Раздел ПД №5-ПОС (2).pdf')
    model = classify_path('Ограждения Швеллер 10 -Модель.pdf')
    signs = classify_path('Знаки УЗА ДНС11.docx')

    assert pos.category == 'Проект'
    assert pos.confidence >= 0.90
    assert model.category == 'Проект'
    assert 0.70 <= model.confidence < 0.90
    assert signs.category == 'На ручную проверку'


def test_metadata():
    path = 'Проект\\ABC-001-ГЧ-010_2.pdf'
    assert extract_project_code(path) == 'ABC-001-ГЧ-010'
    assert extract_revision(path) == '2'


def test_completeness_is_presence_only():
    from app.services.completeness import build_completeness
    report = build_completeness(['Проект', 'Фото'], ['Проект', 'Фото', 'Журналы'])
    assert report['percent'] == 67
    assert report['missing'] == ['Журналы']
    assert report['metric'] == 'category_presence_not_document_completeness'
