"""配置加载模块 — 零依赖（Python 3.11+ 内置 tomllib）。

首次运行自动在程序目录下生成带中文注释的 ``config.toml``；
之后读取用户配置并与内置 ``DEFAULTS`` 深合并（用户未写的项自动补默认，
未知项忽略），保证升级默认配置不会破坏旧配置。

设计要点：
  - 本模块不 import 项目内其他文件，避免循环依赖。
  - 所有可调参数集中在 config.toml，改配置无需改代码。
"""
from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 配置文件路径（与 config.py 同目录）
CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"

# 默认配置（与下方 _CONFIG_TEMPLATE 注释版保持一致）
DEFAULTS: dict[str, Any] = {
    "compression": {
        "quality": 90,
        "target_size_mb": 5.0,
        "scale_step": 0.9,
        "max_scale_iterations": 6,
        "output_format": "jpeg",
        "background_color": [255, 255, 255],
        "progressive": True,
        "optimize": True,
    },
    "output": {
        "subdir_name": "compressed",
        "default_mode": "subdir",
    },
    "parallel": {
        "max_workers": 0,
    },
    "io": {
        "small_file_threshold_mb": 2.0,
        "scan_workers": 8,
    },
    "overwrite": {
        "retry_count": 2,
        "retry_delay_ms": [500, 1000],
    },
    "video": {
        "crf": 28,
        "encoder_priority": ["nvidia", "intel_qsv", "cpu"],
        "timeout": 300,
        "extension": "mp4",
    },
    "video_merge": {
        "encoder": "av1_qsv",          # 首选 AV1 编码器 (HW:QSV)
        "fallback_encoder": "libsvtav1",  # 首选不可用时的回退 (CPU)
        "quality": 26,                 # 质量 (qsv=global_quality / svtav1=crf)
        "qsv_preset": "veryfast",      # av1_qsv 预设 (veryfast=压缩效率低/文件更大/细节保留多)
        "qsv_profile": "main",         # av1_qsv profile
        "keyframe_interval": 2,        # 关键帧间隔 (秒)
        "audio_codec": "aac",
        "audio_profile": "aac_low",    # AAC LC
        "audio_bitrate": "128k",
        "keep_channels": True,         # 声道数保持与原版一致
        "remove_subtitles": True,      # 移除字幕
        "tolerance_px": 2,             # 分辨率容差 (1918x1080 ≈ 1920x1080)
        "max_videos_per_group": 10,    # 单组视频数超过此值不合并
        "max_output_files": 4,         # 分辨率组数(目标文件数)超过此值不合并
        "output_prefix": "V",          # 输出文件名前缀 → V1.mp4, V2.mp4
        "output_extension": "mp4",
        "overwrite_existing": False,   # 目标文件已存在时是否覆盖 (False=跳过)
        "timeout": 3600,               # 单次合并+转码超时 (秒)
    },
    "ui": {
        "max_name_len": 22,
        "max_path_len": 55,
        "title": "图片压缩工具箱",
    },
    "logging": {
        "log_dir": "./logs",
    },
}

# 首次自动生成的配置文件内容（带中文注释）
_CONFIG_TEMPLATE = """\
# 图片压缩工具箱 配置文件
# 修改后无需改代码，重启程序即可生效

[compression]
quality = 90                 # JPEG 质量 0-100
target_size_mb = 5.0         # 超过此体积则循环缩小
scale_step = 0.9             # 每轮尺寸乘以此比例（0.9 = 缩 10%）
max_scale_iterations = 6    # 缩小轮数上限（安全阀, 预算算法下通常 1-2 次即达标）
output_format = "jpeg"       # 输出格式：jpeg
background_color = [255, 255, 255]  # 透明背景填充色 RGB
progressive = true
optimize = true

[output]
subdir_name = "compressed"   # 输出子目录名
default_mode = "subdir"      # subdir | overwrite（默认子目录模式）

[parallel]
max_workers = 0              # 0 = 自动（CPU 核数），或填固定线程数

[io]
small_file_threshold_mb = 2.0  # 小于此体积的文件一次性读入内存（减少 SMB/网络往返）
scan_workers = 8              # 目录扫描并行线程数（SMB/网络路径建议 8-16）

[overwrite]
retry_count = 2               # 覆盖/删除原图失败后的重试次数（0 = 不重试）
retry_delay_ms = [500, 1000]  # 每次重试前等待的随机延迟范围（毫秒）

[video]
crf = 28                     # 质量（越低越好，28 为均衡）
encoder_priority = ["nvidia", "intel_qsv", "cpu"]  # 硬件编码优先级
timeout = 300                # 单文件转换超时（秒）
extension = "mp4"

[video_merge]
encoder = "av1_qsv"          # 视频合并转码首选编码器: av1_qsv (Intel QSV 硬件 AV1)
fallback_encoder = "libsvtav1"  # 首选不可用时的回退 (CPU; 本机未装则用 libaom-av1)
quality = 26                 # 质量 (qsv 用 -global_quality, svtav1/libaom 用 -crf)
qsv_preset = "veryfast"      # av1_qsv 预设: veryfast=压缩效率低/文件更大/细节保留多 (参考 ShanaEncoder)
qsv_profile = "main"         # av1_qsv profile
keyframe_interval = 2        # 关键帧间隔（秒）
audio_codec = "aac"          # 音频编码器
audio_profile = "aac_low"    # 音频规格 (aac_low = AAC LC)
audio_bitrate = "128k"       # 音频码率
keep_channels = true         # true=声道数保持与原版一致, false=强制 2 声道
remove_subtitles = true      # true=移除字幕
tolerance_px = 2             # 分辨率容差: 宽高差 ≤ 此值视为相同分辨率 (1918x1080 ≈ 1920x1080)
max_videos_per_group = 10    # 同一分辨率组视频数超过此值时不合并
max_output_files = 4         # 目录内分辨率组数(即目标文件数)超过此值时不合并
output_prefix = "V"          # 输出文件名前缀 → V1.mp4, V2.mp4
output_extension = "mp4"
overwrite_existing = false   # 目标文件已存在时: true=覆盖, false=跳过该组
timeout = 3600               # 单次合并+转码超时（秒）

[ui]
max_name_len = 22            # 状态栏文件名截断长度
max_path_len = 55            # 列表路径截断长度
title = "图片压缩工具箱"

[logging]
log_dir = "./logs"           # 日志目录
"""


