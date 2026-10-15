import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import {
  branch,
  entries,
  sections,
  pageRoute,
  pages,
  repository,
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
  for (const invalid of [
    '',
    'BiliLiveCut',
    '//example.org/',
    '/a/../',
    '/a?b/',
  ]) {
    assert.throws(() => siteBase(invalid))
  }
})

test('published documents stay local and source references use GitHub', () => {
  assert.equal(
    resolveRepoLink('../../README.md', 'docs/features/README.md'),
    `https://github.com/${repository}/blob/${branch}/README.md`,
  )
  assert.equal(
    resolveRepoLink('../guide/configuration.md#配置', 'docs/features/README.md'),
    '/guide/configuration.html#配置',
  )
  assert.equal(
    resolveRepoLink('../../.env.example', 'docs/features/README.md'),
    `https://github.com/${repository}/blob/${branch}/.env.example`,
  )
  assert.equal(
    resolveRepoLink('example/', 'plugin/README.md'),
    `https://github.com/${repository}/blob/${branch}/plugin/example/`,
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
