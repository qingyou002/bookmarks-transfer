/* Real Chromium API acceptance, using disposable profiles and synthetic URLs only.
 * Never calls the application's all-browser close operation.
 */
"use strict";
const fs = require("node:fs"), path = require("node:path"), assert = require("node:assert/strict");
const {spawn, spawnSync} = require("node:child_process");
const ROOT = path.resolve(__dirname, "..");
const TEST_ROOT = path.join(ROOT, "runtime", "acceptance");
const PYTHON = path.join(ROOT, ".venv", "Scripts", "python.exe");
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

class CDP {
  constructor(socket) {
    this.socket = socket; this.sequence = 0; this.pending = new Map();
    socket.addEventListener("message", event => {
      const response = JSON.parse(event.data);
      const pending = this.pending.get(response.id);
      if (pending) {
        this.pending.delete(response.id); clearTimeout(pending.timer);
        if (response.error) pending.reject(new Error(response.error.message));
        else pending.resolve(response.result);
      }
    });
    socket.addEventListener("close", () => {
      for (const pending of this.pending.values()) {clearTimeout(pending.timer); pending.reject(new Error("Browser connection closed"));}
      this.pending.clear();
    });
  }
  static pipe(child) {
    const listeners = {}, outgoing = child.stdio[3], incoming = child.stdio[4];
    let buffer = Buffer.alloc(0);
    const transport = {
      addEventListener(type, callback) {listeners[type] = callback;},
      send(message) {outgoing.write(message + "\0");},
      close() {outgoing.destroy(); incoming.destroy();}
    };
    incoming.on("data", chunk => {
      buffer = Buffer.concat([buffer, chunk]);
      let end;
      while ((end = buffer.indexOf(0)) !== -1) {
        listeners.message?.({data: buffer.subarray(0, end).toString("utf8")});
        buffer = buffer.subarray(end + 1);
      }
    });
    incoming.on("close", () => listeners.close?.());
    outgoing.on("error", () => listeners.close?.());
    return new CDP(transport);
  }
  call(method, params = {}, sessionId = null) {
    return new Promise((resolve, reject) => {
      const id = ++this.sequence;
      const timer = setTimeout(() => {this.pending.delete(id); reject(new Error(method + " timed out"));}, 10000);
      this.pending.set(id, {resolve, reject, timer});
      this.socket.send(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})}));
    });
  }
  async evaluate(expression, sessionId = null) {
    const response = await this.call("Runtime.evaluate", {expression, awaitPromise: true, returnByValue: true}, sessionId);
    if (response.exceptionDetails) throw new Error(response.exceptionDetails.exception?.description || "Browser evaluation failed");
    return response.result.value;
  }
  close() {this.socket.close();}
}

