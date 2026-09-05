'use strict';

const notice = document.getElementById('notice');
const sleep = ms => new Promise(resolve => window.setTimeout(resolve, ms));

function showToast(text) {
  if (!notice || !text) return;
  notice.textContent = text;
  notice.hidden = false;
  notice.classList.add('psc-toast');
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    notice.hidden = true;
    notice.classList.remove('psc-toast');
  }, 6500);
}

try {
  const stored = sessionStorage.getItem('psc-success');
  if (stored) {
    const value = JSON.parse(stored);
    sessionStorage.removeItem('psc-success');
    if (value && value.text && Date.now() - Number(value.time || 0) < 30000) showToast(value.text);
  }
} catch (_) {
  // sessionStorage can be unavailable in locked-down browsers; UI still works without it.
}

let overlay;
let overlayTitle;
let overlayText;
let overlayPhase;
let overlayElapsed;
let overlayProgress;
let overlaySpinner;
let overlayCheck;
let overlayClose;
let elapsedTimer;
let startedAt = 0;

function ensureOverlay() {
  if (overlay) return;
  overlay = document.createElement('div');
  overlay.className = 'psc-operation-overlay';
  overlay.hidden = true;
  overlay.innerHTML = `
    <section class="psc-operation-card" role="dialog" aria-modal="true" aria-live="polite" aria-labelledby="psc-operation-title">
      <div class="psc-operation-icon"><span class="psc-operation-spinner" aria-hidden="true"></span><span class="psc-operation-check" aria-hidden="true" hidden>✓</span></div>
      <h2 id="psc-operation-title"></h2>
      <p class="psc-operation-text"></p>
      <progress class="psc-operation-progress" max="100"></progress>
      <div class="psc-operation-meta"><span class="psc-operation-phase"></span><span class="psc-operation-elapsed"></span></div>
      <button type="button" class="psc-operation-close" hidden>Закрыть</button>
    </section>`;
  document.body.appendChild(overlay);
  overlayTitle = overlay.querySelector('h2');
  overlayText = overlay.querySelector('.psc-operation-text');
  overlayPhase = overlay.querySelector('.psc-operation-phase');
  overlayElapsed = overlay.querySelector('.psc-operation-elapsed');
  overlayProgress = overlay.querySelector('progress');
  overlaySpinner = overlay.querySelector('.psc-operation-spinner');
  overlayCheck = overlay.querySelector('.psc-operation-check');
  overlayClose = overlay.querySelector('.psc-operation-close');
  overlayClose.addEventListener('click', () => {
    overlay.hidden = true;
    document.body.classList.remove('psc-busy');
  });
}

function startElapsed() {
  window.clearInterval(elapsedTimer);
  startedAt = Date.now();
  const tick = () => {
    const seconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    overlayElapsed.textContent = `${seconds} сек`;
  };
  tick();
  elapsedTimer = window.setInterval(tick, 1000);
}

function showOverlay(title, text, phase, progress = null) {
  ensureOverlay();
  overlay.hidden = false;
  overlay.classList.remove('is-success', 'is-error');
  document.body.classList.add('psc-busy');
  overlaySpinner.hidden = false;
  overlayCheck.hidden = true;
  overlayClose.hidden = true;
  overlayTitle.textContent = title;
  overlayText.textContent = text;
  overlayPhase.textContent = phase;
  if (progress === null) overlayProgress.removeAttribute('value');
  else overlayProgress.value = progress;
  startElapsed();
}

function updateOverlay(title, text, phase, progress = null) {
  if (!overlay || overlay.hidden) return;
  overlayTitle.textContent = title;
  overlayText.textContent = text;
  overlayPhase.textContent = phase;
  if (progress === null) overlayProgress.removeAttribute('value');
  else overlayProgress.value = Math.max(0, Math.min(100, progress));
}

function successOverlay(title, text) {
  if (!overlay || overlay.hidden) return;
  window.clearInterval(elapsedTimer);
  overlay.classList.remove('is-error');
  overlay.classList.add('is-success');
  overlaySpinner.hidden = true;
  overlayCheck.hidden = false;
  overlayTitle.textContent = title;
  overlayText.textContent = text;
  overlayPhase.textContent = 'Готово';
  overlayProgress.value = 100;
  overlayElapsed.textContent = '✓';
}

