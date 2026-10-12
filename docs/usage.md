# 使用指南

安装入口见[项目 README](../README.md)，全部配置见[配置参考](configuration.md)。

## 环境与源码安装

- Python **3.11 / 3.12**（推荐；部分 AI 依赖对 3.13/3.14 的预编译包可能尚未就绪）
- FFmpeg（已加入 PATH，或在 `.env` 指定 `FFMPEG_PATH`）
- *(可选)* C 编译器（MSVC/MinGW/GCC）— 用于编译加速模块；如不可用，自动回退纯 Python 实现

### Python 依赖源

境内安装推荐优先使用**阿里云 PyPI 镜像**，清华大学镜像作为备用源：

```
默认源  https://mirrors.aliyun.com/pypi/simple/
备用源  https://pypi.tuna.tsinghua.edu.cn/simple/
```

可在安装前设置当前 PowerShell 进程的环境变量（不修改系统级 pip 配置）。这两项属于 pip，不写入应用 `.env`：

```powershell
$env:PIP_INDEX_URL='https://mirrors.aliyun.com/pypi/simple/'
$env:PIP_EXTRA_INDEX_URL='https://pypi.tuna.tsinghua.edu.cn/simple/'
```

## 录制与直播间管理

直播间卡片可直接选择「开播后自动录制并分析」，同时启用两个开关并解除人工暂停；只保存单独开关仍保留原暂停状态。服务运行时显示守候、启动、录制、等待下一场及检测错误，程序退出或系统休眠时不会检测。录制前获取最新标题，所有录制入口（含 CLI、手动录制）在运行中默认每 30 秒刷新，单次超时 3 秒；页面轮询后可见。失败保留缓存并标注“上次获取”。场次保留开录标题、观测到的变更与结束标题，旧场次没有历史证据时显示未知；渲染片头按素材时刻的场次观测取标题，不改写已有成片或稿件。每日/每周预约在启动失败或房间已录制时也只创建一个后继，房间需开启「预约录制」。

```powershell
# 在已下载并解压的 BiliLiveCut 项目根目录执行

# 1) 创建虚拟环境并安装
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e . `
  --index-url https://mirrors.aliyun.com/pypi/simple/ `
  --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple/

# 2) 准备配置
if (-not (Test-Path .env)) { Copy-Item .env.example .env }   # 按需修改

# 3) 初始化数据库
python -m app.cli init

# 4) 登记一个你有授权的直播间
python -m app.cli add-room "https://live.bilibili.com/你的房间号" --authorize

# 5) 查看 / 检查
python -m app.cli list-rooms
python -m app.cli check 你的房间号