const instances = [];
async function startBrowser(browser, executable, extensionId) {
  fs.mkdirSync(TEST_ROOT, {recursive: true});
  const directory = fs.mkdtempSync(path.join(TEST_ROOT, browser.toLowerCase() + "-"));
  const extension = path.join(ROOT, "extensions", browser.toLowerCase());
  const child = spawn(executable, ["--headless=new", "--no-first-run", "--no-default-browser-check",
    "--disable-background-networking", "--disable-sync", "--disable-component-update",
    "--enable-unsafe-extension-debugging",
    "--remote-debugging-pipe", "--profile-directory=Default",
    "--user-data-dir=" + directory, "about:blank"],
    {windowsHide: true, stdio: ["ignore", "ignore", "pipe", "pipe", "pipe"]});
  const instance = {browser, child, directory};
  instances.push(instance);
  child.stderr.on("data", () => {});
  child.on("error", error => {instance.error = error;});
  instance.browserCDP = CDP.pipe(child);
  instance.version = (await instance.browserCDP.call("Browser.getVersion")).product;
  const manager = await instance.browserCDP.call("Target.createTarget", {url: "chrome://extensions/"});
  const managerSession = await instance.browserCDP.call("Target.attachToTarget", {targetId: manager.targetId, flatten: true});
  for (let i = 0; i < 30; i++) {
    if (await instance.browserCDP.evaluate("typeof chrome.developerPrivate?.updateProfileConfiguration === 'function'", managerSession.sessionId)) break;
    await delay(100);
  }
  await instance.browserCDP.evaluate("chrome.developerPrivate.updateProfileConfiguration({inDeveloperMode:true})", managerSession.sessionId);
  await delay(200);
  const installed = await instance.browserCDP.call("Extensions.loadUnpacked", {path: extension});
  assert.equal(installed.id, extensionId);
  await delay(1200);
  const {targetId} = await instance.browserCDP.call("Target.createTarget", {url: "chrome-extension://" + extensionId + "/bridge.html"});
  const {sessionId} = await instance.browserCDP.call("Target.attachToTarget", {targetId, flatten: true});
  instance.page = {evaluate: expression => instance.browserCDP.evaluate(expression, sessionId), close() {}};
  for (let i = 0; i < 30; i++) {
    if (await instance.page.evaluate("typeof operate === 'function'")) return instance;
    await delay(100);
  }
  const diagnostic = await instance.page.evaluate("JSON.stringify({url:location.href,ready:document.readyState,text:document.body.innerText.slice(0,800),scripts:[...document.scripts].map(s=>s.src)})");
  const extensions = await instance.browserCDP.call("Extensions.getExtensions");
  const settings = await instance.browserCDP.call("Target.createTarget", {url: "chrome://extensions/"});
  const attached = await instance.browserCDP.call("Target.attachToTarget", {targetId: settings.targetId, flatten: true});
  await delay(500);
  const details = await instance.browserCDP.evaluate("(async()=>({profile:await new Promise(r=>chrome.developerPrivate.getProfileConfiguration(r)),extensions:await new Promise(r=>chrome.developerPrivate.getExtensionsInfo({includeDisabled:true},r))}))()", attached.sessionId);
  throw new Error(browser + " test extension did not become ready: " + diagnostic + " " + JSON.stringify(extensions) + " " + JSON.stringify(details));
}

function pythonComparison(source, target, state = null, sourceKey = "Edge", targetKey = "Chrome", result = null, decision = null) {
  const code = "import json,sys; from browser_transfer.bookmarks import Comparison; d=json.load(sys.stdin); c=Comparison(d['source'],d['target'],d['sourceKey'],d['targetKey'],d['state']); r=d['result']; print(json.dumps({'plan':c.resolve(d['decision'] or {'base':'source'}),'differences':c.differences,'state':c.state_after(r['bar'],r['nodeIds']) if r else None},ensure_ascii=False))";
  const process = spawnSync(PYTHON, ["-X", "utf8", "-c", code], {cwd: ROOT, windowsHide: true, encoding: "utf8",
    input: JSON.stringify({source, target, state, sourceKey, targetKey, result, decision})});
  if (process.status !== 0) throw new Error(process.stderr || "Comparison subprocess failed");
  return JSON.parse(process.stdout);
}

