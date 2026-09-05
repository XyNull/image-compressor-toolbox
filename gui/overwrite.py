"""覆盖/恢复覆盖逻辑 — 从 main.py 提取的纯函数版 (无 Textual 依赖)。

行为与 TUI 完全一致: 清只读 + 重试 + GC + 删除旧格式原图 + 全部成功才删 compressed/。
"""
from __future__ import annotations

import gc
import os
import random
import shutil
import stat
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from compressor import CompressionResult
from config import Config

LogCb = Callable[[str, str], None]  # (text, level) level: info|warn|error|ok


# --------------------------------------------------------------------- #
#  文件工具
# --------------------------------------------------------------------- #

def clear_readonly(path: Path) -> None:
    """清除 Windows 只读属性, 使文件可被覆盖/删除。"""
    try:
        if path.exists():
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        pass


def rmtree_onerror(func, path, exc_info) -> None:
    """shutil.rmtree 遇只读文件/目录失败时, 清除只读后重试。"""
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def file_diagnostics(path: Path) -> str:
    """文件状态诊断 (覆盖失败时写入日志)。"""
    try:
        if not path.exists():
            return "不存在"
        st = os.stat(path)
        attrs = []
        if not (st.st_mode & 0o200):
            attrs.append("只读")
        try:
            import ctypes
            attrs_ = ctypes.c_ulong()
            ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attrs_.value & 0x1:
                attrs.append("READONLY")
        except Exception:
            pass
        a = f" ({','.join(attrs)})" if attrs else ""
        return f"存在 {st.st_size} 字节{a}"
    except OSError as e:
        return f"stat失败: {e}"


# --------------------------------------------------------------------- #
#  残留检测 (恢复覆盖)
# --------------------------------------------------------------------- #

