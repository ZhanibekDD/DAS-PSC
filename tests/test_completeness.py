from app.services.completeness import build_completeness


def test_completeness_reports_missing():
    result = build_completeness(["Проект", "Фото объекта"], ["Проект", "Фото объекта", "Журналы"])
    assert result["percent"] == 67
    assert result["missing"] == ["Журналы"]
