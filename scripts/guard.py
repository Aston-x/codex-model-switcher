# -*- coding: utf-8 -*-
"""provider 修复器 —— 让 Codex 桌面版换模型后不再报 `unknown model` (2013)。

背景
----
Codex 桌面版的模型选择器**结构上就没有 provider 字段**
（全局状态里模型配置只有 {model, reasoningEffort, serviceTier}），
换模型时只写模型名、**沿用旧的 provider**，于是请求用新模型名打到旧端点：
  invalid params, code: 2013, unknown model 'deepseek-flash'

provider 固化在**会话**里（state_5.sqlite 的 threads.model_provider），
桌面版把它缓存在内存 -> 改数据库对**已开着的**会话无效。

本修复器做两件事
----------------

1. config.toml 的 model + model_provider 配对修正
   -> 影响**之后新建**的会话
2. 用 app-server 的 thread/resume 修正**已存在但空闲**的会话
   （实测：除正在跑的会话报 "active writer" 外，其余都能 resume 成功并改对）
   -> 正在跑的那个会话要等它空闲/重开才会被修

生命周期
--------
跟随 Codex 桌面版：Codex 没在跑就等，Codex 退出就一起退出。
完全静默：Windows 用 pythonw 启动（无控制台窗口），
POSIX 用 setsid 脱离终端；全程不起任何子控制台进程。

跨平台
------
平台差异（进程枚举 / 锁自愈 / python 解释器）都收敛在 platform_compat.py。
"""
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import platform_compat  # noqa: E402

CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
CONFIG = os.path.join(CODEX_HOME, "config.toml")
GLOBAL_STATE = os.path.join(CODEX_HOME, ".codex-global-state.json")
PRESETS = os.path.join(CODEX_HOME, ".codex-switcher-presets.json")
STATE_DB = os.path.join(CODEX_HOME, "state_5.sqlite")
LOG = os.path.join(CODEX_HOME, "model-switcher-guard.log")
CODEX_CLI = os.environ.get("CODEX_CLI_PATH") or ""

INTERVAL = 3.0
RESUME_EVERY = 20.0          # 多久扫一次会话（resume 比较重，别太频繁）


def log(msg):
    try:
        lines = []
        if os.path.exists(LOG):
            with io.open(LOG, "r", encoding="utf-8") as f:
                lines = f.read().split("\n")
        lines.append("%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        with io.open(LOG, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines[-200:]))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 归属表
# ---------------------------------------------------------------------------

def read_providers():
    try:
        with io.open(PRESETS, "r", encoding="utf-8-sig") as f:
            return json.load(f).get("providers", [])
    except Exception:
        return []


def model_owner(model):
    """模型名 -> provider id。gpt-/codex-/o1o3o4 前缀归 openai。"""
    model = (model or "").strip()
    if not model:
        return None
    if re.match(r"^(gpt-|codex-|o1|o3|o4)", model, re.I):
        return "openai"
    for pr in read_providers():
        if model in (pr.get("models") or []) or model == pr.get("model"):
            return pr.get("id")
    return None


def last_picked_model():
    """桌面版最近一次选中的模型（最新使用的排在列表最前）。"""
    try:
        with io.open(GLOBAL_STATE, "r", encoding="utf-8") as f:
            d = json.load(f)
        cfgs = (d.get("electron-persisted-atom-state", {})
                 .get("composer-recent-model-configurations-v1") or [])
        if cfgs and isinstance(cfgs[0], dict):
            return (cfgs[0].get("model") or "").strip() or None
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# config.toml
# ---------------------------------------------------------------------------

def read_root_keys():
    try:
        with io.open(CONFIG, "r", encoding="utf-8") as f:
            lines = f.read().split("\n")
    except Exception:
        return None, None, None
    model = provider = None
    for ln in lines:
        s = ln.strip()
        m = re.match(r'^model\s*=\s*"?([^"\n]*)"?', s)
        if m:
            model = m.group(1)
        m = re.match(r'^model_provider\s*=\s*"?([^"\n]*)"?', s)
        if m:
            provider = m.group(1)
    return model, provider, lines


def fix_config(new_model, new_provider):
    """把 config.toml 根级 model / model_provider 改成配对值。原子写 + 备份。"""
    _cm, _cp, lines = read_root_keys()
    if lines is None:
        return False
    new = []
    for ln in lines:
        s = ln.strip()
        if re.match(r'^model\s*=', s):
            new.append('model = "%s"' % new_model)
        elif re.match(r'^model_provider\s*=', s):
            new.append('model_provider = "%s"' % new_provider)
        else:
            new.append(ln)
    text = "\n".join(new)
    try:
        with io.open(CONFIG + ".bak-guard", "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except Exception:
        pass
    tmp = CONFIG + ".tmp-guard"
    try:
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, CONFIG)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False
    return True


# ---------------------------------------------------------------------------
# 已存在的会话：thread/resume 修正
# ---------------------------------------------------------------------------

class AppServer:
    """极简 app-server 客户端（stdio JSON-RPC），只用来发 thread/resume。"""

    def __init__(self, cli_path):
        self.cli = cli_path
        self.p = None
        self.q = None
        self._id = 0

    def _ensure(self):
        if self.p is not None and self.p.poll() is None:
            return True
        if not self.cli or not os.path.exists(self.cli):
            return False
        env = dict(os.environ)
        env["CODEX_HOME"] = CODEX_HOME
        kwargs = dict(
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env,
            bufsize=1, universal_newlines=True, encoding="utf-8")
        if platform_compat.IS_WINDOWS:
            kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                       | getattr(subprocess, "DETACHED_PROCESS", 0))
        else:
            kwargs["start_new_session"] = True
        try:
            self.p = subprocess.Popen([self.cli, "app-server", "--stdio"], **kwargs)
        except Exception:
            return False
        import queue
        self.q = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()
        self._rpc("initialize", {"clientInfo": {"name": "model-switcher-guard",
                                               "title": "Guard", "version": "1.2.0"}}, 20)
        try:
            self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized",
                                           "params": {}}) + "\n")
            self.p.stdin.flush()
        except Exception:
            pass
        return True

    def _reader(self):
        try:
            for line in self.p.stdout:
                line = line.strip()
                if line:
                    self.q.put(line)
        except Exception:
            pass

    def _rpc(self, method, params, timeout=25):
        if not self._ensure():
            return None
        self._id += 1
        rid = self._id
        try:
            self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                           "method": method, "params": params}) + "\n")
            self.p.stdin.flush()
        except Exception:
            return None
        import queue
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                line = self.q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("id") == rid:
                return o
        return None

    def close(self):
        try:
            if self.p:
                self.p.kill()
        except Exception:
            pass
        self.p = None


