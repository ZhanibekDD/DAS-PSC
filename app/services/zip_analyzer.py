from __future__ import annotations

import hashlib
import zipfile
from dataclasses import asdict
from pathlib import PurePosixPath
from typing import BinaryIO

from app.services.classifier import classify_path, extract_project_code, extract_revision

MAX_FILES = 5000
MAX_SINGLE_FILE = 512 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED = 5 * 1024 * 1024 * 1024
HASH_CHUNK = 1024 * 1024


class UnsafeArchive(ValueError):
    pass


def _safe_name(name: str) -> bool:
    p = PurePosixPath(name.replace("\\", "/"))
    return not p.is_absolute() and ".." not in p.parts


def _sha256_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    digest = hashlib.sha256()
    with archive.open(info, "r") as stream:
        while chunk := stream.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def analyze_zip(file_obj: BinaryIO) -> dict:
    file_obj.seek(0)
    with zipfile.ZipFile(file_obj) as archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        if len(infos) > MAX_FILES:
            raise UnsafeArchive(f"слишком много файлов: {len(infos)}")

        total_size = sum(i.file_size for i in infos)
        if total_size > MAX_TOTAL_UNCOMPRESSED:
            raise UnsafeArchive("слишком большой распакованный размер архива")
        if any(i.file_size > MAX_SINGLE_FILE for i in infos):
            raise UnsafeArchive("в архиве есть слишком большой отдельный файл")
        if any(not _safe_name(i.filename) for i in infos):
            raise UnsafeArchive("обнаружен небезопасный путь внутри ZIP")

        rows = []
        hashes: dict[str, list[str]] = {}
        for info in infos:
            classification = classify_path(info.filename)
            sha = _sha256_entry(archive, info)
            hashes.setdefault(sha, []).append(info.filename)
            rows.append(
                {
                    "path": info.filename,
                    "name": PurePosixPath(info.filename).name,
                    "size": info.file_size,
                    "sha256": sha,
                    "project_code": extract_project_code(info.filename),
                    "revision": extract_revision(info.filename),
                    "classification": asdict(classification),
                }
            )

        duplicate_groups = [paths for paths in hashes.values() if len(paths) > 1]
        review_count = sum(r["classification"]["category"] == "На ручную проверку" for r in rows)
        return {
            "file_count": len(rows),
            "total_uncompressed_size": total_size,
            "review_count": review_count,
            "duplicate_groups": duplicate_groups,
            "files": rows,
        }
