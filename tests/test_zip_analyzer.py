import io
import zipfile

import pytest

from app.services.zip_analyzer import UnsafeArchive, analyze_zip


def make_zip(files: dict[str, bytes]) -> io.BytesIO:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    stream.seek(0)
    return stream


def test_analyze_zip_detects_duplicates():
    stream = make_zip({"Проект/A-001-ГЧ-001_0.pdf": b"same", "copy.pdf": b"same"})
    result = analyze_zip(stream)
    assert result["file_count"] == 2
    assert len(result["duplicate_groups"]) == 1


def test_rejects_path_traversal():
    stream = make_zip({"../secret.txt": b"x"})
    with pytest.raises(UnsafeArchive):
        analyze_zip(stream)
