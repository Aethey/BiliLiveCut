import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { defineUserConfig } from 'vuepress'
import { viteBundler } from '@vuepress/bundler-vite'
import { plumeTheme } from 'vuepress-theme-plume'
import {
  root,
  branch,
  pageRoute,
  pages,
  repoLinksPlugin,
  repository,
  sections,
  siteBase,
} from './site.mjs'

const require = createRequire(import.meta.url)
const here = fileURLToPath(new URL('.', import.meta.url))
const sidebar = Object.fromEntries(
  sections.map((section) => [
    section.link,
    section.groups.map(({ text, items, collapsed = false }) => ({
      text,
      collapsed,
      items: items.map(([file, text]) => ({ text, link: pageRoute(file) })),
    })),
  ]),
)

export default defineUserConfig({
  lang: 'zh-CN',
  title: 'BiliLiveCut 文档',
  description: '从直播录制、转写与高光识别，到审核出片的使用、部署与开发指南。',
  base: siteBase(),
  pagePatterns: pages,
  clientConfigFile: path.join(here, 'client.mjs').split(path.sep).join('/'),
  dest: path.join(here, 'dist'),
  temp: path.join(here, '.temp'),
  cache: path.join(here, '.cache'),
  public: path.join(here, 'public'),
  bundler: viteBundler({
    viteOptions: {
      root: path.resolve(here, '..'),
      server: { fs: { allow: [root] } },
      resolve: {
        alias: [
          {
            find: /^vue$/,
            replacement: require.resolve('vue/dist/vue.runtime.esm-bundler.js'),
          },
          {
            find: /^vue\/server-renderer$/,
            replacement: require.resolve('vue/server-renderer'),
          },
        ],
      },
    },
  }),
  plugins: [repoLinksPlugin()],
  theme: plumeTheme({
    autoFrontmatter: false,
    codeHighlighter: { langAlias: { env: 'dotenv' } },
    docsRepo: `https://github.com/${repository}`,
    docsBranch: branch,
    docsDir: '',
    editLink: true,
    contributors: false,
    changelog: false,
    lastUpdated: {},
    search: { provider: 'local' },
    navbar: sections.map(({ text, link }) => ({ text, link })),
    sidebar,
    social: [{ icon: 'github', link: `https://github.com/${repository}` }],
    footer: {
      message: 'BiliLiveCut · 使用与维护文档',
      copyright: 'MIT License · Copyright © 2026 StarGazerQQD',
    },
  }),
})
