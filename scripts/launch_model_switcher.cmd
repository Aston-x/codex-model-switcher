@echo off
setlocal EnableExtensions
rem Codex model-switcher MCP server launcher for Windows.
rem Reuses codex-token switcher.py - one implementation of the switch logic.
rem switcher.py lookup order:
rem   1 - env var CODEX_SWITCHER_HOME
rem   2 - vendor folder next to the plugin
rem   3 - codex-token folder next to the plugin
rem
rem IMPORTANT: keep this file ASCII-only, CRLF, and free of shell metacharacters
rem in the comment lines. cmd.exe reads a BOM-less UTF-8 .cmd with the console
rem code page, which is 936 on this machine, so CJK comments turn into bytes
rem that break the block parser. Parentheses and double-ampersand in a rem
rem line break it the same way, and the MCP server then never starts.
rem PYTHONIOENCODING matters for the same class of reason: Python encodes a
rem piped stdout with the ANSI code page, but MCP clients require UTF-8.
if not defined CODEX_HOME set "CODEX_HOME=%USERPROFILE%\.codex"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "PYEXE="
set "PYARGS="
rem Only accept an interpreter that can really import tomllib.
rem This skips the Microsoft Store python.exe stub that opens the store page.
for %%P in (python.exe) do if not defined PYEXE (
  "%%~$PATH:P" -c "import tomllib" >nul 2>&1 && set "PYEXE=%%~$PATH:P"
)
if not defined PYEXE (
  py -3 -c "import tomllib" >nul 2>&1 && (set "PYEXE=py" & set "PYARGS=-3")
)
if not defined PYEXE (
  echo [model-switcher] No Python 3.11+ interpreter found - tomllib required 1>&2
  echo [model-switcher] Install Python, or run doctor.py --fix to repair it 1>&2
  exit /b 1
)

pushd "%~dp0.."
"%PYEXE%" %PYARGS% "%~dp0..\server.py"
popd
