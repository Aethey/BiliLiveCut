// 在业务脚本之前接入同源会话；过期后原地登录，保留页面草稿且绝不重放写请求。
(() => {
  "use strict";
  const originalFetch = window.fetch.bind(window);
  let state = { enabled: false };
  let dialog;
  let message;
  let ready;
  let loginButton;
  let identity;
  let postponed = false;
  const domReady = document.readyState === "loading"
    ? new Promise(resolve => document.addEventListener("DOMContentLoaded", resolve, { once: true })) : Promise.resolve();
  async function status() {
    const response = await originalFetch("/auth/status", { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) throw new Error("无法读取登录状态，请稍后重试。");
    state = await response.json();
    if (loginButton) loginButton.hidden = state.authenticated;
    if (identity) identity.textContent = "后台账号：" + (state.username || "未登录");
    return state;
  }
  async function post(path, body) {
    const response = await originalFetch(path, {
      method: "POST", credentials: "same-origin", headers: {
        "Content-Type": "application/json", "X-BLC-CSRF": state.csrf_token || ""
      }, body: JSON.stringify(body)
    });
    const payload = await response.json();
    if (!response.ok) {
      if (response.status === 403 && response.headers.get("X-BLC-CSRF-Required") === "1") await status();
      const error = new Error(payload.detail || "登录操作失败。");
      error.status = response.status;
      throw error;
    }
    return payload;
  }
  async function promptLogin(force = false) {
    await domReady;
    state.authenticated = false;
    if (loginButton) loginButton.hidden = false;
    if (postponed && !force) return;
    postponed = false;
    if (dialog) { if (!dialog.open) dialog.showModal(); return; }
    dialog = document.createElement("dialog");
    dialog.className = "blc-auth-card";
    dialog.setAttribute("aria-labelledby", "blc-relogin-title");
    dialog.innerHTML = `<h1 id="blc-relogin-title">请重新登录</h1>
      <p>登录已过期、被撤销或服务已重启。当前页面和草稿仍保留，刚才的操作不会自动重试。</p>
      <p class="blc-session-message" role="status"></p>
      <form><label>账号<input name="username" autocomplete="username" maxlength="128" required /></label>
      <label>密码<input name="password" type="password" autocomplete="current-password" maxlength="1024" required /></label>
      <button type="submit">登录并返回当前页面</button>
      <button type="button" data-close>稍后登录</button></form>
      <p><a href="/auth/login" target="_blank" rel="noopener">首次设置或忘记密码</a></p>`;
    const form = dialog.querySelector("form");
    form.elements.username.value = state.username || "admin";
    const postpone = () => { postponed = true; form.elements.password.value = ""; };
    dialog.addEventListener("cancel", postpone);
    dialog.querySelector("[data-close]").addEventListener("click", () => { postpone(); dialog.close(); });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector("[type=submit]");
      if (button.disabled) return;
      button.disabled = true;
      const error = dialog.querySelector(".blc-session-message");
      try {
        await post("/auth/login", { username: form.elements.username.value, password: form.elements.password.value });
        form.elements.password.value = "";
        await status();
        dialog.close();
        postponed = false;
        loginButton.hidden = true;
        message.textContent = "已重新登录。未自动重试任何操作，请确认页面内容后手动继续。";
        window.dispatchEvent(new Event("blc:reauthenticated"));
      } catch (failure) { error.textContent = failure.message; }
      finally { button.disabled = false; }
    });
    document.body.appendChild(dialog);
    dialog.showModal();
    form.elements.password.focus();
  }
  async function toolbar() {
    await domReady;
    if (!state.enabled) return;
    const bar = document.createElement("nav");
    bar.className = "blc-session-bar";
    bar.setAttribute("aria-label", "后台账号");
    identity = document.createElement("span");
    identity.textContent = "后台账号：" + (state.username || "未登录");
    message = document.createElement("span");
    message.setAttribute("role", "status");
    const password = document.createElement("a");
    password.href = "/auth/password";
    password.textContent = "密码管理";
    loginButton = document.createElement("button");
    loginButton.type = "button";
    loginButton.textContent = "重新登录";
    loginButton.hidden = state.authenticated;
    loginButton.addEventListener("click", () => promptLogin(true));
    const logout = document.createElement("button");
    logout.type = "button";
    logout.textContent = "退出登录";
    const all = document.createElement("button");
    all.type = "button";
    all.textContent = "退出所有设备";
    const leave = async (everywhere) => {
      try {
        await post("/auth/logout", { everywhere });
        state.authenticated = false;
        state.csrf_token = "";
        message.textContent = "已退出。当前草稿仍保留，重新登录后可以继续。";
        location.assign("/auth/login");
      } catch (error) {
        if (error.status === 401) { location.assign("/auth/login"); return; }
        message.textContent = error.message;
      }
    };
    logout.addEventListener("click", () => leave(false));
    all.addEventListener("click", () => leave(true));
    window.addEventListener("blc:reauthenticated", () => { identity.textContent = "后台账号：" + state.username; });
    bar.append(identity, password, loginButton, logout, all, message);
    document.body.prepend(bar);
    if (!state.authenticated) await promptLogin();
  }
  ready = status().then(toolbar);
  // 失败时保持原请求的错误语义；不会把不可用认证状态当作已登录。
  ready.catch(() => {});
  async function ensureReady() {
    const attempt = ready;
    try { await attempt; }
    catch {
      if (ready === attempt) ready = status().then(toolbar);
      await ready;
    }
  }
  window.fetch = async (input, options) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    if (url.origin !== location.origin || url.pathname.startsWith("/auth/") || url.pathname.startsWith("/static/")) {
      return originalFetch(input, options);
    }
    await ensureReady();
    if (!state.enabled) return originalFetch(input, options);
    const request = new Request(input, options);
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method)) request.headers.set("X-BLC-CSRF", state.csrf_token || "");
    const response = await originalFetch(request);
    if (response.status === 401 && response.headers.get("X-BLC-Auth-Required") === "1") {
      await status();
      if (!state.authenticated) await promptLogin();
      else message.textContent = "此前请求的登录状态已失效，当前已登录；请确认后手动重试该操作。";
    }
    if (response.status === 403 && response.headers.get("X-BLC-CSRF-Required") === "1") {
      await status();
      message.textContent = "其他标签页已更新登录状态。本次操作未执行，请确认后重试。";
    }
    return response;
  };
  // 标签页回到前台时检查会话，静默探测不延长空闲期限。
  document.addEventListener("visibilitychange", async () => {
    if (document.visibilityState !== "visible") return;
    try {
      await ready;
      if (!state.enabled) return;
      await status();
      if (!state.authenticated) await promptLogin();
    } catch { /* 实际请求会显示网络错误，不能据此自动重置或退出。 */ }
  });
})();
