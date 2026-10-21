# -*- coding: utf-8 -*-
"""跨平台适配层 —— 把 Windows 专有写法收敛到一处。

本插件原本在 Windows 上开发，踩过的坑都固化在这里：
  · 常驻进程要无窗口（Windows 用 pythonw.exe，POSIX 用 python3 + nohup/脱离终端）
  · 进程检测**绝对不能**每轮调 powershell.exe（会反复弹黑窗口）
    -> Windows 走 Win32 API，Linux 读 /proc，macOS 走 ps
  · 用户级环境变量：Windows setx / macOS launchctl setenv / Linux 写 profile
  · 开机自启：Windows Run 键 / macOS LaunchAgent / Linux systemd user 或 XDG autostart
  · 锁文件要能自愈（强杀进程后残留锁会永久阻塞）

server.py 与 scripts/*.py 都从这里取平台差异，自身保持跨平台写法。
"""
import glob
import os
import re
import shutil
import subprocess
import sys

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

APP_NAME = "model-switcher"
AUTOSTART_NAME = "CodexModelGuard"


# ---------------------------------------------------------------------------
# 基本信息
# ---------------------------------------------------------------------------

def os_label():
    if IS_WINDOWS:
        return "Windows"
    if IS_MACOS:
        return "macOS"
    if IS_LINUX:
        return "Linux"
    return sys.platform


def codex_home():
    """Codex 配置目录。"""
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def home_dir():
    return os.path.expanduser("~")


# ---------------------------------------------------------------------------
# 进程枚举（平台无关接口：返回 [(pid, 可执行路径或命令行), ...]）
# ---------------------------------------------------------------------------

def _win_processes():
    import ctypes
    import ctypes.wintypes as wt

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID",
                                          ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wt.HANDLE
    # Process32FirstW / Process32NextW 在 **kernel32.dll**（不在 psapi.dll）
    kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                                   ctypes.POINTER(wt.DWORD)]

    INVALID = ctypes.c_void_p(-1).value
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID:
        return []
    out = []
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            return []
        while True:
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                                      pe.th32ProcessID)
            path = ""
            if h:
                buf = ctypes.create_unicode_buffer(1024)
                size = wt.DWORD(1024)
                if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    path = buf.value or ""
                kernel32.CloseHandle(h)
            out.append((pe.th32ProcessID, path or pe.szExeFile))
            if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                break
    except Exception:
        pass
    finally:
        kernel32.CloseHandle(snap)
    return out


def _linux_processes():
    out = []
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except Exception:
        return out
    for pid in pids:
        path = ""
        cmd = ""
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except Exception:
            pass
        try:
            path = os.readlink("/proc/%s/exe" % pid)
        except Exception:
            path = ""
        out.append((int(pid), path or cmd))
    return out


