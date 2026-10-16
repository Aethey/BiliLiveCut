import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import {
  branch,
  entries,
  sections,
  pageRoute,
  pages,
  sourceRepository,
  resolveRepoLink,
  root,
  siteBase,
} from './site.mjs'

test('all published pages exist and local-only reviews are excluded', () => {
  assert.equal(new Set(pages).size, pages.length)
  for (const file of pages) assert.ok(existsSync(path.join(root, file)), file)
  assert.ok(!pages.includes('docs/engineering-review-2026-10-09.md'))
})

test('repository and user Pages paths are validated', () => {
  assert.equal(siteBase('/BiliLiveCut/'), '/BiliLiveCut/')
  assert.equal(siteBase('/'), '/')
  assert.equal(siteBase('/文档/'), '/%E6%96%87%E6%A1%A3/')
  assert.equal(siteBase('/%E6%96%87%E6%A1%A3/'), '/%E6%96%87%E6%A1%A3/')
  for (const invalid of [
    '',
    'BiliLiveCut',
    '//example.org/',
    '/a/../',
    '/a?b/',
    '/a/./',
    '/%2e/',
    '/a/.%2e/',
    '/a/%/',
    '/a/%FF/',
  ]) {
    assert.throws(() => siteBase(invalid))
  }
})

test('published documents stay local and source references use GitHub', () => {
  assert.equal(
    resolveRepoLink('../../README.md', 'docs/features/README.md'),
    `https://github.com/${sourceRepository}/blob/${branch}/README.md`,
  )
  assert.equal(
    resolveRepoLink('../guide/configuration.md#配置', 'docs/features/README.md'),
    '/guide/configuration.html#配置',
  )
  assert.equal(
    resolveRepoLink('../../.env.example', 'docs/features/README.md'),
    `https://github.com/${sourceRepository}/blob/${branch}/.env.example`,
  )
  assert.equal(
    resolveRepoLink('example/', 'plugin/README.md'),
    `https://github.com/${sourceRepository}/blob/${branch}/plugin/example/`,
  )
  assert.equal(resolveRepoLink('#标题', 'docs/features/README.md'), '#标题')
  assert.equal(
    resolveRepoLink('https://example.org/', 'docs/features/README.md'),
    'https://example.org/',
  )
  assert.throws(() => resolveRepoLink('missing.md', 'docs/features/README.md'))
  assert.throws(() => resolveRepoLink('../../../etc/passwd', 'docs/features/README.md'))
})

test('archived root-relative links work without modifying changelogs', () => {
  assert.equal(
    resolveRepoLink(
      'docs/changelog/CHANGELOG_INDEX.md',
      'docs/changelog/CHANGELOG_PRE_0.1.11.md',
    ),
    '/releases/archive/',
  )
  assert.equal(pageRoute('docs/README.md'), '/')
  assert.equal(pageRoute('packaging/docker/README.md'), '/guide/docker.html')
})

test('source links follow the PR repository while Pages keeps its deployment base', () => {
  for (const scenario of [
    {
      repository: 'owner/documentation',
      headRepository: '',
      headRef: '',
      refName: 'release/docs',
      expectedBase: '/documentation/',
      expectedSource: 'owner/documentation',
      expectedBranch: 'release/docs',
    },
    {
      repository: 'owner/documentation',
      headRepository: 'contributor/forked-docs',
      headRef: 'docs/fix-links',
      refName: '42/merge',
      expectedBase: '/documentation/',
      expectedSource: 'contributor/forked-docs',
      expectedBranch: 'docs/fix-links',
    },
    {
      repository: 'contributor/forked-docs',
      headRepository: '',
      headRef: '',
      refName: 'main',
      expectedBase: '/forked-docs/',
      expectedSource: 'contributor/forked-docs',
      expectedBranch: 'main',
    },
  ]) {
    const output = execFileSync(process.execPath, ['--input-type=module', '-e', `
      const site = await import('./docs/.vuepress/site.mjs')
      console.log(JSON.stringify({
        base: site.siteBase(),
        source: site.sourceRepository,
        branch: site.branch,
        link: site.resolveRepoLink('../../.env.example', 'docs/guide/README.md'),
      }))
    `], {
      cwd: root,
      encoding: 'utf8',
      env: {
        ...process.env,
        DOCS_BASE: '',
        GITHUB_REPOSITORY: scenario.repository,
        GITHUB_HEAD_REPOSITORY: scenario.headRepository,
        GITHUB_HEAD_REF: scenario.headRef,
        GITHUB_REF_NAME: scenario.refName,
      },
    })
    assert.deepEqual(JSON.parse(output), {
      base: scenario.expectedBase,
      source: scenario.expectedSource,
      branch: scenario.expectedBranch,
      link: `https://github.com/${scenario.expectedSource}/blob/${scenario.expectedBranch}/.env.example`,
    })
  }
})

test('categories own unique routes and contain only their own sidebar documents', () => {
  assert.equal(
    new Set(entries.map(([, , route]) => route)).size,
    entries.length,
  )
  for (const section of sections) {
    const items = section.groups.flatMap((group) => group.items)
    assert.ok(
      items.some(([, , route]) => route === section.link),
      section.text,
    )
    for (const [file, , route] of items) {
      assert.ok(route.startsWith(section.link), file)
      assert.equal(pageRoute(file), route)
    }
  }
  for (const file of [
    'pr80-ci-alert-fix',
    'qr-login-recovery',
    'qr-login-security',
    'asr-repeat-integration',
  ]) {
    assert.ok(!pages.includes(`docs/${file}.md`), file)
  }
})

// VuePress embeds this path directly in an import string in generated JavaScript.
test('client configuration uses a portable JavaScript import path', async () => {
  const { default: config } = await import('./config.mjs')
  assert.equal(config.clientConfigFile.includes('\\'), false)
  assert.ok(existsSync(config.clientConfigFile))
})
