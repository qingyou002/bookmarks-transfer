"use strict";

const params = new URLSearchParams(location.search);
const statusNode = document.getElementById("status");
let port = null;
let serial = Promise.resolve();

function connectDesktop() {
  const run = params.get("session"), profile = params.get("profile");
  if (!run || !profile) {
    statusNode.textContent = "请从桌面工具开始迁移；它会自动打开对应配置的连接页面。";
    return;
  }
  if (port) { port.disconnect(); port = null; }
  const assembler = new WireAssembler();
  const active = chrome.runtime.connectNative("com.local.browser_transfer");
  port = active;
  function send(message) {
    for (const frame of wireFrames(message)) active.postMessage(frame);
  }
  active.onMessage.addListener(frame => {
    try {
      const request = assembler.feed(frame);
      if (!request) return;
      // Writes are serialized; errors returned to desktop contain no personal data.
      serial = serial.then(async () => {
        statusNode.textContent = `${BROWSER_KIND} 已连接，正在处理迁移任务。`;
        try {
          const result = await operate(request.operation, request.payload);
          send({id: request.id, ok: true, result});
          statusNode.textContent = `${BROWSER_KIND} 已连接，等待桌面工具。`;
        } catch (_) {
          send({id: request.id, ok: false, error: "browser-api-error"});
          statusNode.textContent = "某项操作失败，请查看桌面工具中的结果和备份。";
        }
      }).catch(() => { statusNode.textContent = "连接已断开，请从桌面工具重试。"; });
    } catch (_) { active.disconnect(); }
  });
  active.onDisconnect.addListener(() => {
    // Read runtime.lastError to suppress a console warning, without logging payloads.
    void chrome.runtime.lastError;
    if (port === active) port = null;
    statusNode.textContent = "当前连接已结束。如需继续，请从桌面工具重试或重新连接。";
  });
  send({type: "hello", run, profile});
  statusNode.textContent = `${BROWSER_KIND} 正在连接桌面工具…`;
}
document.getElementById("retry").addEventListener("click", connectDesktop);
connectDesktop();
