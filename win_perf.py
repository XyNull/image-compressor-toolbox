"""Windows 后台性能优化 — 防止系统对本进程做功耗节流 / EcoQoS 降频。

背景
----
Windows 11（尤其 25H2+）会自动对"非前台"进程启用 Power Throttling 与
效率模式（EcoQoS）：降低 CPU 频率、调度优先级，并把线程导向小核/能效核。
对本压缩工具的直接表现：终端切到后台后 CPU 功率从 ~50W 掉到 ~30W，
压缩吞吐明显下降。

⚠️ 优先级设计约束（重要，勿改回）：
  实测将进程优先级提到 ABOVE_NORMAL 或把压缩线程提到 ABOVE_NORMAL 后，
  20 个高优先级 CPU 线程会饿死本进程内的 UI 主线程 —— TUI 界面卡顿、
  前台更新不及时（UI 线程调度间隔从 ~16ms 恶化到 ~150-790ms）。
  因此本模块**不改动进程/线程调度优先级**，只关闭"降频"相关机制。

本模块在 Windows 上做两件事（其他平台全部为空操作，无副作用）：
  1. boost_process()：关闭进程级执行速度节流（SetProcessInformation
     (ProcessPowerThrottling) — 等价于 powercfg /powerthrottling disable，
     无需管理员权限），保持优先级为 NORMAL，UI 与 worker 公平调度；
  2. boost_current_thread()：退出 EcoQoS（SetThreadInformation
     (ThreadEfficiencyMode=FALSE)），供线程池 initializer 调用，使压缩
     线程不被降频/导向小核；
  3. restore_priority()：批次开始时调用一次 —— 若系统已把本进程降级到
     低于 NORMAL 的优先级类（如自动/手动效率模式），恢复为 NORMAL。

参考
----
- MSDN SetProcessInformation: ControlMask 选机制, StateMask=0 表示关闭。
  https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-setprocessinformation
- Windows 11 后台计时/吞吐不稳定的已知对策（SO 讨论中同一组 API）。
"""
from __future__ import annotations

import ctypes
import os

_IS_WINDOWS = os.name == "nt"

# --- Win32 常量 ---
_NORMAL_PRIORITY_CLASS = 0x00000020
_IDLE_PRIORITY_CLASS = 0x00000040
_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000  # 与 LOW_PRIORITY_CLASS 同值

# PROCESS_INFORMATION_CLASS::ProcessPowerThrottling
_PROCESS_POWER_THROTTLING = 4
# PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1  (执行速度节流)
# PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x2 (计时器分辨率)
_PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
_PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x2
# PROCESS_POWER_THROTTLING_CURRENT_VERSION
_PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1

# THREAD_INFORMATION_CLASS::ThreadEfficiencyMode
_THREAD_EFFICIENCY_MODE = 1


class _PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
    """PROCESS_POWER_THROTTLING_STATE — 与 Win32 定义一致。"""
    _fields_ = [
        ("Version", ctypes.c_ulong),
        ("ControlMask", ctypes.c_ulong),
        ("StateMask", ctypes.c_ulong),
    ]


def _kernel32() -> ctypes.WinDLL:
    """返回配置好 64 位句柄/指针签名的 kernel32。

    64 位下 HANDLE/LPVOID 都是 64 位, 必须声明 argtypes/restype 为
    c_void_p, 否则默认按 c_int 截断 → OverflowError / 传参错误。
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.GetCurrentThread.restype = ctypes.c_void_p
    k32.GetPriorityClass.restype = ctypes.c_uint32
    k32.GetPriorityClass.argtypes = [ctypes.c_void_p]
    k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k32.SetPriorityClass.restype = ctypes.c_int
    k32.SetProcessInformation.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
    ]
    k32.SetProcessInformation.restype = ctypes.c_int
    k32.SetThreadInformation.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
    ]
    k32.SetThreadInformation.restype = ctypes.c_int
    return k32


def boost_process() -> str:
    """进程级优化：关闭功耗节流（不改优先级）。

    Returns:
        描述字符串（供日志/UI 展示），如
        "Windows 性能优化: 功耗节流=关闭"。
    """
    if not _IS_WINDOWS:
        return "非 Windows，跳过进程性能优化"

    try:
        k32 = _kernel32()
        current = k32.GetCurrentProcess()  # 伪句柄，无需关闭

        # 关闭执行速度节流 + 计时器分辨率节流。
        # ControlMask 选择机制，StateMask=0 表示"关闭"（HighQoS）。
        # 注意：两个掩码要一次 OR 设置，分两次调用后者会覆盖前者。
        state = _PROCESS_POWER_THROTTLING_STATE()
        state.Version = _PROCESS_POWER_THROTTLING_CURRENT_VERSION
        state.ControlMask = (
            _PROCESS_POWER_THROTTLING_EXECUTION_SPEED
            | _PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION
        )
        state.StateMask = 0
        if k32.SetProcessInformation(
            current,
            _PROCESS_POWER_THROTTLING,
            ctypes.byref(state),
            ctypes.sizeof(state),
        ):
            return "Windows 性能优化: 功耗节流=关闭"
        return "Windows 性能优化: API 未生效"
    except Exception as exc:  # 任何失败都不应影响程序
        return f"Windows 性能优化失败: {type(exc).__name__}: {exc}"


def boost_current_thread() -> None:
    """线程级优化（供 ThreadPoolExecutor initializer 调用）。

    压缩/扫描 worker 线程启动时执行一次：
      - 退出 EcoQoS（ThreadEfficiencyMode=FALSE），避免被导向小核/降频
      - **不**改线程优先级（提高会让 UI 主线程被饿死 → TUI 卡顿）
    """
    if not _IS_WINDOWS:
        return
    try:
        k32 = _kernel32()
        thread = k32.GetCurrentThread()  # 伪句柄，无需关闭
        disabled = ctypes.c_int(0)  # BOOL FALSE = 退出效率模式
        k32.SetThreadInformation(
            thread,
            _THREAD_EFFICIENCY_MODE,
            ctypes.byref(disabled),
            ctypes.sizeof(disabled),
        )
    except Exception:
        pass


def restore_priority() -> str:
    """批次开始时调用：若系统已把本进程降级，恢复 NORMAL 优先级。

    Windows 效率模式（自动或手动）会把进程优先级类降到 IDLE/BELOW_NORMAL，
    本函数检测到后恢复为 NORMAL。返回描述字符串；无需恢复时返回 ""。
    """
    if not _IS_WINDOWS:
        return ""
    try:
        k32 = _kernel32()
        current = k32.GetCurrentProcess()
        cls = k32.GetPriorityClass(current)
        if cls in (_IDLE_PRIORITY_CLASS, _BELOW_NORMAL_PRIORITY_CLASS):
            if k32.SetPriorityClass(current, _NORMAL_PRIORITY_CLASS):
                return "优先级已从低恢复为 NORMAL"
    except Exception:
        pass
    return ""
