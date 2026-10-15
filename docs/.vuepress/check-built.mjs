import assert from 'node:assert/strict'
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { pageRoute, pages, siteBase } from './site.mjs'

const dist = fileURLToPath(new URL('./dist/', import.meta.url))
const base = siteBase()
const errors = []
function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const file = path.join(dir, name)
    return statSync(file).isDirectory() ? walk(file) : [file]
  })
}
const files = walk(dist)
const relativePath = (file) =>
  path.relative(dist, file).split(path.sep).join('/')
const anchors = new Map(
  files
    .filter((file) => file.endsWith('.html'))
    .map((file) => [
      path.resolve(file),
      new Set(
        [...readFileSync(file, 'utf8').matchAll(/\bid="([^"]+)"/g)].map(
          ([, id]) => id,
        ),
      ),
    ]),
)
const expectedHtml = pages.map((source) => {
  const route = pageRoute(source).slice(1)
  return route.endsWith('/') || !route ? `${route}index.html` : route
})
expectedHtml.push('404.html')
assert.deepEqual(
  files
    .filter((file) => file.endsWith('.html'))
    .map((file) => relativePath(file))
    .sort(),
  expectedHtml.sort(),
  '构建目录中存在未登记或已删除的页面',
)
for (const source of pages) {
  const route = pageRoute(source).slice(1)
  assert.ok(
    existsSync(
      path.join(
        dist,
        route.endsWith('/') || !route ? `${route}index.html` : route,
      ),
    ),
    source,
  )
}
for (const file of files.filter((file) => file.endsWith('.html'))) {
  const html = readFileSync(file, 'utf8')
  for (const [, href] of html.matchAll(/\b(?:href|src)="([^"]+)"/g)) {
    if (/^(?:[a-z][\w+.-]*:|\/\/)/i.test(href)) continue
    const url = new URL(
      href.replaceAll('&amp;', '&'),
      `https://docs.local${base}${relativePath(file)}`,
    )
    if (!url.pathname.startsWith(base)) {
      errors.push(`${relativePath(file)} → ${href} (outside base)`)
      continue
    }
    let target = path.join(dist, decodeURI(url.pathname.slice(base.length)))
    if (existsSync(target) && statSync(target).isDirectory())
      target = path.join(target, 'index.html')
    if (!existsSync(target)) {
      errors.push(`${relativePath(file)} → ${href}`)
    } else if (
      url.hash &&
      anchors.has(path.resolve(target)) &&
      !anchors
        .get(path.resolve(target))
        .has(decodeURIComponent(url.hash.slice(1)))
    ) {
      errors.push(`${relativePath(file)} → ${href} (missing anchor)`)
    }
  }
}
assert.deepEqual(errors, [], '站内页面或资源链接不可访问')
console.log(
  `Checked ${pages.length} documentation sources and all generated HTML links under ${base}`,
)