# 6) 开始录制（Ctrl+C 停止；默认值由 RECORDING_PIPELINE_ENABLED 控制）
python -m app.cli record <db_id>
```

录制产物位于 `storage/raw/session_<id>/`。默认以 300 秒为分片目标；FFmpeg 按实际关键帧落盘，因此单片时长会在 5 分钟附近小幅浮动。

### 弹幕数据库与原始档

默认开启弹幕采集（设置中心的「采集弹幕」，对应 `COLLECT_DANMAKU`）。内置 Bilibili 来源无需另装直播源插件；能否接收消息取决于实际连接与平台限制，有 Cookie 时先尝试登录访问，失败按现有策略回退匿名连接。

数据库 `storage/blc.db` 的 `danmaku` 表继续用于分析，可能受密度采样影响。每场另存 `storage/raw/session_<id>/danmaku.jsonl`，在解析及数据库采样前保存收到的 Bilibili 消息；外部直播源插件保存校验后的公共事件。它记录已接收到的消息，不能补回断连期间的消息或旧场次。

在工作台「弹幕」页选择场次，点击「下载原始弹幕（JSONL）」；也可访问 `GET /api/sessions/{session_id}/danmaku-archive`。录制中下载只包含请求时已写完的完整记录。每行含 `version`、`kind`、`session_id`、`platform`、`source_id`、UTC `received_at` 和 `payload`：`session` 是场次标识，`raw` 是 Bilibili 解码消息，`normalized` 是插件公共事件（含 `occurred_at`、`content`、`user_name`）。不写入连接鉴权包、Cookie 或宿主连接配置。

禁用采集或来源不支持弹幕时不生成文件。仅有场次标识不代表已成功连接或有弹幕，页面同时显示真实采集状态。磁盘或权限错误会显示写档失败并停止本场文件写入，数据库采集仍继续。每批写完即关闭文件；异常退出可能留下最后一条不完整记录，下载会排除该行。原片自动清理只删除媒体分段，弹幕原始档单独保留；请按需要归档或清理。

### 渲染设备与房间关键词

「设置中心 → 成片与编码」可直接调整响度标准化、去首尾静音、竖屏、字幕、时长与编码选项。视频编码设备选择 CPU 即关闭硬件编码；NVIDIA、Intel、AMD 分别使用 FFmpeg 的 NVENC、QSV、AMF，需要对应显卡和驱动，所用 FFmpeg 也必须包含对应编码器。硬件初始化失败会保留明确错误，不自动改用 CPU。视频滤镜和音频处理仍可能使用 CPU，因此不能用 CPU 占用率判断硬件编码是否启用。

CPU 使用 CRF 和 CPU 速度档；硬件使用独立质量值和速度/均衡/质量档，质量值越小通常越清晰，但不同后端不具有相同画质语义（NVENC/AMF 使用 QP，QSV 使用 ICQ）。主片、字幕互补版和片头拼接沿用任务的选项快照；归档版使用 14、压制版使用 26 的对应后端质量值。保存后的新设置用于下一任务，已有任务和成片保持各自配置。

「房间配置 → 高光关键词」每行一个词，与全局词表合并并忽略大小写去重，仅影响该房间后续的热点分桶和候选评分。清空后恢复仅使用全局词表，不清除其他房间设置，也不自动重算已有素材。ASR 热词仍由独立的「ASR 热词」入口管理。

### 弹幕烧录与样式

「设置中心 → 成片与编码」的「烧录弹幕」默认关闭，与「烧录字幕」独立。开启后把本场已采集或从本地文件导入的普通文本弹幕烧入画面；礼物、SC 和进场消息不参与，无文本时不叠加。可选择从右向左滚动、顶部固定或下方固定，并设置字号、六位 RGB 颜色、不透明度、显示秒数及最大行数。字号以 1080 高画面为基准同比缩放，使用系统可用的微软雅黑或字体回退。

烧录使用场次保存的延迟校准值，按本次精剪起点对齐；跨原始分段和片头拼接保持同一时间轴。主片、审核渲染、归档、压制以及 ASR 字幕互补版本均沿用弹幕设置，因此“无字幕”版本只去掉 ASR 字幕。需要完全无文字时同时关闭字幕和弹幕。竖屏先调整画面，再叠加弹幕；底部 20% 始终预留给字幕。

为控制遮挡，同一行直到前一条离场才复用，满行时舍弃新弹幕。每次剪辑最多处理按时间排序的前 20,000 条普通弹幕，超过会记录警告；每条最多显示 120 字，固定模式按画面宽度进一步截断。控制字符被移除，ASS 指令作为普通文字显示。生成派生版本时，成片目录同时保存复用的 `.ass` 侧车；本地导入的原始颜色、字号与滚动模式不继承，统一使用本次渲染设置。

### 字幕与过期任务

开启「烧录字幕」后优先使用 ASR 词级时间戳。Fun-ASR-Nano 等只返回正文时，按选中片段的正文长度和时长估计字幕位置；这能生成字幕，但不等同于逐字对齐。跨分段切片及有字幕/无字幕变体共用本次剪辑范围，字幕以精剪内容起点为零，不使用整场旧分段错位生成。启用片头时先精剪并烧录，再拼接标题卡，画面和字幕一起后移；这一步会额外转码，主片和派生版本均保留各自编码选项。没有字幕内容的窗口不会生成伪字幕互补版。

运行中的任务持续更新心跳。超过过期阈值后由回收器重排；领取和续写使用统一日期类型，回收按 UTC 时间判断。应用日志中的 `recover_stale` 记录恢复结果，续写异常也会记入错误日志；处于远端发布中或待核对状态的任务继续受防重复发布保护。

## 多引擎 ASR 转写与高光判断

### ASR 流水线

安装 AI 依赖：

```powershell
pip install -e ".[asr-all]" `
  --index-url https://mirrors.aliyun.com/pypi/simple/ `
  --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple/
