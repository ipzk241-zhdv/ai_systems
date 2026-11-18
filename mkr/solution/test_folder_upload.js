'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const upload = require('./file_web_assets/folder-upload.js');
const file = (name, size = 1, path = '') => ({name, size, webkitRelativePath:path});
const handle = (name, children) => ({name, kind:'directory', async *values() { yield* children; }});
const leaf = name => ({name, kind:'file', getFile:async () => file(name)});
function directory(name, batches) {
  return {name, isDirectory:true, createReader() {
    let index = 0;
    return {readEntries(resolve) { resolve(batches[index++] || []); }};
  }};
}
const entry = name => ({name, isFile:true, file:resolve => resolve(file(name))});

test('fallback preserves selected root, nested paths, unicode and zero-byte files', () => {
  const values = [file('текст.txt', 5, 'Моя папка/підпапка/текст.txt'), file('empty', 0, 'Моя папка/empty')];
  const plan = upload.fromFiles(values);
  assert.deepEqual(plan.directories, ['Моя папка', 'Моя папка/підпапка']);
  assert.equal(plan.files[0].file, values[0]);
  assert.deepEqual(upload.manifest(plan, 12), {parent_id:12, directories:plan.directories, files:[
    {path:'Моя папка/підпапка/текст.txt', size:5}, {path:'Моя папка/empty', size:0}]});
});
test('picker includes empty directories without reading file bytes', async () => {
  const plan = await upload.fromHandle(handle('Root', [handle('Empty', []), handle('Nested', [leaf('a.txt')])]));
  assert.deepEqual(plan.directories, ['Root', 'Root/Empty', 'Root/Nested']);
  assert.equal(plan.files[0].path, 'Root/Nested/a.txt');
});
test('drag and drop reads all batches, multiple roots, nested and empty directories', async () => {
  const first = Array.from({length:100}, (_, i) => entry('a' + i));
  const plan = await upload.fromEntries([directory('Root', [first, [entry('last'), directory('Empty', [])], []]), entry('outside')]);
  assert.equal(plan.files.length, 102);
  assert.equal(plan.files[100].path, 'Root/last');
  assert.equal(plan.files[101].path, 'outside');
  assert.deepEqual(plan.directories, ['Root', 'Root/Empty']);
});
test('unsafe, duplicate and excessively deep paths are rejected', () => {
  for (const path of ['../x', '/root/x', 'C:/x', 'a\\x', 'a//x', 'a/./x', 'a/../x', 'a/\0x', 'a'.repeat(181), Array(34).fill('a').join('/')]) {
    assert.throws(() => upload.fromFiles([file('x', 1, path)]), undefined, path);
  }
  assert.throws(() => upload.fromFiles([file('a'), file('a')]));
});
test('entry count bounded and permission failures propagate', async () => {
  assert.throws(() => upload.fromFiles(Array.from({length:10001}, (_, i) => file('a' + i))));
  const denied = {name:'denied', isFile:true, file:(resolve, reject) => reject(new Error('permission denied'))};
  await assert.rejects(upload.fromEntries([denied]), /permission denied/);
});
