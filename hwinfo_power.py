"""从 HWiNFO 共享内存读取 CPU 封装功耗。

前提:
  1. 正在运行 HWiNFO64 (v6.40+);
  2. HWiNFO 设置中已启用 Shared Memory Support
     (Settings → Safety → 勾选 "Shared Memory Support")。

共享内存: Global\\HWiNFO_SENS_SM2
  header 44B (pack=1):
    0x00 u32 magic        'HWiS' 有效 / 'DAED' = HWiNFO 已退出
    0x04 u32 version      1 (旧) / 2 (HWiNFO v7.x+)
    0x08 u32 version2
    0x0C i64 last_update  unix 时间戳
    0x14 u32 sensor_section_offset
    0x18 u32 sensor_element_size   264 (v1) / 392 (v2, 尾部加 UTF-8 名)
    0x1C u32 sensor_element_count
    0x20 u32 entry_section_offset
    0x24 u32 entry_element_size    316 (v1) / 460 (v2, 尾部加长)
    0x28 u32 entry_element_count
  元素前段布局 v1/v2 相同, 新版只在尾部追加字段 — 因此固定解析前
  316 / 264 字节, 元素步长一律取 header 里的 size:
  sensor element: u32 id + u32 instance + char[128] name_original
                  + char[128] name_user            (前 264B)
  entry element  (前 316B):
    u32 type  (0=None 1=Temp 2=Volt 3=Fan 4=Current 5=Power 6=Clock 7=Usage)
    u32 sensor_index   ← 指向 sensor 数组的下标 (不是 id)
    u32 id
    char[128] name_original / char[128] name_user / char[16] unit
    f64 value / value_min / value_max / value_avg
    实测 (v2, entry 460B): value 位于 284, 'CPU Package Power' 空闲
    约 8W、满载峰值 130W+, 数值合理。

字符串编码为 ANSI, 用 latin-1 解码永不失败。
CPU 封装功耗: Power 类型读数中, label 含 "package" 且传感器名含 "cpu";
兜底取 CPU 传感器的第一个 Power 读数 (如 "CPU Core"/"CPU PPT")。
"""
from __future__ import annotations

import ctypes
import struct
import time

SM_NAME = "Global\\HWiNFO_SENS_SM2"
SM_MUTEX = "Global\\HWiNFO_SM2_MUTEX"

_HEADER_FMT = "<3Iq6I"          # 44 bytes
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)
_SENSOR_FIXED = 264             # u32+u32+128s+128s
_ENTRY_FIXED = 316              # u32+u32+u32+128s+128s+16s+4d
_MAGIC_OK = 0x53695748     # b"HWiS" little-endian
_MAGIC_DEAD = 0x44414544   # b"DAED" little-endian (HWiNFO 已退出)

_TYPE_POWER = 5

_RETRY_SECS = 10.0              # 打不开共享内存时的重试间隔

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenFileMappingW.argtypes = [ctypes.c_uint, ctypes.c_int,
                                      ctypes.c_wchar_p]
kernel32.OpenFileMappingW.restype = ctypes.c_void_p
kernel32.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_uint, ctypes.c_uint,
                                   ctypes.c_size_t]
kernel32.MapViewOfFile.restype = ctypes.c_void_p
kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
kernel32.UnmapViewOfFile.restype = ctypes.c_int
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.restype = ctypes.c_int
kernel32.OpenMutexW.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_wchar_p]
kernel32.OpenMutexW.restype = ctypes.c_void_p
kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
kernel32.WaitForSingleObject.restype = ctypes.c_uint
kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
kernel32.ReleaseMutex.restype = ctypes.c_int

_FILE_MAP_READ = 0x0004
_SYNCHRONIZE = 0x00100000
_WAIT_OK = 0x00000000

# 简单进程内缓存: 保持映射句柄, 避免每秒开关内核对象
_handle: int = 0
_view: int = 0
_retry_after: float = 0.0


def _close() -> None:
    global _handle, _view
    if _view:
        kernel32.UnmapViewOfFile(_view)
        _view = 0
    if _handle:
        kernel32.CloseHandle(_handle)
        _handle = 0


