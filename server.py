#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex 模型切换器 —— MCP server.

设计原则：**不重复造轮子**。
切换 / 写入 config.toml 的核心逻辑完全复用现成项目 **codex-token**
的 switcher.py（MIT，已 vendor 进本插件的 vendor/ 目录），
本文件只做一层 MCP 包装，外加几项它没有的能力：
  - 自动发现 provider 真实在售的模型列表
  - 探测厂商协议兼容性 + 真发请求实测连通性
  - 管理桌面版模型目录的可见性（自选哪些模型留在选择器里、清理下架模型）
  - 修正 model 与 model_provider 的错配
  - 按模型真实档位设置推理等级与上下文长度

为什么不走 switcher 自带的 HTTP API？
  它每次启动生成一次性 token、需要常驻子进程与端口，跨进程传 token 容易出岔子。
  而 switcher 的业务函数（get_state / do_switch / do_save_provider / do_delete_provider）
  是**自足的纯函数**，直接 import 调用更稳、更快、零端口占用。

环境变量：
  CODEX_SWITCHER_HOME  switcher.py 所在目录
                       （默认按 vendor/ → 同级 codex-token/ → ~/codex-token/ 依次探测）
  CODEX_HOME           Codex 配置目录（默认 ~/.codex）
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request

import platform_compat

def _find_switcher_home():
    """定位 codex-token 的 switcher.py。

    优先级：
      1) 环境变量 CODEX_SWITCHER_HOME
      2) 插件同级目录的 vendor/（开源时把 codex-token 放这里）
      3) 插件上级目录下的 codex-token/
      4) 用户主目录下的常见位置
    """
    env = os.environ.get("CODEX_SWITCHER_HOME")
    if env and os.path.isfile(os.path.join(env, "switcher.py")):
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.path.join(here, "vendor"),
        os.path.join(os.path.dirname(here), "codex-token"),
        os.path.join(os.path.dirname(os.path.dirname(here)), "codex-token"),
        os.path.join(os.path.expanduser("~"), "codex-token"),
    ]
    for c in cands:
        if os.path.isfile(os.path.join(c, "switcher.py")):
            return c
    return env or ""


SWITCHER_HOME = _find_switcher_home()
# switcher.py 在 import 时读取这两个环境变量，必须在 import 之前设好
os.environ["CODEX_HOME"] = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
os.environ["CODEX_SWITCHER_HOME"] = SWITCHER_HOME

if SWITCHER_HOME not in sys.path:
    sys.path.insert(0, SWITCHER_HOME)

try:
    import switcher  # noqa: E402  (必须在设好环境变量之后)
except ImportError as e:
    raise SystemExit(
        "无法导入 switcher（%s）。\n"
        "请确认 %s 下有 switcher.py，或设置环境变量 CODEX_SWITCHER_HOME。" % (e, SWITCHER_HOME)
    )

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _call(fn, *args):
    """调用 switcher 的业务函数，统一成 (ok, message)。"""
    ok, info = fn(*args)
    return bool(ok), info


# ---------------------------------------------------------------------------
# MCP 工具
# ---------------------------------------------------------------------------

def list_providers() -> str:
    """列出 Codex 当前已配置的 model provider、每个可选的模型，以及当前生效的默认模型。"""
    state = switcher.get_state()
    lines = [
        "当前默认：model=%s  provider=%s" % (state.get("active_model"), state.get("active_provider")),
        "配置文件：%s" % state.get("config_path"),
        "预设文件：%s" % state.get("presets_path"),
        "",
    ]
    for p in state.get("providers", []):
        mark = "  <== 当前生效" if p.get("id") == state.get("active_provider") else ""
        lines.append("[%s] %s%s" % (p.get("id"), p.get("name"), mark))
        lines.append("    base_url : %s" % (p.get("base_url") or "(内置 OpenAI)"))
        lines.append("    model    : %s" % p.get("model"))
        models = p.get("models") or []
        if models:
            lines.append("    可选模型 : %s" % ", ".join(models))
        lines.append("    密钥     : env_key=%s  storage_mode=%s  已配置=%s"
                     % (p.get("env_key") or "-", p.get("storage_mode"), p.get("has_key")))
    return "\n".join(lines)


def switch_model(provider_id: str, model: str = "") -> str:
    """切换 Codex 的默认模型提供方与模型。

    provider_id 用 list_providers 返回的 id（openai / deepseek / kimi / glm / 自定义）。
    model 留空则用该 provider 的默认模型。
    切换后需要**新开 Codex 会话或重启桌面版**才生效（已有会话的模型存在数据库里）。
    """
    ok, info = _call(switcher.do_switch, provider_id, model or None)
    if not ok:
        raise RuntimeError(info)
    return "已切到 %s%s。config.toml 备份：%s\n注意：需新开 Codex 会话或重启桌面版才生效。" % (
        provider_id, (" / " + model) if model else "", info)


def add_provider(provider_id: str, name: str, base_url: str, models: str,
                 api_key: str = "", env_key: str = "",
                 wire_api: str = "responses",
                 storage_mode: str = "inline") -> str:
    """新增或更新一个自定义 model provider 并接入 API key。

    models 用换行或逗号分隔，第一个为默认模型。
    wire_api 目前 Codex 只支持 "responses"（旧的 "chat" 已被 Codex 0.163 移除）。
    storage_mode="inline" 把 key 明文写进 config.toml（桌面版推荐）；
    storage_mode="env" 则只写 env_key，由环境变量提供密钥（CLI 推荐）。
    """
    model_list = [m.strip() for m in re.split(r"[\r\n,]+", models) if m.strip()]
    if not model_list:
        raise RuntimeError("至少要填一个模型名")
    payload = {
        "id": provider_id,
        "name": name or provider_id,
        "base_url": base_url,
        "model": model_list[0],
        "models": model_list,
        "wire_api": wire_api,
        "env_key": env_key or "",
        "storage_mode": storage_mode or "inline",
        "api_key": api_key or "",
    }
    ok, info = _call(switcher.do_save_provider, payload)
    if not ok:
        raise RuntimeError(info)
    out = ["已保存 provider '%s'（模型：%s）。config.toml 备份：%s" % (
        provider_id, ", ".join(model_list), info)]
    # ⚠️ 规则（2026-10-22）：加厂商前先查官网下线公告。
    # 自定义 provider 的公告页没法自动猜，所以只提示不阻断。
    if provider_id in PRESETS:
        try:
            out.append(check_provider_deprecations(provider_id, ",".join(model_list)))
        except Exception as e:
            out.append("（查官网下线公告失败：%s）" % e)
    else:
        out.append("提示：自定义 provider 无法自动查公告页，"
                   "请手动去官网「模型下线 / 更新公告」确认这几个模型名还在售。")
    return "\n".join(out)


def delete_provider(provider_id: str) -> str:
    """删除一个自定义 provider（内置 openai / ollama / lmstudio 不可删）。"""
    ok, info = _call(switcher.do_delete_provider, provider_id)
    if not ok:
        raise RuntimeError(info)
    return "已删除 provider '%s'。config.toml 备份：%s" % (provider_id, info)


# ---------------------------------------------------------------------------
# 一键接入：预设 provider + API key 全部走这里
# ---------------------------------------------------------------------------

