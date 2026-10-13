/* Behavioral tests against in-memory browser API doubles. No live profile is accessed. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const {webcrypto} = require("node:crypto");

function fakeBrowser() {
  let next = 10;
  const storage = {};
  const nodes = new Map([
    ["0", {id: "0", title: "root"}],
    ["1", {id: "1", parentId: "0", title: "bar", folderType: "bookmarks-bar"}],
    ["2", {id: "2", parentId: "0", title: "other", folderType: "other"}],
    ["3", {id: "3", parentId: "1", title: "existing", url: "https://existing.invalid/"}]
  ]);
  const cookies = new Map();
  const clone = obj => JSON.parse(JSON.stringify(obj));
  function siblings(parentId) {
    return [...nodes.values()].filter(n => n.parentId === parentId).sort((a, b) => a.index - b.index);
  }
  for (const id of ["0", "1", "2"]) siblings(id).forEach((n, i) => { n.index = i; });
  function place(node, parentId, position) {
    const oldParent = node.parentId;
    const children = siblings(parentId).filter(n => n.id !== node.id);
    const index = position === undefined ? children.length : Math.min(position, children.length);
    children.splice(index, 0, node);
    node.parentId = parentId;
    children.forEach((n, i) => { n.index = i; });
    if (oldParent !== parentId) siblings(oldParent).forEach((n, i) => { n.index = i; });
  }
  function tree(id) {
    const node = clone(nodes.get(id));
    if (!node.url) node.children = siblings(id).map(n => tree(n.id));
    return node;
  }
  const chrome = {
    storage: {local: {
      get: async key => ({[key]: clone(storage[key] || {})}),
      set: async values => Object.assign(storage, clone(values)),
    }},
    bookmarks: {
      getTree: async () => [tree("0")],
      getSubTree: async id => nodes.has(id) ? [tree(id)] : [],
      get: async id => nodes.has(id) ? [clone(nodes.get(id))] : [],
      create: async values => {
        const node = {id: String(next++), ...values};
        nodes.set(node.id, node); place(node, values.parentId, values.index); return clone(node);
      },
      update: async (id, values) => {
        if (nodes.get(id).unmodifiable) throw new Error("managed-node");
        Object.assign(nodes.get(id), values); return clone(nodes.get(id));
      },
      move: async (id, values) => {
        if (nodes.get(id).unmodifiable) throw new Error("managed-node");
        let ancestor = values.parentId;
        while (ancestor) {
          if (ancestor === id) throw new Error("cycle");
          ancestor = nodes.get(ancestor)?.parentId;
        }
        place(nodes.get(id), values.parentId, values.index); return clone(nodes.get(id));
      },
      remove: async id => {
        if (nodes.get(id).unmodifiable) throw new Error("managed-node");
        const parent = nodes.get(id).parentId;
        nodes.delete(id); siblings(parent).forEach((n, i) => { n.index = i; });
      },
      removeTree: async id => {
        for (const child of siblings(id)) {
          if (child.url) await chrome.bookmarks.remove(child.id);
          else await chrome.bookmarks.removeTree(child.id);
        }
        await chrome.bookmarks.remove(id);
      },
    },
    cookies: {
      getAll: async () => clone([...cookies.values()]),
      set: async details => {
        if (details.name === "rejected") throw new Error("fixture-rejection");
        const cookie = {domain: details.domain || new URL(details.url).hostname,
          hostOnly: !details.domain, path: details.path, name: details.name, value: details.value,
          secure: details.secure, httpOnly: details.httpOnly, sameSite: details.sameSite,
          session: details.expirationDate === undefined, storeId: "0"};
        if (details.expirationDate !== undefined) cookie.expirationDate = details.expirationDate;
        if (details.partitionKey) cookie.partitionKey = details.partitionKey;
        const key = context.cookieKey(cookie);
        if (details.expirationDate === 0) cookies.delete(key);
        else cookies.set(key, cookie);
        return clone(cookie);
      },
    },
  };
  const context = vm.createContext({chrome, crypto: webcrypto, TextEncoder, TextDecoder, Uint8Array,
    URL, Map, Set, Date, JSON, Boolean, Object, console, BROWSER_KIND: "Edge", btoa, atob});
  for (const file of ["protocol.js", "operations.js"]) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, "../extensions/common", file), "utf8"), context);
  }
  return {context, nodes, cookies, storage};
}

async function main() {
  const browser = fakeBrowser(), c = browser.context;
  const namespace = "a".repeat(64);
  const source = {namespace, sourceBrowser: "Chrome", tree: [{id: "0", children: [
    {id: "1", title: "收藏夹栏", children: [{id: "9", title: "测试网址", url: "https://source.invalid/"}]}
  ]}]};
  assert.equal((await c.importBookmarks(source)).counts.added, 2);
  const size = browser.nodes.size;
  assert.equal((await c.importBookmarks(source)).counts.added, 0);
  assert.equal(browser.nodes.size, size);
  source.tree[0].children[0].children[0].title = "改名";
  assert.equal((await c.importBookmarks(source)).counts.updated, 1);
  assert.equal(browser.nodes.get("3").title, "existing");
  const exported = await c.exportBookmarks();
  const flattened = JSON.stringify(exported);
  assert.ok(flattened.includes("existing.invalid"));
  assert.ok(!flattened.includes("source.invalid"), "reverse transfer must not echo managed copies");
  const base = {domain: ".example.invalid", hostOnly: false, path: "/", name: "session", value: "source",
    secure: true, httpOnly: true, sameSite: "no_restriction", session: true, storeId: "9"};
  const partition = {...base, name: "partition", partitionKey: {topLevelSite: "https://top.invalid", hasCrossSiteAncestor: true}};
  const incoming = [base, partition];
  assert.equal((await c.importCookies({cookies: incoming})).counts.added, 2);
  assert.equal((await c.importCookies({cookies: incoming})).counts.skipped, 2);
  assert.equal(c.cookieDetails(base).storeId, undefined);
  assert.equal(c.cookieDetails({...base, hostOnly: true}).domain, undefined);
  const ordinaryKey = c.cookieKey(base), partitionKey = c.cookieKey(partition);
  assert.notEqual(ordinaryKey, partitionKey);
  assert.equal(browser.cookies.get(partitionKey).partitionKey.hasCrossSiteAncestor, true);
  assert.equal((await c.importCookies({cookies: [{...base, value: "changed"}]})).counts.updated, 1);
  const other = {...base, name: "independent", value: "keep"};
  await c.importCookies({cookies: [other]});
  const restored = await c.restoreCookies({before: [{...base, value: "original"}], incoming});
  assert.equal(restored.counts.failed, 0);
  assert.equal(browser.cookies.get(ordinaryKey).value, "original");
  assert.ok(!browser.cookies.has(partitionKey));
  assert.equal(browser.cookies.get(c.cookieKey(other)).value, "keep");
  const failures = await c.importCookies({cookies: [{...base, name: "rejected"},
    {...base, session: false, expirationDate: 1, name: "expired"}]});
  assert.equal(failures.counts.failed, 1);
  assert.equal(failures.counts.skipped, 1);
  const messages = c.wireFrames({value: "中文🙂".repeat(100000)});
  const receiver = vm.runInContext("new WireAssembler()", c);
  let value;
  for (const frame of [...messages].reverse()) value = receiver.feed(frame) || value;
  assert.equal(value.value, "中文🙂".repeat(100000));
  console.log("Extension behavior checks passed (bookmarks, reverse transfer, cookies, restore, large messages).");
}
if (require.main === module) main().catch(error => { console.error(error); process.exitCode = 1; });
module.exports = {fakeBrowser};
