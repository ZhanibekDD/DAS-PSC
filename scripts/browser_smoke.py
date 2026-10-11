"""Live synthetic-only browser acceptance. Optional: pip install playwright==1.57.0."""
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

from playwright.sync_api import sync_playwright, expect


def main():
    with tempfile.TemporaryDirectory() as data:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        env = {**os.environ, 'PSC_DATA_DIR': data, 'PSC_PASSWORD': '', 'PSC_ALLOWED_HOSTS': 'localhost,127.0.0.1', 'PSC_TIMEZONE': 'UTC'}
        server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                try:
                    with urllib.request.urlopen(base+'/health', timeout=1) as r:
                        if r.status == 200:
                            break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError('Test server failed to start')
            with sync_playwright() as pw:
                options = {'headless': True, 'args': ['--no-sandbox']}
                if os.environ.get('PSC_BROWSER_EXECUTABLE'):
                    options['executable_path'] = os.environ['PSC_BROWSER_EXECUTABLE']
                browser = pw.chromium.launch(**options)
                for width, height in [(1440, 1000), (390, 844)]:
                    page = browser.new_page(viewport={'width': width, 'height': height})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(base)
                    create = page.locator('#create form')
                    create.locator('[name=name]').fill(f'Тестовый объект {width}')
                    create.locator('[name=code]').fill(f'TEST-{width}')
                    create.locator('button').click()
                    page.wait_for_url('**/projects/*')
                    # Import through the actual browser: preview -> confirm -> review category.
                    raw = io.BytesIO()
                    with zipfile.ZipFile(raw, 'w') as z:
                        z.writestr('Проект/TEST-001-ГЧ-001.pdf', b'Synthetic proof, not a real PDF')
                    page.locator('input[type=file]').set_input_files({'name':'synthetic.zip','mimeType':'application/zip','buffer':raw.getvalue()})
                    page.get_by_role('button', name='Анализировать архив', exact=True).click()
                    page.wait_for_url('**/imports/*')
                    page.get_by_role('button', name='Подтвердить и сохранить реестр', exact=True).click()
                    page.wait_for_url('**/documents')
                    review = page.locator('form[data-method=PATCH]').first
                    did = review.get_attribute('data-request').split('/')[-1]
                    review.get_by_role('button', name='Подтвердить категорию', exact=True).click()
                    expect(page.get_by_text('Категория проверена', exact=True)).to_be_visible()
                    page.get_by_role('link', name='Ход работ и предписания', exact=True).click()
                    page.wait_for_url('**/control')
                    page.locator('#new-stage > details > summary').click()
                    form = page.locator('#new-stage form')
                    form.locator('[name=name]').fill('Сварка в линию')
                    form.locator('[name=location]').fill('ПК 0+00 — ПК 2+00')
                    form.locator('[name=responsible]').fill('Прораб · тест')
                    form.get_by_role('button', name='Добавить этап', exact=True).click()
                    expect(page.locator('article[id^=stage-]')).to_have_count(1)
                    # Create a blocking prescription, resolve and manually confirm it.
                    page.locator('#new-issue > details > summary').click()
                    form = page.locator('#new-issue form')
                    form.locator('[name=title]').fill('Проверка стыка · тест')
                    form.locator('[name=responsible]').fill('Инженер · тест')
                    form.locator('[name=due_date]').fill('2026-09-10')
                    form.locator('[name=blocking]').check()
                    form.get_by_role('button', name='Добавить предписание', exact=True).click()
                    expect(page.locator('article[id^=issue-]')).to_have_count(1)
                    issue = page.locator('article[id^=issue-]').first
                    issue.locator('> details > summary').click()
                    form = issue.locator('> details form')
                    form.locator('.inline-advanced > summary').click()
                    form.locator('[name=resolution]').fill('Замечание устранено · тест')
                    form.locator('[name=document_id]').fill(did)
                    form.locator('[name=status]').select_option('resolved')
                    form.get_by_role('button', name='Сохранить предписание', exact=True).click()
                    expect(page.locator('article[id^=issue-] .badge')).to_have_text('На проверке')
                    issue = page.locator('article[id^=issue-]').first
                    issue.locator('> details > summary').click()
                    form = issue.locator('> details form')
                    form.locator('.inline-advanced > summary').click()
                    form.locator('[name=status]').select_option('closed')
                    form.locator('[name=verified_by]').fill('Проверяющий · тест')
                    form.get_by_role('button', name='Сохранить предписание', exact=True).click()
                    expect(page.locator('article[id^=issue-] .badge')).to_have_text('Закрыто')
                    stage = page.locator('article[id^=stage-]').first
                    stage.locator('> details > summary').click()
                    form = stage.locator('> details form')
                    form.locator('[name=status]').select_option('done')
                    expect(form.locator('[name=progress]')).to_have_value('100')
                    form.get_by_role('button', name='Сохранить этап', exact=True).click()
                    expect(page.locator('article[id^=stage-] .badge')).to_have_text('Завершен · 100% вручную')
                    page.reload()
                    expect(page.locator('article[id^=stage-] .badge')).to_have_text('Завершен · 100% вручную')
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Horizontal overflow'
                    assert not errors, errors
                    screenshot_dir = os.environ.get('PSC_SCREENSHOTS')
                    if screenshot_dir:
                        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(Path(screenshot_dir)/f'psc-control-{width}.png'), full_page=True)
                    page.close()
                    print(f'Browser {width}x{height}: PASS (create/import/review/stage/prescription/close/reload)')
                browser.close()
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == '__main__':
    main()