function errorOverlay(text) {
  if (!overlay || overlay.hidden) return;
  window.clearInterval(elapsedTimer);
  overlay.classList.remove('is-success');
  overlay.classList.add('is-error');
  overlaySpinner.hidden = true;
  overlayCheck.hidden = false;
  overlayCheck.textContent = '!';
  overlayTitle.textContent = 'Операция не завершена';
  overlayText.textContent = text;
  overlayPhase.textContent = 'Проверьте сообщение и повторите';
  overlayElapsed.textContent = '';
  overlayProgress.removeAttribute('value');
  overlayClose.hidden = false;
}

function operation(form) {
  const request = form.dataset.request || '';
  if (form.dataset.upload) return {
    major: true,
    busyTitle: 'Загружаем архив',
    busyText: 'Передаем ZIP в ПСК. Не закрывайте страницу.',
    phase: 'Загрузка файла',
    processingTitle: 'Архив загружен',
    processingText: 'Проверяем структуру и хэши, затем читаем PDF/DOCX. Это может занять некоторое время.',
    processingPhase: 'Анализ документов',
    successTitle: 'Анализ архива завершён',
    successText: 'Все проверки завершены. Открываю результаты анализа…',
    toast: '✓ Анализ архива завершён — результаты готовы к просмотру.'
  };
  if (/\/imports\/[^/]+\/confirm$/.test(request)) return {
    major: true,
    busyTitle: 'Сохраняем результаты анализа',
    busyText: 'Фиксируем реестр и производные результаты. Исходные файлы не изменяются.',
    phase: 'Сохранение реестра',
    successTitle: 'Реестр сохранён',
    successText: 'Результаты зафиксированы. Открываю реестр документов…',
    toast: '✓ Реестр и результаты анализа сохранены.'
  };
  if (/\/imports\/[^/]+\/refresh$/.test(request)) return {
    major: true,
    busyTitle: 'Применяем анализ содержания',
    busyText: 'Обновляем только разрешённые машинные предложения. Подтверждённые человеком записи защищены.',
    phase: 'Обновление предложений',
    successTitle: 'Анализ содержания применён',
    successText: 'Реестр обновлён. Открываю очередь проверки…',
    toast: '✓ Анализ содержания применён к реестру.'
  };
  if (/\/documents\/reclassify$/.test(request)) return {
    major: true,
    busyTitle: 'Пересчитываем предложения',
    busyText: 'Проверяем текущие машинные правила и защищаем вручную подтверждённые записи.',
    phase: 'Пересчёт классификации',
    successTitle: 'Предложения пересчитаны',
    successText: 'Новые машинные предложения сохранены.',
    toast: '✓ Предложения классификации пересчитаны.'
  };
  if (/\/documents\/bulk-review$/.test(request)) return {
    major: true,
    busyTitle: 'Подтверждаем уверенные предложения',
    busyText: 'Подтверждаются только записи, подходящие под безопасный порог.',
    phase: 'Массовое подтверждение',
    successTitle: 'Подтверждение завершено',
    successText: 'Открываю оставшуюся очередь проверки…',
    toast: '✓ Уверенные предложения подтверждены.'
  };
  return {major: false, toast: '✓ Изменение сохранено.'};
}

function xhrUpload(url, method, headers, body, op, file) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(method, url, true);
    for (const [key, value] of Object.entries(headers)) xhr.setRequestHeader(key, value);
    xhr.upload.addEventListener('progress', event => {
      if (!event.lengthComputable) return;
      const percent = Math.round((event.loaded / event.total) * 100);
      const mib = file ? (file.size / 1048576).toFixed(1) : '';
      updateOverlay('Загружаем архив', file ? `${file.name} · ${mib} МиБ` : op.busyText, `Передано ${percent}%`, percent);
    });
    xhr.upload.addEventListener('load', () => {
      updateOverlay(op.processingTitle, op.processingText, op.processingPhase, null);
    });
    xhr.onload = () => resolve({ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status, text: xhr.responseText});
    xhr.onerror = () => reject(new Error('Соединение с сервером прервано во время загрузки'));
    xhr.ontimeout = () => reject(new Error('Сервер слишком долго не отвечает'));
    xhr.send(body);
  });
}

function detailFrom(data, response) {
  if (response.ok) return '';
  const detail = data && data.detail;
  if (Array.isArray(detail)) return detail.map(x => `${x.loc.join('.')}: ${x.msg}`).join('; ');
  return detail || `Ошибка ${response.status}`;
}

