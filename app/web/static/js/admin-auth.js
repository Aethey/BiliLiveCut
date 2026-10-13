// Docker 首次初始化、登录和修改密码；任何凭据只存在于当前表单内。
(() => {
  "use strict";
  const el = (id) => document.getElementById(id);
  const passwordPage = document.body.dataset.passwordPage === "true";
  let state;
  let mode;
  async function request(path, body) {
    const response = await fetch(path, {
      method: body ? "POST" : "GET", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-BLC-CSRF": state?.csrf_token || "" },
      ...(body ? { body: JSON.stringify(body) } : {})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "操作失败，请稍后重试。");
    return data;
  }
  function nextPage(role) {
    const fallback = role === "reviewer" ? "/review/queue" : "/";
    const value = new URLSearchParams(location.search).get("next");
    if (!value || !value.startsWith("/") || value.startsWith("//") || /[\\\x00-\x1f]/.test(value)) return fallback;
    const url = new URL(value, location.origin);
    if (url.origin !== location.origin || (url.pathname.startsWith("/auth/") && url.pathname !== "/auth/password")) return fallback;
    if (role === "reviewer" && !url.pathname.startsWith("/review/")) return fallback;
    return url.pathname + url.search + url.hash;
  }
  async function refresh() {
    state = await request("/auth/status");
    if (!state.enabled) {
      el("auth-title").textContent = "当前部署未启用 Docker 账号页面";
      el("auth-form").hidden = true;
      el("auth-return").hidden = false;
      return;
    }
    if (!passwordPage && state.authenticated && new URLSearchParams(location.search).has("next")) {
      location.replace(nextPage(state.role));
      return;
    }
    mode = state.setup_required ? "setup" : (passwordPage ? "password" : "login");
    if (passwordPage && !state.authenticated) {
      location.replace("/auth/login?next=%2Fauth%2Fpassword");
      return;
    }
    if (passwordPage && (state.managed_by_environment || state.role !== "admin")) {
      el("auth-title").textContent = "密码由部署配置管理";
      el("auth-description").textContent = state.role === "admin"
        ? "请修改 .env 中的 ADMIN_PASSWORD，再重建容器；新密码生效后需重新登录。"
        : "审核员密码由管理员在部署配置中管理。";
      el("auth-form").hidden = true;
      el("auth-return").hidden = false;
      return;
    }
    el("auth-title").textContent = { setup: "设置管理员密码", login: "登录管理后台", password: "修改管理员密码" }[mode];
    el("auth-description").textContent = mode === "login"
      ? "后台账号与 Bilibili 账号独立。默认管理员账号为 admin。"
      : "新密码为 12–128 个字符，支持中文或密码短语。请妥善保存。";
    el("username-row").hidden = mode !== "login";
    el("code-row").hidden = mode !== "setup";
    el("setup-help").hidden = mode !== "setup";
    el("setup-command").hidden = mode !== "setup";
    el("current-row").hidden = mode !== "password";
    el("confirm-row").hidden = mode === "login";
    el("password-label").textContent = mode === "login" ? "密码" : "新密码";
    el("password").autocomplete = mode === "login" ? "current-password" : "new-password";
    el("password").minLength = mode === "login" ? 1 : 12;
    el("password").maxLength = mode === "login" ? 1024 : 128;
    el("setup-code").required = mode === "setup";
    el("current-password").required = mode === "password";
    el("confirm-password").required = mode !== "login";
    el("auth-submit").textContent = mode === "setup" ? "设置密码" : mode === "password" ? "修改并退出旧会话" : "登录";
    el("auth-return").hidden = !state.authenticated;
    el("auth-form").hidden = false;
    el(mode === "setup" ? "setup-code" : mode === "password" ? "current-password" : "password").focus();
  }
  el("show-password").addEventListener("change", (event) => {
    ["password", "confirm-password", "current-password"].forEach((id) => {
      el(id).type = event.target.checked ? "text" : "password";
    });
  });
  el("auth-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = el("auth-submit");
    if (button.disabled) return;
    el("auth-message").textContent = "";
    if (mode !== "login" && el("password").value !== el("confirm-password").value) {
      el("auth-message").textContent = "两次输入的新密码不一致。";
      return;
    }
    button.disabled = true;
    try {
      const data = await request("/auth/" + mode, {
        username: el("username").value, password: el("password").value,
        code: el("setup-code").value.trim(), current_password: el("current-password").value
      });
      ["password", "confirm-password", "current-password", "setup-code"].forEach((id) => { el(id).value = ""; });
      if (mode === "login") { location.assign(nextPage(data.role)); return; }
      el("auth-message").textContent = data.detail;
      if (mode === "password") {
        location.assign("/auth/login?changed=1");
        return;
      }
      await refresh();
    } catch (error) {
      el("auth-message").textContent = error.message;
    } finally { button.disabled = false; }
  });
  if (new URLSearchParams(location.search).has("changed")) {
    el("auth-message").textContent = "密码已修改，请使用新密码重新登录。";
  }
  refresh().catch(() => {
    el("auth-title").textContent = "暂时无法读取登录状态";
    el("auth-message").textContent = "请检查服务是否启动后刷新页面；不会自动重置已有密码。";
  });
})();
