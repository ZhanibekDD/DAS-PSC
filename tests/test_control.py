"""Synthetic-only API and transactional construction-control acceptance."""
import io
import json
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.control import Control, IssueInput, StageInput, StageUpdate
from app.main import CONFIG, ProjectInput, create_app
from app.store import Conflict, Store

WRITE = {"X-PSC-Request": "1"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, password="")) as c:
        yield c


def project(c):
    r = c.post('/api/projects', json={'name': 'Synthetic project', 'code': 'TEST'}, headers=WRITE)
    assert r.status_code == 201, r.text
    return r.json()['id']


def new_stage(c, p, **kw):
    r = c.post(f'/api/projects/{p}/stages', json={'name': 'Сварка', 'responsible': 'Тестовый ответственный', **kw}, headers=WRITE)
    assert r.status_code == 201, r.text
    return r.json()


def new_issue(c, p, **kw):
    r = c.post(f'/api/projects/{p}/prescriptions', json={'title': 'Тестовое замечание', 'responsible': 'Тест', 'due_date': '2026-09-05', **kw}, headers=WRITE)
    assert r.status_code == 201, r.text
    return r.json()


def patch_stage(c, p, row, **kw):
    from app.control import StageInput
    data = {k: row[k] for k in StageInput.model_fields}
    return c.patch(f'/api/projects/{p}/stages/{row["id"]}', json={**data, 'version': row['version'], **kw}, headers=WRITE)


def patch_issue(c, p, row, **kw):
    data = {k: row[k] for k in IssueInput.model_fields}
    data['blocking'] = bool(data['blocking'])
    return c.patch(f'/api/projects/{p}/prescriptions/{row["id"]}', json={**data, 'version': row['version'], **kw}, headers=WRITE)


def document(c, p, reviewed=True, name='Подтверждение.pdf', data=b'synthetic-bytes'):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr(name, data)
    r = c.post(f'/api/projects/{p}/imports', files={'file': ('test.zip', out.getvalue())}, headers=WRITE)
    assert r.status_code == 201, r.text
    iid = r.json()['id']
    assert c.post(f'/api/projects/{p}/imports/{iid}/confirm', headers=WRITE).status_code == 200
    row = next(r for r in c.get(f'/api/projects/{p}/documents').json()['items'] if r['path'] == name)
    if reviewed:
        assert c.patch(f'/api/projects/{p}/documents/{row["id"]}', json={'version': row['version'], 'category': 'Проект'}, headers=WRITE).status_code == 200
    return row['id']


def test_empty_control_has_no_fake_progress(client):
    p = project(client)
    summary = client.get(f'/api/projects/{p}/control').json()
    assert summary['stages'] == summary['open_issues'] == 0
    assert 'percent' not in summary
    html = client.get(f'/projects/{p}/control')
    assert html.status_code == 200
    assert 'Этапов нет' in html.text
    assert 'Ход работ и предписания' in client.get(f'/projects/{p}').text


def test_stage_create_edit_and_stale_write(client):
    p = project(client)
    s = new_stage(client, p, location='ПК 0+00 — ПК 2+00')
    r = patch_stage(client, p, s, status='in_progress', progress=30)
    assert r.status_code == 200, r.text
    assert patch_stage(client, p, s, progress=0).status_code == 409
    assert client.get(f'/api/projects/{p}/stages').json()['items'][0]['progress'] == 30
    assert client.get(f'/api/projects/{p}').json()['progress'] is None


def test_dependency_completion_and_ancestor_reopen(client):
    p = project(client)
    first = new_stage(client, p)
    second = new_stage(client, p, name='Изоляция', predecessor_id=first['id'])
    assert patch_stage(client, p, second, status='in_progress').status_code == 409
    first = patch_stage(client, p, first, status='done', progress=100).json()
    second = patch_stage(client, p, second, status='in_progress').json()
    assert patch_stage(client, p, first, status='in_progress', progress=90).status_code == 409
    assert patch_stage(client, p, second, status='blocked', note='Повторная проверка').status_code == 200
    assert patch_stage(client, p, first, status='in_progress', progress=90).status_code == 200


