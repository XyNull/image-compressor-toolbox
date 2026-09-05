"""GUI 入口。

用法:
    python -m gui.app            # 推荐 (项目根目录下)
    python gui/app.py            # 亦可
    python -m gui.app "D:/图片"  # 启动时带入路径
    双击 run_gui.vbs             # 无控制台窗口启动 (推荐)
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _install_headless_guard() -> None:
    """pythonw 启动时 sys.stdout / sys.stderr 均为 None。

    后果: 任何 print、未捕获异常、Qt 警告都会**静默丢失** —— 表现为窗口
    一闪而过或突然消失且无任何线索。这里统一重定向到 logs/gui_console.log,
    并安装 excepthook 与 Qt 消息处理器, 保证无控制台模式下问题仍可追溯。

    用 python.exe 启动 (stdout 正常) 时本函数直接跳过, 不影响调试。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return

    log_dir = _ROOT / "logs"
    try:
        log_dir.mkdir(exist_ok=True)
        fp = open(log_dir / "gui_console.log", "a",
                  encoding="utf-8", errors="replace")
    except OSError:
        fp = open(os.devnull, "w", encoding="utf-8")

    if sys.stdout is None:
        sys.stdout = fp
    if sys.stderr is None:
        sys.stderr = fp

    def _excepthook(exc_type, exc, tb) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(f"\n[{stamp}] 未捕获异常 (GUI):\n")
        traceback.print_exception(exc_type, exc, tb, file=sys.stderr)
        sys.stderr.flush()

    sys.excepthook = _excepthook

    try:
        from PySide6.QtCore import qInstallMessageHandler

        def _qt_handler(mode, context, message) -> None:
            try:
                sys.stderr.write(f"[Qt] {message}\n")
                sys.stderr.flush()
            except Exception:
                pass

        qInstallMessageHandler(_qt_handler)
    except Exception:
        pass  # Qt 未就绪时忽略, 不因兜底逻辑本身导致启动失败


_install_headless_guard()

from PySide6.QtGui import QFont, QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from gui.main_window import ICON_PATH, MainWindow  # noqa: E402


def main() -> None:
    cfg = config.load_config()
    initial = [Path(a) for a in sys.argv[1:] if Path(a).exists()]
    app = QApplication(sys.argv)
    app.setApplicationName(cfg.ui.title)
    if sys.platform == "win32":
        app.setFont(QFont("Microsoft YaHei", 9))  # 中文 Windows 标准字体
    if ICON_PATH.exists():
        # 任务栏与所有未来窗口都用这个图标
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    win = MainWindow(cfg, initial)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
