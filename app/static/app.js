'use strict';
for (const form of document.querySelectorAll('form[data-request]')) {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    const message = form.querySelector('.form-message');
    button.disabled = true;
    message.textContent = form.dataset.upload ? 'Архив загружается и анализируется. Не закрывайте страницу…' : 'Сохраняем…';
    const headers = {'X-PSC-Request': '1'};
    let body;
    if (form.dataset.json) {
      headers['Content-Type'] = 'application/json';
      const values = Object.fromEntries(new FormData(form));
      for (const key of ['version', 'progress']) {
        if (key in values) values[key] = values[key] === '' ? null : Number(values[key]);
      }
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
      if (data.url) window.location.assign(data.url); else window.location.reload();
    } catch (error) {
      message.textContent = error.message || 'Нет соединения с сервером';
      button.disabled = false;
    }
  });
}
