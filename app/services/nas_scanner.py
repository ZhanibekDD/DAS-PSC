"""Read-only scanner for a locally mounted NAS directory (Linux/Unix). No web filesystem API."""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path

from app.services.zip_analyzer import DEFAULT_LIMITS, Limits, UnsafeArchive, hash_stream, make_row, safe_path, summarize


def scan_nas(root: Path, relative: str = ".", limits: Limits = DEFAULT_LIMITS) -> dict:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise UnsafeArchive("NAS dry-run требует Unix с O_NOFOLLOW; на Windows используйте ZIP")
    root = Path(root).absolute()
    if ".." in root.parts:
        raise UnsafeArchive("Путь корня NAS не должен содержать ..")
    parts = root.parts[1:]
    if relative != ".":
        parts += tuple(safe_path(relative).split("/"))
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root.anchor, directory_flags)
    try:
        # Open every component relative to the verified directory descriptor.
        for part in parts:
            child = os.open(part, directory_flags, dir_fd=fd)
            os.close(fd)
            fd = child
        rows, warnings = [], []
        total, entries = 0, 0
        deadline = time.monotonic() + limits.max_seconds

        def walk(parent: int, prefix: str, depth: int):
            nonlocal total, entries
            if depth > 40:
                raise UnsafeArchive("Слишком глубокая структура NAS")
            with os.scandir(parent) as iterator:
                for entry in iterator:
                    entries += 1
                    if entries > limits.max_entries or time.monotonic() > deadline:
                        raise UnsafeArchive("Превышен лимит элементов или времени NAS")
                    path = safe_path(f"{prefix}{entry.name}")
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            warnings.append({"path": path, "reason": "символическая ссылка пропущена"})
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            child = os.open(entry.name, directory_flags, dir_fd=parent)
                            try:
                                walk(child, path + "/", depth + 1)
                            finally:
                                os.close(child)
                            continue
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                            warnings.append({"path": path, "reason": "специальный файл или hardlink пропущен"})
                            continue
                        file_fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                        with os.fdopen(file_fd, "rb") as stream:
                            before = os.fstat(stream.fileno())
                            if not stat.S_ISREG(before.st_mode) or before.st_nlink > 1:
                                raise UnsafeArchive("Тип файла NAS изменился во время чтения")
                            if before.st_size > limits.max_file or total + before.st_size > limits.max_total:
                                raise UnsafeArchive("Превышен размер NAS-снимка")
                            sha, size = hash_stream(stream, limits, deadline)
                            after = os.fstat(stream.fileno())
                            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                                raise UnsafeArchive("Файл NAS изменился во время анализа; повторите снимок")
                        total += size
                        if total > limits.max_total:
                            raise UnsafeArchive("Превышен размер NAS-снимка")
                        rows.append(make_row(path, size, sha))
                    except OSError:
                        warnings.append({"path": path, "reason": "недоступен или изменился; проверить вручную"})
        walk(fd, "", 0)
        result = summarize(sorted(rows, key=lambda r: r["path"]))
        return {**result, "warnings": warnings, "read_only": True, "complete_scan": not warnings}
    except OSError as exc:
        raise UnsafeArchive("Корень NAS недоступен или содержит символическую ссылку") from exc
    finally:
        os.close(fd)