# 预设里的 models 只是**兜底**——真正可用的模型以厂商 /v1/models 为准
# （setup_preset 会自动拉取并登记；下架的模型会被自动隐藏）。
#
# needs_proxy=True 表示该厂商**只提供 Chat Completions 协议**，
# 而新版 Codex（0.163+）只认 wire_api="responses"（旧的 "chat" 已被移除），
# 所以直连会失败，必须把 base_url 指向一层 Responses 兼容代理
# （LiteLLM / CLIProxyAPI / OpenRouter 等）。用 check_provider_protocol 现场确认。
PRESETS = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "env_key": "DEEPSEEK_API_KEY",
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        # 官方 Models & Pricing 页会写明哪些模型已退役（legacy 名 retired）
        "deprecation_docs": ["https://api-docs.deepseek.com/quick_start/pricing"],
        "model_pattern": r"deepseek-[a-z0-9.\-]+",
        "needs_proxy": False,
    },
    "minimax": {
        "name": "MiniMax",
        "base_url": "https://api.minimaxi.com/v1",
        "env_key": "MINIMAX_API_KEY",
        # ⚠️ 只留官网「当前在售」的两个模型。
        # M2 / M2.1 / M2.5 / M2.7 全系列已被官网标记「已下线」
        # （2026-10-25 用 check_provider_deprecations 实测确认），
        # 但 MiniMax 的 /v1/models **仍然返回这 9 个 id**——
        # 「还能调到」不等于「还在售」，别再写进来。
        "models": ["MiniMax-M3.1-Flash", "MiniMax-M3"],
        # 官方"模型列表"页：分"可用模型"和"历史模型"两节，还带下线通知
        "lineup_docs": ["https://platform.minimaxi.com/docs/faq/models.md"],
        "deprecation_docs": ["https://platform.minimaxi.com/docs/faq/models.md"],
        "model_pattern": r"MiniMax-[A-Za-z0-9.\-]+",
        "needs_proxy": False,
    },
    "kimi": {
        "name": "Kimi (Moonshot)",
        "base_url": "https://api.moonshot.cn/v1",
        "env_key": "MOONSHOT_API_KEY",
        # kimi-k3 是 2026-07 发布的旗舰（1M 上下文，reasoning_effort: low/high/max）；
        # kimi-k2.5 已停止对新用户开放且将于 2026-08-31 下线，故不列入。
        "models": ["kimi-k3", "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"],
        # 官方"模型列表"页：分"多模态模型"和"已下线模型"两节
        "lineup_docs": ["https://platform.kimi.com/docs/models.md"],
        "deprecation_docs": ["https://platform.kimi.com/docs/models.md"],
        "model_pattern": r"(?:kimi|moonshot)-[a-z0-9.\-]+",
        "needs_proxy": True,
    },
    "glm": {
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "env_key": "ZHIPUAI_API_KEY",
        "models": ["glm-4.7", "glm-4.7-flash", "glm-5", "glm-4.6"],
        "deprecation_docs": ["https://docs.bigmodel.cn/cn/guide/start/model-overview"],
        "lineup_docs": ["https://docs.bigmodel.cn/cn/guide/start/model-overview"],
        "model_pattern": r"glm-[0-9][a-z0-9.\-]*",
        "needs_proxy": True,
    },
    "mimo": {
        "name": "小米 MiMo",
        "base_url": "https://api.xiaomimimo.com/v1",
        "env_key": "MIMO_API_KEY",
        # 官方 Codex 配置指南：https://mimo.mi.com/docs/zh-CN/tokenplan/integration/codex-configuration
        # mimo-v2.5 / mimo-v2.5-pro 已于北京时间 2026-10-21 10:00 正式下线，不要再写进来。
        "models": ["mimo-v2.6-pro", "mimo-v2.6-flash", "mimo-v2.6-pro-ultraspeed"],
        # 语音模型在 /v1/models 里也在售，但不是对话模型，别登记进 Codex 模型目录
        "catalog_exclude": ["mimo-v2.5-asr", "mimo-v2.5-tts",
                            "mimo-v2.5-tts-voiceclone", "mimo-v2.5-tts-voicedesign"],
        # 官方下线公告页 + 当前在售模型页
        "deprecation_docs": ["https://mimo.mi.com/static/docs/updates/deprecate.md",
                             "https://mimo.mi.com/static/docs/quick-start/summary/model.md"],
        "lineup_docs": ["https://mimo.mi.com/static/docs/quick-start/summary/model.md"],
        "model_pattern": r"mimo-v[0-9][A-Za-z0-9.\-]*",
        # MiMo 的 /v1/models 不返回 ctx / 档位，官方目录里的值才是准的
        # （https://example-files.cnbj1.mi-fds.com/example-files/configs/model-catalogs.json）
        "model_defaults": {
            "context_window": 1048576,
            "max_output_tokens": 131072,
            "effort_levels": "none,low,medium,high",
            "default_effort": "low",
            "extra_fields": {
                # MiMo 的自定义工具（apply_patch）必须走 freeform Responses lite 模式，
                # 否则报 "custom tools require MiMo freeform Responses lite mode"
                "use_responses_lite": True,
                "apply_patch_tool_type": "freeform",
                "input_modalities": ["text", "image"],
                "supports_parallel_tool_calls": False,
                # 这几个是 OpenAI 专有字段，MiMo 不认，去掉免得多传参数
                "service_tiers": None,
                "additional_speed_tiers": None,
                "supports_search_tool": None,
                "web_search_tool_type": None,
            },
        },
        "needs_proxy": False,
    },
    "siliconflow": {
        "name": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "env_key": "SILICONFLOW_API_KEY",
        # 实测该站 /v1/responses 返回 404，只有 /v1/chat/completions —— 直连不通。
        "models": ["deepseek-ai/DeepSeek-V4-Pro", "deepseek-ai/DeepSeek-V4-Flash",
                   "zai-org/GLM-5", "Qwen/Qwen3-Coder-480B-A35B-Instruct"],
        "needs_proxy": True,
    },
    "dashscope": {
        "name": "阿里云百炼 DashScope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "env_key": "DASHSCOPE_API_KEY",
        # ⚠️ 不列 kimi-k2.5 / MiniMax-M2.5：这两家官方已把同名模型下线
        #    （百炼是聚合平台，可能还有自己的版本，但同名模型极易踩坑）。
        "models": ["qwen3-max", "qwen3-coder-plus", "deepseek-v3.2", "glm-4.7"],
        "needs_proxy": False,
    },
    "openrouter": {
        "name": "OpenRouter（聚合，400+ 模型）",
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        # ⚠️ 模型名必须按 /v1/models 的**真实 id** 填（2026-10-25 实测 467 个）：
        #    · "minimax/minimax-m3.1-flash:free" **不存在**（无 :free 变体）
        #    · "moonshotai/kimi-k2.5" Moonshot 官方已下线，换成 kimi-k3
        "models": ["minimax/minimax-m3.1-flash", "deepseek/deepseek-v4.1-flash",
                   "qwen/qwen3.8-flash", "z-ai/glm-5.3-flash", "moonshotai/kimi-k3"],
        "needs_proxy": False,
    },
}


