# 抖音直播源接入

抖音直播通过独立的 [DouyinCompatibility 插件](https://github.com/StarGazerQQD/BiliLiveCut_DouyinCompatibility)接入。使用现有插件时，按其仓库的安装说明操作；本文面向维护或开发抖音直播源的开发者，介绍宿主公共接口、依赖安装和测试方法。

## 兼容要求

宿主需要提供 `app.plugins.live_source.LIVE_SOURCE_API_VERSION == "1"`，最低正式版本为 `0.1.18.4-alpha`。插件加载时会检查契约版本；Portable 还需检查实际 Runtime，不能只依据发行版本号判断兼容性。完整类型定义见[直播源契约](live-source-plugins.md)，可加载的最小示例见 [live-source-example](../../plugin/live-source-example/README.md)。

插件使用以下公共导入，平台解析、签名和专有依赖由插件管理：

```python
from app.plugins import BasePlugin, PluginContext, PluginSetting
from app.plugins.live_source import (
    LIVE_SOURCE_API_VERSION, LiveSource, SourceDescriptor, SourceRoom,
    RoomSnapshot, LiveStatus, StreamPreference, StreamSpec,
    SourceError, SourceTemporaryError, SourceRateLimited,
    SourceAuthenticationError, SourceInvalidInput, SourceUnavailable,
    DanmakuSource, DanmakuEvent, DanmakuStatus, DanmakuSink, DanmakuStateSink,
)
```

## 清单与生命周期

插件目录默认为 `storage/plugins/douyin-live/`，目录名与清单 `id` 一致。`plugin.json` 示例：

```json
{
  "id": "douyin-live",
  "name": "抖音直播源",
  "version": "1.0.0",
  "api_version": "1",
  "live_source_api_version": "1",
  "entrypoint": "main.py:Plugin",
  "settings_page": true,
  "capabilities": ["live_source"]
}
```

入口继承 `BasePlugin`，声明 `settings_schema`，在 `on_enable(context)` 中创建来源并调用 `context.register_live_source(source)`。来源注册后，宿主负责关闭它；插件无需访问宿主 ORM、注册器、Recorder、监控器或调度器，也无需修改主程序。

来源的 `descriptor` 使用 `SourceDescriptor(platform="douyin", name="抖音", domains=(...))`，其中 `domains` 填写实际支持的精确主机名，包括支持的短链域名。来源实现以下可取消的异步方法：

```python
async def resolve_room(self, value: str) -> SourceRoom: ...
async def get_room_info(self, room: SourceRoom) -> RoomSnapshot: ...
async def get_streams(self, room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]: ...
async def aclose(self) -> None: ...
```

HTTP 客户端由来源持有，`aclose()` 幂等释放。使用同步 SDK 时，将阻塞 I/O 隔离到线程或受控执行器，并配置有限超时；取消返回前等待已开始的线程工作结束。请求预算、限流退避、录制重连及停用收尾由宿主管理。

## 房间身份、状态与取流

- `source_id` 是稳定的平台房间字符串 ID，不使用每次开播变化的场次 ID 或宿主数据库主键。
- `canonical_url` 是无凭据、无查询参数的规范 HTTP(S) 地址；短链和完整地址应解析到同一个房间身份。
- 无法确定状态时返回 `LiveStatus.UNKNOWN`，网络故障抛出类型化错误，只有明确下播才返回 `OFFLINE`。
- 清晰度使用平台字符串。每次 `get_streams` 刷新临时播放地址，并按 `StreamPreference` 排序候选。
- 请求头通过 `StreamSpec.headers` 提供，媒体输入仅支持 HTTP(S)；FFmpeg 参数由宿主生成。
- Cookie、token 和签名 URL 不写入日志、公开错误或普通持久字段。

## 插件设置

平台凭据可声明为 `PluginSetting(key="cookie", label="抖音 Cookie", kind="password")`，通过 `context.get_setting("cookie", "")` 读取。此设置独立于 Bilibili Cookie 和宿主私有配置。

密码字段留空保存表示保留旧值，JSON `null` 表示清空。来源可在每次请求时读取设置；若实现中缓存设置，需要在插件说明中标明修改后须停用再启用。启用阶段允许尚未填写凭据，便于用户先启用插件，再进入设置页面。

## 可选弹幕能力

来源可实现 `collect_danmaku(room, emit, state)`；缺少该能力不会阻止录制、转写和分析。

实现时，连接成功后才报告 `AVAILABLE`，普通文字事件使用带时区的 UTC 时间戳。礼物金额、平台人气或热度不能直接作为公共弹幕计数。取消返回前关闭连接，`aclose()` 还需能释放残余资源。事件和连接状态字段见[弹幕契约](live-source-plugins.md#可选弹幕契约)。

## 安装与运行

源码部署使用运行宿主的同一 Python 安装插件依赖，再检查兼容性：

```text
python -m pip install -r <插件目录>/requirements.txt
python -m pip check
```

无额外依赖的插件可省略安装步骤。Portable 使用安装根目录的 `.venv/Scripts/python.exe`；所需 wheel 必须兼容其 Windows x64 / Python ABI 和锁定的核心依赖，具体检查方法见 [Portable 依赖边界](live-source-plugins.md#portable-的依赖边界)。插件安装不需要另一份 BiliLiveCut 或 Engine Pack，也不应替换宿主依赖锁。

在 Web 插件页刷新、启用并填写设置后，可以按 URL 添加直播间，或显式选择抖音平台。CLI 共用已保存的启用状态和设置，需要使用相同工作目录、数据库及 `PLUGIN_DIR`：

```text
python -m app.cli add-room <房间ID> --platform douyin --authorize
python -m app.cli check <房间ID> --platform douyin
python -m app.cli record <宿主房间主键> --pipeline
```

宿主没有独立的 CLI 插件启用命令，首次启用和设置在 Web 插件中心完成。

## 开发测试

宿主契约测试使用隔离数据库和本地媒体服务，验证加载、房间登记及录制流程：

```text
python -m pytest tests/unit/test_live_sources.py tests/integration/test_source_rooms.py tests/integration/test_source_runtime.py tests/integration/test_live_source_example.py --fail-on-skip
```

插件自己的离线 HTTP/SDK 测试应覆盖短链、稳定 ID、未知状态与下播、限流、凭据失效、过期地址刷新、取消、客户端关闭和日志脱敏。安装及启停通过正式加载器测试，避免绕过宿主生命周期。

真实平台测试另行验证解析、登录态和媒体可用性，记录插件版本、传输类型与脱敏后的错误信息。宿主离线测试通过不代表平台网页结构或访问策略始终兼容。
