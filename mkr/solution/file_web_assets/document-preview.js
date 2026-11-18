'use strict';
let documentState = null, documentRequest = 0;
function documentDirty() { return documentState && documentState.editable && $('document-editor').value !== documentState.saved; }
function documentStatus() {
  $('document-save').disabled = !documentDirty() || documentState.saving;
  $('document-status').textContent = documentState.saving ? 'Зберігаю…' : documentDirty() ? 'Є незбережені зміни' : 'Зміни збережено';
}
function closeDocument() {
  if (documentState && documentState.saving) return;
  if (documentDirty() && !confirm('Закрити документ без збереження змін?')) return;
  documentRequest++; documentState = null; $('document-dialog').close();
  $('document-body').replaceChildren(); $('document-editor').value = '';
}
function previewTable(rows) {
  const wrapper = document.createElement('div'); wrapper.className = 'document-table-scroll';
  const table = document.createElement('table'); table.className = 'document-table';
  const body = document.createElement('tbody');
  for (const cells of rows) {
    const tr = document.createElement('tr');
    for (const value of cells) { const td = document.createElement('td'); td.textContent = value; tr.append(td); }
    body.append(tr);
  }
  table.append(body); wrapper.append(table); return wrapper;
}
function columnName(number) {
  let text = '';
  while (number) { number--; text = String.fromCharCode(65 + number % 26) + text; number = Math.floor(number / 26); }
  return text;
}
function renderSheet(sheet, parent) {
  parent.replaceChildren();
  const headers = ['№', ...Array.from({length:sheet.columns}, (_, i) => columnName(i + 1))];
  const rows = sheet.rows.map(row => [row.number, ...Array.from({length:sheet.columns}, (_, i) => row.cells[String(i + 1)] || '')]);
  const table = previewTable([headers, ...rows]); table.classList.add('spreadsheet-preview'); parent.append(table);
}
async function openDocument(file) {
  if (documentState && documentState.saving) return;
  if (documentDirty() && !confirm('Відкрити інший файл без збереження змін?')) return;
  const serial = ++documentRequest;
  documentState = null;
  $('document-title').textContent = file.name; $('document-error').textContent = '';
  $('document-status').textContent = 'Відкриваю…'; $('document-save').hidden = true;
  $('document-editor').hidden = true; $('document-editor').value = ''; $('document-body').replaceChildren();
  $('document-download').href = '/api/files/' + file.id + '/download';
  if (!$('document-dialog').open) $('document-dialog').showModal();
  try {
    const data = await api('/api/files/' + file.id + '/preview');
    if (serial !== documentRequest) return;
    const parent = $('document-body');
    documentState = {id:file.id, revision:data.revision, editable:!!data.editable, saved:(data.text || '').replace(/\r\n?/g, '\n'), saving:false};
    if (data.kind === 'text') {
      $('document-editor').hidden = false; $('document-editor').value = data.text;
      $('document-editor').readOnly = !data.editable; $('document-save').hidden = !data.editable;
      if (data.editable) documentStatus(); else $('document-status').textContent = 'Лише перегляд';
    } else if (data.kind === 'pdf') {
      const frame = document.createElement('iframe'); frame.className = 'document-pdf'; frame.title = 'Перегляд PDF: ' + data.name;
      frame.src = data.url; parent.append(frame);
      const fallback = document.createElement('a'); fallback.href = data.url; fallback.target = '_blank'; fallback.rel = 'noopener';
      fallback.textContent = 'Відкрити PDF в окремій вкладці'; parent.append(fallback);
      $('document-status').textContent = 'PDF · якщо браузер не показує документ, відкрий окремо або скачай';
    } else {
      if (data.kind === 'word') {
        for (const block of data.blocks) {
          if (block.type === 'table') parent.append(previewTable(block.rows));
          else { const p = document.createElement('p'); p.className = 'document-paragraph'; p.textContent = block.text || '\u00a0'; parent.append(p); }
        }
      } else if (data.kind === 'excel') {
        const tabs = document.createElement('div'), content = document.createElement('div'); tabs.className = 'sheet-tabs';
        data.sheets.forEach((sheet, index) => {
          const button = document.createElement('button'); button.className = 'secondary'; button.textContent = sheet.name;
          function activate() { renderSheet(sheet, content); for (const item of tabs.children) item.classList.toggle('active', item === button); }
          button.addEventListener('click', activate); tabs.append(button); if (!index) activate();
        });
        parent.append(tabs, content);
      }
      $('document-status').textContent = 'Спрощений перегляд · ' + (data.truncated ? 'показано лише частину документа' : 'оформлення може відрізнятися від Office');
    }
  } catch(error) { if (serial === documentRequest) { $('document-error').textContent = error.message; $('document-status').textContent = 'Перегляд недоступний'; } }
}
$('document-close').addEventListener('click', closeDocument);
$('document-dialog').addEventListener('cancel', event => { event.preventDefault(); closeDocument(); });
$('document-editor').addEventListener('input', documentStatus);
$('document-save').addEventListener('click', async () => {
  if (!documentState || !documentDirty() || documentState.saving) return;
  const state = documentState, text = $('document-editor').value;
  state.saving = true; $('document-editor').readOnly = true; $('document-error').textContent = ''; documentStatus();
  try {
    const result = await api('/api/files/' + state.id + '/content', jsonOptions({text, revision:state.revision}));
    state.revision = result.revision; state.saved = text;
    // Refresh file sizes/quota without discarding the open editor.
    try { await load(); } catch { notice('Текст збережено. Онови сторінку, щоб побачити актуальний список файлів.'); }
  } catch(error) { $('document-error').textContent = error.message; }
  finally { state.saving = false; $('document-editor').readOnly = false; documentStatus(); }
});
window.addEventListener('beforeunload', event => {
  if (documentDirty() || (documentState && documentState.saving)) { event.preventDefault(); event.returnValue = ''; }
});
