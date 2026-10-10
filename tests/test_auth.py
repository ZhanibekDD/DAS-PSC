from fastapi.testclient import TestClient

from app.auth import check_password, hash_password
from app.main import create_app
from app.security import client_address

H = {'X-PSC-Request': '1'}
HTML = {'Accept': 'text/html'}
PW = 'synthetic-admin-pass1'


def client(tmp_path, password=PW, **kw):
    return TestClient(create_app(tmp_path, password=password), **kw)


def login(c, user='psc', password=PW, next='/', headers=None):
    return c.post('/api/login', json={'username': user, 'password': password, 'next': next}, headers={**H, **(headers or {})})


def test_browser_goes_to_login_page_and_scripts_get_401_without_popup(tmp_path):
    with client(tmp_path) as c:
        r = c.get('/projects/abc?x=1', headers=HTML, follow_redirects=False)
        assert r.status_code == 303 and r.headers['location'] == '/login?next=%2Fprojects%2Fabc%3Fx%3D1'
        r = c.get('/api/projects')
        assert r.status_code == 401 and 'www-authenticate' not in r.headers
        page = c.get('/login', headers=HTML)
        assert page.status_code == 200 and 'Вход в ПСК' in page.text and 'Выйти' not in page.text
        assert c.get('/health').status_code == 200 and c.get('/static/app.js').status_code == 200


def test_login_session_and_logout(tmp_path):
    with client(tmp_path) as c:
        r = login(c, next='/projects/x')
        assert r.status_code == 200 and r.json()['url'] == '/projects/x'
        cookie = r.headers['set-cookie'].lower()
        assert 'httponly' in cookie and 'samesite=lax' in cookie and 'secure' not in cookie
        page = c.get('/', headers=HTML)
        assert page.status_code == 200 and 'Администратор ПСК' in page.text and 'Пользователи' in page.text
        assert c.post('/api/logout', headers=H).json()['url'] == '/login'
        assert c.get('/api/projects').status_code == 401


def test_next_never_leaves_the_site(tmp_path):
    with client(tmp_path) as c:
        for target in ('//evil.example/x', 'https://evil.example/', '/\\evil.example'):
            assert login(c, next=target).json()['url'] == '/'


def test_five_wrong_passwords_pause_the_login(tmp_path):
    with client(tmp_path) as c:
        for _ in range(5):
            r = login(c, password='wrong-password-1')
            assert r.status_code == 401 and r.json()['detail'] == 'Неверный логин или пароль'
        r = login(c)
        assert r.status_code == 401 and 'Подождите 15 минут' in r.json()['detail']
        assert c.get('/api/projects', auth=('psc', PW)).status_code == 401


def test_cookie_is_secure_behind_the_https_proxy(tmp_path):
    with client(tmp_path) as c:
        assert 'secure' in login(c, headers={'X-Forwarded-Proto': 'https'}).headers['set-cookie'].lower()


def test_new_user_must_replace_the_temporary_password(tmp_path):
    with client(tmp_path) as admin, client(tmp_path) as ivan:
        login(admin)
        r = admin.post('/api/users', json={'username': 'ivan', 'display_name': 'Иван Петров', 'role': 'user',
                                           'password': 'Temp-pass-123'}, headers=H)
        assert r.status_code == 201 and r.json()['must_change'] is True
        assert login(ivan, 'ivan', 'Temp-pass-123').json()['url'] == '/account'
        assert ivan.get('/', headers=HTML, follow_redirects=False).headers['location'] == '/account'
        assert ivan.get('/api/projects').status_code == 403
        assert 'Задайте свой пароль' in ivan.get('/account', headers=HTML).text
        bad = ivan.post('/api/account/password', json={'current': 'Temp-pass-123', 'new': 'Ivan-own-9x', 'repeat': 'другой'}, headers=H)
        assert bad.status_code == 422
        r = ivan.post('/api/account/password', json={'current': 'Temp-pass-123', 'new': 'Ivan-own-pass9', 'repeat': 'Ivan-own-pass9'}, headers=H)
        assert r.status_code == 200 and r.json()['url'] == '/login'
        assert ivan.get('/api/projects').status_code == 401  # every session ends after a password change
        assert login(ivan, 'ivan', 'Ivan-own-pass9').json()['url'] == '/'
        assert ivan.get('/api/projects').status_code == 200
        assert ivan.get('/users', headers=HTML).status_code == 403
        assert ivan.post('/api/users', json={'username': 'x', 'password': 'Another-pass1'}, headers=H).status_code == 403


