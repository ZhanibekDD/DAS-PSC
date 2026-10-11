from fastapi.testclient import TestClient

from app.auth import check_password, hash_password
from app.main import create_app
from app.security import client_address, trusted_proxies

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
        for _ in range(50):  # refused requests skip the hash: they must not each write to the journal
            login(c, password='wrong-password-1')
            c.get('/api/projects', auth=('psc', 'wrong-password-1'))
        events = [e['event'] for e in c.app.state.auth.events(1000)]
        assert events.count('login_failed') == 5 and events.count('login_blocked') == 1


def test_block_is_journaled_once_per_address_and_window():
    from app.auth import Throttle
    clock = [1000.0]
    throttle = Throttle(limit=5, window=900, clock=lambda: clock[0])
    assert throttle.first_block('addr:x') and not throttle.first_block('addr:x')
    assert throttle.first_block('addr:y')
    clock[0] += 901
    assert throttle.first_block('addr:x')
    assert set(throttle.block_logged) == {'addr:x'}  # expired entries are dropped, memory stays bounded


def test_keys_of_logins_never_tried_again_expire():
    from app.auth import Throttle
    clock = [1000.0]
    throttle = Throttle(limit=5, window=900, clock=lambda: clock[0])
    for n in range(40):  # one-off usernames from rotating addresses
        assert throttle.attempt(f'addr:10.0.0.{n}', f'user:guess{n}') is not None
    assert len(throttle.failures) == 80
    clock[0] += 901
    throttle.attempt('addr:203.0.113.9', 'user:psc')
    assert set(throttle.failures) == {'addr:203.0.113.9', 'user:psc'}


def test_cookie_is_secure_behind_the_https_proxy(tmp_path):
    with client(tmp_path, client=('127.0.0.1', 50000)) as c:  # the configured (loopback) proxy
        assert 'secure' in login(c, headers={'X-Forwarded-Proto': 'https'}).headers['set-cookie'].lower()
    with client(tmp_path, client=('203.0.113.7', 50000)) as c:  # not a proxy: the header is ignored
        assert 'secure' not in login(c, headers={'X-Forwarded-Proto': 'https'}).headers['set-cookie'].lower()


def test_password_change_between_check_and_session_wins(tmp_path):
    with client(tmp_path) as c:
        auth = c.app.state.auth
        verify = auth._verify

        def racing(username, password, address):
            row = verify(username, password, address)
            auth.reset_password(row['id'], 'Replaced-pass-99', 'admin')  # commits before the session insert
            return row
        auth._verify = racing
        r = login(c)
        assert r.status_code == 401 and 'Войдите заново' in r.json()['detail']
        auth._verify = verify
        with auth.store.connect() as db:
            assert db.execute('select count(*) from sessions').fetchone()[0] == 0
        assert c.get('/api/projects').status_code == 401


def test_current_password_guesses_are_limited(tmp_path):
    with client(tmp_path) as c:
        assert login(c).status_code == 200
        change = lambda current: c.post('/api/account/password', json={'current': current, 'new': 'Chosen-by-admin7', 'repeat': 'Chosen-by-admin7'}, headers=H)
        for _ in range(5):
            r = change('wrong-password-1')
            assert r.status_code == 400 and 'неверно' in r.json()['detail']
        r = change(PW)
        assert r.status_code == 400 and 'Подождите 15 минут' in r.json()['detail']


def test_logins_are_latin_so_case_folding_is_exact(tmp_path):
    with client(tmp_path) as c:
        login(c)
        for name in ('Иван', 'иван', 'pétrov'):
            r = c.post('/api/users', json={'username': name, 'password': 'Ivan-pass-123'}, headers=H)
            assert r.status_code == 400 and 'латинские' in r.json()['detail'], name
        ok = c.post('/api/users', json={'username': 'i.petrov@dnepr', 'display_name': 'Иван Петров', 'password': 'Ivan-pass-123'}, headers=H)
        assert ok.status_code == 201 and ok.json()['display_name'] == 'Иван Петров'
        assert c.post('/api/users', json={'username': 'I.Petrov@Dnepr', 'password': 'Ivan-pass-123'}, headers=H).status_code == 409


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


def test_enabling_the_password_after_local_accounts_still_gives_an_admin(tmp_path):
    with client(tmp_path, password='') as c:  # local mode: accounts default to the "user" role
        assert c.post('/api/users', json={'username': 'ivan', 'password': 'Ivan-pass-123'}, headers=H).status_code == 201
    with client(tmp_path) as c:
        assert login(c).status_code == 200
        assert c.get('/users', headers=HTML).status_code == 200
        assert login(c, user='ivan', password='Ivan-pass-123').status_code == 200


