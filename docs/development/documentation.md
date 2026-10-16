# 文档站维护与部署

文档站使用 VuePress 2 和 `vuepress-theme-plume`，依赖与构建入口位于 `docs/`。业务 Web 控制台由 Python 服务提供 HTML 模板和静态文件；文档站独立安装、构建和部署，运行 Python 应用不需要安装文档依赖。

## 本地预览

在项目根目录运行，使用 `docs/package.json` 支持的 Node.js：

```bash
npm --prefix docs ci --ignore-scripts
npm --prefix docs run dev
```

默认打开 `http://127.0.0.1:8080/BiliLiveCut/`。文档源文件修改后会热更新。项目站点默认使用仓库名作为子路径，本地可切换到根路径：

```bash
DOCS_BASE=/ npm --prefix docs run dev
```

生产构建和配置检查（Windows、Linux 均受支持，CI 按顺序验证两个平台）：

```bash
npm --prefix docs audit --include=dev --audit-level=low
npm --prefix docs run check
npm --prefix docs run build
```

构建前清空旧站点产物，再生成 `docs/.vuepress/dist/`，确保被删除的页面不会残留。构建后检查所有已登记页面、站内链接与静态资源。预览生产产物时应保留对应子目录，例如默认构建需要将产物挂载到 `/BiliLiveCut/`，不能直接双击 HTML 文件。

## 依赖安全

CI 安装严格使用锁文件并禁用依赖安装脚本，随后审计全部依赖（包括仅用于构建的 `devDependencies`）；任何级别的已知漏洞或审计服务错误都会阻止构建和部署，不使用漏洞豁免或忽略名单。升级依赖后需重新执行上述审计、配置测试和完整构建。

KaTeX 统一锁定为已修复 [GHSA-238p-pmpm-9mq7](https://github.com/advisories/GHSA-238p-pmpm-9mq7) 的官方 `0.18.10`；`overrides.katex` 引用根依赖版本，确保主题和插件不会再安装较旧副本。现有主题及 Markdown 版本保持固定，公式和站点构建回归验证其兼容性。

`gray-matter` 通过 npm 别名统一使用 [Eleventy 维护的 `@11ty/gray-matter`](https://github.com/11ty/gray-matter) `3.0.0`（MIT）。该发行版保留文档所需的 YAML/JSON frontmatter 接口，使用新的 `js-yaml`，从依赖图移除受 [GHSA-hp3w-g68c-fv3c](https://github.com/advisories/GHSA-hp3w-g68c-fv3c) 影响的 `sprintf-js`，不需要本地补丁。文档元数据使用 YAML/JSON；可执行 JavaScript frontmatter 会被明确拒绝。

回归检查覆盖 frontmatter 往返、YAML 命令行帮助、版本和解析、所有 KaTeX 副本的正常公式与继承信任属性防护，以及 JavaScript frontmatter 拒绝行为。后续升级或移除依赖覆盖仍须通过这些检查。

## 内容与目录

首页内容在 `docs/README.md`，使用主题的标题区、功能卡片和 Markdown 内容区；`docs/.vuepress/styles/home.css` 管理首页布局，`docs/.vuepress/styles/theme.css` 统一管理整站明暗模式的蓝色配色（主色 `#00AEEC`，背景以中性色为主）。首页使用主题自带的色调板背景效果。仓库 README 继续作为项目简介，文档站的相关链接会跳转到 GitHub 原文。

- `docs/` 根目录的 Markdown 只保留站点首页 `README.md`。入门与部署放在 `guide/`，使用指南放在 `features/`，开发及 Pages 配置放在 `development/`，算法说明放在 `reference/`，版本导览与历史记录分别放在 `releases/`、`changelog/`。
- Docker、Portable、插件的说明继续保存在各自目录。站点直接读取原始 Markdown，无需手工复制；站点路由与源文件位置分别维护，移动文件时保留已有访问地址。
- `docs/.vuepress/site.mjs` 是栏目、分组、页面标题与站点路由的唯一配置来源。顶部五个栏目分别对应 `/guide/`、`/features/`、`/development/`、`/reference/`、`/releases/`。入门统一介绍安装部署、配置登录和第一次使用；Windows Portable、Linux 无头服务器、Docker 和跨平台源码运行均归入“开始部署”，位于“快速开始”之后、“配置与登录”之前。
- 每个栏目拥有独立左侧目录：组名是二级目录，具体文档是三级目录；切换顶部栏目后，只展示该栏目的分组和文件。正文的章节目录仍在右侧。
- 新增页面时在相应栏目的 `groups[].items` 登记 `[源码路径, 标题, 站点路径]`。源码 Markdown 保留相对链接；站点构建时按路由表转换，不要求源文件和发布 URL 的目录完全一致。
- 页面白名单只包含首页和已登记的文件。本地工程审查、一次性 PR 修复记录、运行数据和临时文档不会自动发布。
- `docs/.vuepress/config.mjs` 配置导航、侧边栏、本地全文搜索、主题和构建目录。
- 内部文档链接沿用相对 `.md` 路径，GitHub 仓库浏览与站点均可使用。指向源代码、配置和示例目录的相对链接在站点中转为 GitHub 链接。
- 历史 Changelog 原样展示，仅“版本记录”栏目的归档目录自动收录 `docs/changelog/CHANGELOG_PRE_*.md`。历史行为不代表当前版本行为；本次站点建设不修改 Changelog 内容。
- 主题自动写入 frontmatter 已关闭，预览和构建不会改写源文档。

## GitHub Pages

首次部署前，仓库管理员必须在 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。上游仓库入口为 [Pages 设置](https://github.com/StarGazerQQD/BiliLiveCut/settings/pages)。只存在 `docs.yml` 或 PR 预览构建通过，并不表示仓库已经启用 Pages；使用分支发布的旧配置也需要切换来源。工作流不会自行启用站点或修改仓库设置，也不需要添加个人访问令牌。

默认分支的 Linux 构建会先以 `pages: read` 查询站点设置，只有 `build_type=workflow` 才继续。未启用、仍使用分支发布、读取权限不足或 API 错误都会给出具体原因并阻止部署。修正设置后可在默认分支手动运行 Documentation；首次真实部署成功前，不应把站点标记为已上线。

`.github/workflows/docs.yml` 是独立的文档流水线：

1. 文档或站点配置变更的 PR 执行锁文件安装、依赖安全审计、配置检查和生产构建，上传站点预览产物。
2. 默认分支 `main` 更新时构建，成功后使用 Pages Artifact 部署，不创建 `gh-pages` 分支或额外提交。
3. 手动触发可验证其他分支；只有仓库默认分支允许进入部署任务。

正式部署以 `StarGazerQQD/BiliLiveCut` 为准，默认访问地址为 `https://StarGazerQQD.github.io/BiliLiveCut/`。

本地预览默认使用上游仓库链接；Actions 根据 `GITHUB_REPOSITORY` 自动选择当前仓库链接和站点子路径，因此 fork 在自身仓库启用 Pages 后会发布到自己的站点。源码编辑链接本地默认使用 `main`；Actions 构建使用对应分支。`GITHUB_REPOSITORY`、`GITHUB_REF_NAME` 和 `GITHUB_HEAD_REF` 也可在本地显式设置以验证 fork 或分支链接。

参考 [Plume 部署说明](https://theme-plume.vuejs.press/guide/deployment/)和 [GitHub Pages 自定义工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。
