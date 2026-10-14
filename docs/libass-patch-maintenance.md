# Docker libass 补丁维护记录

Docker 构建使用官方 libass 0.17.5 源包，并应用固定提交
`d570c2f5ed55844ec9b77b085deb3cb8ef59cb4a` 的数值溢出处理补丁，目标为
CVE-2026-95127。补丁来自 [上游 PR #955](https://github.com/libass/libass/pull/955)，
截至接入时尚未合并，不能视作上游正式修复发行版。

源包及补丁的 SHA-256、ISC 许可证、提交身份和本地维护版本 `blc1` 记录在
`packaging/docker/patches/libass-source.json`。构建阶段校验源包与补丁哈希，
以零模糊匹配应用补丁，再编译安装到 `/opt/ffmpeg`。FFmpeg 使用这套共享库，
运行镜像保留源包、补丁、身份清单及原始许可证；不再安装 Debian 的 `libass9`。
保留 `libunibreak6` 及字体相关运行库。

## 当前验收状态

独立 `libass-builder` 镜像已完成源包校验、补丁应用、编译和安装。
随后用户明确要求跳过剩余安全检查及全部测试，直接追加提交到 PR #80。
因此尚未执行本次补丁的恶意字幕回归、修复前后渲染结果对比、最终运行镜像
重建和全量复验，也没有重新扫描最终镜像。清单中的 `compile_only` 表示这一限制，
不代表漏洞已通过完整验收或发行产物已经就绪。

后续维护应关注上游正式修复版本。替换补丁或升级源码时，需要同步更新身份清单与
Dockerfile 哈希，并重新验收真实字幕渲染、FFmpeg/PyAV 共享库加载和最终镜像。
