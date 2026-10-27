#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""model-switcher 环境体检 / 自动修复脚本。

在一台**没跑过本机配置**的机器上，大概率会遇到这些问题：
  1. Python 版本不够（需要 3.11+ 的 tomllib）
  2. 没装 mcp 包 -> MCP server 起不来
  3. 插件的 .mcp.json 是别平台的（cmd.exe 在 macOS 上跑不了）
  4. config.toml 里 model 与 model_provider 配不上 -> unknown model (2013)
  5. config.toml 里 model_context_window 写成了字符串 -> Codex 起不来
  6. 目标模型没登记进 model_catalog_json -> 桌面版写回别的模型
  7. 桌面版读不到用户级环境变量（key 必须 inline）
  8. 没装开机自启，provider 配对守护不会跟着 Codex 起来

用法：
    python doctor.py            # 只体检，列出问题（不改任何东西）
    python doctor.py --fix      # 体检 + 自动修
    python doctor.py --json     # 机器可读输出
    python doctor.py --fix --no-autostart   # 修，但不碰开机自启

退出码：0 = 一切正常（或已修好）；1 = 仍有无法自动修的问题
"""
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import platform_compat as pc  # noqa: E402

PLUGIN_JSON = os.path.join(HERE, ".codex-plugin", "plugin.json")
MCP_WIN = os.path.join(HERE, ".mcp.windows.json")
MCP_UNIX = os.path.join(HERE, ".mcp.unix.json")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def read_text(path):
    try:
        with io.open(path, "r", encoding="utf-8-sig") as f:
            return f.read()
    except Exception:
        return None


def write_text(path, text):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    try:
        os.chmod(path, 0o644)
    except Exception:
        pass


def plugin_version():
    d = json.loads(read_text(PLUGIN_JSON) or "{}")
    return d.get("version") or "0"


def plugin_root():
    """插件根目录（源码目录或已安装到 cache 的副本）。"""
    return HERE


def codex_cache_dirs():
    """~/.codex/plugins/cache/<marketplace>/<plugin>/<version>/"""
    out = []
    base = os.path.join(pc.codex_home(), "plugins", "cache")
    if not os.path.isdir(base):
        return out
    for mp in sorted(os.listdir(base)):
        p1 = os.path.join(base, mp)
        if not os.path.isdir(p1):
            continue
        for pl in sorted(os.listdir(p1)):
            p2 = os.path.join(p1, pl)
            if not os.path.isdir(p2):
                continue
            for ver in sorted(os.listdir(p2)):
                p3 = os.path.join(p2, ver)
                if os.path.isdir(p3) and os.path.exists(
                        os.path.join(p3, ".codex-plugin", "plugin.json")):
                    out.append(p3)
    return out


def find_installed_plugin():
    """本插件已安装的目录（挑版本号最大的）。"""
    cands = [d for d in codex_cache_dirs()
             if os.path.exists(os.path.join(d, "server.py"))]
    if not cands:
        return None
    def ver(d):
        try:
            return tuple(int(x) for x in re.findall(r"\d+", os.path.basename(d)))
        except Exception:
            return (0,)
    return sorted(cands, key=ver)[-1]


def run(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, repr(e)


# ---------------------------------------------------------------------------
# 体检
# ---------------------------------------------------------------------------

class Report(object):
    def __init__(self):
        self.items = []      # (level, 标题, 说明, 修复动作描述 or None)
        self.fixed = []

    def add(self, level, title, detail, fixable=None):
        self.items.append({"level": level, "title": title, "detail": detail,
                           "fixable": fixable})

    def ok(self, title, detail=""):
        self.add("ok", title, detail)

    def warn(self, title, detail, fixable=None):
        self.add("warn", title, detail, fixable)

    def err(self, title, detail, fixable=None):
        self.add("err", title, detail, fixable)

    def bad(self):
        return [i for i in self.items if i["level"] in ("warn", "err")]


def check_python(rep):
    v = sys.version_info
    if v < (3, 11):
        rep.err("Python 版本过低",
                "当前 %d.%d，需要 3.11+（要 tomllib）。" % (v[0], v[1]),
                "安装 Python 3.11 或更高版本")
        return False
    rep.ok("Python", "%d.%d.%d @ %s" % (v[0], v[1], v[2], sys.executable))
    return True


def check_mcp(rep, fix):
    try:
        import mcp  # noqa: F401
        rep.ok("mcp 包", "已安装")
        return True
    except ImportError:
        pass
    if fix:
        rc, out = run([sys.executable, "-m", "pip", "install", "--quiet", "mcp"])
        if rc == 0:
            rep.fixed.append("已安装 mcp 包")
            rep.ok("mcp 包", "已自动安装")
            return True
        rep.err("mcp 包未安装", "pip 安装失败：%s" % out.strip()[:200],
                "手动执行：pip install mcp")
        return False
    rep.err("mcp 包未安装", "MCP server 会起不来",
            "pip install mcp")
    return False


def check_mcp_json(rep, fix):
    """当前平台的 .mcp.json 是否就位。"""
    want = MCP_WIN if pc.IS_WINDOWS else MCP_UNIX
    if not os.path.exists(want):
        rep.err("缺少 .mcp 配置模板", "找不到 %s" % os.path.basename(want))
        return
    targets = [HERE]
    inst = find_installed_plugin()
    if inst and inst != HERE:
        targets.append(inst)
    for root in targets:
        cur = os.path.join(root, ".mcp.json")
        want_text = read_text(want)
        if read_text(cur) == want_text:
            continue
        if fix:
            write_text(cur, want_text)
            rep.fixed.append("已按 %s 平台写好 %s" % (pc.os_label(), cur))
        else:
            rep.warn(".mcp.json 与平台不符",
                     "%s 的 .mcp.json 不是 %s 版本（该平台需要 %s）"
                     % (root, pc.os_label(), os.path.basename(want)),
                     "按当前平台重写 .mcp.json")
    rep.ok(".mcp.json", "平台模板：%s" % os.path.basename(want))


def _toml_literal(value):
    """把值写成 TOML **字面量字符串**（单引号）。

    ⚠️ 关键：Windows 路径里有反斜杠（C:\\Users\\...）。
       写成基本字符串（双引号）时，TOML 会把 \\U 当成非法转义 ->
       `Invalid hex value`，config.toml 直接解析失败，Codex 起不来。
       字面量字符串不处理转义，是路径的正确写法。
    """
    if "'" in value:
        return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')
    return "'%s'" % value


def _set_root_string_key(cfg_path, key, value):
    """往 config.toml 顶层写一个字符串键。

    写完**必须回读校验**（tomllib 解析一遍）——
    绝不能把用户的 config.toml 写坏。校验不过就放弃并还原。
    """
    raw = read_text(cfg_path)
    if raw is None:
        return False, "读不到 %s" % cfg_path
    line = '%s = %s' % (key, _toml_literal(value))
    if re.search(r"^\s*%s\s*=" % re.escape(key), raw, re.M):
        new = re.sub(r"^\s*%s\s*=.*$" % re.escape(key),
                     lambda _m: line, raw, count=1, flags=re.M)
    else:
        # 插到第一个顶层键后面
        m = re.search(r"^\s*(\w+\s*=.*)$", raw, re.M)
        if m:
            new = raw[:m.end(1)] + "\n" + line + raw[m.end(1):]
        else:
            new = raw.rstrip("\n") + "\n" + line + "\n"
    try:
        import tomllib
        with open(cfg_path, "wb") as f:
            f.write(new.encode("utf-8"))
        with open(cfg_path, "rb") as f:
            tomllib.load(f)
    except Exception as e:
        return False, "写入 %s 会破坏 config.toml（%s），已放弃" % (key, e)
    write_text(cfg_path, new)
    return True, "已写入 %s = %s" % (key, line)


def check_codex_paths(rep, fix=None):
    """config.toml 里 Codex 自己写的那段路径，升级后可能失效。

    Codex 桌面版升级会换掉 %LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<hash>\\
    里的目录名，但**不一定同步刷新** config.toml 里 [shell_environment_policy]
    那段的 CODEX_CLI_PATH / NODE_REPL_* —— 那些值会指向已删除的旧目录，
    依赖它们的功能（起 codex CLI、browser 插件）会静默失效。
    """
    cfg_path = os.path.join(pc.codex_home(), "config.toml")
    raw = read_text(cfg_path)
    if raw is None:
        return
    stale = []
    for m in re.finditer(r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*'([^']*[\\/][^']*)'",
                         raw, re.M):
        key, val = m.group(1), m.group(2)
        if not re.match(r"^[A-Za-z]:\\", val):
            continue
        if not os.path.exists(val):
            stale.append((key, val))
    if not stale:
        rep.ok("config.toml 里的绝对路径", "全部有效")
        return
    for key, val in stale:
        rep.warn("%s 指向的路径已不存在" % key,
                 "%s\n  原因多半是 Codex 升级换了 bin\\<hash> 目录名，"
                 "但没刷新 config.toml 里的这段。" % val,
                 "完全退出并重开 Codex 桌面版（让它自己重写），"
                 "或把值改成当前实际路径")


def check_config(rep, fix):
    """config.toml 的存在性、可解析性、关键键。"""
    home = pc.codex_home()
    cfg_path = os.path.join(home, "config.toml")
    if not os.path.exists(cfg_path):
        rep.err("找不到 config.toml", "%s 不存在" % cfg_path,
                "先跑一次 Codex，或手动创建")
        return None
    try:
        import tomllib
        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
    except Exception as e:
        rep.err("config.toml 解析失败", repr(e), "手动检查 TOML 语法")
        return None
    rep.ok("config.toml", "%s（%d 个顶层键）" % (cfg_path, len(cfg)))

    # model_context_window 必须是整数
    raw = read_text(cfg_path) or ""
    m = re.search(r'^\s*model_context_window\s*=\s*"([^"]*)"', raw, re.M)
    if m:
        if fix:
            new = re.sub(r'(^\s*model_context_window\s*=\s*)"[^"]*"',
                          r'\g<1>%s' % m.group(1), raw, flags=re.M)
            write_text(cfg_path, new)
            rep.fixed.append("model_context_window 已从字符串改成整数")
            rep.ok("model_context_window", "已修正为整数")
        else:
            rep.err("model_context_window 写成了字符串",
                    '写的是 "%s"，会让 Codex 报 invalid type: string, expected i64'
                    % m.group(1),
                    "改成不带引号的整数")

    # model / model_provider 配对
    model = (cfg.get("model") or "").strip()
    provider = (cfg.get("model_provider") or "").strip()
    if model and provider:
        owner = model_owner(model)
        if owner and owner != provider:
            if fix:
                ok2, msg = _set_root_string_key(cfg_path, "model_provider", owner)
                if ok2:
                    rep.fixed.append("model_provider 已修正：%s -> %s" % (provider, owner))
                    rep.ok("model/provider 配对", "已修正")
                else:
                    rep.err("model_provider 修正失败", msg,
                            "手动把 model_provider 改成 %s" % owner)
            else:
                rep.err("model 与 model_provider 不配对",
                        "model=%s 属于 %s，但 model_provider=%s -> unknown model (2013)"
                        % (model, owner, provider),
                        "把 model_provider 改成 %s" % owner)
        else:
            rep.ok("model/provider 配对", "model=%s provider=%s" % (model, provider))

    # 模型目录
    cat_path = cfg.get("model_catalog_json")
    if not cat_path:
        # 新机器上没这个键 -> 桌面版模型选择器会看不到任何自定义模型
        new_cat = os.path.join(home, "model-catalog.json")
        if fix:
            write_text(new_cat, json.dumps({"models": []}, ensure_ascii=False, indent=1) + "\n")
            ok2, msg = _set_root_string_key(cfg_path, "model_catalog_json", new_cat)
            if ok2:
                rep.fixed.append("已创建模型目录 %s 并在 config.toml 里登记" % new_cat)
                rep.ok("模型目录", "已创建 %s" % new_cat)
            else:
                rep.err("模型目录已创建但登记失败", msg,
                        "手动在 config.toml 加 model_catalog_json = '%s'" % new_cat)
        else:
            rep.warn("没有 model_catalog_json",
                     "桌面版模型选择器看不到自定义模型",
                     "创建模型目录并在 config.toml 登记 model_catalog_json")
    elif not os.path.exists(cat_path):
        if fix:
            write_text(cat_path, json.dumps({"models": []}, ensure_ascii=False, indent=1) + "\n")
            rep.fixed.append("已创建空的模型目录 %s" % cat_path)
            rep.ok("模型目录", "已创建 %s" % cat_path)
        else:
            rep.err("model_catalog_json 指向的文件不存在", cat_path,
                    "创建该文件并登记模型")
    else:
        try:
            with io.open(cat_path, encoding="utf-8-sig") as f:
                cat = json.load(f)
            slugs = {m.get("slug") for m in (cat.get("models") or [])
                     if isinstance(m, dict)}
            if model and model not in slugs:
                rep.warn("当前模型没登记进模型目录",
                         "%s 不在 %s 里 -> 桌面版会把顶层 model 写回别的模型"
                         % (model, cat_path),
                         "用 register_model_in_catalog 登记")
            else:
                rep.ok("模型目录", "%s（%d 条）" % (cat_path, len(slugs)))
        except Exception as e:
            rep.err("模型目录解析失败", repr(e), "手动检查 JSON")

    # provider 的 key 是不是 inline
    for pid, blk in (cfg.get("model_providers") or {}).items():
        if not isinstance(blk, dict):
            continue
        if blk.get("env_key") and not blk.get("experimental_bearer_token"):
            rep.warn("provider '%s' 只用环境变量" % pid,
                     "桌面版（MSIX）继承 explorer 的旧环境快照，读不到 setx 写的变量 "
                     "-> Missing environment variable: %s" % blk.get("env_key"),
                     "改成 inline（key 内联进 config.toml）")
    return cfg


def model_owner(model):
    """模型名 -> provider id。"""
    model = (model or "").strip()
    if not model:
        return None
    if re.match(r"^(gpt-|codex-|o1|o3|o4)", model, re.I):
        return "openai"
    p = os.path.join(pc.codex_home(), ".codex-switcher-presets.json")
    try:
        with io.open(p, encoding="utf-8-sig") as f:
            providers = json.load(f).get("providers", [])
    except Exception:
        return None
    for pr in providers:
        if model in (pr.get("models") or []) or model == pr.get("model"):
            return pr.get("id")
    return None


def check_clipboard(rep):
    """剪贴板后端是否可用（**只查后端，不读内容**）。

    剪贴板是 API key 的安全交接通道（key 不进对话与工具参数），
    后端不可用就等于这条路走不通，必须让用户知道。
    """
    try:
        backend = pc.clipboard_backend()
    except Exception as e:
        rep.warn("剪贴板后端检测失败", repr(e),
                 "剪贴板接入密钥会不可用")
        return
    if backend:
        rep.ok("剪贴板后端", "%s 可用（API key 可走剪贴板交接，不进对话记录）" % backend)
    else:
        rep.warn("没有可用的剪贴板后端",
                 "Windows 走 Win32 API（无需装东西）；macOS 需 pbpaste（系统自带）；"
                 "Linux 需装 wl-paste / xclip / xsel 之一",
                 "装剪贴板工具，或改用明文方式传 key（会进对话记录）")


def check_install(rep, fix):
    inst = find_installed_plugin()
    if not inst:
        rep.warn("插件没装进 Codex 缓存",
                 "找不到 ~/.codex/plugins/cache/*/model-switcher/*/",
                 "执行：codex plugin marketplace add <插件目录> 然后在 Codex 里安装")
        return
    rep.ok("插件已安装", inst)
    ver = plugin_version()
    got = None
    try:
        got = json.loads(read_text(os.path.join(inst, ".codex-plugin", "plugin.json"))
                         or "{}").get("version")
    except Exception:
        pass
    if got and ver and got != ver:
        rep.warn("缓存里的版本与源码不一致",
                 "cache=%s 源码=%s -> 改的源码不会生效" % (got, ver),
                 "重新安装插件或把源码同步到缓存")
    else:
        rep.ok("版本一致", ver)


def check_autostart(rep, fix):
    if pc.IS_WINDOWS:
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
            try:
                val, _ = winreg.QueryValueEx(key, pc.AUTOSTART_NAME)
            except FileNotFoundError:
                val = None
            winreg.CloseKey(key)
        except Exception:
            val = None
        if val:
            rep.ok("开机自启", val)
        else:
            if fix:
                ok, msg = pc.autostart_install()
                (rep.ok if ok else rep.err)("开机自启", msg)
                if ok:
                    rep.fixed.append("已安装开机自启")
            else:
                rep.warn("没装开机自启",
                         "provider 配对守护不会跟着 Codex 起来",
                         "安装开机自启")
    else:
        if pc.IS_MACOS:
            p = os.path.join(pc.home_dir(), "Library", "LaunchAgents",
                             "com.openai.codex.model-switcher.plist")
        else:
            p = os.path.join(pc.home_dir(), ".config", "systemd", "user",
                             "codex-model-switcher-guard.service")
            if not os.path.exists(p):
                p = os.path.join(pc.home_dir(), ".config", "autostart",
                                 "codex-model-switcher-guard.desktop")
        if os.path.exists(p):
            rep.ok("开机自启", p)
        elif fix:
            ok, msg = pc.autostart_install()
            (rep.ok if ok else rep.err)("开机自启", msg)
            if ok:
                rep.fixed.append("已安装开机自启")
        else:
            rep.warn("没装开机自启", "provider 配对守护不会跟着 Codex 起来",
                     "安装开机自启")


def _lock_alive(lock_path):
    """锁文件里记着 PID；PID 还活着 => 进程在跑。"""
    try:
        with io.open(lock_path, "r", encoding="utf-8") as f:
            pid = int((f.read() or "0").strip() or 0)
    except Exception:
        return False
    if pid <= 0:
        return False
    return pc.pid_alive(pid)


def check_guard_running(rep):
    """守护进程是否在跑 —— 用**锁文件**判活，不依赖进程命令行。

    （Windows 上枚举进程只能拿到 exe 路径，拿不到命令行，
      靠 grep "guard.py" 会漏判；锁文件里的 PID 才是可靠判据。）
    """
    if not pc.codex_desktop_running():
        rep.ok("Codex 桌面版", "未在运行（守护进程会等待，符合预期）")
        return
    roots = [HERE]
    if os.path.basename(HERE) == "scripts":
        roots.append(os.path.dirname(HERE))
    inst = find_installed_plugin()
    if inst:
        roots.append(inst)
    alive = False
    for root in roots:
        for lk in (os.path.join(root, "launch_guard.lock"),
                   os.path.join(root, "scripts", "guard.lock")):
            if _lock_alive(lk):
                alive = True
                break
    if alive:
        rep.ok("provider 守护进程", "在跑（锁文件判活）")
    else:
        rep.warn("provider 守护进程没在跑",
                 "Codex 在跑但守护进程不在 -> 换模型可能报 2013",
                 "启动 scripts/launch_guard.py（开机自启可交给 doctor --fix 装）")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_doctor(fix=False, want_json=False, do_autostart=True):
    rep = Report()
    check_python(rep)
    check_mcp(rep, fix)
    check_mcp_json(rep, fix)
    check_clipboard(rep)
    check_codex_paths(rep, fix)
    check_config(rep, fix)
    check_install(rep, fix)
    if do_autostart:
        check_autostart(rep, fix)
    check_guard_running(rep)

    if want_json:
        print(json.dumps({
            "os": pc.os_label(),
            "python": sys.version.split()[0],
            "codex_home": pc.codex_home(),
            "plugin_version": plugin_version(),
            "items": rep.items,
            "fixed": rep.fixed,
        }, ensure_ascii=False, indent=2))
    else:
        print("=" * 68)
        print(" model-switcher 环境体检   %s   插件 v%s" % (pc.os_label(), plugin_version()))
        print(" Codex Home: %s" % pc.codex_home())
        print("=" * 68)
        for it in rep.items:
            icon = {"ok": "[ ok ]", "warn": "[warn]", "err": "[FAIL]"}[it["level"]]
            print("%s %s" % (icon, it["title"]))
            if it.get("detail"):
                print("         %s" % it["detail"])
            if it.get("fixable"):
                print("         → 可自动修：%s" % it["fixable"])
        if rep.fixed:
            print("-" * 68)
            print("已自动修复：")
            for f in rep.fixed:
                print("  · %s" % f)
        print("-" * 68)
        left = rep.bad()
        if not left:
            print("结论：一切正常")
        elif fix:
            print("结论：仍有 %d 项需要人工处理" % len(left))
            for it in left:
                print("  · %s —— %s" % (it["title"], it.get("fixable") or it.get("detail")))
        else:
            print("结论：%d 项问题待修。执行 `python doctor.py --fix` 自动修。"
                  % len(left))
    return 0 if not rep.bad() else 1


def _init_stdio():
    """控制台编码兜底。

    Windows 中文控制台默认 GBK，打印 '✓' / '✗' 这类字符会直接
    UnicodeEncodeError 崩掉整个脚本 —— 必须提前把 stdout/stderr 放宽。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass


def main():
    _init_stdio()
    ap = argparse.ArgumentParser(description="model-switcher 环境体检 / 自动修复")
    ap.add_argument("--fix", action="store_true", help="自动修复能修的")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--no-autostart", action="store_true", help="不检查/不安装开机自启")
    a = ap.parse_args()
    try:
        sys.exit(run_doctor(fix=a.fix, want_json=a.json,
                            do_autostart=not a.no_autostart))
    except Exception as e:
        print("doctor 异常：%r" % e)
        sys.exit(2)


if __name__ == "__main__":
    main()
