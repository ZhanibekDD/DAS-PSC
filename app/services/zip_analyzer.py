from __future__ import annotations

import hashlib
import stat
import time
import unicodedata
import zipfile
import zlib
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import BinaryIO

from app.services.classifier import (
    Classification,
    UNKNOWN,
    classify_path,
    combine_classifications,
    extract_project_code,
    extract_revision,
)
from app.services.content_analyzer import ContentAnalysis, MAX_CONTENT_FILE, analyze_content


class UnsafeArchive(ValueError):
    """Untrusted input rejected before any registry write."""


@dataclass(frozen=True)
class Limits:
    max_entries: int = 5000
    max_upload: int = 256 * 1024 * 1024
    max_file: int = 256 * 1024 * 1024
    max_total: int = 1024 * 1024 * 1024
    max_ratio: int = 1000
    max_seconds: float = 60


DEFAULT_LIMITS = Limits()
HASH_CHUNK = 1024 * 1024
CONTENT_SUFFIXES = {".pdf", ".docx"}


def safe_path(name: str) -> str:
    value = unicodedata.normalize("NFC", name.replace("\\", "/"))
    path = PurePosixPath(value)
    if (not value or len(value) > 1024 or value.startswith("/") or
            any(ord(c) < 32 or ord(c) == 127 for c in value) or ":" in value or
            ".." in path.parts or not path.parts or
            any(part.endswith((" ", ".")) for part in path.parts)):
        raise UnsafeArchive("Небезопасный путь в источнике")
    return str(path)


def hash_stream(stream: BinaryIO, limits: Limits, deadline: float) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    while True:
        if time.monotonic() > deadline:
            raise UnsafeArchive("Превышено время анализа")
        chunk = stream.read(HASH_CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if size > limits.max_file:
            raise UnsafeArchive("Превышен размер файла")
        digest.update(chunk)
    return digest.hexdigest(), size


def read_content_bytes(stream: BinaryIO, expected_size: int, deadline: float) -> bytes:
    data = bytearray()
    while True:
        if time.monotonic() > deadline:
            raise UnsafeArchive("Превышено время анализа")
        chunk = stream.read(HASH_CHUNK)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_CONTENT_FILE:
            raise UnsafeArchive("Внутренняя ошибка лимита содержательного анализа")
    if len(data) != expected_size:
        raise UnsafeArchive("Размер содержимого ZIP изменился между проходами")
    return bytes(data)


def content_classification(payload: dict) -> Classification | None:
    value = payload.get("classification")
    if not value:
        return None
    return Classification(value["category"], float(value["confidence"]), value["reason"])


def make_row(path: str, size: int, sha: str, content: dict) -> dict:
    path_result = classify_path(path)
    combined = combine_classifications(path_result, content_classification(content))
    return {
        "path": path,
        "name": PurePosixPath(path).name,
        "size": size,
        "sha256": sha,
        "project_code": extract_project_code(path),
        "revision": extract_revision(path),
        "classification": asdict(combined),
        "path_classification": asdict(path_result),
        "content_analysis": content,
    }


def summarize(rows: list[dict]) -> dict:
    groups: dict[str, list[str]] = {}
    for row in rows:
        groups.setdefault(row["sha256"], []).append(row["path"])
    return {
        "analysis_version": 1,
        "file_count": len(rows),
        "total_uncompressed_size": sum(r["size"] for r in rows),
        "review_count": sum(r["classification"]["category"] == UNKNOWN for r in rows),
        "duplicate_groups": [v for v in groups.values() if len(v) > 1],
        "content_text_files": sum(r["content_analysis"]["status"] == "text" for r in rows),
        "content_needs_ocr": sum(bool(r["content_analysis"].get("needs_ocr")) for r in rows),
        "content_errors": sum(r["content_analysis"]["status"] in {"error", "timeout"} for r in rows),
        "files": rows,
    }


def analyze_zip(file_obj: BinaryIO, limits: Limits = DEFAULT_LIMITS) -> dict:
    """Streams entries for hashes; content is read only in memory for bounded PDF/DOCX analysis."""
    file_obj.seek(0, 2)
    if file_obj.tell() > limits.max_upload:
        raise UnsafeArchive("Слишком большой ZIP")
    file_obj.seek(0)
    deadline = time.monotonic() + limits.max_seconds
    try:
        with zipfile.ZipFile(file_obj) as archive:
            all_infos = archive.infolist()
            if len(all_infos) > limits.max_entries:
                raise UnsafeArchive("Слишком много элементов ZIP")
            prepared, seen, total = [], set(), 0
            for info in all_infos:
                path = safe_path(info.orig_filename)
                mode = stat.S_IFMT(info.external_attr >> 16)
                if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise UnsafeArchive("Ссылки и специальные файлы в ZIP запрещены")
                if info.flag_bits & 1:
                    raise UnsafeArchive("Зашифрованные ZIP не поддерживаются")
                if info.is_dir():
                    continue
                if mode == stat.S_IFDIR:
                    raise UnsafeArchive("Некорректный тип ZIP-элемента")
                if path in seen:
                    raise UnsafeArchive("Повторяющийся нормализованный путь внутри ZIP")
                seen.add(path)
                total += info.file_size
                if info.file_size > limits.max_file or total > limits.max_total:
                    raise UnsafeArchive("Превышен распакованный размер ZIP")
                if info.file_size > max(info.compress_size, 1) * limits.max_ratio:
                    raise UnsafeArchive("Подозрительная степень сжатия ZIP")
                prepared.append((info, path))

            rows, actual_total = [], 0
            for info, path in prepared:
                with archive.open(info) as stream:
                    sha, size = hash_stream(stream, limits, deadline)
                actual_total += size
                if size != info.file_size or actual_total > limits.max_total:
                    raise UnsafeArchive("Размер содержимого ZIP не соответствует лимитам")

                suffix = PurePosixPath(path.casefold()).suffix
                if suffix in CONTENT_SUFFIXES and size <= MAX_CONTENT_FILE:
                    with archive.open(info) as stream:
                        raw = read_content_bytes(stream, size, deadline)
                    content = analyze_content(path, raw, deadline)
                elif suffix in CONTENT_SUFFIXES:
                    content = ContentAnalysis(
                        "too_large",
                        suffix.lstrip("."),
                        reason=f"Файл больше {MAX_CONTENT_FILE // (1024 * 1024)} МиБ; содержательный разбор пропущен",
                    ).as_payload()
                else:
                    content = analyze_content(path, b"", deadline)
                rows.append(make_row(path, size, sha, content))
            return summarize(rows)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error) as exc:
        raise UnsafeArchive("ZIP поврежден, зашифрован или использует неподдерживаемое сжатие") from exc
