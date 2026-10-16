import assert from 'node:assert/strict'
import test from 'node:test'
import { checkPages } from './check-pages.mjs'

const settings = { repository: 'owner/repository', token: 'test-only-token' }

// Mock only the HTTP boundary; exercise the real deployment decision.
test('Pages workflow configuration passes with a read-only bounded request', async () => {
  let requests = 0
  await checkPages({ ...settings, request: async (url, options) => {
    requests += 1
    assert.equal(url, 'https://api.github.com/repos/owner/repository/pages')
    assert.equal(options.method, 'GET')
    assert.equal(options.headers.Authorization, 'Bearer test-only-token')
    assert.ok(options.signal instanceof AbortSignal)
    return Response.json({ build_type: 'workflow' })
  } })
  assert.equal(requests, 1)
})

for (const body of [{ build_type: 'legacy' }, {}, null]) {
  test(`Pages rejects a non-workflow configuration: ${JSON.stringify(body)}`, async () => {
    await assert.rejects(checkPages({ ...settings, request: async () => Response.json(body) }), /Source.*GitHub Actions/)
  })
}

for (const [status, message] of [[404, /settings\/pages/], [401, /读取.*权限/], [403, /读取.*权限/], [503, /HTTP 503/]]) {
  test(`Pages API HTTP ${status} blocks deployment with an actionable error`, async () => {
    await assert.rejects(checkPages({ ...settings, request: async () => new Response('', { status }) }), message)
  })
}

test('invalid repository or missing token is rejected before requesting the API', async () => {
  const request = async () => { assert.fail('invalid credentials must not cause an HTTP request') }
  for (const repository of [undefined, '', 'owner/repo/extra', 'https://example.invalid/', 'owner/repo?x=1']) {
    await assert.rejects(checkPages({ ...settings, repository, request }), /GITHUB_REPOSITORY/)
  }
  await assert.rejects(checkPages({ ...settings, token: '', request }), /GH_TOKEN/)
})

test('network and malformed API responses fail closed', async () => {
  await assert.rejects(checkPages({ ...settings, request: async () => { throw new Error('connection failed') } }), /connection failed/)
  await assert.rejects(checkPages({ ...settings, request: async () => new Response('not JSON') }), SyntaxError)
})
