"""切片生成与后处理。

把一个高光候选(可能跨多个原始片段)生成为可投稿的 MP4:

1. 选出覆盖候选时间区间的原始片段并用 FFmpeg concat 拼接;
2. 按候选的精确起止时间(含上下文留白)精剪;
3. 后处理:响度标准化 / 去首尾静默 /(可选)竖屏重构 /(可选)烧录字幕;
4. 抽取封面帧;
5. 探测时长/分辨率,计算内容指纹,写入 ``final_clips`` 并更新候选状态。

所有 FFmpeg 参数均在代码中逐项注释说明。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.core.config import settings

# 竖屏目标分辨率(适合手机端短视频)。
_VERT_W, _VERT_H = 1080, 1920


@dataclass(slots=True)
class ClipOptions:
    """切片后处理选项。

    :param loudnorm: 是否做响度标准化。
    :param remove_silence: 是否去除首尾静默。
    :param vertical: 是否竖屏重构。
    :param subtitle: 是否烧录字幕。
    :param danmaku: 是否独立烧录场次普通弹幕。
    :param danmaku_mode: 滚动、顶部或下方固定弹幕。
    :param danmaku_font_size: 以 1080 高画面为基准的字号。
    :param danmaku_color: 六位 RGB 十六进制颜色。
    :param danmaku_opacity: 不透明度(0~1)。
    :param danmaku_duration_s: 单条显示秒数。
    :param danmaku_max_lines: 最大行数，实际行数受画面可用区域限制。
    :param max_duration_s: 最大时长(秒)。
    :param crf: x264 质量(0-51)。
    :param preset: x264 编码速度档。
    :param video_encoder: 编码设备；cpu 使用 libx264，其余使用对应硬件编码器。
    :param hardware_quality: 硬件质量参数；NVENC/AMF 为 QP，QSV 为 ICQ，与 CRF 不等价。
    :param hardware_preset: 硬件编码速度、均衡或质量档。
    """

    loudnorm: bool = True
    remove_silence: bool = False
    vertical: bool = False
    subtitle: bool = False
    max_duration_s: int = 180
    crf: int = 20
    preset: str = "veryfast"
    video_encoder: Literal["cpu", "nvidia", "intel", "amd"] = "cpu"
    hardware_quality: int = 23
    hardware_preset: Literal["speed", "balanced", "quality"] = "balanced"
    danmaku: bool = False
    danmaku_mode: Literal["scroll", "top", "bottom"] = "scroll"
    danmaku_font_size: int = 36
    danmaku_color: str = "FFFFFF"
    danmaku_opacity: float = 0.85
    danmaku_duration_s: float = 6.0
    danmaku_max_lines: int = 8

    def video_encoding_args(self) -> list[str]:
        """生成后端专用编码参数；显式选择硬件时不偷偷回退到 CPU。"""
        if self.video_encoder == "cpu":
            return ["-c:v", "libx264", "-crf", str(self.crf), "-preset", self.preset, "-pix_fmt", "yuv420p"]
        if not 1 <= self.hardware_quality <= 51:
            raise ValueError("硬件编码质量值必须在 1~51 之间")
        if self.hardware_preset not in {"speed", "balanced", "quality"}:
            raise ValueError("硬件编码速度档无效")
        quality = str(self.hardware_quality)
        if self.video_encoder == "nvidia":
            preset = {"speed": "p1", "balanced": "p4", "quality": "p7"}[self.hardware_preset]
            return ["-c:v", "h264_nvenc", "-rc", "constqp", "-qp", quality, "-preset", preset, "-pix_fmt", "yuv420p"]
        if self.video_encoder == "intel":
            preset = {"speed": "veryfast", "balanced": "medium", "quality": "veryslow"}[self.hardware_preset]
            return [
                "-c:v",
                "h264_qsv",
                "-global_quality",
                quality,
                "-look_ahead",
                "0",
                "-preset",
                preset,
                "-pix_fmt",
                "nv12",
            ]
        if self.video_encoder == "amd":
            return [
                "-c:v",
                "h264_amf",
                "-rc",
                "cqp",
                "-qp_i",
                quality,
                "-qp_p",
                quality,
                "-quality",
                self.hardware_preset,
                "-pix_fmt",
                "nv12",
            ]
        raise ValueError(f"不支持的视频编码设备: {self.video_encoder}")

    @classmethod
    def from_settings(cls) -> ClipOptions:
        """从全局配置构造默认选项。

        :returns: 依据当前任务有效配置快照的 :class:`ClipOptions`。
        """
        return cls(
            loudnorm=settings.clip_loudnorm,
            remove_silence=settings.clip_remove_silence,
            vertical=settings.clip_vertical,
            subtitle=settings.clip_subtitle,
            danmaku=settings.clip_danmaku,
            danmaku_mode=settings.clip_danmaku_mode,
            danmaku_font_size=settings.clip_danmaku_font_size,
            danmaku_color=settings.clip_danmaku_color,
            danmaku_opacity=settings.clip_danmaku_opacity,
            danmaku_duration_s=settings.clip_danmaku_duration_s,
            danmaku_max_lines=settings.clip_danmaku_max_lines,
            max_duration_s=settings.clip_max_duration_s,
            crf=settings.clip_video_crf,
            preset=settings.clip_preset,
            video_encoder=settings.clip_video_encoder,
            hardware_quality=settings.clip_hardware_quality,
            hardware_preset=settings.clip_hardware_preset,
        )