def set_api_key(env_key_name: str, api_key: str,
                storage_mode: str = "inline", provider_id: str = "") -> str:
    """把 API key 写入 provider（默认**内联到 config.toml**）。

    storage_mode:
      "inline" —— key 明文写进 config.toml 的 experimental_bearer_token
                   **桌面版唯一可靠的方式**（推荐默认）。
      "env"    —— key 写入用户级环境变量（Windows setx / macOS launchctl setenv /
                   Linux ~/.config/environment.d），provider 用 env_key 引用。
                   ⚠️ **桌面版读不到**：MSIX 应用由 shell 激活，继承的是
                   explorer.exe 的环境快照；explorer 登录后不再刷新，
                   setx 写入的变量不会传进已运行的桌面版进程。
                   实测表现：请求报 "Missing environment variable: XXX"。

    ⚠️ 关于安全性：inline 会把 key 明文写进 config.toml（可能被分享/提交 git）。
       若要兼顾，可两种都写：inline 保证桌面版可用，env 供 CLI 使用。
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_key_name):
        raise RuntimeError("环境变量名只能含字母数字下划线，且不能以数字开头")
    if not api_key.strip():
        raise RuntimeError("api_key 不能为空")
    key = api_key.strip()
    mode = (storage_mode or "inline").strip().lower()
    if mode not in ("inline", "env"):
        raise RuntimeError("storage_mode 只能是 inline 或 env")

    lines = []
    if mode == "env":
        ok, msg = platform_compat.set_user_env(env_key_name, key)
        if not ok:
            raise RuntimeError("写用户级环境变量失败：%s" % msg)
        lines.append("%s（长度 %d）" % (msg, len(key)))

    if provider_id:
        # ⚠️ 不能传 model/models：do_save_provider 会用 payload 覆盖该 provider 的
        #    默认模型与模型列表。只想改 key 时传空，保留 presets 里原有的值
        #    （否则会把当前模型（如 MiniMax）写进 deepseek 的 preset）。
        existing = None
        for pr in _presets_providers():
            if pr.get("id") == provider_id:
                existing = pr
                break
        payload = {
            "id": provider_id,
            "name": (existing or {}).get("name") or provider_id,
            "base_url": (existing or {}).get("base_url") or "",
            "model": "",
            "models": (existing or {}).get("models") or [],
            "wire_api": (existing or {}).get("wire_api") or "responses",
            "env_key": env_key_name if mode == "env" else "",
            "storage_mode": mode,
            "api_key": key if mode == "inline" else "",
        }
        if not payload["base_url"] or not payload["models"]:
            raise RuntimeError(
                "预设里找不到 provider '%s' 的 base_url / models，"
                "请先用 add_provider 或 setup_preset 配好它。" % provider_id)
        ok, info = _call(switcher.do_save_provider, payload)
        if not ok:
            raise RuntimeError(info)
        lines.append("已更新 provider '%s'（storage_mode=%s，保留其原默认模型 %s）。"
                     "config.toml 备份：%s"
                     % (provider_id, mode, payload["model"] or "?", info))
    else:
        lines.append("（未指定 provider_id，只写了环境变量；"
                     "要把 key 落到 config.toml 请用 setup_preset 或 add_provider）")

    lines.append("注意：**新开的进程**才读得到环境变量，Codex 桌面版需完全退出重开。")
    lines.append("（不回显 key 明文，只给长度 %d）" % len(key))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 下线/弃用公告核查（接任何厂商之前的必做步骤）
# ---------------------------------------------------------------------------

# 厂商公告里出现过的"下线/弃用"措辞（中英文都可能）
_DEPRECATION_MARKERS = (
    "下线", "弃用", "停止", "不再", "已退役", "retire", "retired",
    "deprecat", "sunset", "offline", "end of life",
)

# 章节标题里出现这些词 -> 该章节列的是**已退役**的模型，不是在售清单
_RETIRED_HEADING = re.compile(
    r"(下线|已下线|历史|归档|停用|deprecat|retired)", re.I)

# 下线核查的判定状态。
# ⚠️ 只有 DEAD_STATES 里的状态才代表「确实下线了」，可以剔除。
#    UNKNOWN_STATE 表示**查不到**（没配官网在售页 + /v1/models 取不到），
#    查不到 ≠ 下线，绝不能据此剔除模型。
DEAD_STATES = ("已下线", "已公告下线", "已下架")
UNKNOWN_STATE = "无法判定"

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _fetch_page_text(url, timeout=25):
    """抓一个网页/文档，返回 (可见正文, 含脚本的全文)。

    可见正文用来读公告；含脚本的全文用来做"在售清单"匹配 ——
    有的文档站（Moonshot/Kimi、GLM）把模型名放在 JS 数据块里，
    只看可见文字一个都匹配不上。
    """
    req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
    with _opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
    text = None
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", "replace")
    # HTML 标题 -> markdown 标题，这样 HTML 页和 .md 页都能按"章节"切
    text = re.sub(r"<h([1-6])[^>]*>(.*?)</h\1>",
                  lambda m: "\n\n" + "#" * int(m.group(1)) + " " + m.group(2) + "\n\n",
                  text, flags=re.S | re.I)
    full = re.sub(r"<[^>]+>", " ", text)
    full = re.sub(r"[ \t]+", " ", full)
    visible = re.sub(r"<script[^>]*>.*?</script>", " ", full, flags=re.S)
    visible = re.sub(r"<style[^>]*>.*?</style>", " ", visible, flags=re.S)
    return visible, full


def _split_sections(text):
    """按 markdown 标题切段，返回 [(标题, 正文)]；第一段前言标题为 ''。"""
    sections, title, cur = [], "", []
    for line in (text or "").split("\n"):
        if re.match(r"^#{1,6}\s+", line):
            sections.append((title, "\n".join(cur)))
            title = line.lstrip("#").strip()
            cur = []
        else:
            cur.append(line)
    sections.append((title, "\n".join(cur)))
    return sections


def _model_ids(text, pattern):
    return set(x.rstrip(".") for x in re.findall(pattern, text or ""))


def _deprecation_sentences(visible):
    """正文里带"下线/弃用"措辞的句子（公告的原文，报告里要引用）。"""
    out = []
    for sent in re.split(r"[。\n;；]", visible or ""):
        s = re.sub(r"\s+", " ", sent).strip()
        if 6 < len(s) < 400 and any(m in s.lower() for m in _DEPRECATION_MARKERS):
            out.append(s)
    return out


def _dated_notices(notices):
    """公告里带日期的句子——那才是"什么时候下线"的关键信息。"""
    out = []
    for s in notices:
        if re.search(r"20\d{2}\s*[-年./]\s*\d{1,2}|20\d{2}\s*年", s):
            out.append(s)
    return out


def _provider_model_candidates(provider_id, extra=""):
    """该 provider 名下"用户可能用到"的模型名全集。"""
    cands = []

    def add(x):
        x = (x or "").strip()
        if x and x not in cands:
            cands.append(x)

    for m in re.split(r"[\r\n,]+", extra or ""):
        add(m)
    p = PRESETS.get(provider_id) or {}
    for m in p.get("models") or []:
        add(m)
    for pr in _presets_providers():
        if pr.get("id") == provider_id:
            for m in pr.get("models") or []:
                add(m)
    return cands


def _scan_provider_deprecations(provider_id, base_url, key, candidates):
    """核心扫描。返回 (状态字典, 在售列表, 页面信息)。"""
    p = PRESETS.get(provider_id) or {}
    excluded = set(p.get("catalog_exclude") or [])
    pattern = p.get("model_pattern") or r"[A-Za-z][A-Za-z0-9.\-]{2,40}"

    live_ids = []
    if base_url and key:
        try:
            live = _live_models(base_url, key)
            live_ids = [it.get("id") for it in live
                        if isinstance(it, dict) and it.get("id")]
        except Exception:
            live_ids = []

    lineup_ids, retired_ids = set(), set()
    lineup_urls, dep_urls, notices = [], [], []

    # 1) 官网"在售模型列表"页：它通常分成"在售章节"和"已下线/历史章节"
    for u in (p.get("lineup_docs") or []):
        try:
            visible, full = _fetch_page_text(u)
        except Exception:
            continue
        lineup_urls.append(u)
        notices.extend(_deprecation_sentences(visible))
        for title, body in _split_sections(full):
            ids = _model_ids(body, pattern)
            if not title:
                continue          # 前言（可能是下线通知），不当成在售清单
            if _RETIRED_HEADING.search(title):
                retired_ids |= ids
            else:
                lineup_ids |= ids

    # 2) 官网下线公告页：只有"退役章节"里的模型才算被公告点名
    for u in (p.get("deprecation_docs") or []):
        try:
            visible, full = _fetch_page_text(u)
        except Exception:
            continue
        dep_urls.append(u)
        notices.extend(_deprecation_sentences(visible))
        for title, body in _split_sections(full):
            if not title or _RETIRED_HEADING.search(title):
                retired_ids |= _model_ids(body, pattern)

    # 在售清单 = 官网在售章节 ∩ /v1/models；没配在售页就以 /v1/models 为准
    if lineup_ids and live_ids:
        current = lineup_ids & set(live_ids)
    else:
        current = lineup_ids or set(live_ids)

    out = {}
    dated = _dated_notices(notices)
    for m in candidates:
        if m in excluded:
            continue  # 语音/非对话模型，不参与 Codex 侧判定
        if m in current:
            out[m] = ("在售", "")
        elif m in retired_ids:
            out[m] = ("已下线", _retire_reason(m, notices, dated))
        elif live_ids and m in live_ids:
            out[m] = ("已公告下线", "还能调用，但厂商已把它移出在售清单：" +
                      _retire_reason(m, notices, dated))
        elif any(m.lower() in s.lower() for s in notices):
            out[m] = ("已公告下线", "官网公告里点名了它：" + _retire_reason(m, notices, dated))
        elif current:
            # 我们确实拿到了权威在售清单，而它不在里面 -> 可判定为已下架
            out[m] = ("已下架", "厂商在售清单里已没有它")
        else:
            # ⚠️ **没有任何权威来源**（/v1/models 取不到、官网没配在售页、公告也没点名）
            #    绝不能判定成「已下架」—— 否则会把好模型全误杀
            #    （2026-10-25 实测：dashscope / openrouter / moonshot 没配公告页，
            #      旧逻辑把它们的**全部**模型都误报成已下线，
            #      setup_preset 直接把 preset 模型剔光后抛错，厂商根本接不上）
            out[m] = ("无法判定", "没拿到厂商在售清单（/v1/models 取不到且未配官网在售页），"
                                "公告也没点名它 —— 请人工确认，不能当成已下线")
    return out, live_ids, {"lineup": lineup_urls, "dep": dep_urls,
                            "notices": notices, "retired": sorted(retired_ids)}


def _pick_notice(model_id, notices):
    """挑一句最能说明该模型下线的公告原文。"""
    pat = re.compile(r"(?<![\w.\-])" + re.escape(model_id.lower()) + r"(?![\w.\-])")
    for s in notices:
        if pat.search(s.lower()):
            return s
    return notices[0] if notices else ""


def _retire_reason(model_id, notices, dated):
    """退役原因 = 点名这个模型的公告原文；没有就带上最相关的一句日期公告。"""
    hit = _pick_notice(model_id, notices)
    if not hit and dated:
        hit = dated[0]
    if not hit and notices:
        hit = notices[0]
    return hit


def check_provider_deprecations(provider_id: str, models: str = "") -> str:
    """对照厂商**官网公告**查哪些模型已经被弃用/下架。

    为什么不能只看 /v1/models：
      /v1/models 只反映"现在还能不能调到"，反映不了"厂商已经宣布要下线"。
      官方公告一般给两个时间点：
        - 系统替换时间：老名字还会被自动路由到新模型，**按新模型计费**
        - 正式下线时间：老名字直接报错
      两条路都会让用户踩坑，所以接厂商前先查一遍。

    参数：
      provider_id  预设 id（deepseek/minimax/kimi/glm/mimo/...）或已配置的 provider id
      models       可选，逗号/换行分隔的模型名；留空则用
                   预设列表 ∪ presets.json 里该 provider 的列表 ∪ 厂商在售列表
    """
    provider_id = (provider_id or "").strip()
    if not provider_id:
        raise RuntimeError("provider_id 不能为空")
    p = PRESETS.get(provider_id) or {}
    base = p.get("base_url") or ""
    key = ""
    try:
        base2, key, _wire, _env = _provider_creds(provider_id)
        base = base or base2
    except Exception:
        pass
    cands = _provider_model_candidates(provider_id, models)
    status, live_ids, pages = _scan_provider_deprecations(
        provider_id, base, key, cands)
    if not status:
        return ("provider '%s' 没有可比对的模型名。"
                "（厂商在售 %d 个：%s）" % (provider_id, len(live_ids), ", ".join(live_ids[:20])))
    dead = [m for m, (st, _) in status.items() if st in DEAD_STATES]
    unknown = [m for m, (st, _) in status.items() if st == UNKNOWN_STATE]
    lines = ["provider '%s' 下线/弃用核查" % provider_id]
    lines.append("  厂商在售列表（/v1/models%s）：%s"
                 % ("，已取到" if live_ids else "，没 key 只能靠官网", 
                    ", ".join(live_ids[:20]) if live_ids else "(未取到)"))
    seen_urls = []
    for u in pages["lineup"] + pages["dep"]:
        if u not in seen_urls:
            seen_urls.append(u)
    lines.append("  查过的官网页：%s"
                 % (", ".join(seen_urls) or
                    "(该厂商没配公告页，只能人工去官网翻)"))
    retired = [m for m in pages["retired"] if m not in status]
    if retired:
        lines.append("  官网公告点名已退役的模型（全部）：%s" % ", ".join(retired))
    for m in sorted(status):
        st, why = status[m]
        lines.append("  - %-28s %s%s" % (m, st, ("  —— " + why) if why else ""))
    if dead:
        lines.append("建议从模型列表里去掉：%s" % ", ".join(sorted(dead)))
    else:
        lines.append("结论：名单里没有已弃用的模型。")
    if unknown:
        lines.append("⚠️ 无法判定（**不是**已下线）：%s" % ", ".join(sorted(unknown)))
        lines.append("   这些厂商没配官网在售页、/v1/models 也取不到，"
                     "查不到不等于下线 —— 请人工去官网确认后再决定。")
    return "\n".join(lines)


def setup_preset(preset: str, api_key: str, model: str = "", make_default: bool = True,
                 storage_mode: str = "inline") -> str:
    """一键接入某个模型提供方：写 API key -> 建 provider -> 设为默认 -> 登记到桌面版模型目录。

    preset 取值：deepseek / minimax / kimi / glm / mimo / siliconflow / moonshot / dashscope / openrouter
    api_key  是该家的 API key。
    model    可选，默认用该 preset 的首个模型；也可以先调 discover_models 拿真实列表再选。
    make_default=True 会同时把它切成 Codex 的默认模型。
    storage_mode 默认 "inline"（key 写进 config.toml）。
      ⚠️ 桌面版**读不到用户级环境变量**（MSIX 继承 explorer 的旧环境快照），
         用 "env" 会报 "Missing environment variable"。CLI 场景两种都行。

    接入时会自动拉该厂商 /v1/models 的**真实在售列表**来登记模型目录
    （预设里的模型名只是兜底，厂商上下架很频繁）；
    并把该 provider 下已被厂商下架的模型从模型选择器里隐藏。

    ⚠️ 接入前/接入时会**先查厂商官网的下线公告**（check_provider_deprecations）：
    /v1/models 只反映"现在还能调到"，公告才反映"厂商已经宣布要下线"——
    两级时间点（系统替换时间 / 正式下线时间）都会让用户踩坑。
    已下线的模型名不会被写进 provider 块，也不会被登记进模型目录。

    这一步做完，Codex 桌面版就能用这个模型了（需完全退出重开一次）。
    """
    key = (preset or "").strip().lower()
    if key not in PRESETS:
        raise RuntimeError("未知预设 '%s'，可用：%s" % (preset, ", ".join(PRESETS)))
    p = PRESETS[key]
    mode = (storage_mode or "inline").strip().lower()
    if mode not in ("inline", "env"):
        raise RuntimeError("storage_mode 只能是 inline 或 env")

    lines = []
    if mode == "env":
        lines.append(set_api_key(p["env_key"], api_key, storage_mode="env"))
    else:
        ok, msg = platform_compat.set_user_env(p["env_key"], api_key.strip())
        if ok:
            lines.append("%s（供 CLI 使用，长度 %d）"
                         % (msg, len(api_key.strip())))
        else:
            lines.append("（写用户级环境变量失败，仅写入 config.toml：%s）" % msg)

    models = [m.strip() for m in re.split(r"[\r\n,]+", model) if m.strip()] or list(p["models"])

    # ⚠️ 规则（2026-10-22 新增）：接厂商前先查**官网**的下线公告。
    # /v1/models 只说明"现在还能调到"，公告才说明"厂商已经宣布要下线"。
    # 已下线的模型名一旦写进 provider 块，桌面版模型选择器里就会多出一个
    # 点了必挂的选项（unknown model）。
    excluded = set(p.get("catalog_exclude") or [])
    dead = set()
    try:
        _status, _live0, _pages = _scan_provider_deprecations(
            key, p["base_url"], api_key.strip(), models)
        dead = {m for m, (st, _why) in _status.items() if st in DEAD_STATES}
        _unknown = {m for m, (st, _why) in _status.items() if st == UNKNOWN_STATE}
        _seen = []
        for _u in _pages["lineup"] + _pages["dep"]:
            if _u not in _seen:
                _seen.append(_u)
        lines.append("官网下线公告核查（%s）：%s"
                     % (", ".join(_seen) if _seen else "该厂商未配公告页",
                        ("发现已弃用 -> " + ", ".join(sorted(dead))) if dead
                        else "未发现已弃用模型"))
        if _unknown:
            lines.append("（%d 个模型**无法判定**（查不到在售清单≠已下线），保留不动：%s）"
                         % (len(_unknown), ", ".join(sorted(_unknown))))
    except Exception as e:
        lines.append("（查官网下线公告失败：%s）" % e)
    dropped = [m for m in models if m in dead or m in excluded]
    if dropped:
        models = [m for m in models if m not in dead and m not in excluded]
        lines.append("已剔除（已下线 / 不适合 Codex）：%s" % ", ".join(dropped))
    if not models:
        raise RuntimeError("预设里的模型全被判定为已下线或不适合 Codex，"
                           "请先查官网公告再挑模型。")

    payload = {
        "id": key,
        "name": p["name"],
        "base_url": p["base_url"],
        "model": models[0],
        "models": models,
        "wire_api": "responses",
        "env_key": p["env_key"] if mode == "env" else "",
        "storage_mode": mode,
        "api_key": api_key.strip() if mode == "inline" else "",
    }
    ok, info = _call(switcher.do_save_provider, payload)
    if not ok:
        raise RuntimeError(info)
    lines.append("已写入 provider 块 [%s]（storage_mode=%s，模型：%s）"
                 % (key, mode, ", ".join(models)))

    # 协议探测：Codex 只认 Responses 协议，这里提前把结论摆出来
    try:
        lines.append(check_provider_protocol(p["base_url"], provider_id=key))
    except Exception as e:
        lines.append("（协议探测失败：%s）" % e)

    # 厂商真实在售列表 -> 登记进桌面版模型目录。
    # 桌面版会把 UI 里选中的模型写回 config.toml 顶层 model，
    # 模型名不在目录里的话，它会写回别的模型 -> provider/model 组合非法 -> 请求必挂。
    # 用厂商 /v1/models 的真实列表（含真实 ctx / 档位），而不是预设里可能过时的名字。
    live = []
    try:
        live = _live_models(p["base_url"], api_key.strip())
    except Exception as e:
        lines.append("（拉取厂商在售列表失败：%s；改用预设里的模型列表）" % e)
    if live:
        live_ids = [it.get("id") for it in live
                    if isinstance(it, dict) and it.get("id")]
        merged = [m for m in dict.fromkeys(models + live_ids)
                  if m not in excluded and m not in dead]
        lines.append("厂商在售 %d 个模型：%s"
                     % (len(live_ids), ", ".join(live_ids[:12])
                        + (" ..." if len(live_ids) > 12 else "")))
        models = merged
        payload["model"] = models[0]
        payload["models"] = models
        ok, info = _call(switcher.do_save_provider, payload)
        if not ok:
            raise RuntimeError(info)

    try:
        cat_path = _catalog_path()
        md = p.get("model_defaults") or {}
        extra = md.get("extra_fields") or {}
        registered = 0
        for it in (live or [{"id": m} for m in models]):
            if not isinstance(it, dict):
                continue
            s = it.get("id")
            if not s or s in excluded or s in dead:
                continue
            eff = it.get("effort") or {}
            levels = (eff.get("supported_levels") or eff.get("supportedLevels")
                      or [x for x in (md.get("effort_levels") or "").split(",") if x])
            register_model_in_catalog(
                s, it.get("name") or s,
                context_window=int(it.get("context_window") or md.get("context_window") or 0),
                max_output_tokens=int(it.get("max_output_tokens") or md.get("max_output_tokens") or 0),
                effort_levels=",".join(levels),
                default_effort=eff.get("default_level") or md.get("default_effort") or "",
                catalog_path=cat_path,
                extra_fields=extra)
            registered += 1
        lines.append("已登记到桌面版模型目录：%s（%d 个模型）" % (cat_path, registered))
        # 顺带把该 provider 下已被厂商下架的模型从列表里隐藏掉
        try:
            lines.append(sync_provider_models(key, hide_unlisted=True,
                                              register_missing=False))
        except Exception as e:
            lines.append("（对齐在售列表失败：%s）" % e)
    except Exception as e:
        lines.append("（桌面版模型目录登记失败：%s；手动在托盘里切换一次模型即可）" % e)

    if p.get("needs_proxy"):
        lines.append("⚠️ 该厂商只提供 Chat Completions 协议，而新版 Codex 只认 "
                     "wire_api=\"responses\"（旧的 \"chat\" 已被移除）。")
        lines.append("   直连多半会失败——请把 base_url 指向一层 Responses 兼容代理"
                     "（LiteLLM / CLIProxyAPI / OpenRouter），或改用支持 Responses 的厂商。")

    if make_default:
        ok2, info2 = _call(switcher.do_switch, key, models[0])
        if ok2:
            lines.append("已设为默认：model=%s provider=%s" % (models[0], key))
        else:
            lines.append("设为默认失败：%s" % info2)

    lines.append("下一步：**完全退出并重开 Codex 桌面版**（环境变量要新进程才读得到），"
                 "然后在模型选择器里就能看到 %s。" % ", ".join(models))
    lines.append("接完记得跑一次 test_provider('%s', '%s') 确认真能发请求。"
                 % (key, models[0]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 厂商真实模型列表
# ---------------------------------------------------------------------------

def _api_base(base_url):
    """归一化 API base：**只在 base 没有路径段时才补 /v1**。

    ⚠️ 不能无脑补 /v1：智谱的 base 是 https://open.bigmodel.cn/api/paas/v4，
    补成 .../api/paas/v4/v1 会打到根本不存在的路径，
    而智谱的鉴权中间件对**任何**路径都先返回 401 ——
    于是看起来「端点存在」，实际是误判。
    """
    base = (base_url or "").rstrip("/")
    if not base:
        return base
    m = re.match(r"^(https?://[^/]+)(/.*)?$", base, re.I)
    if not m:
        return base
    host, path = m.group(1), m.group(2) or ""
    if path in ("", "/"):
        return host + "/v1"
    return base


def discover_models(base_url: str, api_key: str = "", env_key: str = "") -> str:
    """直连 provider 的 /v1/models 拉取**真实**在售模型列表（避免照抄过时预设）。

    api_key 留空时改从 env_key 指向的环境变量取值。
    返回每个模型的 id、显示名、上下文窗口、最大输出 token，
    以及**厂商声明的推理档位**（若 /v1/models 提供了 effort.supported_levels）。
    部分厂商（如 DeepSeek）会直接返回 effort.supported_levels，
    那是**最权威**的档位来源；没返回的厂商需要查其官方文档。
    """
    base = _api_base(base_url)
    key = api_key
    if not key and env_key:
        key = os.environ.get(env_key, "")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base + "/models", headers=headers)
    try:
        with _opener.open(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError("HTTP %s：%s\n（401 说明 key 无效或没带上 key）" % (e.code, body))
    except Exception as e:
        raise RuntimeError("请求失败：%s" % e)

    items = data.get("data") or []
    if not items:
        return "没有解析到模型列表，原始返回：%s" % json.dumps(data)[:300]
    lines = ["%s 可用模型（%d 个）：" % (base, len(items))]
    for it in items:
        mid = it.get("id")
        name = it.get("name") or ""
        cw = it.get("context_window") or ""
        mx = it.get("max_output_tokens") or ""
        # 厂商声明的真实推理档位
        eff = it.get("effort") or {}
        levels = eff.get("supported_levels") or eff.get("supportedLevels") or []
        dflt = eff.get("default_level") or eff.get("defaultLevel") or ""
        extra = ""
        if levels:
            extra = "  档位=%s%s" % ("/".join(levels), ("(默认 %s)" % dflt) if dflt else "")
        lines.append("  - %s%s  ctx=%s max_out=%s%s"
                     % (mid, ("  (%s)" % name) if name else "", cw, mx, extra))
    if not any((it.get("effort") or {}).get("supported_levels") for it in items):
        lines.append("（该厂商的 /v1/models 未声明推理档位，"
                     "档位需查其官方文档后用 register_model_in_catalog 登记）")
    return "\n".join(lines)


def _catalog_path():
    """从 config.toml 读 model_catalog_json 的路径。"""
    cfg_path = switcher.CONFIG_PATH
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    p = cfg.get("model_catalog_json")
    if not p:
        raise RuntimeError("config.toml 里没有 model_catalog_json 键，桌面版模型选择器将看不到自定义模型。")
    return p


def _load_catalog(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _save_catalog(path, data):
    """原子写 + 备份，UTF-8 无 BOM + LF。"""
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    text = json.dumps(data, ensure_ascii=False, indent=1)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def list_catalog_models(include_hidden: bool = False) -> str:
    """列出桌面版模型目录（model_catalog_json）里登记的模型。

    桌面版会把 UI 里选中的模型写回 config.toml 顶层 model；
    若该模型没登记在这个目录里，桌面版就会写回一个不属于当前 provider 的模型名，
    导致 provider/model 组合非法、请求必挂。所以接新模型时必须先登记。

    include_hidden=True 时把 visibility="hide" 的条目也列出来
    （已下架、或被你手动藏起来的）。
    """
    path = _catalog_path()
    cat = _load_catalog(path)
    models = cat.get("models") or []
    visible = [m for m in models
               if isinstance(m, dict) and (m.get("visibility") or "list") == "list"]
    hidden = [m for m in models
              if isinstance(m, dict) and (m.get("visibility") or "list") != "list"]
    lines = ["模型目录：%s（%d 条，在列表里 %d 条，已隐藏 %d 条）"
             % (path, len(models), len(visible), len(hidden))]
    for m in visible:
        mark = "  <== 默认可选" if m.get("priority", 0) and m["priority"] >= 99 else ""
        lines.append("  - %-34s %-22s ctx=%s%s" % (
            m.get("slug"), m.get("display_name"), m.get("context_window"), mark))
    if include_hidden and hidden:
        lines.append("")
        lines.append("--- 已隐藏（不在模型选择器里） ---")
        for m in hidden:
            lines.append("  - %-34s %-22s ctx=%s" % (
                m.get("slug"), m.get("display_name"), m.get("context_window")))
    if hidden and not include_hidden:
        lines.append("（另有 %d 条已隐藏，用 include_hidden=True 查看）" % len(hidden))
    return "\n".join(lines)


# 桌面版模型目录里**最小可用**的条目模板。
# 用途：新机器上的模型目录是空的（`{"models": []}`），
# 此时既没有 gpt-5.5 也没有任何可抄的条目 —— 登记模型会直接失败。
# 这个模板不含 OpenAI 专属的 model_messages / base_instructions，
# 让 Codex 用自己的默认指令，比硬塞第三方模型的系统提示更合适。
FALLBACK_TEMPLATE = {
    "slug": "model-switcher-template",
    "display_name": "Template",
    "description": "",
    "default_reasoning_level": "medium",
    "supported_reasoning_levels": [
        {"effort": "low", "description": "Fast responses with lighter reasoning"},
        {"effort": "medium", "description": "Balances speed and reasoning depth for everyday tasks"},
        {"effort": "high", "description": "Greater reasoning depth for complex problems"},
    ],
    "shell_type": "unified_exec",
    "visibility": "list",
    "supported_in_api": True,
    "priority": 100,
    "additional_speed_tiers": ["fast"],
    "service_tiers": [
        {"id": "priority", "name": "Fast", "description": "1.5x speed, increased usage"},
    ],
    "availability_nux": None,
    "upgrade": None,
    "include_skills_usage_instructions": True,
    "include_plugin_usage_instructions": True,
    "include_apps_usage_instructions": True,
    "default_reasoning_summary": "none",
    "support_verbosity": True,
    "default_verbosity": "low",
    "apply_patch_tool_type": "freeform",
    "web_search_tool_type": "text_and_image",
    "truncation_policy": {"mode": "tokens", "limit": 10000},
    "supports_image_detail_original": True,
    "context_window": 200000,
    "max_context_window": 200000,
    "effective_context_window_percent": 95,
    "experimental_supported_tools": [],
    "input_modalities": ["text"],
    "supports_search_tool": False,
    "supports_experimental_context": False,
    "use_responses_lite": False,
    "supports_reasoning_effort_updates": True,
    "node_repl_auto_review_required": False,
    "node_repl_disabled": False,
}


def register_model_in_catalog(slug: str, display_name: str = "",
                              context_window: int = 0,
                              max_output_tokens: int = 0,
                              effort_levels: str = "",
                              default_effort: str = "",
                              catalog_path: str = "",
                              extra_fields: dict = None) -> str:
    """把一个模型登记进桌面版模型目录（model_catalog_json），让桌面版能选到它。

    必须登记的原因：桌面版会把 UI 里选中的模型写回 config.toml 顶层 model。
    模型名不在目录里 -> 桌面版写回别的模型 -> provider/model 组合非法 -> 请求必挂。

    上下文窗口与推理档位**必须按模型真实支持值填**：
      - 上下文填错 -> 实际可用窗口与预期不符（超出会被服务端截断/报错）
      - 档位填错 -> 界面只显示你登记的那几档，用户选不到模型真正支持的档位
    拿真实值：先 `discover_models(base_url, ...)` 看厂商 /v1/models 返回
    （部分厂商直接返回 effort.supported_levels 与 context_window），
    没有的话查厂商官方文档，再传给本函数。

    参数留空（0 / ""）时**沿用目录里已有条目的值**（若已登记过），
    否则用保守默认值。

    extra_fields：厂商特有的目录字段，登记时直接覆盖/补充到条目上。
      值为 None 的键会**删除**对应字段（用来去掉 OpenAI 专有字段）。
      例：小米 MiMo 需要 use_responses_lite=True，
          否则自定义工具（apply_patch）报
          "custom tools require MiMo freeform Responses lite mode"。

    模板取自目录里**没有 tool_mode 键**的条目（内置模型里只有 gpt-5.5 是这样）：
    带 tool_mode="code_mode_only" 的条目会逼模型用 code mode 输出，
    第三方模型会吐出 DSML 文本，Codex 不认，工具调用全废。
    """
    path = catalog_path or _catalog_path()
    cat = _load_catalog(path)
    models = cat.get("models") or []

    # 已有条目 -> 沿用它的值作为默认
    prev = next((m for m in models if m.get("slug") == slug), None)

    template = None
    for m in models:
        if m.get("slug") == "gpt-5.5":
            template = m
            break
    if template is None:
        for m in models:
            if "tool_mode" not in m:
                template = m
                break
    if template is None:
        # 模型目录是空的（新机器 / 刚建好的目录）-> 用内置的最小模板
        template = FALLBACK_TEMPLATE

    entry = json.loads(json.dumps(template))  # 深拷贝
    entry["slug"] = slug
    entry["display_name"] = display_name or slug
    entry["description"] = entry.get("description", "") + " (registered via model-switcher MCP)"

    # 上下文窗口
    cw = int(context_window) if context_window else 0
    if not cw and prev:
        cw = int(prev.get("max_context_window") or prev.get("context_window") or 0)
    if not cw:
        cw = int(template.get("max_context_window") or template.get("context_window") or 0)
    if cw:
        entry["context_window"] = cw
        entry["max_context_window"] = cw
        entry["effective_context_window_percent"] = 95

    # 最大输出
    mo = int(max_output_tokens) if max_output_tokens else 0
    if not mo and prev:
        mo = int(prev.get("max_output_tokens") or 0)
    if mo:
        entry["max_output_tokens"] = mo

    # 推理档位
    levels = [e.strip() for e in effort_levels.split(",") if e.strip()]
    if not levels and prev:
        levels = [x.get("effort") for x in (prev.get("supported_reasoning_levels") or [])
                  if isinstance(x, dict) and x.get("effort")]
    if not levels:
        levels = [x.get("effort") for x in (template.get("supported_reasoning_levels") or [])
                  if isinstance(x, dict) and x.get("effort")]
    if levels:
        entry["supported_reasoning_levels"] = [
            {"effort": e, "description": "reasoning effort: %s" % e} for e in levels]

    # 默认档位：必须是自己声明支持的档位之一
    df = (default_effort or "").strip()
    if not df and prev:
        df = prev.get("default_reasoning_level") or ""
    if not df:
        df = "medium"
    if levels and df not in levels:
        df = levels[-1] if "max" in levels else levels[0]
    entry["default_reasoning_level"] = df

    entry["priority"] = 100
    entry["upgrade"] = None
    entry.pop("tool_mode", None)          # 普通 function calling 模式
    entry.pop("multi_agent_version", None)

    # 厂商特有字段（use_responses_lite / apply_patch_tool_type 等）
    if extra_fields:
        for k, v in extra_fields.items():
            if v is None:
                entry.pop(k, None)
            else:
                entry[k] = v

    replaced = False
    for i, m in enumerate(models):
        if m.get("slug") == slug:
            models[i] = entry
            replaced = True
            break
    if not replaced:
        models.append(entry)
    # register_model_in_catalog 期间它自己 load/save 过目录，
    # 这里必须**重新读**再合并 —— 否则会用旧快照覆盖掉刚登记的条目。
    fresh = _load_catalog(path)
    by_slug = {m.get("slug"): m for m in (fresh.get("models") or [])
               if isinstance(m, dict) and m.get("slug")}
    for m in models:
        if isinstance(m, dict) and m.get("slug"):
            by_slug[m["slug"]] = m
    fresh["models"] = list(by_slug.values())
    _save_catalog(path, fresh)

    return ("已%s模型 '%s' 到 %s\n"
            "下一步：codex exec 新开会话，或重启 Codex 桌面版，模型选择器里就能看到它。"
            % ("更新" if replaced else "登记", slug, path))


# ---------------------------------------------------------------------------
# 模型可见性：自选哪些模型留在桌面版模型选择器里
# ---------------------------------------------------------------------------

def _catalog_entry(cat, slug):
    for m in cat.get("models") or []:
        if isinstance(m, dict) and m.get("slug") == slug:
            return m
    return None


def set_model_visibility(slug: str, visible: bool = True) -> str:
    """显示 / 隐藏桌面版模型目录里的某个模型（**不删除条目**）。

    桌面版模型目录里每个条目的 `visibility` 字段只有两个合法值：
      "list" —— 出现在模型选择器里
      "hide" —— 不出现在选择器里，但条目仍保留在目录中，随时可以放回

    为什么用隐藏而不是删除：
      厂商会频繁上下架模型（同一个 slug 过几个月可能又回来了）。
      隐藏保留了档位/上下文等已登记的真实值，放回来时无需重填；
      删除则会把这些信息一并丢掉。

    典型用法：某模型已下架，但先把它藏起来（visible=False），
    等厂商重新上架时用 sync_provider_models 自动放回。
    """
    slug = (slug or "").strip()
    if not slug:
        raise RuntimeError("slug 不能为空")
    path = _catalog_path()
    cat = _load_catalog(path)
    entry = _catalog_entry(cat, slug)
    if entry is None:
        raise RuntimeError("模型目录 %s 里没有 '%s'。\n可用 list_catalog_models 查看现有条目。"
                           % (path, slug))
    old = entry.get("visibility") or "list"
    new = "list" if visible else "hide"
    if old == new:
        return "模型 '%s' 已经是 %s 状态，无需改动。" % (slug, new)
    entry["visibility"] = new
    _save_catalog(path, cat)
    return ("模型 '%s'：visibility %s -> %s（%s）\n目录：%s\n"
            "重启 Codex 桌面版后生效。"
            % (slug, old, new, "会出现在模型选择器里" if visible else "已从模型选择器隐藏",
               path))


def _provider_creds(provider_id):
    """取 provider 的 base_url / 密钥 / wire_api / env_key。

    密钥优先读 config.toml 的 experimental_bearer_token（桌面版真正用的那个），
    其次 presets 里的 api_key，最后回退到环境变量。
    """
    base = wire = env_key = key = ""
    try:
        with open(switcher.CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
        blk = (cfg.get("model_providers") or {}).get(provider_id) or {}
        base = blk.get("base_url") or ""
        wire = blk.get("wire_api") or ""
        env_key = blk.get("env_key") or ""
        key = blk.get("experimental_bearer_token") or ""
    except Exception:
        pass
    for pr in _presets_providers():
        if pr.get("id") == provider_id:
            base = base or (pr.get("base_url") or "")
            wire = wire or (pr.get("wire_api") or "")
            env_key = env_key or (pr.get("env_key") or "")
            key = key or (pr.get("api_key") or "")
            break
    if not key and env_key:
        key = os.environ.get(env_key, "")
    return base, key, wire, env_key


def _live_models(base_url, api_key, timeout=25):
    """拉厂商 /v1/models，返回模型条目列表。"""
    if not (base_url or "").strip():
        raise RuntimeError("该 provider 没有 base_url")
    base = _api_base(base_url)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(base + "/models", headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise RuntimeError("HTTP %s：%s\n（401 = key 无效或没带上 key）" % (e.code, body))
    except Exception as e:
        raise RuntimeError("请求失败：%s" % e)
    items = data.get("data") if isinstance(data, dict) else data
    return items or []


def sync_provider_models(provider_id: str, keep: str = "",
                         hide_unlisted: bool = True,
                         register_missing: bool = True) -> str:
    """以厂商 /v1/models 为准，把桌面版模型目录对齐，并自选哪些模型留在列表里。

    参数：
      provider_id      已有 provider 的 id（list_providers 里能看到）
      keep             逗号/换行分隔，指定**要留在模型选择器里**的模型；
                      留空表示「厂商在售的全部都留」
      hide_unlisted    True = 把该 provider 下、厂商已下架的模型自动隐藏
                      （visibility=hide，条目保留，随时可放回）
      register_missing True = 把在售但目录里还没登记的模型自动登记进去

    为什么需要：
      厂商上下架模型很频繁，而桌面版模型选择器读的是**目录**，
      目录里留着已下架的模型 -> 用户点了就报 unknown model。
      本工具把「厂商真相」和「用户选择」两件事分开：
      厂商真相来自 /v1/models，用户选择来自 keep 列表。
    """
    provider_id = (provider_id or "").strip()
    if not provider_id:
        raise RuntimeError("provider_id 不能为空")
    keep_set = {s.strip() for s in re.split(r"[\r\n,]+", keep) if s.strip()}

    base, key, wire, env_key = _provider_creds(provider_id)
    if not base:
        p = PRESETS.get(provider_id)
        base = (p or {}).get("base_url") or ""
    if not base:
        raise RuntimeError("找不到 provider '%s' 的 base_url。" % provider_id)
    if not key:
        raise RuntimeError(
            "provider '%s' 没有可用密钥（config.toml 里没有 experimental_bearer_token，"
            "presets 里没有 api_key，环境变量 %s 也没设）。\n"
            "先用 setup_preset / set_api_key 写入密钥。"
            % (provider_id, env_key or "(未指定 env_key)"))

    live = _live_models(base, key)
    live_ids = [it.get("id") for it in live if isinstance(it, dict) and it.get("id")]

    # 该 provider 名下已知的模型 = 预设列表 ∪ 厂商在售列表
    known = set(live_ids)
    for pr in _presets_providers():
        if pr.get("id") == provider_id:
            known |= set(pr.get("models") or [])

    # 官网下线公告核查：厂商「还能调到」不等于「还在售」。
    # 已被公告点名的模型同样要下架（MiniMax M2.5/M2.7 就是这种：
    # 2026-11-18 正式下线，现在 /v1/models 还能列出来）。
    retired = set()
    current_models = None
    try:
        status, _live, _pages = _scan_provider_deprecations(
            provider_id, base, key, sorted(known))
        # ⚠️ 只有**确定**下线（DEAD_STATES）才算 retired。
        #    UNKNOWN_STATE 是「查不到在售清单」，查不到 ≠ 下线，
        #    把它算进 retired 会把好模型从选择器里误隐藏。
        retired = set(m for m, (st, _w) in status.items() if st in DEAD_STATES)
        current_models = [m for m in known
                          if status.get(m, ("在售", ""))[0] == "在售"]
    except Exception:
        retired = set()

    path = _catalog_path()
    cat = _load_catalog(path)
    models = cat.get("models") or []

    hidden, shown, delisted, added = [], [], [], []
    for m in models:
        if not isinstance(m, dict):
            continue
        s = m.get("slug")
        if s not in known:
            continue
        if s in retired:
            # 官网已公告下线
            if (m.get("visibility") or "list") != "hide":
                m["visibility"] = "hide"
                hidden.append(s)
            delisted.append(s)
            continue
        if s not in live_ids:
            # 厂商已下架
            if (m.get("visibility") or "list") != "hide":
                m["visibility"] = "hide"
                hidden.append(s)
            delisted.append(s)
            continue
        want_visible = (not keep_set) or (s in keep_set)
        if want_visible and (m.get("visibility") or "list") != "list":
            m["visibility"] = "list"
            shown.append(s)
        elif not want_visible and (m.get("visibility") or "list") != "hide":
            m["visibility"] = "hide"
            hidden.append(s)

    # 在售但没登记的 -> 登记
    if register_missing:
        excluded = set((PRESETS.get(provider_id) or {}).get("catalog_exclude") or [])
        for it in live:
            if not isinstance(it, dict):
                continue
            s = it.get("id")
            if not s or s in excluded or _catalog_entry(cat, s):
                continue
            eff = it.get("effort") or {}
            levels = eff.get("supported_levels") or eff.get("supportedLevels") or []
            cw = int(it.get("context_window") or 0)
            mo = int(it.get("max_output_tokens") or 0)
            try:
                register_model_in_catalog(
                    s, it.get("name") or s,
                    context_window=cw, max_output_tokens=mo,
                    effort_levels=",".join(levels),
                    default_effort=eff.get("default_level") or "",
                    catalog_path=path)
                added.append(s)
            except Exception:
                pass

    cat["models"] = models
    _save_catalog(path, cat)

    # provider 的模型列表也同步：把已下线的名字摘掉
    dropped = []
    if current_models:
        providers = _presets_providers()      # 读一次，改完再存
        for pr in providers:
            if pr.get("id") != provider_id:
                continue
            old = list(pr.get("models") or [])
            new = [m for m in old if m in current_models]
            if len(new) != len(old):
                dropped = [m for m in old if m not in new]
                pr["models"] = new
                if pr.get("model") not in new:
                    pr["model"] = new[0] if new else pr.get("model")
        if dropped:
            switcher.save_presets({"version": switcher.PRESETS_VERSION,
                                   "providers": providers})

    lines = ["provider '%s' 对齐完成（base=%s，厂商在售 %d 个）" % (provider_id, base, len(live_ids))]
    if keep_set:
        lines.append("  指定保留：%s" % ", ".join(sorted(keep_set)))
    if shown:
        lines.append("  已放回列表：%s" % ", ".join(shown))
    if hidden:
        lines.append("  已从列表隐藏：%s" % ", ".join(hidden))
    if delisted:
        lines.append("  厂商已下架/已公告下线（条目保留，visibility=hide）：%s"
                     % ", ".join(sorted(set(delisted))))
    if dropped:
        lines.append("  已从 provider 模型列表里摘掉：%s" % ", ".join(dropped))
    if added:
        lines.append("  新登记：%s" % ", ".join(added))
    if not (shown or hidden or added or dropped):
        lines.append("  目录已是最新，无需改动。")
    lines.append("目录：%s\n重启 Codex 桌面版后生效。" % path)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 协议探测 / 连通性实测
# ---------------------------------------------------------------------------

def check_provider_protocol(base_url: str, provider_id: str = "") -> str:
    """探测厂商支持哪种协议，判断能否直连 Codex。

    新版 Codex（0.163+）只认 wire_api="responses"（旧的 "chat" 已被移除），
    所以厂商必须实现 POST {base}/responses。

    ⚠️ **无密钥探测本质上不可靠**：不少厂商（智谱、Moonshot、MiniMax 等）对
    任何路径都先返回同一个 401 错误体，连不存在的路径也一样 ——
    此时「401」证明不了端点存在。所以本工具分三档给结论：

      1) 指定了已配置且有密钥的 provider -> **直接真发一次请求**，给确定结论
      2) /responses 明确 404             -> 确凿**不支持**，直连必失败
      3) 其余                             -> 只能说「无法用无密钥方式判定」，
                                           拿到 key 后用 test_provider 实测

    provider_id 可选：指定后即使 base_url 传空，也会用该 provider 的 base_url。
    """
    if provider_id:
        base, key, wire, env_key = _provider_creds(provider_id)
        base = base or base_url
    else:
        base = base_url
    base = _api_base(base)
    if not base:
        raise RuntimeError("base_url 不能为空")
    fake = "probe-invalid-key"

    def probe(path, body=None, method="GET"):
        headers = {"Authorization": "Bearer " + fake,
                   "Content-Type": "application/json"}
        req = urllib.request.Request(base + path, headers=headers,
                                     data=json.dumps(body).encode("utf-8") if body else None,
                                     method=method)
        try:
            with _opener.open(req, timeout=20) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")[:120]
        except urllib.error.HTTPError as e:
            try:
                b = e.read().decode("utf-8", "replace")[:120]
            except Exception:
                b = ""
            return e.code, b
        except Exception as e:
            return None, repr(e)[:120]

    st_models, body_models = probe("/models")
    st_resp, body_resp = probe("/responses", {"model": "probe", "input": "ping"}, "POST")
    st_chat, body_chat = probe("/chat/completions",
                               {"model": "probe",
                                "messages": [{"role": "user", "content": "ping"}]},
                               "POST")

    def judge(st):
        if st is None:
            return "连不上"
        if st == 404:
            return "端点不存在(404)"
        if st in (401, 403):
            return "需鉴权(401/403)"
        if st == 405:
            return "端点存在(方法不对)"
        return "HTTP %s" % st

    lines = ["探测 %s" % base,
             "  GET  /models           -> %s" % judge(st_models),
             "  POST /responses        -> %s" % judge(st_resp),
             "  POST /chat/completions -> %s" % judge(st_chat), ""]

    # 1) 有密钥 -> 直接真发一次请求，给确定结论
    if provider_id and key:
        model = ""
        for pr in _presets_providers():
            if pr.get("id") == provider_id:
                model = pr.get("model") or (pr.get("models") or [""])[0]
                break
        if model:
            lines.append("检测到 provider '%s' 已配置密钥，直接实测：" % provider_id)
            lines.append(test_provider(provider_id, model, "ping"))
            lines.append("")
            return "\n".join(lines)

    # 2) 404 = 确凿证据
    if st_resp == 404:
        lines.append("结论：**该厂商没有 /responses 端点**，Codex 直连会失败。")
        lines.append("  两条路：(1) 把 base_url 指向 Responses 兼容代理"
                     "（LiteLLM / CLIProxyAPI / OpenRouter）；")
        lines.append("        (2) 换一个实现了 Responses 协议的厂商"
                     "（DeepSeek / MiniMax / OpenRouter / DashScope 实测支持）。")
        return "\n".join(lines)
    if st_chat == 404 and st_resp != 404:
        lines.append("结论：**该厂商只实现 /responses，不实现 /chat/completions**"
                     "——可以直连 Codex（wire_api=\"responses\"）。")
        return "\n".join(lines)

    # 3) 无法判定
    same = (st_resp == st_chat and body_resp.strip() == body_chat.strip())
    lines.append("结论：**无密钥方式无法判定**。")
    lines.append("  原因：%s" % (
        "该站对 /responses 与 /chat/completions 返回了完全相同的错误体，"
        "说明它有统一鉴权中间件（对不存在的路径也先返 401），401 证明不了端点存在。"
        if same else "端点存在与否无法从状态码区分。"))
    lines.append("  定论办法二选一：")
    lines.append("    (a) 配好密钥后调 test_provider(provider_id, model) —— 真发一次请求，最准；")
    lines.append("    (b) 查该厂商官方文档有没有 Responses 端点。")
    lines.append("  补充：多数国产厂商（智谱 / Moonshot / 硅基流动）官方只提供 "
                 "Chat Completions，而新版 Codex 已移除 wire_api=\"chat\"，")
    lines.append("        这类厂商直连大概率要挂，需经 Responses 兼容代理"
                 "（LiteLLM / CLIProxyAPI / OpenRouter）。")
    return "\n".join(lines)


def test_provider(provider_id: str, model: str = "", prompt: str = "ping") -> str:
    """用已配置的密钥**真发一次请求**，验证 provider + model + 协议真的能跑通。

    这是「接上了到底能不能用」的最终判据：只写完 config.toml 不代表能跑。
    失败时会原样带出厂商的错误码与错误信息。
    """
    provider_id = (provider_id or "").strip()
    base, key, wire, env_key = _provider_creds(provider_id)
    if not base:
        raise RuntimeError("找不到 provider '%s' 的 base_url。" % provider_id)
    if not key:
        raise RuntimeError(
            "provider '%s' 没有可用密钥（config.toml 无 experimental_bearer_token，"
            "presets 无 api_key，环境变量 %s 未设）。"
            % (provider_id, env_key or "(未指定)"))
    if not model:
        model = ""
        for pr in _presets_providers():
            if pr.get("id") == provider_id:
                model = pr.get("model") or (pr.get("models") or [""])[0]
                break
    if not model:
        raise RuntimeError("没有指定 model，且 provider '%s' 里也没有默认模型。" % provider_id)

    url = base.rstrip("/") + "/responses"
    payload = {"model": model, "input": prompt or "ping", "stream": False}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"},
                                 method="POST")
    t0 = time.time()
    try:
        with _opener.open(req, timeout=90) as resp:
            body = resp.read().decode("utf-8", "replace")
            code = resp.status
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return ("✗ 失败 HTTP %s（%.1fs）  provider=%s model=%s\n%s\n"
                "常见原因：模型名不存在 / 该厂商没有 Responses 端点（"
                "用 check_provider_protocol 确认）/ 协议不匹配。"
                % (e.code, time.time() - t0, provider_id, model, body[:600]))
    except Exception as e:
        return "✗ 请求异常（%.1fs） provider=%s model=%s：%r" % (
            time.time() - t0, provider_id, model, e)

    out = []
    try:
        d = json.loads(body)
        for item in (d.get("output") or []):
            for c in (item.get("content") or []):
                t = c.get("text")
                if t:
                    out.append(t)
    except Exception:
        pass
    text = ("".join(out) or "").strip()
    return ("✓ 成功（%.1fs）provider=%s model=%s  wire_api=%s\n%s"
            % (time.time() - t0, provider_id, model, wire or "responses",
               (text[:400] + "...") if len(text) > 400 else (text or "(无文本输出)")))



def _presets_providers():
    """读预设文件里的 provider 列表（含 models 归属表）。"""
    p = switcher.PRESETS_PATH if hasattr(switcher, "PRESETS_PATH") else None
    if not p:
        p = os.path.join(os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex")),
                         ".codex-switcher-presets.json")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f).get("providers", [])


def _model_owner(model):
    """按模型名反查它属于哪个 provider（gpt-/codex-/o1o3o4 前缀归 openai）。"""
    model = (model or "").strip()
    if not model:
        return None
    if re.match(r"^(gpt-|codex-|o1|o3|o4)", model, re.I):
        return "openai"
    for pr in _presets_providers():
        if model in (pr.get("models") or []) or model == pr.get("model"):
            return pr.get("id")
    return None


def _state_db_path():
    """Codex 桌面版的状态库（threads 表里存着每个会话的 model / model_provider）。"""
    return os.path.join(os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex")),
                        "state_5.sqlite")


def _fix_threads_db(dry_run):
    """修正数据库里各会话的 model / model_provider 错配。

    为什么必须改数据库：Codex 桌面版**换模型时只更新会话记录，不更新 config.toml**，
    而且模型选择器结构上就没有 provider 字段（全局状态里只有
    composer-recent-model-configurations = [{model, reasoningEffort, serviceTier}]）。
    => 换到别的 provider 的模型后，会话的 model_provider 停在旧值，
       请求用新模型名打到旧端点 -> `unknown model 'xxx' (2013)`。

    新建会话会从 config.toml 读正确的配对；已存在的会话则存在数据库里，
    所以两边都要修。
    """
    import sqlite3
    path = _state_db_path()
    if not os.path.exists(path):
        return []
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, model, model_provider, title FROM threads "
                            "WHERE model IS NOT NULL").fetchall()
        fixes = []
        for r in rows:
            model = (r["model"] or "").strip()
            prov = (r["model_provider"] or "").strip()
            owner = _model_owner(model)
            if owner and owner != prov:
                fixes.append((r["id"], model, prov, owner, (r["title"] or "")[:24]))
        if fixes and not dry_run:
            for tid, model, prov, owner, _t in fixes:
                conn.execute("UPDATE threads SET model_provider=? WHERE id=?", (owner, tid))
            conn.commit()
        conn.close()
        return fixes
    except Exception:
        return []


def fix_provider_alignment(dry_run: bool = False) -> str:
    """修正 model 与 model_provider 的错配（config.toml + 已存在的会话）。

    为什么需要：Codex 桌面版的模型选择器**只写模型名，不写 provider**。
    全局状态里模型配置只有 {model, reasoningEffort, serviceTier}，
    没有 provider 字段；换到别的 provider 的模型后，会话仍用旧 provider，
    于是请求用新模型名打到旧端点 —— 实测报
    `invalid params, code: 2013, unknown model 'deepseek-flash'`。

    本工具按「模型名 -> provider」的归属表反查并改回正确的 model_provider：
      · config.toml 顶层 model_provider（影响**新建**的会话）
      · state_5.sqlite 里各会话的 model_provider（影响**已存在**的会话）
    只在不一致时改（幂等），改前会备份 config.toml。
    """
    lines = switcher.read_config_lines()
    cur_model, cur_provider = switcher.read_active(lines)
    owner = _model_owner(cur_model)
    out = []

    # --- config.toml ---
    if not owner:
        out.append("config.toml：model=%s 无法从预设表反查归属，未修改。" % cur_model)
    elif owner == cur_provider:
        out.append("config.toml：model=%s / provider=%s 已一致。" % (cur_model, cur_provider))
    elif dry_run:
        out.append("config.toml：[dry-run] 错配 model=%s 应属 %s，当前 %s。"
                   % (cur_model, owner, cur_provider))
    else:
        new_lines = switcher.set_root_key(lines, "model_provider", owner)
        backup = switcher.write_config_lines(new_lines)
        out.append("config.toml：已把 model_provider %s -> %s（备份 %s）"
                   % (cur_provider, owner, backup))

    # --- 数据库里的会话 ---
    fixes = _fix_threads_db(dry_run)
    if not fixes:
        out.append("会话记录：未发现 model/provider 错配。")
    else:
        out.append("会话记录：发现 %d 个错配会话%s" % (len(fixes), "（dry-run，未修改）" if dry_run else "："))
        for tid, model, prov, own, title in fixes[:20]:
            out.append("    %s  model=%s  provider %s -> %s  %s"
                       % (tid[:36], model, prov, own, title))
        if len(fixes) > 20:
            out.append("    ...（其余 %d 个未列出）" % (len(fixes) - 20))
        if not dry_run:
            out.append("已全部修正。已开着的会话可能需要重开才读到新值。")

    return "\n".join(out)


def _global_state_path():
    return os.path.join(os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex")),
                        ".codex-global-state.json")


def _last_picked_model():
    """读桌面版模型选择器**最近一次选中**的模型名。

    桌面版的选择器结构上没有 provider 字段，它只记
    composer-recent-model-configurations-v1 = [{model, reasoningEffort, serviceTier}, ...]
    且**最新使用的排在最前**。
    """
    p = _global_state_path()
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        atom = d.get("electron-persisted-atom-state", {})
        cfgs = atom.get("composer-recent-model-configurations-v1") or []
        if not cfgs:
            return None
        first = cfgs[0]
        if isinstance(first, dict):
            return (first.get("model") or "").strip() or None
    except Exception:
        return None
    return None


def _watch_provider_alignment(stop_evt):
    """后台守护：桌面版换模型后自动把 config.toml 的 model_provider 修正。

    为什么需要这个守护：
      桌面版模型选择器**只写模型名，不写 provider**（全局状态里模型配置只有
      {model, reasoningEffort, serviceTier}，没有 provider 字段）。
      换到别的 provider 的模型后，它沿用旧 provider，
      请求用新模型名打到旧端点 -> `unknown model 'xxx' (2013)`。
      桌面版自己的 app-server 也不重读 config.toml 的 provider。

    守护做的事：盯全局状态里「最近选中的模型」，
      一旦它与 config.toml 的 model 不一致，就按归属表把 model_provider 改对，
      这样**新建的会话**继承到的是配对正确的配置。

    ⚠️ 已在运行的会话仍带旧 provider（存在桌面版内存里），那些会话需要
       在桌面版里重开才生效。
    """
    last_seen = None
    while not stop_evt.is_set():
        try:
            picked = _last_picked_model()
            if picked and picked != last_seen:
                last_seen = picked
                owner = _model_owner(picked)
                if owner:
                    try:
                        lines = switcher.read_config_lines()
                        cur_model, cur_provider = switcher.read_active(lines)
                        if cur_model != picked or cur_provider != owner:
                            new_lines = switcher.set_root_key(lines, "model", picked)
                            new_lines = switcher.set_root_key(new_lines, "model_provider", owner)
                            switcher.write_config_lines(new_lines)
                    except Exception:
                        pass
        except Exception:
            pass
        stop_evt.wait(3)


def list_model_capabilities(model: str = "") -> str:
    """列出模型**真实支持**的推理等级与上下文窗口（不做任何猜测）。

    数据来源（按可靠性排序）：
      1) 桌面版模型目录 model_catalog_json 的 supported_reasoning_levels / context_window
      2) provider 的 /v1/models 接口（部分厂商会返回 effort.supported_levels）
    """
    out = []
    try:
        cat = _load_catalog(_catalog_path())
    except Exception as e:
        cat = {"models": []}
        out.append("（读模型目录失败：%s）" % e)

    entries = cat.get("models") or []
    if model:
        entries = [m for m in entries if m.get("slug") == model]
        if not entries:
            return "模型目录里没有 '%s'。" % model
    for m in entries:
        levels = [x.get("effort") for x in (m.get("supported_reasoning_levels") or [])]
        out.append("%-26s 上下文=%-9s 最大输出=%-8s 推理档位=%s（默认 %s）" % (
            m.get("slug"), m.get("context_window"), m.get("max_output_tokens"),
            "/".join(levels) or "?", m.get("default_reasoning_level")))
    return "\n".join(out)


def set_reasoning_effort(effort: str, model: str = "") -> str:
    """设置推理等级（model_reasoning_effort）。

    effort 必须是**该模型真实支持**的档位；可用 list_model_capabilities 查看。
    不传 model 时按当前 config.toml 里的 model 校验。
    """
    effort = (effort or "").strip().lower()
    if not effort:
        raise RuntimeError("effort 不能为空")
    lines = switcher.read_config_lines()
    cur_model, _ = switcher.read_active(lines)
    target = (model or cur_model or "").strip()
    if not target:
        raise RuntimeError("无法确定目标模型，请显式传 model 参数")

    # 校验档位是否被模型声明支持（目录里有登记才校验）
    try:
        cat = _load_catalog(_catalog_path())
        entry = next((m for m in (cat.get("models") or []) if m.get("slug") == target), None)
        if entry:
            allowed = [x.get("effort") for x in (entry.get("supported_reasoning_levels") or [])]
            if allowed and effort not in allowed:
                raise RuntimeError(
                    "模型 '%s' 不支持推理等级 '%s'。\n它支持的档位：%s\n"
                    "（档位以模型官方声明为准，不要硬填）" % (target, effort, "/".join(allowed)))
    except RuntimeError:
        raise
    except Exception:
        pass

    new_lines = switcher.set_root_key(lines, "model_reasoning_effort", effort)
    backup = switcher.write_config_lines(new_lines)
    return ("已设置推理等级 = %s（模型 %s）。\nconfig.toml 备份：%s\n"
            "（新会话生效；已开着的会话需重开）" % (effort, target, backup))


def _set_root_int(lines, key, value):
    """写根级**整数**键（config.toml 的 int 键不能加引号）。

    switcher.set_root_key 一律写成字符串（'%s = "%s"'），
    对 model_context_window 这类 i64 键会直接导致
    `Error loading config.toml: invalid type: string "1000000", expected i64`。
    """
    end = switcher.first_section_index(lines)
    new_line = "%s = %d" % (key, int(value))
    for i in range(end):
        if re.match(r"\s*" + re.escape(key) + r"\s*=", lines[i]):
            lines[i] = new_line
            return lines
    insert_at = end
    for i in range(end):
        if re.match(r"\s*model\s*=", lines[i]):
            insert_at = i + 1
            break
    lines.insert(insert_at, new_line)
    return lines


def set_context_window(context_window: int, model: str = "") -> str:
    """设置上下文窗口长度（model_context_window）。

    不传 model 时按当前 config.toml 里的 model 校验上限。
    """
    try:
        cw = int(context_window)
    except Exception:
        raise RuntimeError("context_window 必须是整数")
    if cw <= 0:
        raise RuntimeError("context_window 必须大于 0")

    lines = switcher.read_config_lines()
    cur_model, _ = switcher.read_active(lines)
    target = (model or cur_model or "").strip()
    if target:
        try:
            cat = _load_catalog(_catalog_path())
            entry = next((m for m in (cat.get("models") or []) if m.get("slug") == target), None)
            if entry:
                limit = entry.get("max_context_window") or entry.get("context_window")
                if limit and cw > int(limit):
                    raise RuntimeError(
                        "模型 '%s' 的上下文上限是 %s，请求的 %d 超出上限。"
                        % (target, limit, cw))
        except RuntimeError:
            raise
        except Exception:
            pass

    new_lines = _set_root_int(lines, "model_context_window", cw)
    backup = switcher.write_config_lines(new_lines)
    return ("已设置上下文窗口 = %d（模型 %s）。\nconfig.toml 备份：%s\n"
            "（新会话生效；已开着的会话需重开）" % (cw, target or "?", backup))


def _official_live_slugs():
    """桌面版自己下发的实时模型清单（~/.codex/models_cache.json，带 etag）。

    这是「官方当前还提供哪些模型」的权威来源。
    读不到就返回 None（表示查不了，不是「一个都没有」）。
    """
    p = os.path.join(os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex")),
                     "models_cache.json")
    try:
        d = json.load(io.open(p, encoding="utf-8-sig"))
        models = d.get("models") or []
        return {m.get("slug") for m in models if isinstance(m, dict) and m.get("slug")}
    except Exception:
        return None


def _is_official_slug(slug):
    """内置官方模型的命名特征（gpt- / codex- / o1 / o3 / o4）。"""
    return bool(re.match(r"^(gpt-|codex-|o1|o3|o4)", slug or "", re.I))


def prune_stale_models(dry_run: bool = True) -> str:
    """找出并隐藏「目录里有、但已经没人提供」的模型（下架模型）。

    两个真相来源：
      1) 桌面版自己下发的实时清单 ~/.codex/models_cache.json
         （带 fetched_at / etag，是官方当前提供的模型）
      2) 各 provider 的 /v1/models（第三方模型的在售清单）

    **只隐藏（visibility=hide），不删除条目**：
    厂商把模型重新上架时，sync_provider_models 会自动把它放回列表，
    而且已登记的推理档位 / 上下文长度不会丢。

    dry_run=True（默认）只报告不改；dry_run=False 才实际写入。
    """
    path = _catalog_path()
    cat = _load_catalog(path)
    models = cat.get("models") or []

    official = _official_live_slugs()
    # 第三方：按 presets 的归属表找 owner
    owner_of = {}
    for pr in _presets_providers():
        for s in (pr.get("models") or []):
            owner_of[s] = pr.get("id")

    live_cache = {}     # provider_id -> set(slugs)
    stale, unknown = [], []
    for m in models:
        if not isinstance(m, dict):
            continue
        s = m.get("slug")
        if not s:
            continue
        if _is_official_slug(s):
            if official is None:
                unknown.append((s, "读不到 models_cache.json"))
            elif s not in official:
                stale.append((s, "桌面版实时清单里已没有"))
            continue
        pid = owner_of.get(s)
        if not pid:
            unknown.append((s, "不属于任何已配置 provider"))
            continue
        if pid not in live_cache:
            base, key, _w, _e = _provider_creds(pid)
            if not key or not base:
                live_cache[pid] = None
            else:
                try:
                    live_cache[pid] = {it.get("id") for it in _live_models(base, key)
                                       if isinstance(it, dict) and it.get("id")}
                except Exception:
                    live_cache[pid] = None
        got = live_cache.get(pid)
        if got is None:
            unknown.append((s, "provider '%s' 查不到在售列表（缺密钥或请求失败）" % pid))
        elif s not in got:
            stale.append((s, "厂商 '%s' 已下架" % pid))

    lines = ["模型目录：%s（%d 条）" % (path, len(models))]
    if official is not None:
        lines.append("桌面版实时清单：%d 个官方模型" % len(official))
    else:
        lines.append("桌面版实时清单：读不到（models_cache.json 缺失或解析失败）")
    lines.append("疑似下架：%d 个" % len(stale))
    for s, why in stale:
        lines.append("  - %-30s %s" % (s, why))
    if unknown:
        lines.append("无法判定：%d 个" % len(unknown))
        for s, why in unknown:
            lines.append("  - %-30s %s" % (s, why))

    if not stale:
        lines.append("没有需要处理的条目。")
        return "\n".join(lines)

    if dry_run:
        lines.append("")
        lines.append("（dry_run=True，未改动。确认无误后用 dry_run=False 执行。）")
        return "\n".join(lines)

    hidden = []
    for s, _why in stale:
        e = _catalog_entry(cat, s)
        if e and (e.get("visibility") or "list") != "hide":
            e["visibility"] = "hide"
            hidden.append(s)
    cat["models"] = models
    _save_catalog(path, cat)
    lines.append("")
    lines.append("已隐藏 %d 个：%s" % (len(hidden), ", ".join(hidden)))
    lines.append("（条目仍在目录里，随时可放回；厂商重新上架时 "
                 "sync_provider_models 会自动放回）")
    lines.append("重启 Codex 桌面版后生效。")
    return "\n".join(lines)


def main():
    from mcp.server.fastmcp import FastMCP

    try:
        _startup_probe()
    except Exception:
        pass

    # 后台守护：桌面版换模型后自动修正 provider 错配
    try:
        import threading
        _stop = threading.Event()
        _t = threading.Thread(target=_watch_provider_alignment, args=(_stop,),
                             daemon=True, name="provider-aligner")
        _t.start()
    except Exception:
        pass

    mcp = FastMCP("model-switcher")

    mcp.tool()(list_providers)
    mcp.tool()(switch_model)
    mcp.tool()(add_provider)
    mcp.tool()(delete_provider)
    mcp.tool()(discover_models)
    mcp.tool()(register_model_in_catalog)
    mcp.tool()(list_catalog_models)
    mcp.tool()(set_model_visibility)
    mcp.tool()(sync_provider_models)
    mcp.tool()(prune_stale_models)
    mcp.tool()(check_provider_protocol)
    mcp.tool()(test_provider)
    mcp.tool()(set_api_key)
    mcp.tool()(setup_preset)
    mcp.tool()(check_provider_deprecations)
    mcp.tool()(fix_provider_alignment)
    mcp.tool()(list_model_capabilities)
    mcp.tool()(set_reasoning_effort)
    mcp.tool()(set_context_window)

    mcp.run()


if __name__ == "__main__":
    main()
