@echo off
setlocal EnableExtensions
rem Codex 模型切换器 MCP server 启动器（Windows）
rem 底层复用现成轮子 codex-token 的 switcher.py（不重复实现切换逻辑）
rem switcher.py 的位置按以下顺序自动探测：
rem   1) 环境变量 CODEX_SWITCHER_HOME
rem   2) 插件目录下的 vendor/
rem   3) 插件同级的 codex-token/
if not defined CODEX_HOME set "CODEX_HOME=%USERPROFILE%\.codex"

set "PYEXE="
set "PYARGS="
rem 只认「真的能 import tomllib」的解释器
rem （排除 Microsoft Store 那个会把商店页面弹出来的 python.exe 占位符）
for %%P in (python.exe) do if not defined PYEXE (
  "%%~$PATH:P" -c "import tomllib" >nul 2>&1 && set "PYEXE=%%~$PATH:P"
)
if not defined PYEXE (
  py -3 -c "import tomllib" >nul 2>&1 && (set "PYEXE=py" & set "PYARGS=-3")
)
if not defined PYEXE (
  echo [model-switcher] 找不到 Python 3.11+ 解释器（需要 tomllib） 1>&2
  echo [model-switcher] 请安装 Python 后重试，或用 doctor.py --fix 自动修 1>&2
  exit /b 1
)

pushd "%~dp0.."
"%PYEXE%" %PYARGS% "%~dp0..\server.py"
popd
