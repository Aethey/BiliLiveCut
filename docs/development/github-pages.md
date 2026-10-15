# GitHub Pages 仓库配置说明

本文供 `StarGazerQQD/BiliLiveCut` 的仓库维护者首次启用文档站。配置完成后，文档更新合并到 `main` 即会自动构建并发布。

| 项目 | 配置 |
| --- | --- |
| 目标仓库 | [StarGazerQQD/BiliLiveCut](https://github.com/StarGazerQQD/BiliLiveCut) |
| 发布分支 | `main`，同时应为仓库默认分支 |
| 工作流 | `.github/workflows/docs.yml`，Actions 中显示为 **Documentation** |
| 预期站点地址 | `https://StarGazerQQD.github.io/BiliLiveCut/` |
| Pages 发布源 | **GitHub Actions** |
| 部署环境 | `github-pages` |
| Secrets / PAT | 无需新增，使用 GitHub 自动提供的 `GITHUB_TOKEN` |
| Repository variables | 默认无需新增；`DOCS_BASE` 留空即使用 `/BiliLiveCut/` |

## 1. 合并文档与工作流

将文档分支通过 PR 合并到上游 `main`，包含 `.github/workflows/docs.yml`、`docs/package.json`、`docs/package-lock.json`、`docs/.vuepress/` 和文档正文。PR 中的工作流只检查和构建，不发布站点。

下面的仓库设置可在合并前先完成。无需安装 GitHub 建议的其他 Pages 工作流，也无需新建保存编译产物的分支。

## 2. 允许工作流使用 GitHub 官方 Actions

打开 [Settings → Actions → General](https://github.com/StarGazerQQD/BiliLiveCut/settings/actions)：

1. 确认仓库已启用 GitHub Actions。
2. 在 **Actions permissions** 中允许本流程使用的 GitHub 官方 Actions。若使用允许列表，至少允许 `actions/checkout`、`actions/setup-node`、`actions/upload-artifact`、`actions/upload-pages-artifact`、`actions/configure-pages` 和 `actions/deploy-pages`。
3. **Workflow permissions** 可以保留默认的 **Read repository contents and packages permissions**。工作流已单独为部署任务声明 `contents: read`、`pages: write` 和 `id-token: write`，无需将整个仓库的默认 Token 权限改为读写。

无需启用创建或审批 PR 的权限。外部贡献者提交的 PR 若显示等待运行审批，由维护者检查后按 GitHub 提示批准工作流即可。

## 3. 启用 GitHub Pages

打开 [Settings → Pages](https://github.com/StarGazerQQD/BiliLiveCut/settings/pages)，在 **Build and deployment → Source** 选择 **GitHub Actions**。

保持 **Custom domain** 为空，使用默认的 `github.io` 地址。默认部署不需要配置 DNS、CNAME、Secrets 或仓库变量。

## 4. 确认部署环境允许 main

部署任务使用名为 `github-pages` 的 Environment。打开 [Settings → Environments](https://github.com/StarGazerQQD/BiliLiveCut/settings/environments)，检查该环境（若尚不存在，可先创建）：

- **Deployment branches and tags** 应允许 `main`；如需限制来源，选择 **Selected branches and tags** 并添加 Branch 规则 `main`。
- 需要全自动发布时，不设置 **Required reviewers**；如果已有审批规则，则每次部署需要维护者手动批准。
- 此环境不需要添加 Secret。

环境保护规则与工作流条件同时生效。即使手动从功能分支运行，工作流也只构建预览，不进入部署任务。

## 5. 首次发布与验收

工作流文件进入 `main` 且以上设置完成后，打开 [Actions → Documentation](https://github.com/StarGazerQQD/BiliLiveCut/actions/workflows/docs.yml)，选择 **Run workflow → Branch: main → Run workflow**。

1. **Build and check documentation** 安装锁定依赖，检查页面配置，构建站点并检查全部站内链接。
2. **Deploy documentation to GitHub Pages** 发布构建产物；部署任务中的 Environment 链接给出实际网址。
3. 打开 `https://StarGazerQQD.github.io/BiliLiveCut/`，确认首页、顶部分类、左侧目录和搜索可用，再打开一篇文章并刷新，确认路径与静态资源正常。

以后修改文档、站点配置或工作流并合入 `main` 会自动发布。仅修改业务源码且未命中文档路径筛选时，不触发此流程；需要重新发布可手动运行。

## 可选：自定义域名或更换仓库

默认站点不要设置 `DOCS_BASE`。如果未来改用独立域名且文档放在域名根目录：

1. 在 **Settings → Pages → Custom domain** 配置域名，并按 GitHub 提示完成 DNS 与 HTTPS 设置。
2. 在 [Settings → Secrets and variables → Actions → Variables](https://github.com/StarGazerQQD/BiliLiveCut/settings/variables/actions) 新增 Repository variable `DOCS_BASE`，值为 `/`。
3. 从 `main` 重新运行 **Documentation**。

工作流在当前仓库内发布，fork 使用自己的 Pages 设置和域名，不会向上游站点写入。若将默认分支从 `main` 改名，需要同步修改工作流中 `push` 与 `pull_request` 的 `branches`，以及部署环境允许的分支。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 找不到 Documentation 或 Run workflow | 确认工作流已合入默认分支，且 Actions 已启用 |
| Configure Pages 提示找不到站点 | 在 Settings → Pages 将 Source 设为 GitHub Actions，再重跑失败任务 |
| 部署等待审批或被环境拒绝 | 检查 `github-pages` 的审批和分支规则是否允许 `main` |
| PR 构建成功但未发布 | 属于预期行为；合入 `main` 或手动从 `main` 运行后才部署 |
| 首页有内容，但 CSS、脚本或文章返回 404 | 默认地址必须包含 `/BiliLiveCut/`；清除错误的 `DOCS_BASE`，重建后再访问 |
| Action 因策略限制而不能运行 | 检查 Actions permissions 是否允许上面列出的官方 Actions |

配置依据见 [GitHub Pages 发布源](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)、[自定义 Pages 工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)和 [Actions 仓库权限](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository)。本地修改、预览和构建命令见[文档站维护](documentation.md)。