def mismatched_threads():
    """列出 model 与 provider 错配的会话。"""
    out = []
    try:
        conn = sqlite3.connect(STATE_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, model, model_provider FROM threads "
                            "WHERE model IS NOT NULL").fetchall()
        conn.close()
    except Exception:
        return out
    for r in rows:
        owner = model_owner(r["model"])
        if owner and owner != r["model_provider"]:
            out.append((r["id"], r["model"], r["model_provider"], owner))
    return out


def repair_threads(server):
    """用 thread/resume 修正错配会话（正在跑的会报 active writer，跳过）。"""
    fixed = 0
    skipped = 0
    for tid, model, prov, owner in mismatched_threads():
        r = server._rpc("thread/resume",
                        {"threadId": tid, "model": model, "modelProvider": owner}, 20)
        if r and r.get("result"):
            fixed += 1
        else:
            skipped += 1
    return fixed, skipped


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

def _loop():
    log("guard started pid=%d" % os.getpid())
    server = AppServer(CODEX_CLI or platform_compat.codex_cli_path())
    last_resume = 0.0
    while True:
        try:
            picked = last_picked_model()
            if picked:
                owner = model_owner(picked)
                if owner:
                    cm, cp, _ = read_root_keys()
                    if cm != picked or cp != owner:
                        # 只在「UI 状态比 config 新」时才改，
                        # 否则插件自己 switch_model 切了模型会被守护改回去
                        try:
                            newer = (os.path.getmtime(GLOBAL_STATE)
                                     >= os.path.getmtime(CONFIG))
                        except Exception:
                            newer = True
                        if newer:
                            ok = fix_config(picked, owner)
                            log("config: model %s -> %s, provider %s -> %s (%s)"
                                % (cm, picked, cp, owner, "ok" if ok else "FAILED"))

            # 定期修正已存在的会话
            now = time.time()
            if now - last_resume >= RESUME_EVERY:
                last_resume = now
                fixed, skipped = repair_threads(server)
                if fixed:
                    log("threads: 修正 %d 个错配会话（%d 个占用中跳过）" % (fixed, skipped))
        except Exception as e:
            log("error: %r" % e)
        time.sleep(INTERVAL)


def _lock_is_stale(lock_path):
    try:
        with open(lock_path, "r") as f:
            pid = int((f.read() or "0").strip() or 0)
    except Exception:
        return True
    if pid <= 0:
        return True
    return not platform_compat.pid_alive(pid)


def _acquire_single_instance():
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guard.lock")
    for _ in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return True, lock_path
        except FileExistsError:
            if not _lock_is_stale(lock_path):
                return False, lock_path
            try:
                os.unlink(lock_path)
            except Exception:
                return False, lock_path
        except Exception:
            return True, lock_path
    return True, lock_path


def main():
    ok, lock_path = _acquire_single_instance()
    if not ok:
        return 0
    try:
        _loop()
    finally:
        try:
            os.unlink(lock_path)
        except Exception:
            pass


if __name__ == "__main__":
    main()