def test_graph_cycle_and_self_link(client):
    p = project(client)
    a = new_stage(client, p)
    b = new_stage(client, p, predecessor_id=a['id'])
    assert patch_stage(client, p, a, predecessor_id=b['id']).status_code == 409
    assert patch_stage(client, p, a, predecessor_id=a['id']).status_code == 409
    assert client.get(f'/api/projects/{p}/stages').json()['items'][0]['predecessor_id'] is None


@pytest.mark.parametrize('values', [
    {'name':'   '}, {'responsible':''}, {'progress':101}, {'progress':True},
    {'status':'done','progress':90}, {'status':'planned','progress':10},
    {'status':'in_progress','progress':100}, {'status':'blocked','note':''},
    {'start_date':'2026-09-10','due_date':'2026-09-01'}, {'due_date':'bad'},
    {'status':'approved'}, {'predecessor_id':0}, {'extra':'not-allowed'},
])
def test_stage_validation(client, values):
    p=project(client)
    r=client.post(f'/api/projects/{p}/stages',json={'name':'x','responsible':'x',**values},headers=WRITE)
    assert r.status_code == 422, r.text


def test_cross_project_links_rejected(client):
    p, other = project(client), project(client)
    a = new_stage(client, p)
    did = document(client, p)
    assert client.post(f'/api/projects/{other}/stages',json={'name':'x','responsible':'x','predecessor_id':a['id']},headers=WRITE).status_code == 404
    assert client.post(f'/api/projects/{other}/stages',json={'name':'x','responsible':'x','document_id':did},headers=WRITE).status_code == 404
    assert patch_stage(client, other, a).status_code == 404
    for values in [{'stage_id':a['id']}, {'document_id':did}]:
        r=client.post(f'/api/projects/{other}/prescriptions',json={'title':'x','responsible':'x','due_date':'2026-09-05',**values},headers=WRITE)
        assert r.status_code == 404
    i = new_issue(client,p)
    assert patch_issue(client,other,i).status_code == 404


def test_blocking_issue_and_resolution_workflow(client):
    p = project(client)
    s = new_stage(client,p)
    i = new_issue(client,p,stage_id=s['id'],blocking=True)
    assert patch_stage(client,p,s,status='in_progress').status_code == 409
    did = document(client,p)
    assert patch_issue(client,p,i,status='closed',resolution='Исправлено',verified_by='Проверяющий',document_id=did).status_code == 409
    r = patch_issue(client,p,i,status='resolved',resolution='Исправлено',document_id=did)
    assert r.status_code == 200, r.text
    i = r.json()
    assert patch_stage(client,p,s,status='done',progress=100).status_code == 409
    r = patch_issue(client,p,i,status='closed',verified_by='Проверяющий')
    assert r.status_code == 200, r.text
    assert r.json()['closed_at'] is not None
    assert patch_stage(client,p,s,status='done',progress=100).status_code == 200
    assert client.get(f'/api/projects/{p}/control').json()['open_issues'] == 0


def test_nonblocking_issue_does_not_block(client):
    p=project(client)
    s=new_stage(client,p)
    new_issue(client,p,stage_id=s['id'])
    assert patch_stage(client,p,s,status='done',progress=100).status_code == 200


def test_whole_project_block_and_post_completion_defect(client):
    p=project(client)
    s=new_stage(client,p,status='done',progress=100)
    new_issue(client,p,blocking=True)
    rows=client.get(f'/api/projects/{p}/stages').json()['items']
    assert rows[0]['status'] == 'done' and rows[0]['attention']
    assert client.post(f'/api/projects/{p}/stages',json={'name':'x','responsible':'x','status':'in_progress'},headers=WRITE).status_code == 409
    html=client.get(f'/projects/{p}/control').text
    assert 'отдельное противоречие' in html