async function main() {
  const settings = spawnSync(PYTHON, ["-c", "import json; from browser_transfer.windows import discover; from browser_transfer.bridge import EXTENSION_IDS; print(json.dumps({b:{'exe':str(ps[0].executable),'id':EXTENSION_IDS[b]} for b,ps in discover().items() if ps}))"],
    {cwd: ROOT, windowsHide: true, encoding: "utf8"});
  if (settings.status !== 0) throw new Error("Cannot discover installed browsers");
  const config = JSON.parse(settings.stdout);
  const edge = await startBrowser("Edge", config.Edge.exe, config.Edge.id);
  const chrome = await startBrowser("Chrome", config.Chrome.exe, config.Chrome.id);
  const bars = async instance => (await instance.page.evaluate("exportBookmarkBars()")).bars;
  const left = (await bars(edge)).find(b => !b.unmodifiable), right = (await bars(chrome)).find(b => !b.unmodifiable);
  assert.equal(left.children.length, 0, "Temporary source profile must start empty");
  assert.equal(right.children.length, 0, "Temporary target profile must start empty");
  await edge.page.evaluate(`(async () => {const f=await chrome.bookmarks.create({parentId:${JSON.stringify(left.id)},title:'资料'}); await chrome.bookmarks.create({parentId:f.id,title:'中文🙂',url:'https://source.example.invalid/'}); await chrome.bookmarks.create({parentId:${JSON.stringify(left.id)},title:'B',url:'https://b.example.invalid/'});})()`);
  await chrome.page.evaluate(`(async () => {const f=await chrome.bookmarks.create({parentId:${JSON.stringify(right.id)},title:'资料'}); await chrome.bookmarks.create({parentId:f.id,title:'旧名称',url:'https://source.example.invalid/'}); await chrome.bookmarks.create({parentId:${JSON.stringify(right.id)},title:'仅目标',url:'https://only-target.example.invalid/'});})()`);
  let source = (await bars(edge)).find(b => b.id === left.id), target = (await bars(chrome)).find(b => b.id === right.id);
  let compared = pythonComparison(source, target);
  const result = await chrome.page.evaluate("applyBookmarkBar(" + JSON.stringify(compared.plan) + ")");
  assert.equal(result.complete, true, JSON.stringify(result));
  assert.equal(result.counts.deleted, 1);
  const state = pythonComparison(source, target, null, "Edge", "Chrome", result).state;
  target = (await bars(chrome)).find(b => b.id === right.id);
  compared = pythonComparison(source, target, state);
  assert.equal(compared.plan.preview.added, 0);
  assert.equal(compared.plan.preview.updated, 0);
  assert.equal(compared.plan.preview.deleted, 0);
  assert.equal((await chrome.page.evaluate("applyBookmarkBar(" + JSON.stringify(compared.plan) + ")")).complete, true);
  const reverse = pythonComparison(target, source, state, "Chrome", "Edge");
  assert.equal(reverse.plan.preview.added, 0);
  assert.equal((await edge.page.evaluate("applyBookmarkBar(" + JSON.stringify(reverse.plan) + ")")).complete, true);
  const sourceBookmark = source.children[0].children[0].id;
  await edge.page.evaluate("chrome.bookmarks.remove(" + JSON.stringify(sourceBookmark) + ")");
  source = (await bars(edge)).find(b => b.id === left.id);
  const deletion = pythonComparison(source, target, state);
  assert.ok(deletion.differences.some(d => d.label === "来源已删除"));
  assert.equal(deletion.plan.preview.deleted, 1);
  const preserved = pythonComparison(source, target, state, "Edge", "Chrome", null, {base: "source",
    choices: {[deletion.differences.find(d => d.label === "来源已删除").key]: "target"}});
  assert.equal(preserved.plan.preview.deleted, 0);
  assert.equal((await chrome.page.evaluate("applyBookmarkBar(" + JSON.stringify(deletion.plan) + ")")).complete, true);
  console.log(JSON.stringify({real_browser_api: "passed", browsers: ["Edge", "Chrome"], directions: "both",
    versions: {Edge: edge.version, Chrome: chrome.version},
    scenarios: ["main bar layout", "rename", "delete", "preserve exception", "repeat", "reverse", "Unicode"]}));
}

async function cleanup() {
  for (const instance of instances.reverse()) {
    if (instance.page) instance.page.close();
    if (instance.browserCDP) {
      try {await instance.browserCDP.call("Browser.close");} catch (_) {}
      instance.browserCDP.close();
    }
    for (let i = 0; i < 25 && instance.child.exitCode === null; i++) await delay(200);
    if (instance.child.exitCode === null) spawnSync("taskkill.exe", ["/PID", String(instance.child.pid), "/T", "/F"], {windowsHide: true, stdio: "ignore"});
    // Check containment before recursively deleting a Windows test directory.
    const absolute = path.resolve(instance.directory);
    if (!absolute.toLowerCase().startsWith((path.resolve(TEST_ROOT) + path.sep).toLowerCase())) throw new Error("Unsafe test cleanup path");
    fs.rmSync(absolute, {recursive: true, force: true, maxRetries: 5, retryDelay: 200});
  }
}

main().catch(error => {console.error(error.message); process.exitCode = 1;}).finally(cleanup);