def _mac_processes():
    """macOS 没有 /proc，用 ps（POSIX 下开子进程不会闪黑窗口）。"""
    out = []
    try:
        p = subprocess.run(["ps", "-Ao", "pid=,command="], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=15)
        for line in (p.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            pid_s, _, cmd = line.partition(" ")
            try:
                out.append((int(pid_s), cmd.strip()))
            except ValueError:
                continue
    except Exception:
        pass
    return out


def iter_processes():
    """返回 [(pid, 路径或命令行), ...]。"""
    if IS_WINDOWS:
        return _win_processes()
    if IS_LINUX:
        return _linux_processes()
    if IS_MACOS:
        return _mac_processes()
    return []


def _is_our_own(text):
    t = (text or "").lower()
    return ("model-switcher" in t or "launch_guard" in t or "guard.py" in t
            or "platform_compat" in t)


def codex_desktop_running():
    """官方 Codex 桌面版是否在跑。

    Windows：WindowsApps\\OpenAI.Codex_*\\ChatGPT.exe
    macOS  ：/Applications/Codex.app/... 或含 "Codex.app/Contents/MacOS"
    Linux  ：命令行含 "codex" 且含桌面端特征（.desktop / codex-desktop）

    可用环境变量 CODEX_DESKTOP_MATCH 覆盖（正则，匹配进程路径或命令行）。
    """
    override = os.environ.get("CODEX_DESKTOP_MATCH")
    me = os.getpid()
    for pid, path in iter_processes():
        if pid == me or _is_our_own(path):
            continue
        if override:
            if re.search(override, path or "", re.I):
                return True
            continue
        if IS_WINDOWS:
            if "openai.codex_" in (path or "").lower() and \
                    "windowsapps" in (path or "").lower():
                return True
        elif IS_MACOS:
            if "codex.app/contents/macos" in (path or "").lower():
                return True
        else:
            low = (path or "").lower()
            if "codex" in low and ("codex-desktop" in low or "/codex.app/" in low):
                return True
    return False


def pid_alive(pid):
    """进程是否还活着（跨平台）。"""
    if IS_WINDOWS:
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        except Exception:
            return True
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


# ---------------------------------------------------------------------------
# 解释器 / 工具
# ---------------------------------------------------------------------------

def python_launcher(silent=False):
    """返回用于「静默启动」的 python 路径。

    Windows -> pythonw.exe（无控制台窗口）
    POSIX   -> 当前解释器（用 start_new_session 脱离终端即可）
    """
    if IS_WINDOWS:
        cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(cand):
            return cand
        for pat in (r"C:\Users\*\AppData\Local\Programs\Python\Python3*\pythonw.exe",
                    r"C:\Python3*\pythonw.exe"):
            hits = sorted(glob.glob(pat))
            if hits:
                return hits[-1]
    return sys.executable


def find_python():
    """找一个可用的 python 解释器（用于写 .mcp.json 的 command）。"""
    for name in ("python3", "python"):
        p = shutil.which(name)
        if p:
            return p
    return sys.executable


def codex_cli_path():
    """找 codex CLI（guard 用它起 app-server 做 thread/resume 修正）。"""
    cands = []
    env = os.environ.get("CODEX_CLI_PATH")
    if env:
        cands.append(env)
    if IS_WINDOWS:
        cands += sorted(glob.glob(os.path.join(
            home_dir(), "AppData", "Local", "OpenAI", "Codex", "bin", "*", "codex.exe")))
        cands += sorted(glob.glob(
            r"C:\Program Files\WindowsApps\OpenAI.Codex_*\app\codex.exe"))
    elif IS_MACOS:
        cands += sorted(glob.glob(os.path.join(
            home_dir(), "Applications", "Codex.app", "Contents", "Resources", "codex")))
        cands += sorted(glob.glob("/Applications/Codex.app/Contents/Resources/codex"))
    else:
        cands += sorted(glob.glob(os.path.join(
            home_dir(), ".local", "share", "codex", "bin", "codex")))
    p = shutil.which("codex")
    if p:
        cands.append(p)
    for c in cands:
        if c and os.path.exists(c):
            return c
    return ""


# ---------------------------------------------------------------------------
# 用户级环境变量
# ---------------------------------------------------------------------------

def set_user_env(name, value):
    """写用户级环境变量。返回 (ok, 说明)。

    Windows -> setx（写注册表，新进程可见；**已在跑的 explorer 看不到**）
    macOS   -> launchctl setenv（GUI 应用可见）+ 追加到 ~/.zshrc
    Linux   -> 写 ~/.config/environment.d/<name>.conf + ~/.profile
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
        return False, "环境变量名只能含字母数字下划线，且不能以数字开头"
    value = str(value)
    try:
        if IS_WINDOWS:
            p = subprocess.run(["setx", name, value], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if p.returncode != 0:
                return False, (p.stderr or p.stdout or "setx 失败").strip()
            return True, "已用 setx 写入用户级环境变量 %s（**已在运行的进程读不到**，需重开）" % name
        if IS_MACOS:
            subprocess.run(["launchctl", "setenv", name, value], capture_output=True)
            for rc in (".zshrc", ".bash_profile", ".profile"):
                p = os.path.join(home_dir(), rc)
                try:
                    with open(p, "a", encoding="utf-8") as f:
                        f.write('\nexport %s="%s"\n' % (name, value))
                except Exception:
                    continue
            return True, "已用 launchctl setenv 写入 %s（并追加到 shell rc）" % name
        d = os.path.join(home_dir(), ".config", "environment.d")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "%s.conf" % name), "w", encoding="utf-8") as f:
            f.write("%s=%s\n" % (name, value))
        try:
            with open(os.path.join(home_dir(), ".profile"), "a", encoding="utf-8") as f:
                f.write('\nexport %s="%s"\n' % (name, value))
        except Exception:
            pass
        return True, "已写入 %s 与 ~/.profile" % os.path.join(d, "%s.conf" % name)
    except Exception as e:
        return False, repr(e)


# ---------------------------------------------------------------------------
# 开机自启
# ---------------------------------------------------------------------------

def _launcher_script():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "scripts", "launch_guard.py")


def autostart_install():
    """安装开机自启（跟随 Codex 生命周期）。返回 (ok, 说明)。"""
    script = _launcher_script()
    if not os.path.exists(script):
        return False, "找不到 %s" % script
    py = python_launcher()
    try:
        if IS_WINDOWS:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
            cmd = '"%s" "%s"' % (py, script)
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, cmd)
            winreg.CloseKey(key)
            return True, "已写 HKCU Run 键 %s -> %s" % (AUTOSTART_NAME, cmd)
        if IS_MACOS:
            d = os.path.join(home_dir(), "Library", "LaunchAgents")
            os.makedirs(d, exist_ok=True)
            plist = os.path.join(d, "com.openai.codex.model-switcher.plist")
            body = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0">\n<dict>\n'
                '  <key>Label</key><string>com.openai.codex.model-switcher</string>\n'
                '  <key>ProgramArguments</key>\n  <array>\n'
                '    <string>%s</string>\n    <string>%s</string>\n  </array>\n'
                '  <key>RunAtLoad</key><true/>\n'
                '  <key>KeepAlive</key><false/>\n'
                '  <key>StandardOutPath</key><string>%s</string>\n'
                '  <key>StandardErrorPath</key><string>%s</string>\n'
                '</dict>\n</plist>\n' % (py, script,
                                         os.path.join(home_dir(), ".codex",
                                                      "model-switcher-guard.log"),
                                         os.path.join(home_dir(), ".codex",
                                                      "model-switcher-guard.err")))
            with open(plist, "w", encoding="utf-8") as f:
                f.write(body)
            subprocess.run(["launchctl", "unload", plist], capture_output=True)
            subprocess.run(["launchctl", "load", plist], capture_output=True)
            return True, "已安装 LaunchAgent：%s（重开登录后生效）" % plist
        # Linux
        unit_dir = os.path.join(home_dir(), ".config", "systemd", "user")
        if os.path.isdir(unit_dir) or shutil.which("systemctl"):
            os.makedirs(unit_dir, exist_ok=True)
            unit = os.path.join(unit_dir, "codex-model-switcher-guard.service")
            body = ("[Unit]\nDescription=Codex provider alignment guard\n\n"
                    "[Service]\nType=simple\nExecStart=%s %s\nRestart=on-failure\n\n"
                    "[Install]\nWantedBy=default.target\n" % (py, script))
            with open(unit, "w", encoding="utf-8") as f:
                f.write(body)
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            subprocess.run(["systemctl", "--user", "enable", "--now",
                            "codex-model-switcher-guard.service"], capture_output=True)
            return True, "已安装 systemd user unit：%s" % unit
        d = os.path.join(home_dir(), ".config", "autostart")
        os.makedirs(d, exist_ok=True)
        desk = os.path.join(d, "codex-model-switcher-guard.desktop")
        body = ("[Desktop Entry]\nType=Application\nName=Codex provider guard\n"
                "Exec=%s %s\nX-GNOME-Autostart-enabled=true\n" % (py, script))
        with open(desk, "w", encoding="utf-8") as f:
            f.write(body)
        return True, "已安装 XDG autostart：%s" % desk
    except Exception as e:
        return False, repr(e)


def autostart_uninstall():
    if IS_WINDOWS:
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass
            winreg.CloseKey(key)
            return True, "已删除 HKCU Run 键 %s" % AUTOSTART_NAME
        except Exception as e:
            return False, repr(e)
    if IS_MACOS:
        plist = os.path.join(home_dir(), "Library", "LaunchAgents",
                             "com.openai.codex.model-switcher.plist")
        subprocess.run(["launchctl", "unload", plist], capture_output=True)
        if os.path.exists(plist):
            os.unlink(plist)
        return True, "已移除 LaunchAgent"
    unit = os.path.join(home_dir(), ".config", "systemd", "user",
                         "codex-model-switcher-guard.service")
    if os.path.exists(unit):
        subprocess.run(["systemctl", "--user", "disable", "--now",
                        "codex-model-switcher-guard.service"], capture_output=True)
        os.unlink(unit)
    return True, "已移除 systemd user unit / autostart 项"