def test_close_requires_reviewed_document_and_reopen_audit(client):
    p=project(client)
    did=document(client,p,reviewed=False)
    i=new_issue(client,p,status='resolved',resolution='Исправлено',document_id=did)
    assert patch_issue(client,p,i,status='closed',verified_by='Инженер').status_code == 400
    assert client.patch(f'/api/projects/{p}/documents/{did}',json={'version':1,'category':'Проект'},headers=WRITE).status_code == 200
    closed=patch_issue(client,p,i,status='closed',verified_by='Инженер').json()
    assert patch_issue(client,p,closed,status='in_progress',verified_by='').status_code == 409
    r=patch_issue(client,p,closed,status='open',verified_by='')
    assert r.status_code == 200
    assert r.json()['closed_at'] is None
    with client.app.state.store.connect() as db:
        snapshots=db.execute('SELECT before_json,after_json FROM control_changes WHERE entity=? ORDER BY id',('prescriptions',)).fetchall()
    assert json.loads(snapshots[-1]['before_json'])['verified_by'] == 'Инженер'
    assert json.loads(snapshots[-1]['after_json'])['status'] == 'open'


@pytest.mark.parametrize('values', [
    {'title':' '}, {'due_date':''}, {'due_date':'2026-02-30'}, {'responsible':' '},
    {'status':'resolved'}, {'status':'closed','resolution':'fixed'},
    {'verified_by':'not closed'}, {'blocking':'true'}, {'status':'deleted'},
])
def test_issue_validation(client, values):
    p=project(client)
    r=client.post(f'/api/projects/{p}/prescriptions',json={'title':'x','responsible':'x','due_date':'2026-09-05',**values},headers=WRITE)
    assert r.status_code == 422, r.text


def test_empty_document_cannot_be_evidence(client):
    p=project(client)
    did=document(client,p,data=b'')
    assert client.post(f'/api/projects/{p}/stages',json={'name':'x','responsible':'x','document_id':did},headers=WRITE).status_code == 400


def test_overdue_filters_and_today_boundary(client, monkeypatch):
    p=project(client)
    monkeypatch.setattr(client.app.state.control,'today',lambda:'2026-09-05')
    new_issue(client,p,title='Вчера',due_date='2026-09-04')
    new_issue(client,p,title='Сегодня',due_date='2026-09-05')
    new_issue(client,p,title='На проверке',due_date='2026-09-03',status='resolved',resolution='Исправлено')
    r=client.get(f'/api/projects/{p}/prescriptions',params={'overdue':'true'}).json()
    assert r['total'] == 2
    assert client.get(f'/api/projects/{p}/control').json()['overdue_issues'] == 2
    assert client.get(f'/api/projects/{p}/prescriptions',params={'status':'resolved'}).json()['total'] == 1
    assert client.get(f'/api/projects/{p}/prescriptions',params={'status':'bogus'}).status_code == 400
    assert client.get(f'/api/projects/{p}/prescriptions',params={'page':0}).status_code == 422


def test_security_covers_new_endpoints(client):
    p=project(client)
    url=f'/api/projects/{p}/stages'
    assert client.post(url,json={'name':'x','responsible':'x'}).status_code == 403
    assert client.post(url,json={'name':'x','responsible':'x'},headers={**WRITE,'Origin':'https://evil.example'}).status_code == 403


def test_xss_in_control_html(client):
    p=project(client)
    new_stage(client,p,name='<script>alert(1)</script>')
    new_issue(client,p,description='<img src=x onerror=alert(1)>')
    html=client.get(f'/projects/{p}/control').text
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html
    assert '<img src=x' not in html


def test_additive_migration_preserves_registry_and_restart(tmp_path):
    store=Store(tmp_path/'psc.sqlite3',CONFIG['required_categories'])
    p=store.create_project(ProjectInput(name='Old project',code='OLD').model_dump())['id']
    with TestClient(create_app(tmp_path,password='')) as c:
        s=new_stage(c,p)
        new_issue(c,p,stage_id=s['id'])
        did=document(c,p)
    with TestClient(create_app(tmp_path,password='')) as c:
        assert c.get(f'/api/projects/{p}/control').json()['stages'] == 1
        assert c.get(f'/api/projects/{p}/prescriptions').json()['total'] == 1
        assert c.get(f'/api/projects/{p}/documents').json()['items'][0]['id'] == did
        assert c.get(f'/api/projects/{p}').json()['name'] == 'Old project'
        with c.app.state.store.connect() as db:
            assert db.execute('PRAGMA foreign_key_check').fetchall() == []
            assert db.execute('PRAGMA user_version').fetchone()[0] == 1


