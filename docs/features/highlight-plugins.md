# 高光评分插件

插件 API v1 支持 `highlight_scorer` 能力，同一时间只允许启用一个提供者。宿主把片段、会话、房间、转写、词时间戳、弹幕窗口、聚合音频、ASR 辅助信息和规则分转换为无 ORM 的只读 DTO；插件不能直接依赖主程序数据库。

- `off`：不执行模型，保持原规则链路；
- `shadow`：保存 Champion/Shadow 概率和模型身份，不改变最终主评分；
- `champion`：用 Champion 概率替换规则主评分，再进入原有 LLM 融合、阈值、去重和审核流程；
- 插件缺失、停用、模型不可用、Schema 不兼容、异常或概率非法时，宿主记录原因并回退规则评分。

人工审核提交后，宿主把明确批准映射为正样本，把 `rejected/not_exciting` 映射为负样本；保留、上下文/边界/字幕/画面问题及撤销不被伪造为负样本，而是通知原插件删除同一 `sample_id` 的旧标签。反馈写入失败不会回滚已经提交的审核。房间级模式可在控制台“配置 → 功能开关 → 高光评分插件”中设为继承、关闭、Shadow 或 Champion。

真实插件联调是跨仓库显式检查，不进入宿主默认测试集，也不会在缺少外部插件时产生跳过项。Windows PowerShell 可执行：

```powershell
$env:BILILIVECUT_HIGHLIGHT_SOURCE = "D:\path\to\BiliLiveCut_HighLight"
python -m pytest scripts/external_tests/test_highlight_plugin_external.py
```

独立参考实现、训练 CLI 和模型注册表位于 [StarGazerQQD/BiliLiveCut_Highlight](https://github.com/StarGazerQQD/BiliLiveCut_Highlight)。
