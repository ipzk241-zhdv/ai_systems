'use strict';
// Enumeration keeps File references only; file bytes are streamed later by XHR.
const FolderUpload = (() => {
  function builder(maxItems = 10000) {
    const directories = new Set(), files = [], seen = new Set();
    function check(path, file = false) {
      const names = path.split('/');
      if (!path || /[\\\x00-\x1f]/.test(path) || /^[A-Za-z]:/.test(path) ||
          names.length > (file ? 33 : 32) || names.some(name => !name.trim() || name === '.' || name === '..' || name.length > 180)) {
        throw new Error('Некоректна назва або надто велика глибина папки.');
      }
      return names;
    }
    function limit() {
      if (directories.size + files.length > maxItems) throw new Error('За раз можна завантажити до ' + maxItems + ' файлів і папок.');
    }
    function directory(path) {
      const names = check(path);
      for (let depth = 1; depth <= names.length; depth++) { directories.add(names.slice(0, depth).join('/')); limit(); }
    }
    function file(value, path) {
      const names = check(path, true);
      if (seen.has(path)) throw new Error('Повторний шлях до файла: ' + path);
      if (names.length > 1) directory(names.slice(0, -1).join('/'));
      seen.add(path); files.push({file:value, path}); limit();
    }
    return {directory, file, result:() => ({directories:[...directories], files})};
  }
  function fromFiles(values, maxItems) {
    const plan = builder(maxItems);
    for (const file of values) plan.file(file, file.webkitRelativePath || file.name);
    return plan.result();
  }
  async function fromHandle(handle, maxItems) {
    const plan = builder(maxItems);
    async function visit(node, path) {
      if (node.kind === 'file') plan.file(await node.getFile(), path);
      else {
        plan.directory(path);
        for await (const child of node.values()) await visit(child, path + '/' + child.name);
      }
    }
    await visit(handle, handle.name); return plan.result();
  }
  async function fromEntries(entries, maxItems) {
    const plan = builder(maxItems);
    async function visit(entry, path) {
      if (entry.isFile) plan.file(await new Promise((resolve, reject) => entry.file(resolve, reject)), path);
      else if (entry.isDirectory) {
        plan.directory(path);
        const reader = entry.createReader();
        // Chromium returns directory entries in batches, often 100 at a time.
        for (;;) {
          const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
          if (!batch.length) break;
          for (const child of batch) await visit(child, path + '/' + child.name);
        }
      }
    }
    for (const entry of entries) await visit(entry, entry.name);
    return plan.result();
  }
  function manifest(plan, parentId) {
    return {directories:plan.directories, files:plan.files.map(item => ({path:item.path, size:item.file.size})), parent_id:parentId};
  }
  return {fromFiles, fromHandle, fromEntries, manifest};
})();
if (typeof module !== 'undefined') module.exports = FolderUpload;
