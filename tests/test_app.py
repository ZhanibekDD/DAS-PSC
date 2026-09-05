import csv
import io
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from app.main import CONFIG, ProjectInput, create_app
from app.store import Store, Conflict
from app.services.zip_analyzer import analyze_zip
from test_zip_analyzer import make_zip

WRITE = {'X-PSC-Request': '1'}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, password='')) as c:
        yield c


def create(client, name='Тестовый объект'):
    response = client.post('/api/projects', json={'name': name, 'code': 'TEST-001'}, headers=WRITE)
    assert response.status_code == 201, response.text
    return response.json()['id']


def stage(client, pid, files=None):
    raw = make_zip(files or {'Проект/A-001-ГЧ-001_2.pdf': b'same', 'copy.pdf': b'same', '999.unknown': b'other'}).getvalue()
    response = client.post(f'/api/projects/{pid}/imports', files={'file': ('test.zip', raw)}, headers=WRITE)
    assert response.status_code == 201, response.text
    return response.json()['id']


def confirm(client, pid, iid):
    return client.post(f'/api/projects/{pid}/imports/{iid}/confirm', headers=WRITE)


def test_empty_dashboard_and_create(client):
    assert client.get('/health').json()['status'] == 'ok'
    html = client.get('/').text
    assert 'Начните с одного объекта' in html
    assert '42%' not in html
    pid = create(client)
    assert client.get(f'/projects/{pid}').status_code == 200
    assert client.get(f'/api/projects/{pid}').json()['progress'] is None


def test_preview_then_confirm_then_repeat(client):
    pid = create(client)
    iid = stage(client, pid)
    assert client.get(f'/api/projects/{pid}/documents').json()['total'] == 0
    assert client.get(f'/projects/{pid}/imports/{iid}').status_code == 200
    assert confirm(client, pid, iid).json()['added'] == 3
    assert confirm(client, pid, iid).json()['already_confirmed'] is True
    assert stage(client, pid) == iid
    assert client.get(f'/api/projects/{pid}/documents').json()['total'] == 3
    assert client.get(f'/projects/{pid}/documents').status_code == 200


def test_persistence_across_app_recreate(tmp_path):
    with TestClient(create_app(tmp_path, password='')) as c:
        pid = create(c)
        iid = stage(c, pid)
    with TestClient(create_app(tmp_path, password='')) as c:
        assert confirm(c, pid, iid).json()['added'] == 3
    with TestClient(create_app(tmp_path, password='')) as c:
        assert c.get(f'/api/projects/{pid}/documents').json()['total'] == 3


def test_review_filters_and_concurrency(client):
    pid = create(client)
    confirm(client, pid, stage(client, pid))
    endpoint = f'/api/projects/{pid}/documents'
    assert client.get(endpoint, params={'duplicates': 'true'}).json()['total'] == 2
    assert client.get(endpoint, params={'q': 'ПРОЕКТ'}).json()['total'] == 1
    assert client.get(endpoint, params={'q': "' OR 1=1 --"}).json()['total'] == 0
    row = client.get(endpoint, params={'q': '999'}).json()['items'][0]
    values = {'category': 'Фото объекта', 'version': row['version']}
    assert client.patch(f'{endpoint}/{row["id"]}', json=values, headers=WRITE).status_code == 200
    assert client.patch(f'{endpoint}/{row["id"]}', json=values, headers=WRITE).status_code == 409
    assert client.get(endpoint, params={'category': 'Фото объекта'}).json()['total'] == 1
    assert client.get(endpoint, params={'review': 'true'}).json()['total'] == 2
    assert client.get(endpoint, params={'page_size': 1, 'page': 2}).json()['page'] == 2
    assert client.get(endpoint, params={'page': 0}).status_code == 422


def test_unknown_category_rejected(client):
    pid = create(client)
    confirm(client, pid, stage(client, pid))
    row = client.get(f'/api/projects/{pid}/documents').json()['items'][0]
    assert client.patch(f'/api/projects/{pid}/documents/{row["id"]}',
                        json={'category': 'invented', 'version': 1}, headers=WRITE).status_code == 400


def test_cross_project_isolation(client):
    first, other = create(client), create(client)
    iid = stage(client, first)
    assert confirm(client, other, iid).status_code == 404
    confirm(client, first, iid)
    did = client.get(f'/api/projects/{first}/documents').json()['items'][0]['id']
    assert client.patch(f'/api/projects/{other}/documents/{did}',
                        json={'category': 'Проект', 'version': 1}, headers=WRITE).status_code == 404
    assert client.get(f'/api/projects/{other}/documents').json()['total'] == 0


def test_same_path_changed_content_keeps_versions(client):
    pid = create(client)
    confirm(client, pid, stage(client, pid, {'a.pdf': b'v1'}))
    confirm(client, pid, stage(client, pid, {'a.pdf': b'v2'}))
    assert client.get(f'/api/projects/{pid}/documents').json()['total'] == 2


def test_cancel_and_retry(client):
    pid = create(client)
    iid = stage(client, pid)
    assert client.post(f'/api/projects/{pid}/imports/{iid}/cancel', headers=WRITE).status_code == 200
    assert confirm(client, pid, iid).status_code == 409
    assert stage(client, pid) == iid
    assert confirm(client, pid, iid).status_code == 200
    assert client.post(f'/api/projects/{pid}/imports/{iid}/cancel', headers=WRITE).status_code == 409


def test_zero_size_placeholders_not_coverage(client):
    pid = create(client)
    confirm(client, pid, stage(client, pid, {'Проект/empty.pdf': b'', 'Фото/Thumbs.db': b'system'}))
    summary = client.get(f'/api/projects/{pid}').json()['summary']
    assert summary['documents'] == 2
    assert summary['coverage']['present'] == 0


