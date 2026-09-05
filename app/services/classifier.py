from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

UNKNOWN = "На ручную проверку"
SERVICE = "Служебный файл"
HIGH_CONFIDENCE = 0.90
MEDIUM_CONFIDENCE = 0.70


@dataclass(frozen=True)
class Classification:
    category: str
    confidence: float  # Rule score, NOT a calibrated probability.
    reason: str


def confidence_level(score: float) -> str:
    if score >= HIGH_CONFIDENCE:
        return "high"
    if score >= MEDIUM_CONFIDENCE:
        return "medium"
    return "low"


# Ordered from more specific document contexts to broader ones. These rules inspect
# only a filename/path. They do not claim anything about the document contents.
RULES = [
    ("Сертификаты и паспорта", ("сертификат", "сертиф", "паспорт", "качества"), 0.94),
    ("Геодезическая съемка", ("исполнительная съемка", "исполнительная съёмка", "геодез", "картограмма"), 0.94),
    ("Заключения ЛНК", ("лнк", "неразруша", "рентген", "узк", "вик"), 0.94),
    ("Журналы ведения работ", ("журнал",), 0.92),
    ("ППР и ТК", ("ппр", "технологичес", "техкарт", "ту на пересеч"), 0.92),
    ("Уведомление о закрытие предписаний", ("закрытие предпис", "закрытия предпис", "уведомление о закры"), 0.94),
    ("Отчеты технадзора", ("технадзор", "тех надзор"), 0.94),
    ("Допускные документы", ("допуск", "разрешение на работ", "наряд-допуск", "наряд допуск"), 0.92),
    ("ИД ростехнадзор", ("ростехнадзор",), 0.96),
    ("ИД черновая", ("ид черн", "исполнительная документац", "акт освидетельствования", "акт скрытых работ", "аоср"), 0.86),
    ("Согласование изменение объекта", ("согласование измен",), 0.94),
    ("Ход строильства объекта", ("ход стро", "сводка строительства", "сводка по строитель"), 0.90),
    ("Фото объекта", ("фото/", "фото объекта", "фотофиксац"), 0.94),
]
PROJECT_CODE_RE = re.compile(r"[А-ЯA-Z0-9]+(?:-[А-ЯA-Z0-9.]+){2,}", re.IGNORECASE)
REVISION_RE = re.compile(r"(?:[_-](\d{1,2}))(?=\.[^.]+$)")
PROJECT_HINTS = (
    "рабочая документац",
    "проектная документац",
    "раздел пд",
    "раздел рд",
    "чертеж",
    "чертёж",
    "спецификац",
)
SERVICE_SUFFIXES = {".bak", ".tmp", ".temp", ".swp", ".swo", ".old", ".orig", ".autosave"}
POS_RE = re.compile(r"(?:^|[\s._()№\-])пос(?:$|[\s._()№\-])", re.IGNORECASE)


def classify_path(path: str) -> Classification:
    normalized = path.replace("\\", "/").casefold().replace("ё", "е")
    name = PurePosixPath(normalized).name
    suffix = PurePosixPath(name).suffix

    if (
        name in {"thumbs.db", ".ds_store"}
        or "__macosx/" in normalized
        or suffix in SERVICE_SUFFIXES
    ):
        return Classification(SERVICE, 1.0, "служебный/резервный файл")

    # Explicit document context outranks extension: a scanned certificate is not a site photo.
    for category, hints, score in RULES:
        if any(hint in normalized for hint in hints):
            return Classification(category, score, f"имя/путь: {category}")

    # Explicit PD/RD/POS wording is stronger than a generic engineering code.
    if suffix in {".pdf", ".doc", ".docx", ".dwg", ".dxf", ".xls", ".xlsx", ".xlsm"}:
        if any(hint in normalized for hint in PROJECT_HINTS) or POS_RE.search(name):
            return Classification("Проект", 0.92, "явный признак проектной/рабочей документации")

    # A generic image outside an explicitly named photo folder is deliberately low
    # confidence: it can be a scan of a document, scheme, passport, etc.
    if suffix in {".jpg", ".jpeg", ".png", ".heic", ".bmp"}:
        return Classification("Фото объекта", 0.60, "изображение без явного контекста; требуется проверка")

    has_code = bool(PROJECT_CODE_RE.search(name))
    if suffix in {".pdf", ".dwg", ".dxf", ".xls", ".xlsx", ".xlsm"} and has_code:
        if "/проект/" in f"/{normalized}":
            return Classification("Проект", 0.93, "папка проекта и инженерный шифр")
        return Classification("Проект", 0.82, "инженерный шифр; раздел нужно подтвердить")

    if suffix in {".dwg", ".dxf"} or "/проект/" in f"/{normalized}":
        return Classification("Проект", 0.78, "CAD или папка проекта; требуется проверка")

    # "Модель" is useful for engineering exports but still too broad for auto-confirmation.
    if suffix in {".pdf", ".dwg", ".dxf"} and "модель" in normalized:
        return Classification("Проект", 0.72, "инженерная модель; требуется проверка")

    return Classification(UNKNOWN, 0.30, "недостаточно признаков в имени/пути")


def extract_project_code(path: str) -> str | None:
    match = PROJECT_CODE_RE.search(PurePosixPath(path.replace("\\", "/")).stem)
    return match.group(0) if match else None


def extract_revision(path: str) -> str | None:
    match = REVISION_RE.search(PurePosixPath(path.replace("\\", "/")).name)
    return match.group(1) if match else None