def _ensure_open() -> bool:
    """打开共享内存映射 (惰性, 失败后按 _RETRY_SECS 节流重试)。"""
    global _handle, _view, _retry_after
    if _handle and _view:
        return True
    now = time.monotonic()
    if now < _retry_after:
        return False
    _retry_after = now + _RETRY_SECS
    try:
        h = kernel32.OpenFileMappingW(_FILE_MAP_READ, False, SM_NAME)
        if not h:
            return False
        view = kernel32.MapViewOfFile(h, _FILE_MAP_READ, 0, 0, 0)
        if not view:
            kernel32.CloseHandle(h)
            return False
        _handle, _view = h, view
        _retry_after = 0.0
        return True
    except Exception:
        return False


def _read_header() -> tuple:
    raw = ctypes.string_at(_view, _HEADER_SIZE)
    return struct.unpack(_HEADER_FMT, raw)


def _lock_mutex() -> tuple:
    """短暂持有 HWiNFO 更新互斥锁, 防止读到撕裂数据。失败则直接读。"""
    m = kernel32.OpenMutexW(_SYNCHRONIZE, False, SM_MUTEX)
    if m:
        if kernel32.WaitForSingleObject(m, 200) != _WAIT_OK:
            kernel32.CloseHandle(m)
            return None, None
        return m, m
    return None, None


def _unlock(pair: tuple) -> None:
    m, keep = pair
    if m:
        kernel32.ReleaseMutex(m)
        kernel32.CloseHandle(m)


def _decode(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("latin-1", "replace").strip()


def read_cpu_package_watts() -> float | None:
    """返回 CPU 封装功耗 (瓦特); 不可用时返回 None。线程不安全 — 请在同一线程调用。"""
    if not _ensure_open():
        return None
    try:
        magic, version, _v2, _upd, s_off, s_size, s_cnt, \
            e_off, e_size, e_cnt = _read_header()
    except Exception:
        _close()
        return None
    if magic != _MAGIC_OK:
        if magic == _MAGIC_DEAD:
            _close()  # HWiNFO 已退出
        return None
    if version not in (1, 2):   # v1 旧版 / v2 HWiNFO v7.x+ 元素尾部加长
        return None
    if e_size < _ENTRY_FIXED or e_cnt == 0:
        return None

    lock = _lock_mutex()
    try:
        # 传感器名表 (index -> 名称), 用于判断读数属于 CPU
        sensor_names: dict = {}
        if s_size >= _SENSOR_FIXED:
            need = s_off + s_size * s_cnt
            raw = ctypes.string_at(_view, need)
            for i in range(s_cnt):
                base = s_off + i * s_size
                orig = _decode(raw[base + 8:base + 136])
                user = _decode(raw[base + 136:base + 264])
                sensor_names[i] = user or orig

        raw = ctypes.string_at(_view, e_off + e_size * e_cnt)
        cpu_power: list = []      # (label, value) 属于 CPU 传感器
        any_package: list = []    # (label, value) 任何 Package 功耗
        for i in range(e_cnt):
            base = e_off + i * e_size
            rtype, sidx, _rid = struct.unpack_from("<3I", raw, base)
            if rtype != _TYPE_POWER:
                continue
            label = _decode(raw[base + 12:base + 140]) or \
                _decode(raw[base + 140:base + 268])
            value = struct.unpack_from("<d", raw, base + 0x11C)[0]
            if not label or value <= 0:
                continue
            sname = (sensor_names.get(sidx) or "").lower()
            low = label.lower()
            if "cpu" in sname or sname.startswith("cpu"):
                cpu_power.append((low, value))
                if "package" in low:
                    # 最精确: CPU 传感器的 Package 功耗
                    return value
            if "package" in low:
                any_package.append(value)
        # 兜底 1: CPU 传感器的第一个功耗读数
        if cpu_power:
            return cpu_power[0][1]
        # 兜底 2: 任何 Package 功耗 (极罕见: 无 CPU 传感器名)
        if any_package:
            return any_package[0]
        return None
    finally:
        _unlock(lock)


def test_self() -> None:  # pragma: no cover
    w = read_cpu_package_watts()
    print("CPU Package:", f"{w:.2f} W" if w is not None else "不可用")


if __name__ == "__main__":
    test_self()
