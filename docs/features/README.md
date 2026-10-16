# 功能指南

按工作流选择专题。安装部署与第一次运行见[入门](../guide/README.md)，身份认证与配置也在该栏目集中介绍。

## 录制与输入

- [直播间、录制与弹幕](recording.md)：CLI 与 Web 录制、开播守候、预约、断流重连和弹幕原始档。
- [录播导入与分析](recording-import.md)：视频与弹幕格式、上传、预处理、恢复和分析进度。

## 分析与出片

- [语音转写与模型](asr.md)：ASR 主引擎与回退、CUDA、远程转写、质量与整场总结。
- [审核、字幕与剪辑](review-render.md)：人工审核、编码、弹幕与字幕、派生版本和后台作业。
- [高光评分插件](highlight-plugins.md)：规则、Shadow 与 Champion 模式及不可用时的回退。
- [上传与发布](publishing.md)：手动材料、biliup、前置校验和待核对的上传结果。

## 工作台与排错

- [Web 工作台与配置草稿](workspace.md)：功能入口、草稿、统一保存和配置生效边界。
- [账号与登录](../guide/login.md)：管理员会话、Bilibili 扫码与异常恢复。
- [故障排查](troubleshooting.md)：录制、转写、静态资源、任务取消和服务关闭。

## CUDA 安装与实际设备确认

GPU 运行时安装、离线 wheel 和实际设备诊断已集中到[转写指南](asr.md#cuda-安装与实际设备确认)。ASR 设备字段不会安装 CUDA 依赖。

## 排错

按[故障排查](troubleshooting.md)采集最小诊断信息；记录版本、故障时间和场次/任务编号，避免提交凭据或数据库。
