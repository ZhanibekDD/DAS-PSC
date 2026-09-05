from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class Classification:
    category: str
    confidence: float
    reason: str


RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Фото объекта", ("фото", ".jpg", ".jpeg", ".png", ".heic", ".bmp")),
    ("Сертификаты и паспорта", ("сертификат", "паспорт", "качества")),
    ("Геодезическая съемка", ("исполнительная съемка", "геодез", "картограмма")),
    ("Заключения ЛНК", ("лнк", "неразруша", "рентген", "узк", "вик")),
    ("Журналы ведения работ", ("журнал",)),
    ("ППР и ТК", ("ппр", "технологичес", "техкарт", "ту на пересеч")),
    ("Отчеты технадзора", ("технадзор", "тех надзор", "отчет")),
    ("Уведомление о закрытие предписаний", ("предписан", "уведомлен")),
    ("Допускные документы", ("допуск", "разреш", "наряд")),
    ("ИД ростехнадзор", ("ростехнадзор",)),
    ("ИД черновая", ("ид черн", "исполнительная документац")),
    ("Ход строительства объекта", ("ход стро", "сводка")),
]

PROJECT_CODE_RE = re.compile(r"[А-ЯA-Z0-9]+(?:-[А-ЯA-Z0-9.]+){2,}", re.IGNORECASE)
REVISION_RE = re.compile(r"(?:[_-](\d{1,2}))(?=\.[^.]+$)")


def classify_path(path: str) -> Classification:
    normalized = path.replace("\\", "/").lower()
    name = PurePosixPath(normalized).name

    if name in {"thumbs.db", ".ds_store"}:
        return Classification("Служебный файл", 1.0, "служебный файл ОС")

    for category, hints in RULES:
        if any(hint in normalized for hint in hints):
            return Classification(category, 0.92, f"совпадение по признаку: {category}")

    suffix = PurePosixPath(name).suffix.lower()
    if suffix in {".pdf", ".dwg", ".dxf"} and PROJECT_CODE_RE.search(name):
        return Classification("Проект", 0.82, "проектный код и инженерный формат")
    if suffix in {".dwg", ".dxf"}:
        return Classification("Проект", 0.70, "CAD-файл")

    return Classification("На ручную проверку", 0.35, "недостаточно признаков")


def extract_project_code(path: str) -> str | None:
    match = PROJECT_CODE_RE.search(PurePosixPath(path).name)
    return match.group(0) if match else None


def extract_revision(path: str) -> str | None:
    match = REVISION_RE.search(PurePosixPath(path).name)
    return match.group(1) if match else None