async function goToResult(data, op) {
  successOverlay(op.successTitle || 'Готово', op.successText || 'Операция завершена.');
  try {
    sessionStorage.setItem('psc-success', JSON.stringify({text: op.toast || '✓ Готово.', time: Date.now()}));
  } catch (_) {}
  await sleep(850);
  if (!data.url) {
    window.location.reload();
    return;
  }
  const target = new URL(data.url, window.location.href);
  if (target.origin !== window.location.origin) throw new Error('Некорректный адрес перехода');
  if (target.pathname === window.location.pathname && target.search === window.location.search) {
    window.location.hash = target.hash;
    window.location.reload();
  } else {
    window.location.assign(target.href);
  }
}

for (const form of document.querySelectorAll('form[data-request]')) {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;

    const button = form.querySelector('button');
    const message = form.querySelector('.form-message');
    const op = operation(form);
    const originalButtonText = button ? button.textContent : '';
    if (button) button.disabled = true;
    if (message) {
      message.classList.remove('is-success', 'is-error');
      message.classList.add('is-working');
      message.textContent = form.dataset.upload ? 'Начинаем загрузку…' : 'Выполняем…';
    }

    const headers = {'X-PSC-Request': '1'};
    let body;
    let uploadFile = null;

    if (form.dataset.json) {
      headers['Content-Type'] = 'application/json';
      const values = Object.fromEntries(new FormData(form));
      for (const key of ['version', 'progress', 'predecessor_id', 'stage_id', 'document_id']) {
        if (key in values) values[key] = values[key] === '' ? null : Number(values[key]);
      }
      for (const input of form.querySelectorAll('input[type=checkbox][name]')) values[input.name] = input.checked;
      body = JSON.stringify(values);
    } else if (form.dataset.upload) {
      const data = new FormData(form);
      uploadFile = data.get('file');
      if (!uploadFile || typeof uploadFile.size !== 'number') {
        if (message) message.textContent = 'Выберите ZIP-архив.';
        if (button) button.disabled = false;
        return;
      }
      if (uploadFile.size > 256 * 1024 * 1024) {
        if (message) {
          message.classList.remove('is-working');
          message.classList.add('is-error');
          message.textContent = 'ZIP больше 256 МиБ. Разделите архив на части.';
        }
        if (button) button.disabled = false;
        return;
      }
      body = data;
    }

    if (op.major) showOverlay(op.busyTitle, op.busyText, op.phase, form.dataset.upload ? 0 : null);

    try {
      let response;
      if (form.dataset.upload) {
        response = await xhrUpload(form.dataset.request, form.dataset.method || 'POST', headers, body, op, uploadFile);
      } else {
        const raw = await fetch(form.dataset.request, {method: form.dataset.method || 'POST', headers, body});
        response = {ok: raw.ok, status: raw.status, text: await raw.text()};
      }

      let data;
      try { data = JSON.parse(response.text); }
      catch (_) { throw new Error('Сервер вернул некорректный ответ. Проверьте соединение.'); }
      if (!response.ok) throw new Error(detailFrom(data, response));

      if (message) {
        message.classList.remove('is-working', 'is-error');
        message.classList.add('is-success');
        message.textContent = 'Готово ✓';
      }

      if (op.major) {
        await goToResult(data, op);
      } else {
        showToast(op.toast);
        if (data.url) {
          const target = new URL(data.url, window.location.href);
          if (target.origin !== window.location.origin) throw new Error('Некорректный адрес перехода');
          window.location.assign(target.href);
        } else {
          await sleep(300);
          window.location.reload();
        }
      }
    } catch (error) {
      const text = error.message || 'Нет соединения с сервером';
      if (message) {
        message.classList.remove('is-working', 'is-success');
        message.classList.add('is-error');
        message.textContent = text;
      }
      if (op.major) errorOverlay(text);
      if (button) {
        button.disabled = false;
        button.textContent = originalButtonText;
      }
    }
  });
}

for (const form of document.querySelectorAll('form[data-stage]')) {
  form.elements.status.addEventListener('change', () => {
    const status = form.elements.status.value;
    if (status === 'done') form.elements.progress.value = 100;
    else if (status === 'planned') form.elements.progress.value = 0;
    else if (Number(form.elements.progress.value) === 100) form.elements.progress.value = 99;
    form.elements.note.required = status === 'blocked';
  });
}

for (const form of document.querySelectorAll('form[data-issue]')) {
  form.elements.status.addEventListener('change', () => {
    if (form.elements.status.value !== 'closed') form.elements.verified_by.value = '';
  });
}
