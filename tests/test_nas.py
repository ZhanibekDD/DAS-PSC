import os
from pathlib import Path
from dataclasses import replace
import pytest
from app.services.nas_scanner import scan_nas
from app.services.zip_analyzer import UnsafeArchive, DEFAULT_LIMITS

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='NAS scanner requires Unix')


def test_nas_read_only_and_links(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    (root / 'a.pdf').write_bytes(b'a')
    (root / 'copy.pdf').write_bytes(b'a')
    secret = tmp_path / 'private.txt'
    secret.write_bytes(b'do-not-read')
    (root / 'link').symlink_to(secret)
    (root / 'dirlink').symlink_to(tmp_path, target_is_directory=True)
    result = scan_nas(root)
    assert result['file_count'] == 2
    assert len(result['duplicate_groups']) == 1
    assert len(result['warnings']) == 2
    assert not result['complete_scan']
    assert (root / 'a.pdf').read_bytes() == b'a'
    assert set(p.name for p in root.iterdir()) == {'a.pdf', 'copy.pdf', 'link', 'dirlink'}


@pytest.mark.parametrize('relative', ['../', '/etc', 'C:/etc'])
def test_nas_escape(tmp_path, relative):
    with pytest.raises(UnsafeArchive):
        scan_nas(tmp_path, relative)


def test_nas_root_symlink(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(UnsafeArchive):
        scan_nas(alias)


def test_nas_subdir_and_hardlink(tmp_path):
    sub = tmp_path / 'sub'
    sub.mkdir()
    (sub / 'x.pdf').write_bytes(b'x')
    result = scan_nas(tmp_path, 'sub')
    assert result['files'][0]['path'] == 'x.pdf'
    os.link(sub / 'x.pdf', sub / 'hard')
    assert scan_nas(tmp_path, 'sub')['file_count'] == 0


def test_nas_limit(tmp_path):
    (tmp_path / 'x').write_bytes(b'ab')
    with pytest.raises(UnsafeArchive):
        scan_nas(tmp_path, limits=replace(DEFAULT_LIMITS, max_file=1))
