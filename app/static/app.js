'use strict';
for (const form of document.querySelectorAll('form[data-request]')) {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
    const button = form.querySelector('button');
    const message = form.querySelector('.form-message');
    button.disabled = true;
    message.textContent = form.dataset.upload ? 'Архив загружается и анализируется. Не закрывайте страницу…' : 'Сохраняем…';
    const headers = {'X-PSC-Request': '1'};
    let body;
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
      if (data.get('file').size > 256 * 1024 * 1024) {
        message.textContent = 'ZIP больше 256 МиБ. Разделите архив на части.';
        button.disabled = false;
        return;
      }
      body = data;
    }
    try {
      const response = await fetch(form.dataset.request, {method: form.dataset.method || 'POST', headers, body});
      let data;
      try { data = await response.json(); } catch { throw new Error('Сервер вернул некорректный ответ. Проверьте соединение.'); }
      if (!response.ok) {
        const detail = Array.isArray(data.detail) ? data.detail.map(x => `${x.loc.join('.')}: ${x.msg}`).join('; ') : data.detail;
        throw new Error(detail || `Ошибка ${response.status}`);
      }
      message.textContent = 'Сохранено';
      if (data.url) {
        const target = new URL(data.url, window.location.href);
        if (target.origin !== window.location.origin) throw new Error('Некорректный адрес перехода');
        // A fragment-only navigation does not fetch updated server-rendered forms.
        if (target.pathname === window.location.pathname && target.search === window.location.search) {
          window.location.hash = target.hash;
          window.location.reload();
        } else window.location.assign(target.href);
      } else window.location.reload();
    } catch (error) {
      message.textContent = error.message || 'Нет соединения с сервером';
      button.disabled = false;
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
