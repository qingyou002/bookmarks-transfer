"use strict";

function newCounts() { return {added: 0, updated: 0, skipped: 0, failed: 0}; }

async function exportCookies() {
  const ordinary = await chrome.cookies.getAll({});
  // Empty partitionKey matches all top-level partition sites in supported Chromium.
  const partitioned = await chrome.cookies.getAll({partitionKey: {}});
  const union = new Map([...ordinary, ...partitioned].map(cookie => [cookieKey(cookie), cookie]));
  return {cookies: [...union.values()]};
}

async function importCookies(payload) {
  const counts = newCounts();
  const existing = new Map((await exportCookies()).cookies.map(c => [cookieKey(c), c]));
  for (const cookie of payload.cookies) {
    if (!cookie.session && cookie.expirationDate <= Date.now() / 1000) {
      counts.skipped++;
      continue;
    }
    try {
      const old = existing.get(cookieKey(cookie));
      if (old && JSON.stringify(cookieDetails(old)) === JSON.stringify(cookieDetails(cookie))) {
        counts.skipped++;
        continue;
      }
      const written = await chrome.cookies.set(cookieDetails(cookie));
      if (!written || cookieKey(written) !== cookieKey(cookie) || written.value !== cookie.value ||
          written.secure !== cookie.secure || written.httpOnly !== cookie.httpOnly ||
          written.sameSite !== cookie.sameSite || written.session !== cookie.session) {
        counts.failed++;
      } else {
        counts[old ? "updated" : "added"]++;
      }
    } catch (_) { counts.failed++; }
  }
  return {counts};
}

async function restoreCookies(payload) {
  const counts = newCounts();
  const incoming = new Map(payload.incoming.map(c => [cookieKey(c), c]));
  const old = new Map(payload.before.map(c => [cookieKey(c), c]));
  // Undo only identities touched by this migration, preserving independent target cookies.
  for (const [key, cookie] of incoming) {
    if (old.has(key)) continue;
    try {
      await chrome.cookies.set(cookieDetails(cookie, 0));
      counts.updated++;
    } catch (_) { counts.failed++; }
  }
  const restored = await importCookies({cookies: [...old].filter(([key]) => incoming.has(key)).map(([, c]) => c)});
  for (const field of Object.keys(counts)) counts[field] += restored.counts[field];
  return {counts};
}

async function exportBookmarks() {
  const {bookmarkMappings = {}} = await chrome.storage.local.get("bookmarkMappings");
  const managed = new Set();
  for (const mapping of Object.values(bookmarkMappings)) {
    if (mapping.root) managed.add(mapping.root);
    for (const id of Object.values(mapping.nodes || {})) managed.add(id);
  }
  function prune(node) {
    if (managed.has(node.id)) return null;
    return {...node, children: node.children ? node.children.map(prune).filter(Boolean) : undefined};
  }
  // Omit tool-imported nodes so a reverse migration cannot recursively import its own copies.
  return {tree: (await chrome.bookmarks.getTree()).map(prune).filter(Boolean)};
}

async function importBookmarks(payload) {
  const counts = newCounts();
  const namespace = payload.namespace;
  if (!/^[a-f0-9]{64}$/.test(namespace)) throw new Error("invalid-namespace");
  const {bookmarkMappings = {}} = await chrome.storage.local.get("bookmarkMappings");
  let mapping = bookmarkMappings[namespace] || {root: null, nodes: {}};
  const tree = await chrome.bookmarks.getTree();
  const roots = tree[0].children || [];
  const writable = roots.find(node => !node.unmodifiable && node.folderType === "other") ||
    roots.find(node => !node.unmodifiable && node.id === "2") ||
    roots.find(node => !node.unmodifiable);
  if (!writable) throw new Error("no-writable-root");
  const index = new Map();
  function indexTree(node) {
    index.set(node.id, node);
    for (const child of node.children || []) indexTree(child);
  }
  indexTree(tree[0]);
  const existingRoot = index.get(mapping.root);
  if (!existingRoot || existingRoot.url || existingRoot.unmodifiable) {
    const root = await chrome.bookmarks.create({parentId: writable.id, title: `${payload.sourceBrowser} 收藏夹`});
    mapping = {root: root.id, nodes: {}};
    index.set(root.id, root);
  }
  function inManagedFolder(id) {
    const visited = new Set();
    while (id && !visited.has(id)) {
      if (id === mapping.root) return true;
      visited.add(id);
      id = index.get(id)?.parentId;
    }
    return false;
  }
  async function persist() {
    bookmarkMappings[namespace] = mapping;
    await chrome.storage.local.set({bookmarkMappings});
  }
  await persist();
  async function copy(node, parentId) {
    if (node.unmodifiable) { counts.skipped++; return; }
    let target = index.get(mapping.nodes[node.id]);
    try {
      if (!target || !inManagedFolder(target.id) || Boolean(target.url) !== Boolean(node.url)) {
        target = await chrome.bookmarks.create({parentId, title: node.title || "", ...(node.url ? {url: node.url} : {})});
        mapping.nodes[node.id] = target.id;
        index.set(target.id, target);
        counts.added++;
        await persist(); // Partial failures are restartable without duplicate nodes.
      } else {
        const changes = {};
        if (target.title !== node.title) changes.title = node.title || "";
        if (node.url && target.url !== node.url) changes.url = node.url;
        let changed = Object.keys(changes).length > 0;
        if (changed) Object.assign(target, await chrome.bookmarks.update(target.id, changes));
        if (target.parentId !== parentId) {
          Object.assign(target, await chrome.bookmarks.move(target.id, {parentId}));
          changed = true;
        }
        counts[changed ? "updated" : "skipped"]++;
      }
      for (const child of node.children || []) await copy(child, target.id);
    } catch (_) { counts.failed++; }
  }
  for (const root of payload.tree[0]?.children || []) {
    // Preserve names of the source's bar/other/mobile roots as ordinary subfolders.
    await copy(root, mapping.root);
  }
  await persist();
  return {counts};
}

async function operate(operation, payload) {
  switch (operation) {
    case "ping": return {browser: BROWSER_KIND};
    case "cookies_export": return exportCookies();
    case "cookies_import": return importCookies(payload);
    case "cookies_restore": return restoreCookies(payload);
    case "bookmarks_export": return exportBookmarks();
    case "bookmarks_import": return importBookmarks(payload);
    case "mappings_export": return chrome.storage.local.get("bookmarkMappings");
    case "mappings_restore":
      await chrome.storage.local.set({bookmarkMappings: payload.bookmarkMappings || {}});
      return {};
    default: throw new Error("unsupported-operation");
  }
}

if (typeof module !== "undefined") module.exports = {exportCookies, importCookies, restoreCookies,
  exportBookmarks, importBookmarks, operate};