def test_foreign_keys_enforce_scope_in_database(client):
    p,other=project(client),project(client)
    a=new_stage(client,p)
    b=new_stage(client,other)
    with client.app.state.store.connect(True) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('UPDATE work_stages SET predecessor_id=? WHERE id=?',(a['id'],b['id']))


def test_parallel_stale_stage_update_only_one_wins(client):
    p=project(client)
    s=new_stage(client,p)
    service=client.app.state.control
    def update(_):
        try:
            service.save_stage(p,StageUpdate(name='Updated',responsible='Test',version=1),s['id'])
            return True
        except Conflict:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(update,range(4))) == 1


def test_unknown_ids_never_create_via_patch(client):
    p=project(client)
    s=new_stage(client,p)
    i=new_issue(client,p)
    for missing in [0,-1,999999]:
        assert patch_stage(client,p,{**s,'id':missing}).status_code == 404
        assert patch_issue(client,p,{**i,'id':missing}).status_code == 404
    assert client.get(f'/api/projects/{p}/control').json()['stages'] == 1


def test_control_version_refuses_future_schema(client):
    service=client.app.state.control
    with service.store.connect(True) as db:
        db.execute('UPDATE control_meta SET version=99')
    with pytest.raises(RuntimeError):
        service.initialize()


def test_stage_issue_caps(client,monkeypatch):
    import app.control as module
    p=project(client)
    monkeypatch.setattr(module,'MAX_STAGES',1)
    monkeypatch.setattr(module,'MAX_ISSUES',1)
    s=new_stage(client,p)
    new_issue(client,p)
    assert client.post(f'/api/projects/{p}/stages',json={'name':'x','responsible':'x'},headers=WRITE).status_code == 400
    assert client.post(f'/api/projects/{p}/prescriptions',json={'title':'x','responsible':'x','due_date':'2026-09-05'},headers=WRITE).status_code == 400
    assert patch_stage(client,p,s,name='Edited').status_code == 200


def test_closed_record_requires_reopen_before_edit(client):
    p=project(client)
    did=document(client,p)
    i=new_issue(client,p,status='resolved',resolution='Fixed',document_id=did)
    closed=patch_issue(client,p,i,status='closed',verified_by='Reviewer').json()
    assert patch_issue(client,p,closed,title='Rewritten after closure').status_code == 409
    assert patch_issue(client,p,closed).status_code == 200


def test_closed_issue_is_not_overdue(client,monkeypatch):
    p=project(client)
    did=document(client,p)
    monkeypatch.setattr(client.app.state.control,'today',lambda:'2026-09-05')
    i=new_issue(client,p,status='resolved',resolution='Fixed',document_id=did,due_date='2026-09-01')
    assert patch_issue(client,p,i,status='closed',verified_by='Reviewer').status_code == 200
    assert client.get(f'/api/projects/{p}/prescriptions',params={'overdue':'true'}).json()['total'] == 0


def test_configured_timezone_and_protected_page(tmp_path,monkeypatch):
    monkeypatch.setenv('PSC_TIMEZONE','Asia/Yekaterinburg')
    with TestClient(create_app(tmp_path,password='synthetic-password')) as c:
        assert c.get('/projects/missing/control').status_code == 401
        c.auth=('psc','synthetic-password')
        p=project(c)
        assert c.get(f'/api/projects/{p}/control').json()['timezone'] == 'Asia/Yekaterinburg'


def test_issue_pagination_and_optimistic_lock(client):
    p=project(client)
    i=new_issue(client,p)
    assert patch_issue(client,p,i,title='Updated').status_code == 200
    assert patch_issue(client,p,i,title='Stale').status_code == 409
    for n in range(50):
        new_issue(client,p,title=f'Synthetic {n}')
    r=client.get(f'/api/projects/{p}/prescriptions',params={'page':2}).json()
    assert r['total'] == 51 and len(r['items']) == 1
