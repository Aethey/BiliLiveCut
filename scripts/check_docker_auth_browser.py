"""在真实浏览器中验证 Docker 初始化、改密、退出及保留草稿的重新登录。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def check_browser(
    url: str,
    code: str,
    password: str,
    expire_session: Callable[[], None],
    *,
    channel: str | None = None,
    artifacts: Path | None = None,
) -> str:
    """使用独立浏览器上下文执行表单，不读取用户浏览器数据或打印凭据。"""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel=channel)
        context = browser.new_context()
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url + "/")
        expect(page.locator("#auth-title")).to_have_text("设置管理员密码")
        if artifacts is not None:
            page.screenshot(path=str(artifacts / "docker-setup.png"), full_page=True)
        page.locator("#setup-code").fill(code)
        page.locator("#password").fill(password)
        page.locator("#confirm-password").fill(password)
        page.locator("#auth-submit").click()
        expect(page.locator("#auth-title")).to_have_text("登录管理后台")
        page.locator("#password").fill(password)
        page.locator("#auth-submit").click()
        page.wait_for_url(url + "/")
        expect(page.locator(".blc-session-bar")).to_be_visible()
        page.goto(url + "/imports")
        page.locator("#import-title").fill("尚未保存的录播草稿")
        writes: list[str] = []
        page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
        expire_session()
        status = page.evaluate(
            "() => fetch('/api/recording-imports', {method:'POST', headers:{'Content-Type':'application/json'}, "
            "body:'{}'}).then(response => response.status)"
        )
        assert status == 401
        expect(page.locator("dialog")).to_be_visible()
        if artifacts is not None:
            page.screenshot(path=str(artifacts / "docker-relogin.png"), full_page=True)
        page.locator("dialog input[name=password]").fill(password)
        page.locator("dialog button[type=submit]").click()
        expect(page.locator("dialog")).not_to_be_visible()
        expect(page.locator("#import-title")).to_have_value("尚未保存的录播草稿")
        assert len([request for request in writes if request.endswith("/api/recording-imports")]) == 1
        assert page.evaluate("() => document.cookie.includes('blc_session')") is False
        assert page.evaluate("() => localStorage.length") == 0
        # 清理测试草稿后验证显式改密与退出的页面导航。
        page.locator("#import-title").fill("")
        page.goto(url + "/auth/password")
        expect(page.locator("#auth-title")).to_have_text("修改管理员密码")
        changed = password + "-updated"
        page.locator("#current-password").fill(password)
        page.locator("#password").fill(changed)
        page.locator("#confirm-password").fill(changed)
        page.locator("#auth-submit").click()
        page.wait_for_url(url + "/auth/login?changed=1")
        page.locator("#password").fill(changed)
        page.locator("#auth-submit").click()
        page.wait_for_url(url + "/")
        page.get_by_role("button", name="退出登录", exact=True).click()
        page.wait_for_url(url + "/auth/login")
        expect(page.locator("#auth-title")).to_have_text("登录管理后台")
        assert not errors, errors
        context.close()
        browser.close()
        return changed
