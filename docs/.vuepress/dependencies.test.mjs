import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const themeRequire = createRequire(import.meta.resolve('vuepress-theme-plume'))
const matter = themeRequire('gray-matter')
const matterRequire = createRequire(themeRequire.resolve('gray-matter'))
const yamlPackage = matterRequire.resolve('js-yaml/package.json')
const yamlMetadata = JSON.parse(readFileSync(yamlPackage, 'utf8'))
const yamlCli = path.join(path.dirname(yamlPackage), yamlMetadata.bin['js-yaml'])

// Exercise the actual parser used by the theme, including its command-line API.
test('frontmatter metadata survives parsing and serialization', () => {
  const input = '---\ntitle: 文档\nsidebar: false\ntags:\n  - 安全\n  - YAML\n---\n正文\n'
  const parsed = matter(input)
  assert.deepEqual(parsed.data, { title: '文档', sidebar: false, tags: ['安全', 'YAML'] })
  const restored = matter(matter.stringify(parsed.content, parsed.data))
  assert.deepEqual(restored.data, parsed.data)
  assert.equal(restored.content, parsed.content)
})

test('YAML CLI retains help, version, parsing and invalid-input errors', () => {
  const run = (args, input) => spawnSync(process.execPath, [yamlCli, ...args], {
    input, encoding: 'utf8', timeout: 10_000,
  })
  const help = run(['--help'])
  assert.equal(help.status, 0, help.stderr)
  assert.match(help.stdout, /--compact/)
  const version = run(['--version'])
  assert.equal(version.status, 0, version.stderr)
  assert.equal(version.stdout.trim(), JSON.parse(readFileSync(yamlPackage, 'utf8')).version)
  const parsed = run([], 'title: 文档\ncount: 2\n')
  assert.equal(parsed.status, 0, parsed.stderr)
  assert.deepEqual(JSON.parse(parsed.stdout), { title: '文档', count: 2 })
  const malformed = run(['--compact'], 'broken: [\n')
  assert.equal(malformed.status, 1, malformed.stderr)
  assert.match(malformed.stderr, /YAMLException/)
})

test('all installed renderers reject inherited trust while rendering ordinary formulas', () => {
  const lock = JSON.parse(readFileSync(new URL('../package-lock.json', import.meta.url), 'utf8'))
  const renderers = Object.keys(lock.packages).filter((name) => name.endsWith('/katex'))
  assert.ok(renderers.length > 0)
  for (const renderer of renderers) {
    const katex = require(path.join(fileURLToPath(new URL('../', import.meta.url)), renderer))
    assert.match(katex.renderToString('x^2 + y^2'), /class="katex"/)
    const formula = String.raw`\href{https://example.invalid/}{link}`
    const inherited = Object.assign(Object.create({ trust: true }), { strict: 'ignore' })
    assert.doesNotMatch(katex.renderToString(formula, inherited), /<a\b/)
    assert.match(katex.renderToString(formula, { trust: true }), /<a\b/)
    assert.doesNotMatch(katex.renderToString(
      String.raw`\includegraphics{https://example.invalid/pixel.png}`, inherited,
    ), /<img\b/)
  }
})

test('the complete locked dependency graph excludes the vulnerable formatter', () => {
  const lock = JSON.parse(readFileSync(new URL('../package-lock.json', import.meta.url), 'utf8'))
  assert.ok(!Object.keys(lock.packages).some((name) => name.endsWith('/sprintf-js')))
})

// Executable frontmatter is unnecessary for the documentation site.
test('JavaScript frontmatter is rejected before evaluation', () => {
  assert.throws(() => matter('---js\n({title: "unsafe"})\n---\nbody', {}), /no longer supported/)
})
