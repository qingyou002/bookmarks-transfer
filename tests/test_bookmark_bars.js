const assert = require("node:assert/strict");
const {fakeBrowser} = require("./test_extensions.js");

async function plan(browser, tree) {
  const bar = (await browser.context.exportBookmarkBars()).bars[0];
  return {barId: bar.id, expected: await browser.context.barFingerprint(bar), tree};
}

async function main() {
  const browser = fakeBrowser(), c = browser.context;
  const folder = await c.chrome.bookmarks.create({parentId: "1", title: "F"});
  const extra = await c.chrome.bookmarks.create({parentId: "1", title: "删除", url: "https://delete.invalid/"});
  const outside = await c.chrome.bookmarks.create({parentId: "2", title: "其他", url: "https://other.invalid/"});
  const desired = [{key: "folder", targetId: folder.id, title: "文件夹改名", children: [
    {key: "a", targetId: "3", title: "网址改名", url: "https://existing.invalid/"},
    {key: "b", targetId: null, title: "中文🙂", url: "https://new.invalid/"}
  ]}];
  const result = await c.applyBookmarkBar(await plan(browser, desired));
  assert.equal(result.complete, true);
  assert.equal(result.counts.added, 1);
  assert.equal(result.counts.deleted, 1);
  assert.equal(browser.nodes.get("3").parentId, folder.id);
  assert.ok(!browser.nodes.has(extra.id));
  assert.ok(browser.nodes.has(outside.id));
  assert.equal(result.bar.children[0].children[1].title, "中文🙂");
  desired[0].children[1].targetId = result.nodeIds.b;
  const repeated = await c.applyBookmarkBar(await plan(browser, desired));
  assert.equal(repeated.complete, true);
  assert.equal(repeated.counts.added, 0);
  assert.equal(repeated.counts.updated, 0);
  assert.equal(repeated.counts.deleted, 0);
  const reverseExport = JSON.stringify(await c.exportBookmarkBars());
  assert.ok(reverseExport.includes("new.invalid"));

  // A user edit between comparison and execution blocks all mutations.
  const stale = await plan(browser, []);
  await c.chrome.bookmarks.create({parentId: "1", title: "并发新增", url: "https://concurrent.invalid/"});
  const size = browser.nodes.size;
  const refused = await c.applyBookmarkBar(stale);
  assert.equal(refused.reason, "stale-snapshot");
  assert.equal(browser.nodes.size, size);

  // Failing a non-destructive step never proceeds into deletion.
  const broken = fakeBrowser(), bc = broken.context;
  const snapshot = await plan(broken, [{key: "new", title: "新", url: "https://new.invalid"}]);
  bc.chrome.bookmarks.create = async () => { throw new Error("test-failure"); };
  const failed = await bc.applyBookmarkBar(snapshot);
  assert.equal(failed.complete, false);
  assert.equal(failed.counts.failed, 1);
  assert.ok(broken.nodes.has("3"));

  const managed = fakeBrowser(), mc = managed.context;
  managed.nodes.get("3").unmodifiable = "managed";
  await assert.rejects(mc.applyBookmarkBar(await plan(managed, [])), /managed-node/);
  assert.ok(managed.nodes.has("3"));
  const same = [{key: "existing", targetId: "3", title: "existing", url: "https://existing.invalid/"}];
  assert.equal((await mc.applyBookmarkBar(await plan(managed, same))).complete, true);

  // Reverse a historical parent-child relationship while reusing both folders.
  const nested = fakeBrowser(), nc = nested.context;
  const a = await nc.chrome.bookmarks.create({parentId: "1", title: "A"});
  const b = await nc.chrome.bookmarks.create({parentId: a.id, title: "B"});
  const flipped = [{key: "b", targetId: b.id, title: "B", children: [
    {key: "a", targetId: a.id, title: "A", children: []}
  ]}];
  const flippedResult = await nc.applyBookmarkBar(await plan(nested, flipped));
  assert.equal(flippedResult.complete, true);
  assert.equal(nested.nodes.get(a.id).parentId, b.id);
  assert.equal(flippedResult.counts.added, 0);

  // Moving a retained folder out of a deleted ancestor must not double-count
  // its separately deleted children.
  const prune = fakeBrowser(), pc = prune.context;
  const removedParent = await pc.chrome.bookmarks.create({parentId: "1", title: "删父目录"});
  const retainedFolder = await pc.chrome.bookmarks.create({parentId: removedParent.id, title: "保留目录"});
  await pc.chrome.bookmarks.create({parentId: retainedFolder.id, title: "删网址", url: "https://remove.invalid/"});
  const pruned = await pc.applyBookmarkBar(await plan(prune, [
    {key: "kept", targetId: retainedFolder.id, title: "保留目录", children: []}
  ]));
  assert.equal(pruned.complete, true);
  assert.equal(pruned.counts.deleted, 3);

  // A new descendant arriving during upserts must survive a planned folder deletion.
  const concurrent = fakeBrowser(), cc = concurrent.context;
  const doomed = await cc.chrome.bookmarks.create({parentId: "1", title: "待删"});
  const pending = await plan(concurrent, [{key: "existing", targetId: "3", title: "改名", url: "https://existing.invalid/"}]);
  const update = cc.chrome.bookmarks.update;
  cc.chrome.bookmarks.update = async (...args) => {
    await cc.chrome.bookmarks.create({parentId: doomed.id, title: "用户刚新增", url: "https://user-new.invalid/"});
    return update(...args);
  };
  const concurrentResult = await cc.applyBookmarkBar(pending);
  assert.equal(concurrentResult.complete, false);
  assert.equal(concurrentResult.counts.deleted, 0);
  assert.ok(concurrent.nodes.has(doomed.id));

  const scope = fakeBrowser(), sc = scope.context;
  const scoped = await plan(scope, [{key: "existing", targetId: "3", title: "改名", url: "https://existing.invalid/"}]);
  const get = sc.chrome.bookmarks.get;
  let relocated = false;
  sc.chrome.bookmarks.get = async id => {
    if (id === "3" && !relocated) {relocated = true; await sc.chrome.bookmarks.move("3", {parentId: "2"});}
    return get(id);
  };
  assert.equal((await sc.applyBookmarkBar(scoped)).complete, false);
  assert.equal(scope.nodes.get("3").title, "existing");
  assert.equal(scope.nodes.get("3").parentId, "2");

  const empty = fakeBrowser(), ec = empty.context;
  const cleared = await ec.applyBookmarkBar(await plan(empty, []));
  assert.equal(cleared.complete, true);
  assert.equal(cleared.counts.deleted, 1);
  const secondBar = await ec.chrome.bookmarks.create({parentId: "0", title: "账号栏"});
  empty.nodes.get(secondBar.id).folderType = "bookmarks-bar";
  empty.nodes.get(secondBar.id).syncing = true;
  assert.equal((await ec.exportBookmarkBars()).bars.length, 2);

  // Python/JS fingerprints must match even with Unicode and ignored date/index fields.
  const unicode = {id: "1", title: "书签栏", children: [{id: "10", title: "中文🙂", url: "https://a.invalid", index: 9}]};
  const {spawnSync} = require("node:child_process"), path = require("node:path");
  const python = spawnSync(path.join(__dirname, "../.venv/Scripts/python.exe"), ["-X", "utf8", "-c",
    "import json,sys; from browser_transfer.bookmarks import fingerprint; print(fingerprint(json.load(sys.stdin)))"],
    {input: JSON.stringify(unicode), encoding: "utf8", cwd: path.join(__dirname, "..")});
  assert.equal(python.status, 0, python.stderr);
  assert.equal(await ec.barFingerprint(unicode), python.stdout.trim());
  console.log("Bookmark bar checks passed (apply, repeat, reverse export, stale snapshot, concurrent edits, subtree scope, failures, managed nodes, folder reversal, deletion counts, multiple bars, fingerprints).");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