def test_export_neutralizes_formula(client):
    pid = create(client)
    confirm(client, pid, stage(client, pid, {'=1+1.csv': b'demo'}))
    response = client.get(f'/api/projects/{pid}/documents.csv')
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.text.lstrip('\ufeff'))))
    assert rows[1][0] == "'=1+1.csv"


def test_csrf_and_host(client):
    assert client.post('/api/projects', json={'name': 'x', 'code': 'x'}).status_code == 403
    assert client.post('/api/projects', json={'name': 'x', 'code': 'x'},
                       headers={**WRITE, 'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/', headers={'Host': 'evil.example'}).status_code == 400


def test_password(tmp_path):
    with TestClient(create_app(tmp_path, password='synthetic-test-password')) as c:
        assert c.get('/health').status_code == 200
        assert c.get('/').status_code == 401
        assert c.get('/', auth=('psc', 'wrong')).status_code == 401
        assert c.get('/', auth=('psc', 'synthetic-test-password')).status_code == 200
    with pytest.raises(RuntimeError):
        create_app(tmp_path, password='weak')


def test_xss_escaping(client):
    pid = create(client, '<script>alert(1)</script>')
    html = client.get(f'/projects/{pid}').text
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html
    assert "script-src 'self'" in client.get('/').headers['content-security-policy']


@pytest.mark.parametrize('values', [{'name': '   ', 'code': 'x'}, {'name': 'x', 'code': ''},
    {'name': 'x', 'code': 'x', 'progress': 101}, {'name': 'x', 'code': 'x', 'start_date': 'bad'},
    {'name': 'x', 'code': 'x', 'start_date': '2026-12-20', 'end_date': '2026-01-01'}])
def test_invalid_project(client, values):
    assert client.post('/api/projects', json=values, headers=WRITE).status_code == 422


def test_edit_project_and_optimistic_lock(client):
    pid = create(client)
    data = client.get(f'/api/projects/{pid}').json()
    data['progress'] = 25
    data['stage'] = 'Подготовка'
    assert client.patch(f'/api/projects/{pid}', json=data, headers=WRITE).status_code == 200
    assert client.patch(f'/api/projects/{pid}', json=data, headers=WRITE).status_code == 409
    assert '25%' in client.get(f'/projects/{pid}').text


def test_bad_zip_does_not_write(client):
    pid = create(client)
    response = client.post(f'/api/projects/{pid}/imports', files={'file': ('bad.zip', b'bad')}, headers=WRITE)
    assert response.status_code == 400
    assert client.get(f'/api/projects/{pid}/documents').json()['total'] == 0


def test_import_atomic_and_thread_idempotency(tmp_path):
    db = Store(tmp_path / 'test.sqlite3', CONFIG['required_categories'])
    p = db.create_project(ProjectInput(name='Synthetic', code='TEST').model_dump())
    analysis = analyze_zip(make_zip({'a.pdf': b'a', 'b.pdf': b'b'}))
    iid = db.stage_import(p['id'], 'synthetic', analysis)['id']
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: db.confirm_import(p['id'], iid), range(4)))
    assert sum(r['added'] for r in results) == 2
    bad = analyze_zip(make_zip({'c.pdf': b'c', 'd.pdf': b'd'}))
    bad['files'][1].pop('size')
    bid = db.stage_import(p['id'], 'synthetic-bad', bad)['id']
    with pytest.raises(KeyError):
        db.confirm_import(p['id'], bid)
    assert db.documents(p['id'])['total'] == 2


def test_large_body_header_rejected(client):
    response = client.post('/api/projects', content='{}', headers={**WRITE, 'Content-Length': str(300 * 1024 * 1024)})
    assert response.status_code == 413


def test_remote_requires_password(tmp_path):
    with TestClient(create_app(tmp_path, password=''), client=('203.0.113.7', 50000)) as c:
        assert c.get('/').status_code == 403
        assert c.get('/', headers={'X-Forwarded-For': '127.0.0.1'}).status_code == 403


def test_malformed_origin(client):
    assert client.post('/api/projects', json={'name':'x','code':'x'},
                       headers={**WRITE, 'Origin':'http://['}).status_code == 403


def test_nonascii_login(tmp_path):
    import base64
    token = base64.b64encode('имя:password'.encode()).decode()
    with TestClient(create_app(tmp_path, password='synthetic-password')) as c:
        assert c.get('/', headers={'Authorization': 'Basic '+token}).status_code == 401


def test_chunked_body_limit(tmp_path, monkeypatch):
    from dataclasses import replace
    import app.main as module
    monkeypatch.setattr(module, 'DEFAULT_LIMITS', replace(module.DEFAULT_LIMITS, max_upload=4))
    with TestClient(module.create_app(tmp_path, password='')) as c:
        chunks = (b' ' * (512 * 1024) for _ in range(3))
        assert c.post('/api/projects', content=chunks, headers={**WRITE,'Content-Type':'application/json'}).status_code == 413


def test_custom_host_requires_password(tmp_path, monkeypatch):
    monkeypatch.setenv('PSC_ALLOWED_HOSTS', 'psc.example.test')
    with pytest.raises(RuntimeError):
        create_app(tmp_path, password='')


def test_template_preserves_folder_placeholders():
    assert len(CONFIG['required_categories']) == 14
    assert 'Согласование изменение объекта' in CONFIG['required_categories']
    assert 'Ход строильства объекта' in CONFIG['required_categories']
