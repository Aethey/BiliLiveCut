import { pathToFileURL } from 'node:url'

/** Verify the repository has explicitly selected GitHub Actions for Pages. */
export async function checkPages({ repository, token, request = fetch }) {
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository ?? '')) {
    throw new Error('缺少有效的 GITHUB_REPOSITORY（owner/repository）。')
  }
  if (!token) throw new Error('缺少 GH_TOKEN；检查 Pages 需要 pages: read 权限。')

  const response = await request(`https://api.github.com/repos/${repository}/pages`, {
    method: 'GET',
    headers: {
      Accept: 'application/vnd.github+json',
      Authorization: `Bearer ${token}`,
      'X-GitHub-Api-Version': '2022-11-28',
    },
    signal: AbortSignal.timeout(15_000),
  })
  const settings = `https://github.com/${repository}/settings/pages`
  if (response.status === 404) {
    throw new Error(`未找到 Pages 站点。请由管理员打开 ${settings}，在 Build and deployment → Source 选择 GitHub Actions；若已启用，请核对 Pages 读取权限。`)
  }
  if (response.status === 401 || response.status === 403) {
    throw new Error(`无法读取 Pages（HTTP ${response.status}）；请核对令牌 pages: read 权限和仓库访问策略。`)
  }
  if (!response.ok) throw new Error(`Pages API 检查失败（HTTP ${response.status}），本次部署已阻止。`)
  const site = await response.json()
  if (site?.build_type !== 'workflow') {
    throw new Error(`Pages 尚未使用 GitHub Actions。请在 ${settings} 的 Build and deployment → Source 选择 GitHub Actions。`)
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    await checkPages({ repository: process.env.GITHUB_REPOSITORY, token: process.env.GH_TOKEN })
    console.log('Pages 配置检查通过：来源为 GitHub Actions。')
  } catch (error) {
    console.error(error instanceof Error ? error.message : 'Pages 配置检查失败。')
    process.exitCode = 1
  }
}
