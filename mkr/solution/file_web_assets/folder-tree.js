'use strict';
const FolderTree = {
  visible(folders, expanded) {
    const children = new Map();
    for (const folder of folders) {
      if (!children.has(folder.parent_id)) children.set(folder.parent_id, []);
      children.get(folder.parent_id).push(folder);
    }
    function branch(parent, depth) {
      if (depth > 31) return [];
      return (children.get(parent) || []).map(folder => ({
        folder, hasChildren:children.has(folder.id), expanded:expanded.has(folder.id),
        children:expanded.has(folder.id) ? branch(folder.id, depth + 1) : []
      }));
    }
    return branch(null, 0);
  }
};
if (typeof module !== 'undefined') module.exports = FolderTree;
