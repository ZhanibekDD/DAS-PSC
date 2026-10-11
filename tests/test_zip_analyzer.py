import io
import stat
import zipfile
from dataclasses import replace

import pytest
from app.services.zip_analyzer import DEFAULT_LIMITS, UnsafeArchive, analyze_zip


def make_zip(files, compression=zipfile.ZIP_STORED):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression=compression) as z:
        for name, data in files.items():
            z.writestr(name, data)
    out.seek(0)
    return out


def test_duplicates_and_no_extraction(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = analyze_zip(make_zip({'Проект/A-001-ГЧ-001_0.pdf': b'same', 'copy.pdf': b'same'}))
    assert result['file_count'] == 2
    assert len(result['duplicate_groups']) == 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('path', ['../secret', '/root/x', 'C:/secret', 'C:\\secret', '\\\\server\\share',
                                     'a/../../x', 'a\x00evil', 'a:stream', 'a/../b', '../folder/', 'a\nb'])
def test_unsafe_paths(path):
    if '\x00' in path:
        # zipfile itself truncates filenames on write; test the validator directly.
        from app.services.zip_analyzer import safe_path
        with pytest.raises(UnsafeArchive):
            safe_path(path)
        return
    with pytest.raises(UnsafeArchive):
        analyze_zip(make_zip({path: b'x'}))


def test_symlink_rejected():
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        info = zipfile.ZipInfo('link')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, '/etc/passwd')
    with pytest.raises(UnsafeArchive):
        analyze_zip(out)


def test_duplicate_normalized_paths():
    with pytest.raises(UnsafeArchive):
        analyze_zip(make_zip({'x/a.pdf': b'x', 'x\\a.pdf': b'y'}))


@pytest.mark.parametrize('limit', [dict(max_upload=1), dict(max_file=1), dict(max_total=1), dict(max_entries=0), dict(max_seconds=-1)])
def test_limits(limit):
    with pytest.raises(UnsafeArchive):
        analyze_zip(make_zip({'a.pdf': b'abc'}), replace(DEFAULT_LIMITS, **limit))


def test_directory_count_limit():
    with pytest.raises(UnsafeArchive):
        analyze_zip(make_zip({'a/': b'', 'b/': b''}), replace(DEFAULT_LIMITS, max_entries=1))


def test_ratio_limit():
    with pytest.raises(UnsafeArchive):
        analyze_zip(make_zip({'a': b'0' * 10000}, zipfile.ZIP_DEFLATED), replace(DEFAULT_LIMITS, max_ratio=10))


def test_crc_damage_rejected():
    raw = make_zip({'a': b'original-data'}).getvalue().replace(b'original-data', b'corrupt--data')
    with pytest.raises(UnsafeArchive):
        analyze_zip(io.BytesIO(raw))


def test_invalid_zip():
    with pytest.raises(UnsafeArchive):
        analyze_zip(io.BytesIO(b'not zip'))


def test_encrypted_flag_rejected():
    raw = bytearray(make_zip({'a': b'test'}).getvalue())
    raw[6] |= 1
    central = raw.index(b'PK\x01\x02')
    raw[central + 8] |= 1
    with pytest.raises(UnsafeArchive):
        analyze_zip(io.BytesIO(raw))
