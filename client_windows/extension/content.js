// 内容脚本：周期性向本地客户端上报当前页面的 {domain, title, seconds}
// 不采集任何页面内容，只取 location.hostname 与 document.title。
// 与本机客户端通信：http://127.0.0.1:8765
//
// 注意：内容脚本运行在页面上下文，**无权访问 chrome.tabs**。
// 关闭标签页必须经 chrome.runtime.sendMessage 交给 background.js 执行。
(function () {
  const API_BASE = "http://127.0.0.1:8765";
  const REPORT_INTERVAL_MS = 5000;

  let lastReportAt = Date.now();

  function currentDomain() {
    try {
      return location.hostname || "";
    } catch (e) {
      return "";
    }
  }

  function currentTitle() {
    try {
      return document.title || "";
    } catch (e) {
      return "";
    }
  }

  // 只有「可见且聚焦」的标签才代表用户当前正在看的页面，
  // 避免后台标签同时上报造成域名互相覆盖。
  function isActiveTab() {
    return document.visibilityState === "visible" && document.hasFocus();
  }

  async function report() {
    if (!isActiveTab()) return;
    const now = Date.now();
    const seconds = Math.round((now - lastReportAt) / 1000);
    lastReportAt = now;
    const payload = {
      domain: currentDomain(),
      title: currentTitle(),
      seconds: seconds,
      ts: now
    };
    try {
      await fetch(`${API_BASE}/report`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
    } catch (e) {
      // 客户端未启动时静默失败，不抛出
    }
  }

  // 轮询关标签请求：客户端通过 /poll-close 让扩展主动拉取
  async function pollCloseRequest() {
    try {
      const resp = await fetch(`${API_BASE}/poll-close`, { method: "POST" });
      const data = await resp.json();
      const req = data && data.request;
      if (req && req.domain) {
        closeTabsByDomain(req.domain);
      }
    } catch (e) {
      // 静默
    }
  }

  // 交给 background service worker 执行（内容脚本无 chrome.tabs 权限）
  function closeTabsByDomain(domain) {
    if (!domain) return;
    try {
      chrome.runtime.sendMessage({ type: "close-tab", domain: domain });
    } catch (e) {
      // 扩展上下文失效（如已重载）时静默
    }
  }

  // 启动定时任务
  setInterval(report, REPORT_INTERVAL_MS);
  setInterval(pollCloseRequest, REPORT_INTERVAL_MS);
  // 焦点/可见性变化时立即上报一次，让客户端尽快感知标签切换
  window.addEventListener("focus", report);
  document.addEventListener("visibilitychange", report);
  // 启动时立即探测一次
  setTimeout(report, 1000);
  setTimeout(pollCloseRequest, 1500);
})();
