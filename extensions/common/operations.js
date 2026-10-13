"use strict";

function newCounts() { return {added: 0, updated: 0, skipped: 0, failed: 0}; }

function barShape(node) {
  return [String(node.id), node.title || "", node.url ?? null, node.unmodifiable ?? null,
    (node.children || []).map(barShape)];
}

async function barFingerprint(node) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(barShape(node))));
  return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("");
}

async function exportBookmarkBars() {
  const tree = await chrome.bookmarks.getTree();
  const bars = [];
  function visit(node) {
    if (node.folderType === "bookmarks-bar") bars.push(node);
    for (const child of node.children || []) visit(child);
  }
  tree.forEach(visit);
  // Legacy Chromium has a single local bar with ID 1. Never guess another root.
  if (!bars.length) {
    const legacy = tree[0]?.children?.find(n => n.id === "1" && !n.url);
    if (legacy) bars.push(legacy);
  }
  return {bars};
}

async function applyBookmarkBar(payload) {
  const counts = {...newCounts(), deleted: 0};
  const before = (await exportBookmarkBars()).bars.find(b => b.id === payload.barId);
  if (!before || before.unmodifiable || await barFingerprint(before) !== payload.expected) {
    return {counts: {...counts, failed: 1}, complete: false, reason: "stale-snapshot"};
  }
  const old = new Map(), oldParent = new Map();
  function index(node, parent) {
    old.set(node.id, node);
    oldParent.set(node.id, parent);
    (node.children || []).forEach(c => index(c, node.id));
  }
  index(before, null);
  const retained = new Set([before.id]), keys = new Set(), wanted = new Map();
  function validate(nodes, parent, depth = 0) {
    if (!Array.isArray(nodes) || depth > 100) throw new Error("invalid-bar-plan");
    nodes.forEach((node, position) => {
      if (!node || typeof node.key !== "string" || keys.has(node.key) ||
          typeof node.title !== "string" || (node.url !== undefined && typeof node.url !== "string")) {
        throw new Error("invalid-bar-plan");
      }
      keys.add(node.key);
      if (node.targetId != null) {
        const previous = old.get(node.targetId);
        if (!previous || retained.has(node.targetId) || Boolean(previous.url) !== Boolean(node.url)) {
          throw new Error("invalid-target-node");
        }
        retained.add(node.targetId);
        wanted.set(node.targetId, node);
        if (previous.unmodifiable && (previous.title !== node.title || previous.url !== node.url ||
            oldParent.get(previous.id) !== parent || beforeIndex(previous.id) !== position)) {
          throw new Error("managed-node");
        }
      }
      if (node.url === undefined) validate(node.children || [], node.targetId || null, depth + 1);
      else if (node.children !== undefined) throw new Error("invalid-bar-plan");
    });
  }
  function beforeIndex(id) {
    return (old.get(oldParent.get(id))?.children || []).findIndex(n => n.id === id);
  }
  validate(payload.tree, before.id);
  for (const [id, node] of old) {
    if (!retained.has(id) && node.unmodifiable) throw new Error("managed-node");
  }
  // Move ancestors out of descendants first. Mixed per-item choices can reverse
  // an existing folder relationship, so destination ancestors must be ready.
  const nodeIds = {};
  async function currentInBar(id) {
    const [node] = await chrome.bookmarks.get(id);
    if (!node) throw new Error("node-disappeared");
    let cursor = node, visited = new Set();
    while (cursor.id !== before.id) {
      if (!cursor.parentId || visited.has(cursor.id)) throw new Error("node-left-bar");
      visited.add(cursor.id);
      [cursor] = await chrome.bookmarks.get(cursor.parentId);
      if (!cursor) throw new Error("node-left-bar");
    }
    return node;
  }
  async function upsert(nodes, parentId) {
    for (let position = 0; position < nodes.length; position++) {
      const node = nodes[position];
      let id = node.targetId;
      if (id == null) {
        const created = await chrome.bookmarks.create({parentId, index: position,
          title: node.title, ...(node.url === undefined ? {} : {url: node.url})});
        id = created.id;
        counts.added++;
      } else {
        const previous = old.get(id), changes = {};
        const current = await currentInBar(id);
        if ((current.title || "") !== (previous.title || "") || current.url !== previous.url) {
          throw new Error("concurrent-edit");
        }
        if ((previous.title || "") !== node.title) changes.title = node.title;
        if (node.url !== undefined && previous.url !== node.url) changes.url = node.url;
        if (Object.keys(changes).length) {
          await chrome.bookmarks.update(id, changes);
        }
        // Read the current index, because earlier creates/moves shift siblings.
        if (current.parentId !== parentId || current.index !== position) {
          await chrome.bookmarks.move(id, {parentId, index: position});
        }
        const changed = (previous.title || "") !== node.title || previous.url !== node.url ||
          oldParent.get(id) !== parentId || beforeIndex(id) !== position;
        counts[changed ? "updated" : "skipped"]++;
      }
      nodeIds[node.key] = id;
      await upsert(node.children || [], id);
    }
  }
  try {
    // Detach retained folders whose new parent currently lies within them.
    // Only folders involved in a reversal move temporarily to the bar root.
    for (const [id, node] of wanted) {
      if (node.url !== undefined || old.get(id).unmodifiable) continue;
      function destinationParent(nodes, parent) {
        for (const n of nodes) {
          if (n.targetId === id) return parent;
          const found = destinationParent(n.children || [], n.targetId || null);
          if (found !== undefined) return found;
        }
      }
      let parent = destinationParent(payload.tree, before.id), cursor = parent;
      const visited = new Set();
      while (cursor && !visited.has(cursor)) {
        visited.add(cursor);
        if (cursor === id && parent !== oldParent.get(id)) {
          // Detach the destination descendant instead, making the later move valid.
          await currentInBar(parent);
          await chrome.bookmarks.move(parent, {parentId: before.id});
          break;
        }
        cursor = oldParent.get(cursor);
      }
    }
    await upsert(payload.tree, before.id);
    // No deletion begins if any create/update/move failed.
    const missing = new Set([...old.keys()].filter(id => !retained.has(id)));
    function plannedRemoval(node) {
      return {...node, children: (node.children || []).filter(c => missing.has(c.id)).map(plannedRemoval)};
    }
    for (const id of missing) {
      if (missing.has(oldParent.get(id))) continue;
      const node = old.get(id);
      await currentInBar(id);
      const [current] = await chrome.bookmarks.getSubTree(id);
      // Never delete newly added or edited descendants that were not previewed.
      if (!current || await barFingerprint(current) !== await barFingerprint(plannedRemoval(node))) {
        throw new Error("concurrent-edit");
      }
      if (node.url === undefined) await chrome.bookmarks.removeTree(id);
      else await chrome.bookmarks.remove(id);
      function removedCount(n) {
        return (missing.has(n.id) ? 1 : 0) + (n.children || []).reduce((sum, c) => sum + removedCount(c), 0);
      }
      counts.deleted += removedCount(current);
    }
    const bar = (await exportBookmarkBars()).bars.find(b => b.id === before.id);
    function content(nodes) {
      return nodes.map(n => [n.title || "", n.url ?? null, content(n.children || [])]);
    }
    if (!bar || JSON.stringify(content(bar.children || [])) !== JSON.stringify(content(payload.tree))) {
      throw new Error("verification-failed");
    }
    return {counts, complete: true, bar, nodeIds};
  } catch (_) {
    counts.failed++;
    return {counts, complete: false, reason: "write-failed"};
  }
}

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
    case "ping": return {browser: BROWSER_KIND, bookmarkBarVersion: 1};
    case "cookies_export": return exportCookies();
    case "cookies_import": return importCookies(payload);
    case "cookies_restore": return restoreCookies(payload);
    case "bookmarks_export": return exportBookmarks();
    case "bookmarks_import": return importBookmarks(payload);
    case "bookmarks_bar_export": return exportBookmarkBars();
    case "bookmarks_bar_apply": return applyBookmarkBar(payload);
    case "mappings_export": return chrome.storage.local.get("bookmarkMappings");
    case "mappings_restore":
      await chrome.storage.local.set({bookmarkMappings: payload.bookmarkMappings || {}});
      return {};
    default: throw new Error("unsupported-operation");
  }
}

if (typeof module !== "undefined") module.exports = {exportCookies, importCookies, restoreCookies,
  exportBookmarks, importBookmarks, exportBookmarkBars, applyBookmarkBar, barFingerprint, operate};
