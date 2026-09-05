from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

UNKNOWN = "На ручную проверку"
SERVICE = "Служебный файл"


@dataclass(frozen=True)
class Classification:
    category: str
    confidence: float  # Rule score, NOT a calibrated probability.
    reason: str


RULES = [
    ("Сертификаты и паспорта", ("сертификат", "паспорт", "качества")),
    ("Геодезическая съемка", ("исполнительная съемка", "геодез", "картограмма")),
    ("Заключения ЛНК", ("лнк", "неразруша", "рентген", "узк", "вик")),
    ("Журналы ведения работ", ("журнал",)),
    ("ППР и ТК", ("ппр", "технологичес", "техкарт", "ту на пересеч")),
    ("Уведомление о закрытие предписаний", ("предписан", "уведомлен")),
    ("Отчеты технадзора", ("технадзор", "тех надзор", "отчет")),
    ("Допускные документы", ("допуск", "разреш", "наряд")),
    ("ИД ростехнадзор", ("ростехнадзор",)),
    ("ИД черновая", ("ид черн", "исполнительная документац")),
    ("Согласование изменение объекта", ("согласование измен",)),
    ("Ход строильства объекта", ("ход стро", "сводка")),
    ("Фото объекта", ("фото",)),
]
PROJECT_CODE_RE = re.compile(r"[А-ЯA-Z0-9]+(?:-[А-ЯA-Z0-9.]+){2,}", re.IGNORECASE)
REVISION_RE = re.compile(r"(?:[_-](\d{1,2}))(?=\.[^.]+$)")


def classify_path(path: str) -> Classification:
    normalized = path.replace("\\", "/").casefold().replace("ё", "е")
    name = PurePosixPath(normalized).name
    suffix = PurePosixPath(name).suffix
    if name in {"thumbs.db", ".ds_store"} or "__macosx/" in normalized:
        return Classification(SERVICE, 1.0, "служебный файл ОС")
    # Explicit document context outranks image extension: a scanned certificate is not a site photo.
    for category, hints in RULES:
        if any(hint in normalized for hint in hints):
            return Classification(category, 0.92, f"имя/путь: {category}")
    if suffix in {".jpg", ".jpeg", ".png", ".heic", ".bmp"}:
        return Classification("Фото объекта", 0.70, "изображение; назначение нужно проверить")
    if suffix in {".pdf", ".dwg", ".dxf"} and PROJECT_CODE_RE.search(name):
        return Classification("Проект", 0.82, "код документа и инженерный формат")
    if suffix in {".dwg", ".dxf"} or "/проект/" in f"/{normalized}":
        return Classification("Проект", 0.70, "CAD или папка проекта; проверить")
    return Classification(UNKNOWN, 0.35, "недостаточно признаков в имени/пути")


def extract_project_code(path: str) -> str | None:
    match = PROJECT_CODE_RE.search(PurePosixPath(path.replace("\\", "/")).stem)
    return match.group(0) if match else None


def extract_revision(path: str) -> str | None:
    match = REVISION_RE.search(PurePosixPath(path.replace("\\", "/")).name)
    return match.group(1) if match else None
