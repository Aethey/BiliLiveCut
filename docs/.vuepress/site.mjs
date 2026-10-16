import { existsSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export const root = fileURLToPath(new URL('../../', import.meta.url))
export const repository =
  process.env.GITHUB_REPOSITORY || 'StarGazerQQD/BiliLiveCut'
export const sourceRepository =
  process.env.GITHUB_HEAD_REPOSITORY || repository
export const branch =
  process.env.GITHUB_HEAD_REF || process.env.GITHUB_REF_NAME || 'main'

export const archives = readdirSync(path.join(root, 'docs/changelog'))
  .filter((name) => /^CHANGELOG_PRE_[\d.]+\.md$/.test(name))
  .sort((a, b) => b.localeCompare(a, 'en', { numeric: true }))
  .map((name) => {
    const version = name.replace('CHANGELOG_PRE_', '').replace('.md', '')
    return [
      `docs/changelog/${name}`,
      version,
      `/releases/archive/${version}.html`,
    ]
  })

// Each top-level category owns its sidebar and routes. Source files remain usable on GitHub.
export const sections = [
  {
    text: '入门',
    link: '/guide/',
    groups: [
      {
        text: '快速开始',
        items: [
          ['docs/guide/README.md', '入门导览', '/guide/'],
          [
            'docs/guide/getting-started.md',
            '选择安装方式',
            '/guide/installation.html',
          ],
          [
            'docs/guide/quick-start.md',
            '第一次完成工作流',
            '/guide/quick-start.html',
          ],
        ],
      },
      {
        text: '开始部署',
        items: [
          [
            'packaging/portable/USER_GUIDE_ZH.md',
            'Windows Portable',
            '/guide/portable.html',
          ],
          ['docs/guide/linux-server.md', 'Linux 无头服务器', '/guide/linux.html'],
          ['packaging/docker/README.md', 'Docker 部署', '/guide/docker.html'],
          ['docs/guide/source.md', '源码部署', '/guide/source.html'],
        ],
      },
      {
        text: '配置与登录',
        items: [
          ['docs/guide/configuration.md', '配置参考', '/guide/configuration.html'],
          ['docs/guide/login.md', '后台认证与 Bilibili 登录', '/guide/login.html'],
        ],
      },
    ],
  },
  {
    text: '功能指南',
    link: '/features/',
    groups: [
      {
        text: '功能导览',
        items: [['docs/features/README.md', '工作流与阅读入口', '/features/']],
      },
      {
        text: '录制与输入',
        items: [
          [
            'docs/features/recording.md',
            '直播间、录制与弹幕',
            '/features/recording.html',
          ],
          [
            'docs/features/recording-import.md',
            '录播导入与分析',
            '/features/imports.html',
          ],
        ],
      },
      {
        text: '分析与出片',
        items: [
          ['docs/features/asr.md', '语音转写与模型', '/features/asr.html'],
          [
            'docs/features/highlight-plugins.md',
            '高光评分插件',
            '/features/highlight-plugins.html',
          ],
          [
            'docs/features/review-render.md',
            '审核、字幕与剪辑',
            '/features/review-render.html',
          ],
          [
            'docs/features/publishing.md',
            '上传与发布',
            '/features/publishing.html',
          ],
        ],
      },
      {
        text: '工作台与排错',
        items: [
          [
            'docs/features/workspace.md',
            'Web 工作台与配置草稿',
            '/features/workspace.html',
          ],
          [
            'docs/features/troubleshooting.md',
            '故障排查',
            '/features/troubleshooting.html',
          ],
        ],
      },
    ],
  },
  {
    text: '开发扩展',
    link: '/development/',
    groups: [
      {
        text: '开发导览',
        items: [
          ['docs/development/README.md', '维护宿主与扩展能力', '/development/'],
        ],
      },
      {
        text: '宿主开发',
        items: [
          [
            'docs/development/environment.md',
            '开发环境与验证',
            '/development/environment.html',
          ],
          [
            'docs/development/frontend.md',
            'Web 页面开发与验证',
            '/development/frontend.html',
          ],
          [
            'docs/development/documentation.md',
            '文档站维护',
            '/development/documentation.html',
          ],
        ],
      },
      {
        text: '插件开发',
        items: [
          ['plugin/README.md', '插件接口', '/development/plugins.html'],
          [
            'docs/development/live-source-plugins.md',
            '直播源契约',
            '/development/live-sources.html',
          ],
          [
            'docs/development/douyin-plugin-handoff.md',
            '抖音直播源接入',
            '/development/douyin.html',
          ],
        ],
      },
      {
        text: '发行维护',
        items: [
          [
            'packaging/portable/README.md',
            'Portable 构建',
            '/development/portable.html',
          ],
        ],
      },
    ],
  },
  {
    text: '技术参考',
    link: '/reference/',
    groups: [
      {
        text: '参考导览',
        items: [['docs/reference/README.md', '算法与模块边界', '/reference/']],
      },
      {
        text: '高光分析',
        items: [
          [
            'docs/reference/hotspot-detector.md',
            '热点检测器',
            '/reference/hotspot-detector.html',
          ],
        ],
      },
      {
        text: '原生加速',
        items: [
          [
            'docs/reference/native-acceleration.md',
            '模块与调用边界',
            '/reference/native-acceleration.html',
          ],
          [
            'docs/reference/repeat-cpu-kernel.md',
            '任意周期重复检测',
            '/reference/repeat-detection.html',
          ],
        ],
      },
    ],
  },
  {
    text: '版本记录',
    link: '/releases/',
    groups: [
      {
        text: '版本与许可',
        items: [
          ['docs/releases/README.md', '版本导览', '/releases/'],
          ['CHANGELOG.md', '当前版本更新', '/releases/current.html'],
          [
            'docs/changelog/CHANGELOG_INDEX.md',
            '历史版本索引',
            '/releases/archive/',
          ],
          [
            'packaging/portable/licenses/THIRD_PARTY_NOTICES.md',
            '第三方模型声明',
            '/releases/licenses.html',
          ],
        ],
      },
      { text: '历史归档', collapsed: true, items: archives },
    ],
  },
]

export const entries = sections.flatMap((section) =>
  section.groups.flatMap((group) => group.items),
)
// Only explicitly listed Markdown enters the published site.
export const pages = ['docs/README.md', ...entries.map(([file]) => file)]
const pageRoutes = new Map([
  ['docs/README.md', '/'],
  ...entries.map(([file, , route]) => [file, route]),
])

export function pageRoute(file) {
  const route = pageRoutes.get(file)
  if (!route) throw new Error(`文档未登记分类与路由: ${file}`)
  return route
}

export function siteBase(value = process.env.DOCS_BASE || undefined) {
  const repoName = repository.split('/')[1]
  const base =
    value ?? (repoName.endsWith('.github.io') ? '/' : `/${repoName}/`)
  if (
    !/^\/(?:[^?#\\\s]*\/)?$/.test(base) ||
    base.startsWith('//') ||
    base.includes('..')
  ) {
    throw new Error(
      'DOCS_BASE 必须是以 / 开头和结尾的路径，例如 /BiliLiveCut/ 或 /',
    )
  }
  let decoded
  try {
    decoded = decodeURI(base)
  } catch (error) {
    throw new Error('DOCS_BASE 包含无效的 URL 编码。', { cause: error })
  }
  if (decoded.split('/').some((part) => part === '.' || part === '..')) {
    throw new Error('DOCS_BASE 不得包含 . 或 .. 路径段。')
  }
  return new URL(base, 'https://docs.local').pathname
}

export function resolveRepoLink(href, source) {
  if (!href || /^(?:[a-z][\w+.-]*:|\/|#)/i.test(href)) return href
  const match = href.match(/^([^?#]*)(.*)$/)
  let target = path.posix.normalize(
    path.posix.join(path.posix.dirname(source), decodeURI(match[1])),
  )
  // Archived notes retain their original repository-root link syntax.
  if (
    !existsSync(path.join(root, target)) &&
    existsSync(path.join(root, match[1]))
  ) {
    target = path.posix.normalize(match[1])
  }
  if (target.startsWith('../') || !existsSync(path.join(root, target))) {
    throw new Error(`文档链接不存在: ${source} → ${href}`)
  }
  if (pageRoutes.has(target)) return `${pageRoute(target)}${match[2]}`
  return `https://github.com/${sourceRepository}/blob/${branch}/${target}${match[2]}`
}

export function repoLinksPlugin() {
  return {
    name: 'bili-live-cut-docs-links',
    extendsPageOptions(options) {
      if (!options.filePath) return
      const source = path
        .relative(root, options.filePath)
        .split(path.sep)
        .join('/')
      if (pageRoutes.has(source)) options.path = pageRoute(source)
    },
    extendsPage(page) {
      if (page.filePathRelative !== 'docs/README.md') return
      const actions = page.frontmatter.config[0].hero.actions
      actions.find((action) => action.text === 'GitHub').link =
        `https://github.com/${repository}`
    },
    extendsMarkdown(md) {
      md.core.ruler.after('inline', 'bili-live-cut-repo-links', (state) => {
        const source = state.env.filePathRelative
        if (!source) return
        for (const block of state.tokens) {
          for (const token of block.children || []) {
            if (token.type !== 'link_open') continue
            token.attrSet(
              'href',
              resolveRepoLink(token.attrGet('href'), source),
            )
          }
        }
      })
    },
  }
}
