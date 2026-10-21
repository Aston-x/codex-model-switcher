#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Codex 模型切换器 (Codex Model Switcher)
----------------------------------------
一个零依赖的本地网页工具，用来给 Codex 桌面版 / CLI 切换默认模型和
自定义 model provider（DeepSeek / Kimi / 智谱 GLM / 任意 OpenAI 兼容端点）。

原理：Codex 桌面版没有给自定义 provider 提供模型选择器，但它会读取
~/.codex/config.toml。本工具用纯标准库做"行级精准编辑"——只改写根级的
model / model_provider，并按需新增/更新 [model_providers.<id>] 块，不会破坏
你 config.toml 里已有的 plugins / mcp_servers / projects 等其它配置；对一个
provider 块内用户手加的额外键、以及它的子表（[model_providers.x.http_headers]
等）也会原样保留。每次写入前都会自动备份。

安全：服务只监听 127.0.0.1，并用一次性随机 token 保护所有接口——必须通过
启动时打印（并自动打开）的带 token 链接访问，其它本机进程/网页无法读到密钥
或改写配置。

用法：
    python3 switcher.py
然后用终端打印的带 token 链接打开（脚本会自动尝试打开）。
"""

import json
import os
import re
import secrets
import shutil
import stat
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# 串行化所有 config.toml 读改写，避免多线程并发请求互相覆盖
_LOCK = threading.RLock()

# 一次性会话 token（main() 中生成），保护所有 HTTP 接口
TOKEN = ""

# 一个 TOML 基本字符串（双引号）的字符串体，能正确跨过 \" 转义
_TOML_STR = r'"((?:[^"\\]|\\.)*)"'
# 一个 TOML 字面字符串（单引号）的字符串体（无转义）
_TOML_LIT = r"'([^']*)'"
# 任意 section 头（宽松）
_HDR_ANY = re.compile(r"\s*\[")
# 由本工具管理、保存时会重写的 provider 键；其余键原样保留
MANAGED_KEYS = {"name", "base_url", "wire_api", "experimental_bearer_token", "env_key"}
RESERVED_IDS = ("openai", "ollama", "lmstudio")


def toml_escape(value):
    """把任意字符串安全写进 TOML 双引号字符串。"""
    s = str(value)
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return s


def toml_unescape(s):
    """反转义 TOML 双引号字符串体（尽力而为）。"""
    out = []
    i = 0
    mp = {'"': '"', "\\": "\\", "n": "\n", "r": "\r", "t": "\t"}
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            out.append(mp.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def parse_str_value(rest):
    """从 '= 之后' 的文本里解析一个 TOML 字符串值（支持双引号/单引号），失败返回 None。"""
    m = re.match(r"\s*" + _TOML_STR, rest)
    if m:
        return toml_unescape(m.group(1))
    m = re.match(r"\s*" + _TOML_LIT, rest)
    if m:
        return m.group(1)
    return None


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
CONFIG_PATH = os.path.join(CODEX_HOME, "config.toml")
PRESETS_PATH = os.path.join(CODEX_HOME, ".codex-switcher-presets.json")
HOST = os.environ.get("SWITCHER_HOST") or "127.0.0.1"
try:
    PORT = int(os.environ.get("SWITCHER_PORT") or 8765)
except ValueError:
    PORT = 8765

# ---------------------------------------------------------------------------
# 内置预设（首次运行时写入 presets.json，之后以文件为准）
# 说明：DeepSeek / Kimi / GLM 都是 Chat Completions 协议；最新版 Codex 只认
# wire_api = "responses"。直连能否成功取决于你的 Codex 版本——若直连报协议
# 错误，把 base_url 指向本地代理（LiteLLM 等）即可，其它字段照填。
# ---------------------------------------------------------------------------
DEFAULT_PRESETS = {
    "version": 2,
    "providers": [
        {
            "id": "openai",
            "name": "OpenAI (官方默认)",
            "base_url": "",
            "model": "gpt-5.5",
            "models": ["gpt-5.5"],
            "wire_api": "responses",
            "env_key": "",
            "storage_mode": "none",
            "builtin": True,
        },
        {
            "id": "deepseek",
            "name": "DeepSeek",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-v4-pro",
            # 现役：v4-pro(性能)/v4-flash(速度)。旧的 deepseek-chat / deepseek-reasoner
            # 将于 2026/07/24 弃用，如仍需用可在编辑框手动添加。
            "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
            "wire_api": "responses",
            "env_key": "DEEPSEEK_API_KEY",
            "storage_mode": "inline",
            "builtin": False,
        },
        {
            "id": "kimi",
            "name": "Kimi (Moonshot)",
            "base_url": "https://api.moonshot.cn/v1",
            "model": "kimi-k2.7-code",
            "models": ["kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6", "kimi-k2.5", "moonshot-v1-128k"],
            "wire_api": "responses",
            "env_key": "MOONSHOT_API_KEY",
            "storage_mode": "inline",
            "builtin": False,
        },
        {
            "id": "glm",
            "name": "智谱 GLM",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "model": "glm-4.6",
            "models": ["glm-4.6", "glm-4.7", "glm-5", "glm-5.1", "glm-5.2"],
            "wire_api": "responses",
            "env_key": "ZHIPUAI_API_KEY",
            "storage_mode": "inline",
            "builtin": False,
        },
    ]
}

# 预设结构版本：用于把旧 presets.json 迁移到带 models 列表的新结构
PRESETS_VERSION = 2


# ---------------------------------------------------------------------------
# presets.json 读写（原子写 + 损坏时另存而非丢弃）
# ---------------------------------------------------------------------------
def _atomic_write(path, text, mode=0o600):
    tmp = path + ".tmp.switcher"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass  # Windows 上 chmod 基本是 no-op
    os.replace(tmp, path)


def migrate_presets(data):
    """把旧结构迁移到带 models 列表的新结构；刷新已知预设的模型名。"""
    changed = False
    for p in data["providers"]:
        if not isinstance(p.get("models"), list) or not p.get("models"):
            p["models"] = [p["model"]] if p.get("model") else []
            changed = True
    if data.get("version", 0) < PRESETS_VERSION:
        defmap = {d["id"]: d for d in DEFAULT_PRESETS["providers"]}
        for p in data["providers"]:
            d = defmap.get(p.get("id"))
            if d:
                # 刷新已知预设(deepseek/kimi/glm/openai)的模型列表；保留用户的 name/base_url/env_key/密钥
                p["models"] = list(d["models"])
                if p.get("model") not in d["models"]:
                    p["model"] = d["model"]
                changed = True
        data["version"] = PRESETS_VERSION
        changed = True
    if changed:
        save_presets(data)
    return data


def load_presets():
    if not os.path.exists(PRESETS_PATH):
        save_presets(DEFAULT_PRESETS)
        return json.loads(json.dumps(DEFAULT_PRESETS))
    try:
        with open(PRESETS_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("providers"), list):
            raise ValueError("bad presets")
        return migrate_presets(data)
    except Exception:
        # 损坏：另存一份再回退默认，避免静默丢失用户自定义 provider
        try:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(PRESETS_PATH, PRESETS_PATH + ".corrupt-" + stamp)
        except OSError:
            pass
        save_presets(DEFAULT_PRESETS)
        return json.loads(json.dumps(DEFAULT_PRESETS))


def save_presets(data):
    _atomic_write(PRESETS_PATH, json.dumps(data, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# config.toml 行级编辑（纯标准库，不依赖 tomllib）
# ---------------------------------------------------------------------------
def read_config_lines():
    if not os.path.exists(CONFIG_PATH):
        return []
    # utf-8-sig 吃掉 BOM；统一换行，避免行尾 \r 混进取值
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        text = f.read()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.split("\n")


def backup_config():
    if not os.path.exists(CONFIG_PATH):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = CONFIG_PATH + ".bak.switcher-" + stamp
    shutil.copy2(CONFIG_PATH, dst)
    try:
        os.chmod(dst, 0o600)  # 备份可能含明文 key，收紧权限
    except OSError:
        pass
    return dst


def write_config_lines(lines):
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    # 不做全局空行折叠：避免误改其它 section 里的多行字符串值。
    # 含明文密钥时强制 0o600；否则尽量保留原权限。
    has_secret = "experimental_bearer_token" in text
    mode = 0o600
    if not has_secret and os.path.exists(CONFIG_PATH):
        try:
            mode = stat.S_IMODE(os.stat(CONFIG_PATH).st_mode)
        except OSError:
            mode = 0o600
    # 内容无变化则跳过备份与写入
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                cur = f.read().replace("\r\n", "\n").replace("\r", "\n")
            if cur == text:
                return None
        except OSError:
            pass
    backup = backup_config()
    _atomic_write(CONFIG_PATH, text, mode)
    return backup


def first_section_index(lines):
    """根表区域 = 从开头到第一个 [section] 头之前。"""
    for i, ln in enumerate(lines):
        if _HDR_ANY.match(ln):
            return i
    return len(lines)


def set_root_key(lines, key, value):
    end = first_section_index(lines)
    new_line = '%s = "%s"' % (key, toml_escape(value))
    for i in range(end):
        if re.match(r"\s*" + re.escape(key) + r"\s*=", lines[i]):
            lines[i] = new_line
            return lines
    # 没找到 -> 插在根级 model 行之后，否则插在根区域末尾
    insert_at = end
    for i in range(end):
        if re.match(r"\s*model\s*=", lines[i]):
            insert_at = i + 1
            break
    lines.insert(insert_at, new_line)
    return lines


def read_active(lines):
    end = first_section_index(lines)
    model = None
    provider = None
    for i in range(end):
        m = re.match(r"\s*model\s*=\s*(.*)$", lines[i])
        if m:
            v = parse_str_value(m.group(1))
            if v is not None:
                model = v
        m = re.match(r"\s*model_provider\s*=\s*(.*)$", lines[i])
        if m:
            v = parse_str_value(m.group(1))
            if v is not None:
                provider = v
    return model, (provider or "openai")


def _header_re(header):
    """精确匹配 [header]（容忍行尾注释）。"""
    return re.compile(r"\s*\[" + re.escape(header) + r"\]\s*(#.*)?$")


def _descendant_re(header):
    """匹配 [header] 或其子表 [header.xxx]（容忍行尾注释）。"""
    return re.compile(r"\s*\[" + re.escape(header) + r"(\.[^\]]+)?\]\s*(#.*)?$")


def find_section_range(lines, header):
    """主表块 (start, end)：从 [header] 到下一个任意 section 头之前。找不到返回 None。"""
    hre = _header_re(header)
    start = None
    for i, ln in enumerate(lines):
        if hre.match(ln):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _HDR_ANY.match(lines[j]):
            end = j
            break
    return (start, end)


def find_logical_block_range(lines, header):
    """整个逻辑块 (start, end)：主表 + 其后紧跟的子表 [header.xxx]。找不到返回 None。"""
    hre = _header_re(header)
    dre = _descendant_re(header)
    start = None
    for i, ln in enumerate(lines):
        if hre.match(ln):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _HDR_ANY.match(lines[j]):
            if dre.match(lines[j]):
                continue  # 子表，属于本逻辑块
            end = j
            break
    return (start, end)


def list_config_providers(lines):
    ids = []
    for ln in lines:
        m = re.match(r"\s*\[model_providers\.([A-Za-z0-9_\-]+)\]\s*(#.*)?$", ln)
        if m:
            ids.append(m.group(1))
    return ids


def read_provider_block(lines, pid):
    """读取主表块里的键值（不含子表）。"""
    rng = find_section_range(lines, "model_providers." + pid)
    if not rng:
        return None
    s, e = rng
    out = {}
    for ln in lines[s + 1 : e]:
        m = re.match(r"\s*([A-Za-z0-9_]+)\s*=\s*(.*)$", ln)
        if m:
            v = parse_str_value(m.group(2))
            if v is not None:
                out[m.group(1)] = v
    return out


def build_provider_block(p):
    out = ["[model_providers.%s]" % p["id"]]
    out.append('name = "%s"' % toml_escape(p.get("name", p["id"])))
    if p.get("base_url"):
        out.append('base_url = "%s"' % toml_escape(p["base_url"]))
    if p.get("wire_api"):
        out.append('wire_api = "%s"' % toml_escape(p["wire_api"]))
    mode = p.get("storage_mode", "inline")
    if mode == "inline" and p.get("api_key"):
        # 内联密钥：桌面版 GUI 能直接读到（GUI 不继承 shell 环境变量）
        out.append('experimental_bearer_token = "%s"' % toml_escape(p["api_key"]))
    elif mode == "env" and p.get("env_key"):
        out.append('env_key = "%s"' % toml_escape(p["env_key"]))
    return out


def upsert_provider_block(lines, p):
    """新增/更新 provider 块；保留用户手加的非托管键与子表。"""
    new_main = build_provider_block(p)
    rng = find_logical_block_range(lines, "model_providers." + p["id"])
    if rng:
        s, e = rng
        body = lines[s + 1 : e]
        # 主表体在前、子表在后；找到第一个子表头的位置
        sub_start = len(body)
        for idx, ln in enumerate(body):
            if _HDR_ANY.match(ln):
                sub_start = idx
                break
        main_body = body[:sub_start]
        sub_tail = body[sub_start:]
        # 保留主表里非托管的键行与注释（丢弃空行，由下面统一管理）
        preserved = []
        for ln in main_body:
            if ln.strip() == "":
                continue
            m = re.match(r"\s*([A-Za-z0-9_]+)\s*=", ln)
            if m and m.group(1) in MANAGED_KEYS:
                continue
            preserved.append(ln)
        replacement = new_main + preserved + sub_tail
        if e < len(lines):
            replacement = replacement + [""]
        lines[s:e] = replacement
    else:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.extend(new_main)
        lines.append("")
    return lines


def remove_provider_block(lines, pid):
    rng = find_logical_block_range(lines, "model_providers." + pid)
    if not rng:
        return lines
    s, e = rng
    del lines[s:e]
    return lines


# ---------------------------------------------------------------------------
# 业务逻辑
# ---------------------------------------------------------------------------
def get_state():
    presets = load_presets()
    lines = read_config_lines()
    active_model, active_provider = read_active(lines)
    cfg_ids = list_config_providers(lines)
    providers = []
    for p in presets["providers"]:
        pid = p.get("id")
        if not pid:
            continue
        block = read_provider_block(lines, pid) if pid in cfg_ids else None
        has_inline = bool(block and block.get("experimental_bearer_token"))
        has_env = bool(block and block.get("env_key"))
        if p.get("builtin"):
            configured = True
        else:
            configured = bool(block) and (has_inline or has_env)
        # 当前激活 provider 的 model 以 config 根级为准，其余以预设为准
        model = p.get("model", "")
        if pid == active_provider and active_model:
            model = active_model
        models = p.get("models") or ([p["model"]] if p.get("model") else [])
        # 当前激活的真实 model 若不在列表里，补进去，方便下拉显示
        if model and model not in models:
            models = [model] + models
        item = {
            "id": pid,
            "name": (block.get("name") if block else "") or p.get("name", pid),
            "base_url": (block.get("base_url") if block else "") or p.get("base_url", ""),
            "model": model,
            "models": models,
            "wire_api": (block.get("wire_api") if block else "") or p.get("wire_api", "responses"),
            "env_key": (block.get("env_key") if block else "") or p.get("env_key", ""),
            "storage_mode": p.get("storage_mode", "inline"),
            "builtin": bool(p.get("builtin")),
            "configured": configured,
            "in_config": pid in cfg_ids,
            "has_key": has_inline or has_env,
            # 不再回传明文密钥；编辑时留空即保留已存的密钥
            "api_key": "",
        }
        providers.append(item)
    return {
        "config_path": CONFIG_PATH,
        "presets_path": PRESETS_PATH,
        "active_model": active_model,
        "active_provider": active_provider,
        "providers": providers,
    }


def do_switch(pid, model=None):
    presets = load_presets()
    preset = next((x for x in presets["providers"] if x.get("id") == pid), None)
    if not preset:
        return False, "未知 provider: %s" % pid
    name = preset.get("name", pid)
    lines = read_config_lines()
    if not preset.get("builtin"):
        block = read_provider_block(lines, pid)
        if not block or not block.get("base_url"):
            return False, "provider「%s」还没配置好（缺 base_url）。请先点编辑保存。" % name
        if not (block.get("experimental_bearer_token") or block.get("env_key")):
            return False, "provider「%s」缺少密钥，请先编辑填写 API Key 或环境变量名。" % name
    # 选定的 model：优先用前端传来的；否则用预设默认
    chosen = (model or "").strip() or preset.get("model") or read_active(lines)[0] or "gpt-5.5"
    if not chosen:
        return False, "provider「%s」未设置 model，请先编辑填写。" % name
    # 记住这次选择，作为该 provider 下次默认
    if chosen != preset.get("model"):
        preset["model"] = chosen
        if chosen not in (preset.get("models") or []):
            preset["models"] = [chosen] + list(preset.get("models") or [])
        save_presets(presets)
    lines = set_root_key(lines, "model_provider", pid)
    lines = set_root_key(lines, "model", chosen)
    backup = write_config_lines(lines)
    return True, backup


def do_save_provider(payload):
    pid = (payload.get("id") or "").strip()
    if not re.match(r"^[A-Za-z0-9_\-]+$", pid):
        return False, "provider id 只能含字母/数字/下划线/连字符"
    if pid in RESERVED_IDS:
        return False, "「%s」是 Codex 保留的内置 id，请换一个" % pid
    # 模型列表：前端可传 models 数组；兼容只传单个 model
    raw_models = payload.get("models")
    if isinstance(raw_models, list):
        models = [str(m).strip() for m in raw_models if str(m).strip()]
    else:
        models = []
    single = (payload.get("model") or "").strip()
    if single and single not in models:
        models = [single] + models
    # 去重保序
    seen = set()
    models = [m for m in models if not (m in seen or seen.add(m))]
    rec = {
        "id": pid,
        "name": (payload.get("name") or pid).strip(),
        "base_url": (payload.get("base_url") or "").strip(),
        "model": single or (models[0] if models else ""),
        "models": models,
        "wire_api": (payload.get("wire_api") or "responses").strip(),
        "env_key": (payload.get("env_key") or "").strip(),
        "storage_mode": payload.get("storage_mode") or "inline",
        "builtin": False,
    }
    if not rec["base_url"]:
        return False, "base_url 不能为空"
    if not models:
        return False, "至少填写一个 model"
    if not rec["model"]:
        rec["model"] = models[0]
    if rec["storage_mode"] == "env" and not rec["env_key"]:
        return False, "环境变量模式需要填写环境变量名。"

    lines = read_config_lines()
    _, active_provider = read_active(lines)

    block_input = dict(rec)
    block_input["api_key"] = (payload.get("api_key") or "").strip()
    # inline 模式但没传新 key：尝试保留 config 里已有的内联 key；都没有则拒绝
    if block_input["storage_mode"] == "inline" and not block_input["api_key"]:
        existing = read_provider_block(lines, pid)
        if existing and existing.get("experimental_bearer_token"):
            block_input["api_key"] = existing["experimental_bearer_token"]
        else:
            return False, "内联模式首次保存需要填写 API Key；或改用环境变量模式。"

    # 更新 presets.json（同 id 覆盖）
    presets = load_presets()
    found = False
    for i, x in enumerate(presets["providers"]):
        if x.get("id") == pid:
            presets["providers"][i] = rec
            found = True
            break
    if not found:
        presets["providers"].append(rec)
    save_presets(presets)

    # 写 config.toml 的 provider 块
    lines = upsert_provider_block(lines, block_input)
    # 若编辑的是当前激活 provider，同步根级 model
    if active_provider == pid:
        lines = set_root_key(lines, "model", rec["model"])
    backup = write_config_lines(lines)
    return True, backup


def do_delete_provider(pid):
    pid = (pid or "").strip()
    if not pid:
        return False, "未指定要删除的 provider"
    if pid in RESERVED_IDS:
        return False, "不能删除内置 provider「%s」" % pid
    presets = load_presets()
    in_presets = any(x.get("id") == pid for x in presets["providers"])
    lines = read_config_lines()
    in_config = pid in list_config_providers(lines)
    if not in_presets and not in_config:
        return False, "provider「%s」不存在" % pid

    presets["providers"] = [x for x in presets["providers"] if x.get("id") != pid]
    save_presets(presets)
    # 如果当前激活的是它，先回退到 openai
    _, active_provider = read_active(lines)
    if active_provider == pid:
        lines = set_root_key(lines, "model_provider", "openai")
        op = next((x for x in DEFAULT_PRESETS["providers"] if x["id"] == "openai"), None)
        lines = set_root_key(lines, "model", (op or {}).get("model", "gpt-5.5"))
    lines = remove_provider_block(lines, pid)
    backup = write_config_lines(lines)
    return True, backup


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------
INFO_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Codex 模型切换器</title></head>
<body style="font-family:-apple-system,sans-serif;background:#0f1115;color:#e6e8ec;padding:40px">
<h2>需要带 token 的链接</h2>
<p>为安全起见，本工具要求通过启动时<strong>终端打印的带 token 链接</strong>访问。</p>
<p>请回到终端，复制那条 <code>http://127.0.0.1:%d/?t=...</code> 链接打开。</p>
</body></html>""" % PORT

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Codex 模型切换器</title>
<style>
  :root{
    --bg:#0f1115; --card:#171a21; --card2:#1d2129; --line:#2a2f3a;
    --txt:#e6e8ec; --mut:#9aa3b2; --acc:#10a37f; --acc2:#0d8a6c;
    --warn:#3a2f12; --warnln:#6b551f; --warntx:#f0d28a;
    --bad:#b3402f;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif}
  .wrap{max-width:920px;margin:0 auto;padding:28px 20px 64px}
  h1{font-size:20px;margin:0 0 4px}
  .sub{color:var(--mut);font-size:12.5px;margin-bottom:18px}
  .path{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;color:var(--mut)}
  .active{background:var(--card);border:1px solid var(--line);border-radius:12px;
    padding:14px 16px;margin-bottom:16px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
  .active b{color:var(--acc)}
  .pill{display:inline-block;background:var(--card2);border:1px solid var(--line);
    border-radius:999px;padding:2px 10px;font-size:12px}
  .warn{background:var(--warn);border:1px solid var(--warnln);color:var(--warntx);
    border-radius:10px;padding:11px 14px;font-size:12.5px;margin-bottom:18px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  @media(max-width:680px){.grid{grid-template-columns:1fr}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 15px}
  .card.cur{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc) inset}
  .card h3{margin:0 0 2px;font-size:15px;display:flex;align-items:center;gap:8px}
  .card .meta{color:var(--mut);font-size:11.5px;word-break:break-all;margin:2px 0}
  .badge{font-size:10.5px;padding:1px 7px;border-radius:999px;border:1px solid var(--line)}
  .ok{color:#7ee0bf;border-color:#23694f;background:#10231c}
  .no{color:#e7b48a;border-color:#6b4a23;background:#241a10}
  .row{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
  button{font:inherit;border-radius:8px;border:1px solid var(--line);
    background:var(--card2);color:var(--txt);padding:7px 12px;cursor:pointer}
  button:hover{border-color:#3a4150}
  button.primary{background:var(--acc);border-color:var(--acc);color:#04130d;font-weight:600}
  button.primary:hover{background:var(--acc2)}
  button.danger{color:#ffb4a6;border-color:#5a2a22}
  button:disabled{opacity:.5;cursor:not-allowed}
  .addbtn{margin:18px 0 0}
  dialog{background:var(--card);color:var(--txt);border:1px solid var(--line);
    border-radius:14px;padding:0;width:min(520px,92vw)}
  dialog::backdrop{background:rgba(0,0,0,.55)}
  .dlg-h{padding:16px 18px;border-bottom:1px solid var(--line);font-size:16px;font-weight:600}
  .dlg-b{padding:16px 18px;max-height:70vh;overflow:auto}
  .dlg-f{padding:14px 18px;border-top:1px solid var(--line);display:flex;justify-content:flex-end;gap:8px}
  label{display:block;font-size:12px;color:var(--mut);margin:12px 0 4px}
  input,select,textarea{width:100%;background:var(--card2);border:1px solid var(--line);
    color:var(--txt);border-radius:8px;padding:8px 10px;font:inherit}
  textarea{resize:vertical;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
  select.modelsel{width:auto;display:inline-block;padding:3px 6px;font-size:12px;
    max-width:60%;vertical-align:middle}
  .hint{font-size:11px;color:var(--mut);margin-top:4px}
  .toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);
    background:#11241d;border:1px solid #23694f;color:#aef0d6;padding:10px 16px;
    border-radius:10px;font-size:13px;opacity:0;transition:.25s;pointer-events:none;max-width:90vw}
  .toast.show{opacity:1}
  .toast.err{background:#2a1411;border-color:#5a2a22;color:#ffb4a6}
  a{color:var(--acc)}
</style>
</head>
<body>
<div class="wrap">
  <h1>Codex 模型切换器</h1>
  <div class="sub">改写 <span class="path" id="cfgpath"></span> 的默认模型 / provider。每次写入自动备份。</div>

  <div class="active" id="active"></div>

  <div class="warn">
    ⚠️ <b>关于能否连通</b>：DeepSeek / Kimi / GLM 是 Chat Completions 协议，而新版 Codex 只认
    <code>wire_api = "responses"</code>。直连能否成功取决于你的 Codex 版本。若切换后报协议/格式错误，
    说明需要一层翻译代理（LiteLLM / OpenRouter / CLIProxyAPI）：把该 provider 的 <code>base_url</code>
    改成本地代理地址即可，本切换器照常使用。详见同目录 README。
  </div>

  <div class="grid" id="grid"></div>

  <button class="primary addbtn" onclick="openEdit(null)">＋ 添加自定义 provider</button>

  <p class="sub" style="margin-top:22px">
    切换后请在 Codex 桌面版<b>开一个新会话</b>（或重启 App）才会生效——已有会话的模型存在数据库里，不受 config 影响。
  </p>
</div>

<dialog id="dlg">
  <div class="dlg-h" id="dlgh">编辑 provider</div>
  <div class="dlg-b">
    <label>ID（写进 config 的 <code>[model_providers.&lt;id&gt;]</code>，仅字母数字）</label>
    <input id="f_id" placeholder="deepseek">
    <label>显示名称</label>
    <input id="f_name" placeholder="DeepSeek">
    <label>base_url（直连填官方端点；走代理填本地代理地址）</label>
    <input id="f_base" placeholder="https://api.deepseek.com/v1">
    <label>模型 models（每行一个，可填多个；第一行为默认）</label>
    <textarea id="f_models" rows="4" placeholder="deepseek-v4-pro&#10;deepseek-v4-flash"></textarea>
    <div class="hint">在卡片上可用下拉随时切换这些模型。</div>
    <label>wire_api</label>
    <select id="f_wire">
      <option value="responses">responses（新版唯一支持；直连或经兼容代理）</option>
      <option value="chat">chat（仅旧版 Codex 支持）</option>
    </select>
    <label>密钥存放方式</label>
    <select id="f_mode" onchange="onMode()">
      <option value="inline">内联到 config（桌面版 App 推荐，GUI 读不到 shell 环境变量）</option>
      <option value="env">环境变量（CLI 推荐，需自行 export）</option>
    </select>
    <div id="box_inline">
      <label>API Key（写入 <code>experimental_bearer_token</code>，明文存于 config.toml）</label>
      <input id="f_key" placeholder="sk-..." autocomplete="off">
      <div class="hint" id="key_hint">⚠️ 明文保存（文件权限 600）。<b>留空 = 保留已存的密钥</b>，不会显示原值。切勿分享 config.toml。</div>
    </div>
    <div id="box_env" style="display:none">
      <label>环境变量名</label>
      <input id="f_env" placeholder="DEEPSEEK_API_KEY">
      <div class="hint">CLI：mac/Linux <code>export 变量名=key</code>；Windows <code>set 变量名=key</code>。<br>桌面版 GUI 要读到它：mac 跑 <code>launchctl setenv 变量名 key</code>，Windows 跑 <code>setx 变量名 key</code>，之后重启 App。</div>
    </div>
  </div>
  <div class="dlg-f">
    <button onclick="closeDlg()">取消</button>
    <button class="primary" onclick="saveProvider()">保存</button>
  </div>
</dialog>

<div class="toast" id="toast"></div>

<script>
const TOKEN="__SWITCHER_TOKEN__";
let STATE=null;
function toast(msg,err){const t=document.getElementById('toast');t.textContent=msg;
  t.className='toast show'+(err?' err':'');setTimeout(()=>t.className='toast',2800);}
async function api(path,body){
  try{
    const r=await fetch(path,{method:body?'POST':'GET',
      headers:{'Content-Type':'application/json','X-Switcher-Token':TOKEN},
      body:body?JSON.stringify(body):undefined});
    if(r.status===403){return {ok:false,error:'会话已失效（多半是重启过切换器）。请关掉此标签页，回到终端用最新打印的带 ?t=... 的链接重新打开。'};}
    return await r.json();
  }catch(e){return {ok:false,error:'网络/服务错误（服务可能已停止）：'+e};}
}
async function load(){
  const s=await api('/api/state');
  if(!s||s.ok===false){toast((s&&s.error)||'读取状态失败，服务可能已停止',true);return;}
  STATE=s;render();
}
function render(){
  document.getElementById('cfgpath').textContent=STATE.config_path;
  const am=STATE.active_model||'(未设置)';
  document.getElementById('active').innerHTML=
    '当前 Provider：<span class="pill"><b>'+esc(STATE.active_provider)+'</b></span>'+
    '当前 Model：<span class="pill"><b>'+esc(am)+'</b></span>';
  const g=document.getElementById('grid');g.innerHTML='';
  for(const p of STATE.providers){
    const cur=p.id===STATE.active_provider;
    const badge=p.builtin?'<span class="badge ok">内置</span>'
      :(p.configured?'<span class="badge ok">已配置</span>':'<span class="badge no">未配置</span>');
    const div=document.createElement('div');
    div.className='card'+(cur?' cur':'');
    div.innerHTML=
      '<h3>'+esc(p.name)+' '+badge+(cur?' <span class="pill">使用中</span>':'')+'</h3>'+
      '<div class="meta">id: '+esc(p.id)+'</div>'+
      (p.base_url?'<div class="meta">base_url: '+esc(p.base_url)+'</div>':'');
    // model 行：多个模型用下拉，单个用文本
    const mrow=document.createElement('div');mrow.className='meta';
    let sel=null;
    const models=p.models&&p.models.length?p.models:(p.model?[p.model]:[]);
    if(models.length>1){
      mrow.appendChild(document.createTextNode('model: '));
      sel=document.createElement('select');sel.className='modelsel';
      for(const m of models){const o=document.createElement('option');o.value=m;o.textContent=m;
        if(m===p.model)o.selected=true;sel.appendChild(o);}
      mrow.appendChild(sel);
    }else{mrow.textContent='model: '+(p.model||'-');}
    div.appendChild(mrow);
    const row=document.createElement('div');row.className='row';div.appendChild(row);
    const label=cur?'应用所选模型':'切到这个';
    const bSwitch=mkbtn(label,'primary',()=>switchTo(p.id,sel?sel.value:null));
    row.appendChild(bSwitch);
    if(!p.builtin){
      row.appendChild(mkbtn('编辑','',()=>openEdit(p.id)));
      row.appendChild(mkbtn('删除','danger',()=>del(p.id)));
    }
    g.appendChild(div);
  }
}
function mkbtn(text,cls,fn){const b=document.createElement('button');
  b.textContent=text;if(cls)b.className=cls;b.addEventListener('click',fn);return b;}
function esc(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function switchTo(id,model){const r=await api('/api/switch',{id,model});
  if(r.ok){toast('已切到 '+id+(model?(' / '+model):'')+'（开新会话生效）');load();}else{toast(r.error||'失败',true);}}
async function del(id){if(!confirm('删除 provider「'+id+'」？会移除 config 里对应的块。'))return;
  const r=await api('/api/delete-provider',{id});if(r.ok){toast('已删除');load();}else{toast(r.error||'失败',true);}}
function onMode(){const m=document.getElementById('f_mode').value;
  document.getElementById('box_inline').style.display=m==='inline'?'':'none';
  document.getElementById('box_env').style.display=m==='env'?'':'none';}
function openDlg(){const d=document.getElementById('dlg');
  if(d.showModal)d.showModal();else{d.setAttribute('open','');d.style.display='block';}}
function closeDlg(){const d=document.getElementById('dlg');
  if(d.close)try{d.close();}catch(e){}d.removeAttribute('open');d.style.display='';}
function openEdit(id){
  const p=id?STATE.providers.find(x=>x.id===id):null;
  document.getElementById('dlgh').textContent=p?('编辑：'+p.name):'添加自定义 provider';
  document.getElementById('f_id').value=p?p.id:'';
  document.getElementById('f_id').readOnly=!!p;
  document.getElementById('f_name').value=p?p.name:'';
  document.getElementById('f_base').value=p?p.base_url:'';
  const models=p?(p.models&&p.models.length?p.models:(p.model?[p.model]:[])):[];
  document.getElementById('f_models').value=models.join('\n');
  document.getElementById('f_wire').value=p?(p.wire_api||'responses'):'responses';
  document.getElementById('f_mode').value=p?(p.storage_mode||'inline'):'inline';
  document.getElementById('f_key').value='';
  document.getElementById('f_env').value=p?(p.env_key||''):'';
  document.getElementById('key_hint').innerHTML=(p&&p.has_key)
    ?'已存有密钥：<b>留空 = 保留原密钥</b>；要更换才填新值。明文存于 config（权限 600）。'
    :'⚠️ 明文保存（文件权限 600）。首次需填入 API Key。切勿分享 config.toml。';
  onMode();
  openDlg();
}
async function saveProvider(){
  const models=document.getElementById('f_models').value.split('\n').map(s=>s.trim()).filter(Boolean);
  const body={
    id:document.getElementById('f_id').value.trim(),
    name:document.getElementById('f_name').value.trim(),
    base_url:document.getElementById('f_base').value.trim(),
    models:models,
    model:models[0]||'',
    wire_api:document.getElementById('f_wire').value,
    storage_mode:document.getElementById('f_mode').value,
    api_key:document.getElementById('f_key').value.trim(),
    env_key:document.getElementById('f_env').value.trim(),
  };
  const r=await api('/api/save-provider',body);
  if(r.ok){closeDlg();toast('已保存');load();}
  else{toast(r.error||'保存失败',true);}
}
load();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静音访问日志

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端断开，忽略

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return {}
        if n <= 0:
            return {}
        try:
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        allowed = (
            "127.0.0.1:%d" % PORT, "localhost:%d" % PORT,
            "127.0.0.1", "localhost",
        )
        return host in allowed

    def _guard_api(self):
        """所有 /api/* 接口：校验 Host + token。"""
        if not self._host_ok():
            self._json(403, {"ok": False, "error": "forbidden host"})
            return False
        if (self.headers.get("X-Switcher-Token") or "") != TOKEN:
            self._json(403, {"ok": False, "error": "forbidden: bad or missing token"})
            return False
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            if not self._host_ok():
                self._send(403, "forbidden host", "text/plain; charset=utf-8")
                return
            tok = (parse_qs(parsed.query).get("t") or [""])[0]
            if tok != TOKEN:
                self._send(403, INFO_HTML, "text/html; charset=utf-8")
                return
            self._send(200, INDEX_HTML.replace("__SWITCHER_TOKEN__", TOKEN),
                       "text/html; charset=utf-8")
        elif path == "/api/state":
            if not self._guard_api():
                return
            try:
                with _LOCK:
                    state = get_state()
            except Exception as e:
                self._json(200, {"ok": False, "error": "读取状态失败: %s" % e})
                return
            self._json(200, state)
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._guard_api():
            return
        try:
            body = self._read_body()
            with _LOCK:
                if path == "/api/switch":
                    ok, info = do_switch(body.get("id"), body.get("model"))
                elif path == "/api/save-provider":
                    ok, info = do_save_provider(body)
                elif path == "/api/delete-provider":
                    ok, info = do_delete_provider(body.get("id"))
                else:
                    self._json(404, {"ok": False, "error": "not found"})
                    return
        except Exception as e:
            self._json(200, {"ok": False, "error": "服务器异常: %s" % e})
            return
        if ok:
            self._json(200, {"ok": True, "backup": info})
        else:
            self._json(200, {"ok": False, "error": info})


def main():
    global TOKEN
    if not os.path.isdir(CODEX_HOME):
        print("找不到 Codex 目录：%s" % CODEX_HOME)
        print("如果 Codex 装在别处，请先设置环境变量 CODEX_HOME 指向它：")
        print("  macOS/Linux:  export CODEX_HOME=/path/to/.codex")
        print('  Windows:      set CODEX_HOME=C:\\path\\to\\.codex')
        return
    load_presets()  # 确保预设文件存在
    TOKEN = secrets.token_urlsafe(18)
    url = "http://%s:%d/?t=%s" % (HOST, PORT, TOKEN)
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print("端口 %d 无法启动：%s" % (PORT, e))
        print("可能已经有一个切换器在运行；或先关掉占用该端口的程序后重试。")
        print("（也可用环境变量换端口：SWITCHER_PORT=8790 python3 switcher.py）")
        return
    print("=" * 60)
    print("  Codex 模型切换器已启动")
    print("  config : %s" % CONFIG_PATH)
    print("  请用下面这条带 token 的链接打开（已尝试自动打开）：")
    print("    %s" % url)
    print("  按 Ctrl+C 退出")
    print("=" * 60)
    sys.stdout.flush()
    if not os.environ.get("SWITCHER_NO_BROWSER"):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