def test_weak_or_duplicate_accounts_are_refused(tmp_path):
    with client(tmp_path) as c:
        login(c)
        for password in ('1234567890', 'aaaaaaaaaa', 'short1'):
            r = c.post('/api/users', json={'username': 'u1', 'password': password}, headers=H)
            assert r.status_code == 400, password
        assert c.post('/api/users', json={'username': 'u 1', 'password': 'Good-pass-123'}, headers=H).status_code == 400
        assert c.post('/api/users', json={'username': 'PSC', 'password': 'Good-pass-123'}, headers=H).status_code == 409


def test_disabled_user_loses_access_and_the_last_admin_stays(tmp_path):
    with client(tmp_path) as admin, client(tmp_path) as ivan:
        login(admin)
        uid = admin.post('/api/users', json={'username': 'ivan', 'password': 'Temp-pass-123'}, headers=H).json()['id']
        login(ivan, 'ivan', 'Temp-pass-123')
        ivan.post('/api/account/password', json={'current': 'Temp-pass-123', 'new': 'Ivan-own-pass9', 'repeat': 'Ivan-own-pass9'}, headers=H)
        login(ivan, 'ivan', 'Ivan-own-pass9')
        assert admin.post(f'/api/users/{uid}/active', json={'active': 'false'}, headers=H).status_code == 200
        assert ivan.get('/api/projects').status_code == 401
        assert login(ivan, 'ivan', 'Ivan-own-pass9').status_code == 401
        me = [u for u in admin.get('/api/users').json() if u['username'] == 'psc'][0]
        assert admin.post(f"/api/users/{me['id']}/active", json={'active': False}, headers=H).status_code == 409


def test_server_password_governs_psc_until_changed_in_the_interface(tmp_path):
    with client(tmp_path, password='first-env-pass1') as c:
        assert login(c, password='first-env-pass1').status_code == 200
    with client(tmp_path, password='second-env-pass2') as c:
        assert login(c, password='first-env-pass1').status_code == 401
        assert login(c, password='second-env-pass2').status_code == 200
        c.post('/api/account/password', json={'current': 'second-env-pass2', 'new': 'Chosen-by-admin7', 'repeat': 'Chosen-by-admin7'}, headers=H)
    with client(tmp_path, password='third-env-pass3') as c:
        assert login(c, password='third-env-pass3').status_code == 401
        assert login(c, password='Chosen-by-admin7').status_code == 200


def test_scripts_may_send_the_password_in_the_header(tmp_path):
    with client(tmp_path) as c:
        assert c.get('/api/projects', auth=('psc', PW)).status_code == 200
        r = c.get('/api/projects', auth=('psc', 'wrong-password-1'))
        assert r.status_code == 401 and 'www-authenticate' not in r.headers


def test_local_mode_without_password_is_unchanged(tmp_path):
    with client(tmp_path, password='') as c:
        assert c.get('/', headers=HTML).status_code == 200
    with client(tmp_path, password='', client=('203.0.113.7', 50000)) as c:
        assert c.get('/').status_code == 403
        assert c.get('/health').status_code == 200


def test_login_journal_is_visible_to_the_admin(tmp_path):
    with client(tmp_path) as c:
        login(c, password='wrong-password-1')
        login(c)
        page = c.get('/users', headers=HTML).text
        assert 'неверный пароль' in page and 'вход' in page and 'первый администратор' in page


def test_password_hashes_and_proxy_address():
    encoded = hash_password(PW, iterations=1000)
    assert encoded.startswith('pbkdf2_sha256$1000$') and PW not in encoded
    assert check_password(PW, encoded) and not check_password('other', encoded) and not check_password(PW, 'garbage')
    headers = {b'x-real-ip': b'203.0.113.9'}
    assert client_address({'client': ('172.18.0.1', 1)}, headers) == '203.0.113.9'
    assert client_address({'client': ('93.184.216.34', 1)}, {b'x-real-ip': b'10.0.0.1'}) == '93.184.216.34'
    assert client_address({'client': ('172.18.0.1', 1)}, {b'x-real-ip': b'not-an-ip'}) == '172.18.0.1'
