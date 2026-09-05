@echo off
setlocal
chcp 936 >nul 2>&1
cd /d "%~dp0"

rem ====================================================================
rem  图片压缩工具箱 - GUI 启动器
rem
rem  pythonw.exe 启动  -> 没有控制台窗口
rem  start 之后本窗口立即退出 -> 不残留空终端
rem
rem  两套变量:
rem    PY  = 有控制台的解释器, 只用于探测/自检 (exit code 可靠)
rem    PYW = 无控制台的解释器, 用于真正启动
rem  pythonw 属于 GUI 子系统进程, 拿不到可靠的 exit code,
rem  所以 "探测用 python.exe, 启动用同目录 pythonw.exe"。
rem
rem  !! 本文件必须保存为 GBK(936) 编码 !!
rem  若存成 UTF-8, cmd 会按 GBK 解析中文, 而 UTF-8 中文字节被 GBK
rem  解码后, 第二字节落在 0x40-0x7E (ASCII 可打印区), 会凭空冒出
rem  引号和管道符, 破坏整段脚本的语法 —— 表现就是双击后毫无反应。
rem ====================================================================

set "PY="
set "PYW="

rem --- 1) 环境变量 ICT_PYTHON (手动指定) ---
if defined ICT_PYTHON if exist "%ICT_PYTHON%" (
    set "PY=%ICT_PYTHON%"
    set "PYW=%ICT_PYTHON%"
)

rem --- 2) 项目内虚拟环境 ---
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY=%~dp0.venv\Scripts\python.exe"
    set "PYW=%~dp0.venv\Scripts\python.exe"
    if exist "%~dp0.venv\Scripts\pythonw.exe" set "PYW=%~dp0.venv\Scripts\pythonw.exe"
)

rem --- 3) PATH 上第一个通过校验的 python.exe ---
if not defined PY call :pick
if not defined PY set "PY=python"
if not defined PYW set "PYW=%PY%"

rem --- 依赖自检 ---
"%PY%" -c "import PySide6; from PySide6.QtWidgets import QApplication" >nul 2>&1
if errorlevel 1 goto :no_pyside6

rem --- 记录本次实际使用的解释器, 便于排查 ---
if not exist "%~dp0logs" md "%~dp0logs" >nul 2>&1
> "%~dp0logs\launch.log" echo %date% %time%  OK  PY=%PY%  PYW=%PYW%

rem --- 启动: start 之后本窗口立即关闭, 不留空终端 ---
start "" "%PYW%" -m gui.app %*
endlocal
exit /b 0

rem --------------------------------------------------------------------
:pick
rem 遍历 PATH 上的 python.exe, 取第一个通过 :try_one 的
rem --------------------------------------------------------------------
for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PY call :try_one "%%P"
)
exit /b 0

rem --------------------------------------------------------------------
:try_one
rem %1 = 解释器完整路径; 通过则设置 PY / PYW
rem --------------------------------------------------------------------
rem 跳过 Microsoft Store 存根: 它是 0 字节占位程序, 执行后什么也不做
rem 却返回 exit 0, 会被误判为可用 (表现为双击后毫无反应)。
echo %~1 | findstr /i /c:"WindowsApps" >nul
if not errorlevel 1 exit /b 0

"%~1" -c "import PySide6; from PySide6.QtWidgets import QApplication" >nul 2>&1
if errorlevel 1 exit /b 0

set "PY=%~1"
set "PYW=%~1"
rem 同目录有 pythonw.exe 就用它启动 (无控制台窗口)
if exist "%~dp1pythonw.exe" set "PYW=%~dp1pythonw.exe"
exit /b 0

rem --------------------------------------------------------------------
:no_pyside6
rem 错误写入日志文件并用记事本打开。
rem 刻意不使用 pause —— 在隐藏窗口下 pause 会永久等待键盘输入,
rem 用户看不见也点不到, 正是 "双击后什么都没发生" 的成因。
rem --------------------------------------------------------------------
if not exist "%~dp0logs" md "%~dp0logs" >nul 2>&1
set "ERRLOG=%~dp0logs\launch_error.txt"
> "%ERRLOG%" echo 图片压缩工具箱 - 启动失败
>> "%ERRLOG%" echo.
>> "%ERRLOG%" echo 未找到装有 PySide6 的 Python 环境。
>> "%ERRLOG%" echo.
>> "%ERRLOG%" echo 探测到的解释器: %PY%
>> "%ERRLOG%" echo.
>> "%ERRLOG%" echo 请安装依赖后重试:
>> "%ERRLOG%" echo     pip install -r requirements.txt
>> "%ERRLOG%" echo.
>> "%ERRLOG%" echo 或手动指定解释器:
>> "%ERRLOG%" echo     set ICT_PYTHON=D:\Python312\python.exe
start "" notepad "%ERRLOG%"
endlocal
exit /b 1
