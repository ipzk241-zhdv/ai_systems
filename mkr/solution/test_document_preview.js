'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const FolderTree = require('./file_web_assets/folder-tree.js');
const FolderUpload = require('./file_web_assets/folder-upload.js');

function environment() {
  const nodes = new Map();
  function node(tag = '') {
    return {tag, children:[], handlers:{}, _value:'', get value() { return this._value; }, set value(text) { this._value = text.replace(/\r\n?/g, '\n'); }, hidden:false, disabled:false, open:false,
      classList:{toggle() {}, add() {}},
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      addEventListener(name, handler) { this.handlers[name] = handler; },
      showModal() { this.open = true; }, close() { this.open = false; }};
  }
  const $ = id => { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); };
  const context = vm.createContext({$, document:{createElement:node}, window:{addEventListener() {}},
    api:async () => ({kind:'text', text:'before', editable:true, revision:'r1'}),
    jsonOptions:body => ({body}), load:async () => {}, notice() {}, confirm:() => true});
  vm.runInContext(fs.readFileSync(path.join(__dirname, 'file_web_assets/document-preview.js'), 'utf8'), context);
  return {context, $, run:code => vm.runInContext(code, context)};
}

test('editor saves with the viewed revision and updates dirty state only after success', async () => {
  const {context, $, run} = environment();
  await context.openDocument({id:7, name:'notes.md'});
  assert.equal(run('documentDirty()'), false);
  $('document-editor').value = 'after'; $('document-editor').handlers.input();
  assert.equal(run('documentDirty()'), true);
  let posted;
  context.api = async (url, options) => { posted = {url, ...options.body}; return {revision:'r2'}; };
  await $('document-save').handlers.click();
  assert.deepEqual(posted, {url:'/api/files/7/content', text:'after', revision:'r1'});
  assert.equal(run('documentDirty()'), false);
  assert.equal($('document-save').disabled, true);
});
test('a conflict preserves local edits and the stale version instead of retrying over newer content', async () => {
  const {context, $, run} = environment();
  await context.openDocument({id:7, name:'notes.txt'});
  $('document-editor').value = 'unsaved';
  context.api = async () => { throw new Error('Version conflict'); };
  await $('document-save').handlers.click();
  assert.equal($('document-editor').value, 'unsaved');
  assert.equal($('document-error').textContent, 'Version conflict');
  assert.equal(run('documentState.revision'), 'r1');
  assert.equal(run('documentDirty()'), true);
  context.confirm = () => false; context.closeDocument();
  assert.equal($('document-dialog').open, true);
});
test('closing a loading preview prevents a late response reopening its content', async () => {
  const {context, $, run} = environment();
  let resolve;
  context.api = () => new Promise(done => { resolve = done; });
  const opening = context.openDocument({id:7, name:'notes.txt'});
  context.closeDocument();
  resolve({kind:'text', text:'late', editable:true, revision:'r1'}); await opening;
  assert.equal(run('documentState'), null);
  assert.equal($('document-dialog').open, false);
  assert.equal($('document-editor').value, '');
});
test('untrusted Office cell content is inserted as text', async () => {
  const {context, $} = environment();
  const text = '<img src=x onerror=alert(1)>';
  context.api = async () => ({kind:'word', blocks:[{type:'paragraph', text}, {type:'table', rows:[[text]]}]});
  await context.openDocument({id:7, name:'notes.docx'});
  assert.equal($('document-body').children[0].textContent, text);
  assert.equal($('document-save').hidden, true);
});
test('thousands of nested folders stay hidden until their parent is expanded', () => {
  const folders = [{id:1, parent_id:null, name:'Root'}, {id:2, parent_id:1, name:'Child'},
    ...Array.from({length:10000}, (_, i) => ({id:i + 3, parent_id:2, name:'Nested ' + i}))];
  const collapsed = FolderTree.visible(folders, new Set());
  assert.equal(collapsed.length, 1); assert.equal(collapsed[0].hasChildren, true);
  assert.deepEqual(collapsed[0].children, []);
  const expanded = FolderTree.visible(folders, new Set([1]));
  assert.equal(expanded[0].children.length, 1);
  assert.deepEqual(expanded[0].children[0].children, []);
});
test('configured upload count can exceed the old fixed 10000 limit', () => {
  const files = Array.from({length:10001}, (_, i) => ({name:'file' + i, size:0}));
  assert.equal(FolderUpload.fromFiles(files, 20000).files.length, 10001);
  assert.throws(() => FolderUpload.fromFiles(files, 10000));
  assert.throws(() => FolderUpload.fromFiles(files, 1));
});

test('CRLF text is initially clean after browser textarea newline normalization', async () => {
  const {context, $, run} = environment();
  context.api = async () => ({kind:'text', text:'line1\r\nline2\r\n', editable:true, revision:'r1'});
  await context.openDocument({id:7, name:'notes.txt'});
  assert.equal($('document-editor').value, 'line1\nline2\n');
  assert.equal(run('documentDirty()'), false);
});