pip install -e ".[llm]" `
  --index-url https://mirrors.aliyun.com/pypi/simple/ `
  --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple/
# 多引擎 ASR 需要 funasr + modelscope
pip install funasr modelscope
```

```powershell
# 对已录制的片段
python -m app.cli process <segment_id>
python -m app.cli list-candidates       # 查看高光候选

# 边录边分析（显式覆盖全局默认值）
python -m app.cli record <db_id> --pipeline
```

`RECORDING_PIPELINE_ENABLED=true` 时，Web 手动开始/恢复、预约、崩溃恢复以及未显式传参的 CLI 录制都会启用实时转写与高光分析。可在控制台“设置中心 → 全部参数 → 录制与自动化”覆盖该默认值；CLI 可用 `--pipeline` 或 `--no-pipeline` 对单次录制覆盖。`TRANSCRIPT_LLM_REFINE_ENABLED=true` 时，每个切片完成 ASR 后还会调用已配置的大模型补全标点、整理正文并生成片段概括；调用失败时保留原始 ASR，不阻断流水线。录制流水线开关从下一次开始或恢复录制生效，转写整理开关从下一次转写任务生效。

主播下播、断流或平台暂时无法返回播放地址时，录制器会继续重试，但不会无限挂起。连续失败达到 `RECORDING_RECONNECT_MAX_ATTEMPTS`（默认 20 次）或从断流开始经过 `RECORDING_RECONNECT_MAX_ELAPSED_S`（默认 180 秒）时，任一条件先满足都会自动结束本场录制并正常执行会话收尾。若平台仍报告直播中，自动监控会等待一次真实离线再允许下一次开播，避免立即进入相同失败循环；成功恢复并累计产出至少一个目标分段时长后，次数和计时才会归零，短末片不代表恢复。将某一项设为 `0` 可单独禁用该限制；两项都设为 `0` 时仍保留 20 次安全上限。所有等待（包括来源返回的 retry_after）都受剩余时长预算限制。磁盘、权限、FFmpeg 缺失和参数/编解码器故障直接结束为错误，并暂停自动重新启动；处理原因后手动启动或重新开启自动录制。网络、上游、受损直播输入及未知错误按预算重取流；HTTP 403 不直接解释为 Cookie 过期。

`COLLECT_DANMAKU=true` 时，录制器会按 Bilibili 直播网页的 WBI 请求格式获取短期弹幕 token。有已保存 Cookie 时优先使用登录请求，并以 Cookie 中的 `DedeUserID` 完成 WebSocket 鉴权；登录接口返回业务错误或登录鉴权被拒绝后，会立即改用不带 Cookie、`uid=0` 的匿名链路，录制和弹幕接收不会因此停止。匿名连接存活期间，程序按 `DANMAKU_LOGIN_RETRY_INTERVAL_S` 定时探测登录链路；单场累计失败达到 `DANMAKU_LOGIN_RETRY_MAX_ATTEMPTS`（默认 5 次，首次计入）后，本场不再发送 Cookie。将最大次数设为 `0` 可始终匿名采集。

默认启用以 Fun-ASR-Nano 为首选的四层 ASR 流水线（`ASR_PRIMARY=funasr_nano`），也可切换到 Paraformer 或纯 Whisper：

```env
ASR_PRIMARY=funasr_nano       # 默认；也可设为 paraformer 或 whisper
ASR_VAD_MAX_SEGMENT_S=30      # Nano 的 FSMN-VAD 单句上限（秒）
ASR_TASK_MAX_CONCURRENCY=1    # 同时转写的录制分段数；CUDA 可按显存提高到 2～8
ASR_FALLBACK_WHISPER=true     # 主引擎失败时自动兜底
TRANSCRIPT_LLM_REFINE_ENABLED=true  # 用已配置 LLM 整理正文并生成片段概括
TRANSCRIPT_LLM_REFINE_MAX_TOKENS=65536  # 五分钟转写整理的最大输出预算
HIGHLIGHT_LLM_MAX_TOKENS=65536      # 高光复核的最大输出预算（含推理 token）
```

`ASR_TASK_MAX_CONCURRENCY` 控制任务级并行，`ASR_PRIMARY_MAX_CONCURRENCY` 等变量控制对应模型角色的同时推理数。模型池按实际模型身份复用空闲实例，同一实例的推理互斥；提高并发可能加载更多模型并增加显存占用。设备和模型选择在下一任务生效，活动任务保留原快照；并发与常驻策略在后续检查生效，空闲卸载不会中断推理。

转写整理和高光复核默认各预留 `65536` 个最大输出 token，避免推理模型在处理五分钟切片时耗尽额度而没有正文；可按模型能力分别在 `128-65536` 和 `512-65536` 范围内调低。该值是单次请求上限，实际用量仍以模型返回的 token 数为准。

实时转写页先用“录制场次”下拉选择历史直播，再显示该场全部转写；旧场次不会因为新增记录超过固定条数而消失。“重新识别”会删除当前场次可重建的自动分析结果后重新排队；若存在正在运行的任务，服务端会拒绝并说明原因。每条转写还会显示源 TS 文件名；需要直接进入 NLE 剪辑时使用“无损导出 MP4”，应用会保留原始编码并把首个有效视频帧校准到 `0s`，避免普通 TS→MP4 重封装因 AAC 预滚产生首帧黑画面。

**工作原理与成本控制**：先用零成本规则特征（音量峰值、关键词、语速突增、音频特征、弹幕热度）算出 `rule_score`；只有超过初筛阈值才调用大模型复核。新直播间默认初筛/候选/人工审核阈值为 `0.28/0.38/0.32`，自动批准/发布阈值为 `0.72/0.80`，也可通过设置中心的全局阈值调整新房间默认，`.env` 同名参数仍提供部署默认；房间级值可在“直播间”及“房间自动化与端口”中调整。Web「设置中心 → 大模型服务商」没有启用服务商时自动走**纯规则模式**，完全可用、零费用。

Event-first 热点层会先按 10 秒信号桶、90 秒直播自身滚动基线和 20 秒检测 tick 生成 `provisional HotspotEvent`。弹幕、音频、SenseVoice、可选 ASR 与缓存趋势按可用证据动态归一化，因此尚无 ASR 不会把热点分数拉成零。热点峰值会反向生成默认前 35 秒、后 55 秒的高优先级局部 ASR；完成后仍保留后台完整 ASR，用于历史、搜索、字幕与整场总结。相邻检测结果会在录制连续且语义或信号连续时跨原始分段归并，稳定结束后进入 `confirmed`；代表弹幕会同时保留高频反应和信息量较高的上下文，不再让多条 `???/666` 挤掉事件内容。EventEnricher 仅根据带 ID 的事件证据束生成结构化标题、摘要和类别；未知引用、证据外实体/数字、无归因的弹幕猜测及陈旧结果都会被拒绝并降级为保守说明。随后 ClipScorer 按完整事件的多信号、证据质量和录制连续性计算成片价值；只有达到房间阈值才关联既有候选审核链，动态边界也始终限制在同一连续录像块内，不会跨断流缺口。详细参数、生命周期、评分和证据字段见 [热点检测器说明](hotspot-detector.md)。

> **大模型选型（境内）**：系统采用 **OpenAI 兼容协议**，可在 Web「设置中心 → 大模型服务商」同时配置 DeepSeek / 通义千问 / Kimi / 智谱 GLM，并按优先级执行运行时故障切换。

### OpenAI 兼容远程转写

在「设置中心 → ASR」同时填写远程转写基础地址、模型 ID、服务要求的密钥，并将主引擎切换为 `openai_compatible` 后保存；默认继续使用本地 Fun-ASR-Nano。基础地址包含服务需要的 `/v1` 等前缀，程序追加 `/audio/transcriptions`；不接受 URL 凭据、查询参数和重定向。本机 HTTP 服务可不填密钥，公网服务使用 HTTPS。模型 ID 由服务提供方确定，程序不替换或猜测模型名称。

远程转写独立于 LLM 服务商：选择它会将标准化音频上传到所配服务。默认 `json` 接收正文，字幕使用明确的估算时间；仅在服务支持时选择 `verbose_json`，同时请求词与句的时间戳。程序校验时间戳范围、顺序、句段不重叠、响应格式、单块 2 MiB 及整次任务累计 8 MiB 响应上限，不把服务未提供的置信度当作零或满分。接口格式参考 [OpenAI 官方转写接口](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)。

`ASR_REMOTE_CHUNK_S` 默认 300 秒、范围 1~600 秒，音频统一为 16kHz 单声道 PCM16，每块低于 20 MB，串行上传并恢复原窗口时间轴；长录音的分块边界可能截断词句，可按服务要求调整。任一块失败则整个本次转写失败，不保存部分文本。空文本仍需本地 VAD 确认无语音，所以运行环境需保留现有 `asr-whisper` 音频/VAD 依赖；无需下载 Whisper 模型即可做这项核验。

远程主引擎不加载 FunASR、SenseVoice 或局部复核模型；`ASR_PRIMARY_MAX_CONCURRENCY` 控制远程任务并发，主引擎 device/keep_loaded 对 HTTP 调用不生效。`ASR_FALLBACK_WHISPER` 开启时，远程失败或文本退化会按现有机制回退本地 Whisper，需准备本地模型；关闭后保留远程失败，由任务已有的有限重试处理。HTTP 客户端本身不自动重试；`ASR_REMOTE_TIMEOUT_S` 控制读写超时，ASR 总推理预算、取消和租约丢失继续由生产子进程监管。接口响应成功并不代表费用为零，远程 ASR 费用不计入 LLM 文本预算。

## 自动切片与文案

把高光候选生成为可投稿的 MP4：

```powershell
python -m app.cli produce <candidate_id>     # 切片 + 文案一步到位

