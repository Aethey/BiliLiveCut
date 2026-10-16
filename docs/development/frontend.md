# Web 页面开发与验证

Web 控制台由 FastAPI 提供，使用 Jinja2 HTML 模板、CSS 和原生 JavaScript 模块。页面与静态文件直接随应用交付；源码运行、Docker 和 Portable 均不需要单独编译页面，也不需要安装 Node.js 才能启动。

## 目录与入口

| 路径 | 职责 |
| --- | --- |
| `app/web/main.py`、`app/web/routers/` | Web 服务、页面路由与 API |
| `app/web/templates/` | 工作台、设置、录播导入、审核与认证等 HTML 模板 |
| `app/web/static/style.css`、`admin-auth.css` | 页面与认证界面的样式 |
| `app/web/static/app.js` | 工作台脚本入口 |
| `app/web/static/js/` | 直播间、录制、设置、导入、审核、任务等功能模块 |
| `scripts/check_frontend.py` | JavaScript 语法与交互检查入口 |

模板通过页面路由渲染，JavaScript 模块调用现有 API 更新数据与提交表单。修改功能时应同时检查对应模板、模块和 API 契约；凭据回显、配置版本校验和草稿保存规则仍由原有接口与页面逻辑负责。

## 本地修改与刷新

先按[开发环境](environment.md)安装 Python 依赖，在项目根目录启动：

```bash
uv run --locked blc serve --host 127.0.0.1 --port 8000 --reload
```

传统虚拟环境激活后可用 `python -m app.cli serve --host 127.0.0.1 --port 8000 --reload`。打开 `http://127.0.0.1:8000`，修改 HTML、CSS 或 JavaScript 后重新加载页面；浏览器缓存旧静态文件时使用强制刷新。

`--reload` 用于源码开发时自动重启 Python 服务，不会自动刷新浏览器或保留重新加载前的页面草稿。保存正在编辑的配置后再刷新；开发与验收使用独立运行目录，不复用正在运行实例的数据库和 Cookie。

## 语法与交互检查

开发检查需要 PATH 中可用的 Node.js 22 或更高版本，无需安装 npm 依赖。在项目根目录执行：

```bash
uv run --locked python scripts/check_frontend.py
```

也可在传统虚拟环境运行 `python scripts/check_frontend.py`。检查入口会验证 Git 已跟踪的 JavaScript 文件与 HTML 模板中的可执行内联脚本，并执行四组真实模块交互检查：

- `scripts/check_frontend_interactions.mjs`：工作台交互。
- `scripts/check_waveform_interactions.mjs`：波形错误提示、请求失败与恢复。
- `scripts/check_configuration_interactions.mjs`：配置草稿、校验与保存。
- `scripts/check_recording_import_interactions.mjs`：录播导入、轮询与恢复。

CI 的 Python 测试矩阵使用 Node.js 22。波形检查入口为独立 `.mjs` 文件；pytest 的超时为 30 秒，失败报告包含已完成的检查阶段。

新增脚本后应先纳入 Git 跟踪，再执行完整入口，避免漏检。涉及 Web 页面与相关检查脚本的提交也会触发 pre-commit 中的这一检查。

页面与 API 行为还由 `tests/unit/test_frontend_javascript.py`、`tests/unit/test_static_assets.py` 和 `tests/integration/` 下的 Web、认证、配置及导入测试覆盖。根据改动选择目标测试，并在真实浏览器中确认布局、导航、未保存提示和网络失败时的反馈。

## 文档站

文档站使用独立的依赖和构建流程，维护入口见[文档站说明](documentation.md)。文档站的预览服务与依赖不参与业务控制台运行。