def find_compressed_dirs(root: Path) -> List[Path]:
    """递归查找目录树中所有"含文件的 compressed/ 目录"。"""
    result: List[Path] = []
    stack: List[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    p = Path(entry.path)
                    if entry.name == "compressed":
                        try:
                            if any(e.is_file() for e in os.scandir(p)):
                                result.append(p)
                        except OSError:
                            pass
                    else:
                        stack.append(p)
        except OSError:
            pass
    return result


def find_original(comp_dir: Path, comp_file: Path) -> Path:
    """推断压缩产物对应的原图路径 (stem 相同后缀不同优先)。"""
    parent = comp_dir.parent
    stem = comp_file.stem
    ext = comp_file.suffix.lower()
    try:
        candidates = [p for p in parent.iterdir()
                      if p.is_file() and p.stem == stem]
    except OSError:
        candidates = []
    for c in candidates:
        if c.suffix.lower() != ext:
            return c
    if candidates:
        return candidates[0]
    return parent / comp_file.name


def detect_leftover(paths: List[Path],
                    seen_outputs: Optional[set] = None) -> List[CompressionResult]:
    """检测拖入目录中的 compressed/ 残留 (崩溃遗留的未覆盖压缩产物)。"""
    found: List[CompressionResult] = []
    seen = set(seen_outputs or ())
    for path in paths:
        try:
            if path.is_file():
                continue
        except OSError:
            continue
        for comp_dir in find_compressed_dirs(path):
            try:
                files = sorted(comp_dir.iterdir())
            except OSError:
                continue
            for f in files:
                if not f.is_file() or f in seen:
                    continue
                seen.add(f)
                try:
                    size = f.stat().st_size
                except OSError:
                    size = 0
                found.append(CompressionResult(
                    input_path=find_original(comp_dir, f),
                    output_path=f,
                    success=True,
                    original_size=size,
                    output_size=size,
                    fmt="jpeg",
                ))
    return found


# --------------------------------------------------------------------- #
#  覆盖执行
# --------------------------------------------------------------------- #

def execute_overwrite(results: List[CompressionResult], cfg: Config,
                      log_cb: Optional[LogCb] = None) -> Dict[str, int]:
    """Move compressed files from compressed/ subdirs to parent dirs.

    与 TUI _execute_overwrite 行为一致:
      - 按压缩产物路径中的 "compressed" 分段定位目标路径
      - os.replace 原子覆盖 (先清只读), 失败 GC+随机延迟后重试
      - 原图与压缩图后缀不同时删除原图
      - 全部成功才删除 compressed/ 目录, 任一失败保留
    """
    def say(text: str, level: str = "info") -> None:
        if log_cb:
            try:
                log_cb(text, level)
            except Exception:
                pass

    say("正在执行覆盖移动…", "warn")

    dir_files: Dict[Path, List[Tuple[Path, Path, Path]]] = {}
    for result in results:
        if not result.success or result.skipped or result.output_path is None:
            continue
        parts = result.output_path.parts
        if "compressed" not in parts:
            continue
        idx = parts.index("compressed")
        compressed_dir = Path(*parts[: idx + 1])
        target_path = Path(*parts[:idx] + parts[idx + 1:])
        dir_files.setdefault(compressed_dir, []).append(
            (result.output_path, target_path, result.input_path))

    if not dir_files:
        say("无可覆盖的文件。")
        return {"moved": 0, "deleted": 0, "failed": 0}

    ow = cfg.overwrite
    retry_count = max(0, int(ow.retry_count))
    delay_ms = ow.retry_delay_ms or [500, 1000]
    delay_lo = float(delay_ms[0])
    delay_hi = float(delay_ms[1]) if len(delay_ms) > 1 else delay_lo

    total_moved = 0
    total_deleted = 0
    total_failed = 0

    for compressed_dir, file_triples in dir_files.items():
        all_ok = True
        moved = 0
        deleted = 0

        for src, dst, original in file_triples:
            last_err: Optional[Exception] = None
            replaced = False
            for attempt in range(retry_count + 1):
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    clear_readonly(dst)
                    os.replace(src, dst)
                    replaced = True
                    break
                except Exception as e:
                    last_err = e
                    if attempt < retry_count:
                        gc.collect()
                        time.sleep(random.uniform(delay_lo, delay_hi) / 1000.0)

            if not replaced:
                all_ok = False
                total_failed += 1
                say(f"✗ 覆盖失败: {src.name} → {dst.name}: "
                    f"{type(last_err).__name__}: {last_err}", "error")
                continue
            moved += 1
            total_moved += 1

            if original.exists() and original.resolve() != dst.resolve():
                unlinked = False
                for attempt in range(retry_count + 1):
                    try:
                        clear_readonly(original)
                        original.unlink()
                        unlinked = True
                        break
                    except Exception as e:
                        last_err = e
                        if attempt < retry_count:
                            gc.collect()
                            time.sleep(
                                random.uniform(delay_lo, delay_hi) / 1000.0)
                if unlinked:
                    deleted += 1
                    total_deleted += 1
                else:
                    all_ok = False
                    total_failed += 1
                    say(f"⚠ 已覆盖 {dst.name}，但删除原图 {original.name} 失败: "
                        f"{type(last_err).__name__}: {last_err}", "warn")

        if all_ok and moved > 0:
            try:
                shutil.rmtree(compressed_dir, onerror=rmtree_onerror)
                del_info = f"，删除原图 {deleted} 个" if deleted else ""
                say(f"✓ 已移动 {moved} 个文件{del_info}，已删除 compressed/ 目录",
                    "ok")
            except Exception as e:
                say(f"⚠ 文件已移动，但无法删除目录 {compressed_dir}: {e}", "warn")
        elif not all_ok:
            say(f"⚠ 部分失败，保留: {compressed_dir}", "warn")

    say(f"覆盖阶段结束: 移动 {total_moved}, 删除原图 {total_deleted}, "
        f"失败 {total_failed}", "ok" if not total_failed else "warn")
    return {"moved": total_moved, "deleted": total_deleted,
            "failed": total_failed}
