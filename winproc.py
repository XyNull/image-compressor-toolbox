"""Windows 子进程启动辅助: 隐藏控制台窗口。

背景: GUI 用 pythonw.exe (GUI 子系统, 无控制台) 启动后, 任何通过
subprocess 调用的控制台程序 (ffprobe / ffmpeg / nvidia-smi / wmic)
都会默认分配一个**新的**控制台窗口 —— 表现为扫描、合并、转码时
黑框一闪而过, 转码期间还常驻一个 "ffmpeg.exe" 终端。

CREATE_NO_WINDOW (0x08000000) 让子进程不创建控制台窗口;
对本身就有控制台的进程 (TUI 模式下跑 python.exe) 也完全无副作用。

用法:
    from winproc import HIDDEN_KW
    subprocess.run([...], capture_output=True, **HIDDEN_KW)

非 Windows 平台 HIDDEN_KW 为空字典, 调用处 **HIDDEN_KW 展开为空,
保持跨平台兼容。
"""
from __future__ import annotations

import sys

if sys.platform == "win32":
    NO_WINDOW = 0x08000000            # = subprocess.CREATE_NO_WINDOW
    #: 展开到 subprocess.run / Popen 的关键字参数
    HIDDEN_KW: dict = {"creationflags": NO_WINDOW}
else:
    NO_WINDOW = 0
    HIDDEN_KW: dict = {}
