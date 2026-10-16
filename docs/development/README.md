# 开发与扩展

本栏目面向修改宿主、开发插件和维护发行的开发者。运行应用先看[入门](../guide/README.md)，算法细节见[技术参考](../reference/README.md)。

## 维护宿主

1. [开发环境与验证](environment.md)：uv、venv + pip、原生扩展、测试与发行约束。
2. [Web 页面开发与验证](frontend.md)：HTML 模板、CSS、JavaScript 模块与交互检查。
3. [文档站维护](documentation.md)：分类、路由、链接检查和 Pages 部署。

## 扩展能力

- [插件接口](../../plugin/README.md)：清单、设置、生命周期和能力注册。
- [直播源契约](live-source-plugins.md)：房间身份、取流、可选弹幕、停用与安装。
- [抖音直播源接入](douyin-plugin-handoff.md)：现有插件的安装入口、公共接口和开发测试。

## 发行

[Portable 构建](../../packaging/portable/README.md)描述 Lite/Full、运行时、Payload 与模型包交付。Docker 的实际运行与构建命令在[部署说明](../../packaging/docker/README.md)维护。
