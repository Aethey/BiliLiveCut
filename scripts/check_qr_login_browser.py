"""通过真实浏览器验收扫码恢复、管理员重登和取消结果；只连接显式隔离夹具。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from playwright.sync_api import Route, expect, sync_playwright


def check(url: str, password: str, *, channel: str | None = None, artifacts: Path | None = None) -> None:
    """运行真实前端与认证，外部平台响应由独立测试服务提供。"""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel=channel)
        context = browser.new_context()
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url + "/")
        page.locator("#password").fill(password)
        page.locator("#auth-submit").click()
        page.wait_for_url(url + "/")
        page.locator('[data-tab="settings"]').click()
        page.locator('[data-tab="login"]').click()
        expect(page.locator("#btn-login")).to_be_enabled()

        def control(state: str | None = None, *, expire: bool = False) -> dict[str, object]:
            csrf = context.request.get(url + "/auth/status").json()["csrf_token"]
            path = "/api/qr-fixture" + ("/expire" if expire else "")
            if state is None and not expire:
                response = context.request.get(url + path)
            else:
                response = context.request.post(
                    url + path, data={} if expire else {"state": state}, headers={"Origin": url, "X-BLC-CSRF": csrf}
                )
            assert response.status == 200, response.status
            return response.json()

        page.locator("#btn-login").click()
        expect(page.locator("#login-qrcode")).to_be_visible(timeout=15000)
        assert control()["generates"] == 1
        page.reload()
        page.locator('[data-tab="settings"]').click()
        page.locator('[data-tab="login"]').click()
        expect(page.locator("#login-qrcode")).to_be_visible()
        assert control()["generates"] == 1
        control("confirm")
        expect(page.locator("#login-status")).to_contain_text("手机上确认", timeout=10000)
        context.set_offline(True)
        expect(page.locator("#login-status")).to_contain_text("连接中断", timeout=10000)
        expect(page.locator("#login-qrcode")).not_to_be_visible()
        context.set_offline(False)
        expect(page.locator("#login-qrcode")).to_be_visible(timeout=15000)
        assert control()["generates"] == 1

        control(expire=True)
        expect(page.locator("dialog")).to_be_visible(timeout=10000)
        expect(page.locator("#login-qrcode")).not_to_be_visible()
        page.locator("dialog input[name=password]").fill(password)
        page.locator("dialog button[type=submit]").click()
        expect(page.locator("dialog")).not_to_be_visible()
        expect(page.locator("#btn-login")).to_be_enabled(timeout=10000)
        assert control()["generates"] == 1
        control("scan")
        page.locator("#btn-login").click()
        expect(page.locator("#login-qrcode")).to_be_visible(timeout=10000)
        control("done")
        expect(page.locator("#login-status")).to_contain_text("登录成功", timeout=10000)
        expect(page.locator("#cookie-hint")).to_contain_text("123")
        expect(page.locator("#login-qrcode")).not_to_be_visible()
        assert "browser-fixture-only" not in page.locator("body").inner_text()
        assert page.evaluate("() => document.cookie.includes('blc_qr_browser')") is False

        control("scan")
        page.locator("#btn-login").click()
        expect(page.locator("#login-qrcode")).to_be_visible()
        before = int(control()["cancel_posts"])

        def lose_cancel_reply(route: Route) -> None:
            response = route.fetch()
            assert response.status == 200
            route.abort("failed")

        page.route("**/api/login/qrcode/*/cancel", lose_cancel_reply, times=1)
        page.locator("#btn-cancel-login").click()
        expect(page.locator("#login-status")).to_contain_text("连接中断", timeout=10000)
        page.locator("#btn-resume-login").click()
        expect(page.locator("#login-status")).to_contain_text("已取消", timeout=10000)
        assert control()["cancel_posts"] == before + 1
        expect(page.locator("#cookie-hint")).to_contain_text("123")

        control("invalid")
        page.locator("#btn-login").click()
        expect(page.locator("#login-status")).to_contain_text("未确认登录成功", timeout=10000)
        expect(page.locator("#cookie-hint")).to_contain_text("123")
        expect(page.locator("#btn-login")).to_be_enabled()
        control("timeout")
        page.locator("#btn-login").click()
        expect(page.locator("#login-status")).to_contain_text("二维码已过期", timeout=10000)
        expect(page.locator("#btn-login")).to_be_enabled()
        expect(page.locator("#login-qrcode")).not_to_be_visible()
        previous = int(control()["generates"])
        page.wait_for_timeout(1700)
        assert control()["generates"] == previous
        control("scan")

        def lose_start_reply(route: Route) -> None:
            if route.request.method == "POST":
                response = route.fetch()
                assert response.status == 200
                route.abort("failed")
            else:
                route.continue_()

        page.route("**/api/login/qrcode", lose_start_reply)
        page.locator("#btn-login").click()
        expect(page.locator("#login-status")).to_contain_text("操作结果尚未确认", timeout=10000)
        page.unroute("**/api/login/qrcode", lose_start_reply)
        page.locator("#btn-resume-login").click()
        expect(page.locator("#login-qrcode")).to_be_visible(timeout=10000)
        assert control()["generates"] == previous + 1
        page.locator("#btn-cancel-login").click()
        expect(page.locator("#login-status")).to_contain_text("已取消", timeout=10000)
        if artifacts is not None:
            artifacts.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(artifacts / "qr-recovery.png"), full_page=True)
        assert not errors, errors
        context.close()
        browser.close()
    print(
        "PASS: real browser QR refresh, network recovery, reauthentication, save, "
        "lost write responses, expiry and invalid credentials"
    )


def main() -> None:
    """读取独立测试服务地址，密码只通过进程环境传入。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--channel")
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args()
    check(args.url.rstrip("/"), os.environ["BLC_QR_FIXTURE_PASSWORD"], channel=args.channel, artifacts=args.artifacts)


if __name__ == "__main__":
    main()