# 全自动链路
python -m app.cli record <db_id> --pipeline --produce
```

**后处理选项**（在“设置中心 → 全部参数 → 成片与编码”配置）：响度标准化 `CLIP_LOUDNORM`、去首尾静默 `CLIP_REMOVE_SILENCE`、烧录字幕 `CLIP_SUBTITLE`、烧录弹幕 `CLIP_DANMAKU`、最大时长 `CLIP_MAX_DURATION_S`、画质 `CLIP_VIDEO_CRF`。主成片、审片预览和派生版本都会重建音视频起始时间戳，首个真实画面从 MP4 的 `0s` 开始，不需要在剪辑软件里手工抽掉黑帧。

**多版本出片**：每个 HighlightEvent 可生成多个 ClipVariant（单段版、完整上下文版、带 ASR 字幕版、无 ASR 字幕版、投稿压制版、高码率归档版），横屏输出以 1920×1080 为主。

## Web 管理后台

```powershell
pip install -e ".[web]" `
  --index-url https://mirrors.aliyun.com/pypi/simple/ `
  --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple/
python -m app.cli serve              # 默认 http://127.0.0.1:8000
```

Portable Launcher 的 Web 端口可在“设置中心 → 房间自动化与端口”保存，范围为 `1..65535`。配置写入安装根目录的 `config/launcher.json`，只在下次启动 Web 时生效；源码 CLI 未传 `--port` 时也使用该保存值，显式参数优先；页面会同时显示已保存端口、当前实际端口和是否需要重启。端口被占用时会明确失败，不会随机切换端口。无密码模式仍只监听并接受 `localhost`、`127.0.0.0/8` 或 `[::1]` authority；远程部署必须设置 `ADMIN_PASSWORD`。

功能概览：**直播间管理 / 录制状态 / 实时转写 / 候选审核（横屏审片工作台）/ 成品切片 / 主题管理 / 合集编辑 / 插件中心 / 运维面板 / 任务队列监控 / 上传设置**。

插件默认从 `./storage/plugins` 读取，可通过 `PLUGIN_DIR` 修改。扫描只读取 `plugin.json`，入口代码仅在管理员显式启用后执行；插件与主程序同进程运行，因此只应启用可信插件。开发接口、清单 Schema 和最小示例见 [`plugin/README.md`](../plugin/README.md)。

### 统一配置与持久化

左侧“设置中心”是唯一总入口。全部参数按录制、语音、大模型、网感、高光、审核、成片、存储、发布、通知与系统分类；默认显示常用项，高级项按类别展开，支持中文和环境变量名搜索。大模型服务商、房间自动化、账号登录、插件及模板仍有独立子页；原 `?tab=features/models/plugins/login/templates/intro-templates` 链接继续直达。上传和网感业务页保留运行状态，并链接到对应设置。

修改只形成内存草稿，底部展示本次影响与生效时间，点击“保存修改”才提交。切换页面及定时刷新保留草稿；校验失败定位字段，版本冲突时可读取最新值并保留草稿供核对。密码不会保存在浏览器本地存储，离开页面会提示未保存内容。模型计费单价可编辑，空白字段不会被当成删除服务商；密钥留空保持，勾选清空才删除。

`GET /api/settings/configuration` 返回完整的 151 项配置清单、有效值、来源、范围和生效时机；`PATCH` 接受 `values`、`reset`、`clear` 和 `revision`，整体校验后一次提交。Web 覆盖优先于环境/项目默认，重启后保留。凭据不回显，留空保持，显式清空会阻止环境回退；恢复默认删除覆盖。正在执行的任务使用启动快照。

高光入选、进入人工审核、自动批准和自动投稿这四项全局阈值是**新房间默认值**；已有房间使用自己的保存值。自动投稿须同时满足全局自动上传、biliup、房间自动上传开启，并达到房间自动投稿阈值；否则留待人工确认。自动清理默认关闭，启用后每小时检查；清理保留活动任务、媒体使用、审核、重分析、共享文件和未登记文件。

数据库、存储根目录、插件目录、日志和登录身份仍作为启动配置，避免运行中搬迁数据或改变身份。完整字段映射及边界见 [统一配置清单](configuration.md)。

### 可插拔高光评分

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

多人审核入口为 `/review/queue`。管理员仍使用 `ADMIN_PASSWORD`；审核员账号在 `.env` 中配置；领取租约和盲审开关也可在设置中心的“审核”分类保存覆盖。部署默认示例：

```env
ADMIN_PASSWORD=change-admin-password
REVIEWER_ACCOUNTS_JSON={"reviewer":"change-reviewer-password"}
REVIEW_CLAIM_TTL_S=900
REVIEW_BLIND_MODE=true
```

审核员只能访问 `/review/*` 和审片所需的视频/封面接口；管理员可显式强制接管他人的有效领取。远程部署必须同时设置 `ADMIN_PASSWORD`，真实密码不得提交到仓库。

批准出片、审核重渲染、合集渲染和上传/重试会立即返回后台作业，不再长时间占用 HTTP 请求。作业状态持久化，可在控制台“任务队列”查看进度、错误和结果，也可通过 `GET /api/jobs`、`GET /api/jobs/{job_id}` 查询，通过 `POST /api/jobs/{job_id}/cancel` 或 `/retry` 取消、重试。服务重启会恢复安全可重跑的渲染作业；已开始的上传不会自动重放，避免平台已收件但本地未知时重复投稿。FFmpeg 渲染取消会主动终止当前外部进程并清理未完成输出。

### 自动化开关

五个独立开关，可自由组合：

- `auto_record` — 自动检测开播并录制
- `auto_analyze` — 自动转写 + 高光分析
- `auto_render` — 自动生成切片
- `auto_approve` — 高分候选自动批准
- `auto_upload` — 自动提交上传

每个开关逐阶段独立判断，修改后未完成任务按新配置执行。支持房间级别配置覆盖。

Portable Web 控制台可在“设置中心 → 房间自动化与端口”中按直播间独立修改上述五项开关；预约录制、阈值自学习、弹幕情绪与审核阈值也集中在同一页。房间级 `auto_upload` 需配合设置中心“上传与发布”分类的两个全局开关，并达到房间自动投稿分数阈值。

## 上传与部署

- 默认 `ManualUploader`：不调用任何平台接口，只导出待上传清单。
- `BiliupUploader`：默认关闭，需手动在 Web 后台开启并配置 `BILIUP_UPLOAD_CMD`。⚠ 走你自己的登录态，风险自负。

**上传前置校验**：文件完整性、标题/简介合规查重、投稿频率限制（`UPLOAD_MAX_PER_HOUR`），失败重试（`UPLOAD_MAX_RETRIES`）。

上传超时或结果无法确认时，系统保留尝试记录并等待人工核对，不会自动重新投稿。核对平台结果后，可在成品页确认发布；启动和运行期间也会恢复已经记录的发布成功结果。已发布素材及活动任务引用的文件不会被拒绝候选清理删除。

### Docker 部署

```bash
test -f .env || cp .env.example .env
# Docker 构建上下文为仓库根目录，Compose 文件位于 packaging/docker/
docker compose -f packaging/docker/compose.yaml up --build -d
# 打开 http://localhost:8000
```

或者使用便捷脚本：

```bash
# Windows
scripts\docker-up.bat

# Linux/macOS
bash scripts/docker-up.sh
```

详情参见 [packaging/docker/README.md](../packaging/docker/README.md)。

## 排错

Bilibili 录制以 `STREAM_QUALITY` 为画质上限，从高到低尝试：默认原画 `10000`、蓝光 `400`、超清 `250`、高清 `150`、流畅 `80`，并纳入接口声明的中间档位；每次画质协商最多请求 8 个不同档位，为标准低档保留预算。有可用地址即采用接口实际返回的 qn，同画质才按 `PREFERRED_STREAM_PROTOCOL` 选择协议；画质未知保留为空，不把请求值伪装为实际画质。确认直播中但所有档位均无地址时按取流临时故障重试，不解释为下播，也不为该房间失败设置平台限流。登录态、风控和网络错误按原有来源预算处理，不通过降画质绕过。录制重连重新获取临时地址，重试次数及时间上限仍有效；连续临时取流失败耗尽预算保留 ERROR 状态。只有 HTTP/TLS/TCP 日志明确出现 EOF 时才归为传输异常，普通 EOF 不证明本地媒体损坏。

优先提供程序版本、故障时间以及房间、场次或任务编号。Web 请求出现“服务处理失败”时，同时提供页面上的日志定位编号。后台“错误日志”保存 WARNING/ERROR 的安全异常链；`storage/logs/blc.log` 还保留 DEBUG 信息及具体调用文件/行号。应用与 Uvicorn 服务器错误均进入同一诊断通道，访问日志不记录查询参数。两类日志使用同一脱敏规则，网络地址仅保留主机名，不记录签名路径、查询串、请求凭据或异常局部变量。提交前仍请检查个人路径等内容，不要上传 `.env`、数据库或模型缓存。

`blc doctor --yes` 使用当前 `FFMPEG_PATH`、`FFPROBE_PATH`、`STORAGE_ROOT` 和数据库配置检查环境。模型目录按运行时 `BLC_MODELS_DIR` 检查；未设置时明确提示使用 SDK 缓存。磁盘/内存读取失败代表未知，不代表剩余空间为零或资源充足。

录制的片段登记、stderr 排空或磁盘保护失败会结束当前录制并保留 ERROR 状态。弹幕本地故障只停止弹幕采集，记录实际确认到的覆盖时间。上传显示失败或待人工核对时，先核对平台结果；后台作业不会把这些状态当作上传完成。

| 现象 | 排查 |
|---|---|
| `ffmpeg 不是内部或外部命令` | 安装 FFmpeg 或在 `.env` 设置 `FFMPEG_PATH` |
| `check` 显示未开播 | 主播未直播时无流，属正常 |
| 取流报错 / 403 | 部分高清晰度需登录态，可在设置中心登录账号或填写 Cookie |
| 弹幕 token 返回 `code=-352` | 登录请求失败时会立即匿名兜底并定时重试；匿名请求也被风控时会按配置间隔重试，录制与实时转写继续。无需反复手动登录 |
| 片段未生成 | 看 `storage/logs/blc.log` 中 `[ffmpeg]` 行 |
| ASR 主引擎未加载 | 确认 `pip install funasr modelscope` 已执行 |

### 转写整理与时间边界

开启 LLM 转写整理后，整理正文与摘要会持久化，转写页面、整段语义分析和整场总结使用整理结果。原始 ASR 正文与词时间戳单独保留，人工修改后旧整理结果失效。整理正文没有逐词对齐信息：已有词时间戳的局部高光及成片文案继续读取对应时间范围内的原始词；覆盖完整分段时使用整理正文。原本没有词时间戳的分段沿用按时长比例裁剪，并标记为非精确。

审核队列按当前筛选分页，可通过上一页、下一页浏览全部候选，计数包含全部历史记录。上传预检因文件缺失、标题或频率限制而跳过后，处理原因并重试即可重新预检；平台结果未知的任务仍须先核对。

## ASR 取消、无语音与整场覆盖

分段任务取消分为请求与执行收尾：已领取的转写、分析、渲染显示“取消中”，禁止重试，原租约只用于确认收尾，不能提交结果；执行、模型进程及资源释放后才显示“已取消”。无法确认模型进程或 IPC 清理时保留取消中和错误诊断，不能被过期恢复提前结算。排队任务可立即取消。异常重启后过期的取消请求会结算为已取消，不会自动重新运行；手动重试仍回到原执行阶段。正在发布的任务不接受取消，须等待发布完成或核对平台结果，避免误报未投稿。

人工批准出片也使用同一套取消收尾：任务取消和后台作业取消都能中止媒体处理，不会将用户取消重新排队；每次执行使用独占成片与封面文件名，保留既有版本。成片及文案提交前在写事务内复验原租约，取消后的迟到结果不能写回。

所有生产 Worker 与默认 CLI 转写使用受监管 ASR 子进程。模型调用、结果迭代、管道背压和标准化都受取消及有限期限控制；取消旧租约后等待原进程退出再给新尝试分配资源，提交前在写事务内复验租约。`asr_progress` / `asr_diagnostic` 记录任务、场次、租约、PID、模型阶段、音频样本数与耗时，故障保留脱敏异常链。实际现场的 GPU 卡死仍需日志与设备证据，不能由“VAD removed all audio”一行推断。

空文件、损坏 PCM 和 SDK 错误不会伪装成功；合法音频经 VAD 确认没有语音后保存 `no_speech`，继续使用已取得的音乐/笑声事件。单独安装 `.[asr-funasr]` 也会安装公共 CPU VAD 所需的 faster-whisper。

Nano 与 Paraformer 的 FSMN-VAD 固定使用 FP32 浮点参数，Nano 的语言模型仍采用自身配置的精度，避免 VAD 的 `Float / BFloat16` 冲突。在线加载通过 SDK 的 `model_revision` 使用各引擎固定版本。模型按角色、来源和配置在每个 ASR 子进程内独占复用；初始化失败释放名额，下次任务可以重试。主模型推理失败时依次回退 Paraformer、启用的 Whisper，保留主模型错误和次级错误，不将失败异常帧长期留在流水线缓存中。不同 PID、配置变化或空闲回收后的重新加载有各自独立的缓存，须结合进程及模型诊断判断。

整场总结等待活动任务收尾，终态缺口允许生成“部分完成”报告并列出缺失片段、时间和原因；后台扫描已结束场次，手动重试、补齐转写后重新生成并标明补齐完成，无需打开时间线触发。超时预算与角色并发详见[配置参考](configuration.md#asr-与-llm-有限等待)。

Windows 静态 JS/MJS/CSS 响应不依赖系统 MIME 映射。白屏或“添加直播间”无响应时记录浏览器控制台错误、失败资源 URL 和响应 Content-Type；服务端仍保留 HEAD、Range、缓存与 404 语义。

关闭服务时先通知计算停止，再依次收尾监控、录制、Web 作业、Worker、插件及指标采样。启动失败或某一服务关闭失败仍会执行其余资源清理，并保留异常。受监管媒体命令使用独占进程树，取消、超时及主命令退出都会清理后代，不影响其他程序；关闭期间不再启动新媒体命令，中断的任务重新排队且不消耗业务重试次数。Web 作业关闭等待超时会保留运行/取消中的处理器登记并阻止新作业，不会把尚未退出的同步线程当作已完成；需要等外部请求的有限超时或处理器实际返回。`Finished server process` 仅表示服务器生命周期结束，须以实际 Python 进程退出及命令行返回确认关闭。
