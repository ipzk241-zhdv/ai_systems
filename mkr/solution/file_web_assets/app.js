'use strict';
const $ = id => document.getElementById(id);
let params = new URLSearchParams(location.hash.slice(1));
let code = params.get('access') || '', csrf = '', files = [], folders = [], storage = {};
let currentFolder = Number(params.get('folder')) || null, view = params.get('view') || 'drive', grid = false, editState = null;
if (code) history.replaceState(null, '', location.pathname);
const labels = { drive: 'Спільний диск', recent: 'Нещодавні', documents: 'Документи', images: 'Зображення' };
const notice = text => { $('notice').textContent = text; $('notice').hidden = !text; };
const jsonOptions = body => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
async function api(path, options = {}) {
  const response = await fetch(path, { credentials: 'same-origin', ...options,
    headers: { ...(csrf ? { 'X-CSRF-Token': csrf } : {}), ...options.headers } });
  let data; try { data = await response.json(); } catch { throw new Error('Сервер не прийняв запит. Спробуй ще раз.'); }
  if (!response.ok) throw new Error(data.error || 'Не вдалося виконати запит.');
  return data;
}
function size(bytes) {
  if (!bytes) return '0 Б';
  if (bytes >= 1024 ** 3) return (bytes / 1024 ** 3).toLocaleString('uk-UA', {maximumFractionDigits:1}) + ' ГБ';
  if (bytes >= 1024 ** 2) return (bytes / 1024 ** 2).toLocaleString('uk-UA', {maximumFractionDigits:1}) + ' МБ';
  return Math.max(1, Math.round(bytes / 1024)) + ' КБ';
}
function pathTo(id) {
  const path = [], seen = new Set();
  while (id && !seen.has(id) && path.length < 32) {
    seen.add(id); const folder = folders.find(item => item.id === id); if (!folder) break;
    path.unshift(folder); id = folder.parent_id;
  }
  return path;
}
function navigate(id = null, nextView = 'drive') {
  notice('');
  currentFolder = id; view = nextView; $('search').value = '';
  history.pushState(null, '', id ? '#folder=' + id : '#view=' + view);
  document.body.classList.remove('menu-open'); render();
}
function fileType(name) {
  const extension = name.split('.').pop().toLowerCase();
  if (['png','jpg','jpeg','webp','gif','svg','heic'].includes(extension)) return {label:'IMG', style:'image'};
  if (extension === 'pdf') return {label:'PDF', style:'pdf'};
  if (['csv','xlsx','xls'].includes(extension)) return {label:extension.toUpperCase(), style:'sheet'};
  return {label:extension.slice(0,4).toUpperCase() || 'FILE', style:''};
}
function tree() {
  $('folder-tree').replaceChildren();
  function branch(parentId, parent, depth) {
    if (depth > 31) return;
    for (const folder of folders.filter(item => item.parent_id === parentId)) {
      const li = document.createElement('li'), button = document.createElement('button');
      button.textContent = '▱  ' + folder.name; button.title = folder.name;
      if (currentFolder === folder.id) button.classList.add('active');
      button.addEventListener('click', () => navigate(folder.id)); li.append(button);
      const children = document.createElement('ul'); branch(folder.id, children, depth + 1);
      if (children.children.length) li.append(children);
      parent.append(li);
    }
  }
  branch(null, $('folder-tree'), 0);
}
function iconButton(text, label, action) {
  const button = document.createElement('button'); button.className = 'icon-button'; button.textContent = text;
  button.title = label; button.setAttribute('aria-label', label); button.addEventListener('click', action); return button;
}
async function askFile(file) {
  const command = '/file ' + file.id + ' поясни головне';
  try { await navigator.clipboard.writeText(command); notice('Скопійовано: ' + command + '. Надішли команду боту в особистий чат.'); }
  catch { notice('Надішли боту: ' + command); }
}
function render() {
  if (currentFolder && !folders.some(folder => folder.id === currentFolder)) currentFolder = null;
  const query = $('search').value.trim().toLocaleLowerCase('uk');
  const folder = folders.find(item => item.id === currentFolder);
  $('view-title').textContent = query ? 'Результати пошуку' : (folder ? folder.name : labels[view] || labels.drive);
  $('breadcrumbs').replaceChildren();
  if (currentFolder) {
    const root = document.createElement('button'); root.textContent = 'Спільний диск'; root.addEventListener('click', () => navigate()); $('breadcrumbs').append(root);
    for (const part of pathTo(currentFolder)) {
      const separator = document.createElement('span'); separator.textContent = '›';
      const button = document.createElement('button'); button.textContent = part.name; button.addEventListener('click', () => navigate(part.id));
      $('breadcrumbs').append(separator, button);
    }
  }
  document.querySelectorAll('[data-view]').forEach(button => button.classList.toggle('active', button.dataset.view === view && !currentFolder));
  tree();
  let visibleFiles = files.filter(file => query || view !== 'drive' || file.folder_id === currentFolder);
  let visibleFolders = folders.filter(item => query || (view === 'drive' && item.parent_id === currentFolder));
  if (view === 'images') visibleFiles = visibleFiles.filter(file => fileType(file.name).style === 'image');
  if (view === 'documents') visibleFiles = visibleFiles.filter(file => /\.(pdf|docx?|txt|md|csv|json|log|xlsx?)$/i.test(file.name));
  if (query) {
    visibleFiles = visibleFiles.filter(file => file.name.toLocaleLowerCase('uk').includes(query));
    visibleFolders = visibleFolders.filter(item => item.name.toLocaleLowerCase('uk').includes(query));
  }
  const entries = [...visibleFolders.map(folder => ({...folder, kind:'folder'})), ...visibleFiles.map(file => ({...file, kind:'file'}))];
  const sort = view === 'recent' ? 'newest' : $('sort').value;
  entries.sort((a,b) => (a.kind !== b.kind ? (a.kind === 'folder' ? -1 : 1) :
    sort === 'newest' ? b.created_at - a.created_at : sort === 'size' ? (b.size || 0) - (a.size || 0) : a.name.localeCompare(b.name, 'uk')));
  $('files').replaceChildren(); $('files').classList.toggle('grid', grid); document.querySelector('.file-panel').classList.toggle('grid-mode', grid);
  const amount = entries.length, last = amount % 10, lastTwo = amount % 100;
  const noun = last === 1 && lastTwo !== 11 ? 'об’єкт' : last >= 2 && last <= 4 && !(lastTwo >= 12 && lastTwo <= 14) ? 'об’єкти' : 'об’єктів';
  $('item-count').textContent = amount + ' ' + noun; $('empty').hidden = amount > 0;
  $('empty-title').textContent = query ? 'Нічого не знайдено' : 'Тут поки порожньо';
  for (const entry of entries) {
    const isFolder = entry.kind === 'folder', type = isFolder ? {label:'▰', style:'folder'} : fileType(entry.name);
    const row = document.createElement('article'); row.className = 'file-row';
    const icon = document.createElement('span'); icon.className = 'file-icon ' + type.style; icon.textContent = type.label;
    const details = document.createElement('div'); details.className = 'name-cell';
    const name = document.createElement('button'); name.className = 'file-name'; name.textContent = entry.name; name.title = entry.name;
    name.addEventListener('click', () => {
      if (isFolder) navigate(entry.id);
      else { document.querySelectorAll('.file-row').forEach(item => item.classList.remove('selected')); row.classList.add('selected'); }
    });
    if (!isFolder) name.addEventListener('dblclick', () => { location.href = '/api/files/' + entry.id + '/download'; });
    const meta = document.createElement('div'); meta.className = 'file-meta';
    meta.textContent = isFolder ? 'Папка' : size(entry.size) + ' · #' + entry.id;
    details.append(name, meta);
    const date = document.createElement('span'); date.className = 'file-date'; date.textContent = new Date(entry.created_at * 1000).toLocaleDateString('uk-UA');
    const bytes = document.createElement('span'); bytes.className = 'file-size'; bytes.textContent = isFolder ? '—' : size(entry.size);
    const actions = document.createElement('div'); actions.className = 'file-actions';
    if (!isFolder) {
      actions.append(iconButton('✦', 'Команда для ШІ: ' + entry.name, () => askFile(entry)));
      const download = document.createElement('a'); download.className = 'icon-button'; download.textContent = '↓'; download.title = 'Завантажити ' + entry.name;
      download.setAttribute('aria-label', download.title); download.href = '/api/files/' + entry.id + '/download'; actions.append(download);
    } else actions.append(iconButton('↗', 'Відкрити папку ' + entry.name, () => navigate(entry.id)));
    actions.append(iconButton('⋮', 'Властивості: ' + entry.name, () => edit(entry)));
    row.append(icon, details, date, bytes, actions); $('files').append(row);
  }
  if (storage.limit_bytes) {
    $('storage-meter').max = storage.limit_bytes; $('storage-meter').value = storage.used_bytes;
    $('storage-used').textContent = (storage.used_bytes ? size(storage.used_bytes) : '0 МБ') + ' із ' + size(storage.limit_bytes);
    $('storage-free').textContent = 'Доступно: ' + size(storage.available_bytes);
    $('file-limit').textContent = 'Один файл — до ' + size(storage.max_file_bytes);
  }
}
async function load() { const data = await api('/api/files'); files = data.files; folders = data.folders; storage = data.storage; render(); }
function showWorkspace(userId) {
  $('login').hidden = true; $('workspace').hidden = false; $('search-wrap').hidden = false; $('account').hidden = false; $('menu').hidden = false;
  $('user-label').textContent = 'ID ' + userId; document.body.classList.add('signed-in');
}
function edit(entry = null) {
  editState = entry;
  $('edit-error').textContent = '';
  $('edit-title').textContent = entry ? 'Властивості й розташування' : 'Нова папка';
  $('item-name').value = entry ? entry.name : '';
  $('item-parent').replaceChildren();
  const root = new Option('Спільний диск', ''); $('item-parent').append(root);
  for (const folder of folders) {
    if (entry && entry.kind === 'folder' && pathTo(folder.id).some(part => part.id === entry.id)) continue;
    $('item-parent').append(new Option(pathTo(folder.id).map(part => part.name).join(' / '), folder.id));
  }
  $('item-parent').value = entry ? (entry.kind === 'folder' ? entry.parent_id : entry.folder_id) || '' : currentFolder || '';
  $('edit-hint').textContent = entry ? (entry.kind === 'file' ? 'Файл #' + entry.id + ' · ' + size(entry.size) + '. Після переміщення номер для бота залишиться тим самим.' : 'Можна змінити назву або перемістити папку.') : 'Папка буде спільною для всіх дозволених учасників.';
  $('edit-dialog').showModal(); $('item-name').focus();
}
$('edit-form').addEventListener('submit', async event => {
  event.preventDefault(); $('save-edit').disabled = true;
  const parentId = $('item-parent').value ? Number($('item-parent').value) : null;
  try {
    const path = editState ? '/api/' + (editState.kind === 'folder' ? 'folders/' : 'files/') + editState.id : '/api/folders';
    await api(path, jsonOptions({name:$('item-name').value, parent_id:parentId})); $('edit-dialog').close(); await load(); notice('Збережено.');
  } catch(error) { $('edit-error').textContent = error.message; } finally { $('save-edit').disabled = false; }
});
$('close-dialog').addEventListener('click', () => $('edit-dialog').close()); $('cancel-edit').addEventListener('click', () => $('edit-dialog').close());
$('new-folder').addEventListener('click', () => edit());
$('login-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = $('login-form').querySelector('button'); button.disabled = true; notice('');
  try { const data = await api('/api/login', jsonOptions({code, password:$('password').value})); csrf = data.csrf; code = ''; $('password').value = '';
    const session = await api('/api/session'); showWorkspace(session.user_id); await load(); }
  catch(error) { notice(error.message); } finally { button.disabled = false; }
});
function uploadOne(file, folderId) {
  return new Promise((resolve,reject) => {
    const request = new XMLHttpRequest(); request.open('POST', '/api/files?name=' + encodeURIComponent(file.name) + (folderId ? '&folder=' + folderId : ''));
    request.withCredentials = true; request.setRequestHeader('X-CSRF-Token', csrf); request.setRequestHeader('Content-Type','application/octet-stream');
    request.upload.addEventListener('progress', event => { if (event.lengthComputable) $('upload-progress').value = Math.round(event.loaded / event.total * 100); });
    request.onload = () => { try { const result = JSON.parse(request.responseText); request.status >= 200 && request.status < 300 ? resolve(result) : reject(new Error(result.error || 'Завантаження не вдалося.')); }
      catch { reject(new Error('Сервер не прийняв файл. Перевір його розмір і налаштування ліміту.')); } };
    request.onerror = () => reject(new Error('З’єднання перервалось. Спробуй завантажити файл ще раз.'));
    request.send(file);
  });
}
$('upload-button').addEventListener('click', () => $('upload').click()); $('sidebar-upload').addEventListener('click', () => { document.body.classList.remove('menu-open'); $('upload').click(); });
$('upload').addEventListener('change', async () => {
  const selected = [...$('upload').files]; if (!selected.length) return;
  if (selected.some(file => !file.size || file.size > storage.max_file_bytes)) { notice('Обери непорожні файли, кожен до ' + size(storage.max_file_bytes) + '.'); $('upload').value = ''; return; }
  const target = currentFolder; $('upload-button').disabled = true; $('sidebar-upload').disabled = true; $('upload-state').hidden = false; notice('');
  try { for (let index=0; index<selected.length; index++) { $('upload-label').textContent = (index+1) + '/' + selected.length + ' · ' + selected[index].name; $('upload-progress').value = 0; await uploadOne(selected[index], target); }
    notice('Завантажено файлів: ' + selected.length + '.'); }
  catch(error) { notice(error.message); }
  finally { $('upload').value = ''; $('upload-button').disabled = false; $('sidebar-upload').disabled = false; $('upload-state').hidden = true; try { await load(); } catch(error) { notice(error.message); } }
});
$('logout').addEventListener('click', async () => { try { await api('/api/logout',{method:'POST'}); location.reload(); } catch(error) { notice(error.message); } });
$('search').addEventListener('input', render); $('sort').addEventListener('change', render);
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => navigate(null,button.dataset.view)));
$('menu').addEventListener('click', () => document.body.classList.toggle('menu-open'));
$('close-menu').addEventListener('click', () => document.body.classList.remove('menu-open'));
$('list-view').addEventListener('click', () => changeView(false)); $('grid-view').addEventListener('click', () => changeView(true));
function changeView(value) { grid=value; for (const [id,active] of [['list-view',!grid],['grid-view',grid]]) { $(id).classList.toggle('chosen',active); $(id).setAttribute('aria-pressed',String(active)); } render(); }
window.addEventListener('popstate', () => { const hash=new URLSearchParams(location.hash.slice(1)); currentFolder=Number(hash.get('folder'))||null; view=hash.get('view')||'drive'; $('search').value=''; render(); });
(async () => { try { const session=await api('/api/session'); csrf=session.csrf; showWorkspace(session.user_id); await load(); }
  catch { if (!code) notice('Для входу отримай посилання командою /files в особистому чаті бота.'); } })();