# --- 分层配置数据类 --------------------------------------------------- #

@dataclass
class CompressionConfig:
    quality: int = 90
    target_size_mb: float = 5.0
    scale_step: float = 0.9
    max_scale_iterations: int = 6
    output_format: str = "jpeg"
    background_color: list = field(default_factory=lambda: [255, 255, 255])
    progressive: bool = True
    optimize: bool = True


@dataclass
class OutputConfig:
    subdir_name: str = "compressed"
    default_mode: str = "subdir"


@dataclass
class ParallelConfig:
    max_workers: int = 0


@dataclass
class IoConfig:
    small_file_threshold_mb: float = 2.0
    scan_workers: int = 8


@dataclass
class OverwriteConfig:
    retry_count: int = 2
    retry_delay_ms: list = field(default_factory=lambda: [500, 1000])


@dataclass
class VideoConfig:
    crf: int = 28
    encoder_priority: list = field(default_factory=lambda: ["nvidia", "intel_qsv", "cpu"])
    timeout: int = 300
    extension: str = "mp4"


@dataclass
class VideoMergeConfig:
    """视频合并转码配置 (按 v 合并同目录同分辨率视频 → AV1)。"""
    encoder: str = "av1_qsv"
    fallback_encoder: str = "libsvtav1"
    quality: int = 26
    qsv_preset: str = "veryfast"
    qsv_profile: str = "main"
    keyframe_interval: int = 2
    audio_codec: str = "aac"
    audio_profile: str = "aac_low"
    audio_bitrate: str = "128k"
    keep_channels: bool = True
    remove_subtitles: bool = True
    tolerance_px: int = 2
    max_videos_per_group: int = 10
    max_output_files: int = 4
    output_prefix: str = "V"
    output_extension: str = "mp4"
    overwrite_existing: bool = False
    timeout: int = 3600


@dataclass
class UiConfig:
    max_name_len: int = 22
    max_path_len: int = 55
    title: str = "图片压缩工具箱"


@dataclass
class LoggingConfig:
    log_dir: str = "./logs"


@dataclass
class Config:
    compression: CompressionConfig = field(default_factory=CompressionConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    parallel: ParallelConfig = field(default_factory=ParallelConfig)
    io: IoConfig = field(default_factory=IoConfig)
    overwrite: OverwriteConfig = field(default_factory=OverwriteConfig)
    video: VideoConfig = field(default_factory=VideoConfig)
    video_merge: VideoMergeConfig = field(default_factory=VideoMergeConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


# --- 内部工具 --------------------------------------------------------- #

def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并 override 到 base（override 优先），返回新字典。"""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _section(cls: type, data: dict) -> Any:
    """用 data 构造某配置子类，忽略 data 中不存在于字段名的键。"""
    fields = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in fields})


def _to_config(data: dict) -> Config:
    """将合并后的 dict 转为 Config 对象。"""
    return Config(
        compression=_section(CompressionConfig, data.get("compression", {})),
        output=_section(OutputConfig, data.get("output", {})),
        parallel=_section(ParallelConfig, data.get("parallel", {})),
        io=_section(IoConfig, data.get("io", {})),
        overwrite=_section(OverwriteConfig, data.get("overwrite", {})),
        video=_section(VideoConfig, data.get("video", {})),
        video_merge=_section(VideoMergeConfig, data.get("video_merge", {})),
        ui=_section(UiConfig, data.get("ui", {})),
        logging=_section(LoggingConfig, data.get("logging", {})),
    )


# --- 公开 API --------------------------------------------------------- #

def load_config(path: str | Path = CONFIG_PATH) -> Config:
    """加载配置：文件不存在则自动生成默认 config.toml 并返回默认配置。"""
    path = Path(path)
    if not path.exists():
        path.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
        return _to_config(_deep_merge(DEFAULTS, {}))

    with path.open("rb") as f:
        user = tomllib.load(f)

    merged = _deep_merge(DEFAULTS, user)
    return _to_config(merged)


def as_dict(cfg: Config) -> dict:
    """将 Config 转回普通 dict（用于调试打印）。"""
    return dataclasses.asdict(cfg)


if __name__ == "__main__":
    import json
    c = load_config()
    print(json.dumps(as_dict(c), ensure_ascii=False, indent=2))