def test_login_journal_is_visible_to_the_admin(tmp_path):
    with client(tmp_path) as c:
        login(c, password='wrong-password-1')
        login(c)
        page = c.get('/users', headers=HTML).text
        assert 'неверный пароль' in page and 'вход' in page and 'первый администратор' in page


def test_control_page_keeps_the_menu_and_account_links(tmp_path):
    with client(tmp_path) as c:
        login(c)
        pid = c.post('/api/projects', json={'name': 'КС Ноябрьск', 'code': 'KS-1'}, headers=H).json()['id']
        page = c.get(f'/projects/{pid}/control', headers=HTML)
        assert page.status_code == 200
        for text in ('Объекты', 'Администратор ПСК', 'Пользователи', 'Выйти', 'v0.'):
            assert text in page.text, text


def test_password_hashes_and_proxy_address():
    encoded = hash_password(PW, iterations=1000)
    assert encoded.startswith('pbkdf2_sha256$1000$') and PW not in encoded
    assert check_password(PW, encoded) and not check_password('other', encoded) and not check_password(PW, 'garbage')
    headers = {b'x-real-ip': b'203.0.113.9'}
    proxy = trusted_proxies('172.18.0.1/32')
    assert client_address({'client': ('172.18.0.1', 1)}, headers, proxy) == '203.0.113.9'
    assert client_address({'client': ('93.184.216.34', 1)}, {b'x-real-ip': b'10.0.0.1'}, proxy) == '93.184.216.34'
    assert client_address({'client': ('172.18.0.1', 1)}, {b'x-real-ip': b'not-an-ip'}, proxy) == '172.18.0.1'
    # Not a configured proxy: another private peer cannot choose its address.
    assert client_address({'client': ('172.18.0.5', 1)}, headers, proxy) == '172.18.0.5'
    assert client_address({'client': ('172.18.0.1', 1)}, headers, trusted_proxies('')) == '172.18.0.1'
    loopback = trusted_proxies('127.0.0.1/32,::1/128')
    assert client_address({'client': ('127.0.0.1', 1)}, headers, loopback) == '203.0.113.9'


def test_gateway_keyword_trusts_only_the_bridge_gateway(tmp_path):
    route = tmp_path / 'route'
    route.write_text(
        'Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n'
        'eth0\t00000000\t010019AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n'
        'eth0\t000019AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n')
    headers = {b'x-real-ip': b'203.0.113.9'}
    proxy = trusted_proxies('gateway', route_file=str(route))
    assert [str(n) for n in proxy] == ['172.25.0.1/32']
    assert client_address({'client': ('172.25.0.1', 1)}, headers, proxy) == '203.0.113.9'
    assert client_address({'client': ('172.25.0.7', 1)}, headers, proxy) == '172.25.0.7'
    assert client_address({'client': ('127.0.0.1', 1)}, headers, proxy) == '127.0.0.1'
    # No routing table (not Linux, or unreadable): nothing is trusted rather than everything.
    assert trusted_proxies('gateway', route_file=str(tmp_path / 'missing')) == ()


def test_parallel_attempts_cannot_exceed_the_limit():
    import threading
    from app.auth import Throttle
    throttle = Throttle(limit=5, window=900)
    barrier = threading.Barrier(20)
    results = []

    def go():
        barrier.wait()
        results.append(throttle.attempt('addr:203.0.113.9', 'user:psc'))

    threads = [threading.Thread(target=go) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(r is not None for r in results) == 5


def test_own_login_does_not_reset_failures_of_the_address():
    from app.auth import Throttle
    clock = [1000.0]
    throttle = Throttle(limit=5, window=900, clock=lambda: clock[0])
    for name in ('a', 'b', 'c', 'd'):  # four wrong passwords for other logins
        clock[0] += 1
        assert throttle.attempt('addr:x', 'user:' + name) is not None
    clock[0] += 1
    stamp = throttle.attempt('addr:x', 'user:me')  # own valid account
    throttle.succeeded(stamp, 'addr:x', 'user:me')
    clock[0] += 1
    assert throttle.attempt('addr:x', 'user:e') is not None  # fifth failure from the address
    clock[0] += 1
    assert throttle.attempt('addr:x', 'user:f') is None  # blocked: the success did not wipe the count
    assert throttle.attempt('addr:y', 'user:me') is not None  # the login's own count was reset

