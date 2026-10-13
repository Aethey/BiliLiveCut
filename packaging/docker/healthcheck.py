"""探测容器 Web 服务，不读取或输出认证凭据。"""

from __future__ import annotations

from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener


def main() -> int:
    """认证挑战也表明已完成启动的 Web 服务可以响应。"""
    try:
        # 不让 HTTP_PROXY/ALL_PROXY 将本机健康检查送到外部代理。
        with build_opener(ProxyHandler({})).open("http://127.0.0.1:8000/", timeout=3) as response:
            return 0 if response.status == 200 else 1
    except HTTPError as exc:
        return 0 if exc.code == 401 else 1
    except (OSError, URLError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
