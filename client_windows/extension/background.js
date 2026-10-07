// MV3 service worker：维持扩展存活，定时向本地客户端发心跳
// 心跳通过 /health 触发，客户端据此判断扩展是否在线
const API_BASE = "http://127.0.0.1:8765";
const HEARTBEAT_INTERVAL_MS = 10000;

async function heartbeat() {
  try {
    await fetch(`${API_BASE}/health`, { method: "GET" });
  } catch (e) {
    // 客户端未启动，静默
  }
}

// 监听来自 content script 的运行时消息（预留扩展，主流程靠定时轮询）
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.type === "close-tab" && message.domain) {
    const pattern = `*://${message.domain}/*`;
    chrome.tabs.query({ url: pattern }, function (tabs) {
      const ids = (tabs || []).map(t => t.id);
      if (ids.length > 0) {
        chrome.tabs.remove(ids, () => sendResponse({ ok: true, closed: ids.length }));
      } else {
        sendResponse({ ok: true, closed: 0 });
      }
    });
    return true; // 异步响应
  }
  sendResponse({ ok: false });
});

// 启动心跳定时器
chrome.runtime.onStartup.addListener(heartbeat);
chrome.runtime.onInstalled.addListener(heartbeat);
setInterval(heartbeat, HEARTBEAT_INTERVAL_MS);
