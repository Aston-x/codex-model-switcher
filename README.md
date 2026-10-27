# codex-model-switcher

在 Codex 里直接接 API key、切模型、调推理等级与上下文长度。
Manage API keys, switch models, set reasoning levels and context window — right inside Codex.

纯插件形态（MCP + Skill），不注入桌面版、不起代理、不改客户端二进制。
Pure plugin (MCP + Skill). No injection, no proxy, no patching of the client binary.

**中文** | [English](#english)

---

## 中文

```
用 deepseek，key在剪贴板中,进行读取且不回显
推理等级调到 max
这家厂商能不能接？
哪些模型下线了？
```

### 它修的四个坑

| 现象 | 根因 |
|---|---|
| `unknown model 'xxx'`（2013） | 桌面版换模型**只写模型名、不写 provider** |
| `Missing environment variable: XXX` | 桌面版是 MSIX 应用，读不到 `setx` 写的环境变量 |
| 换厂商就报协议错 | Codex 0.163 起只认 `wire_api = "responses"` |
| 模型列表里有点不动的 | 目录里留着厂商已下架的模型 |

四件事都有对应工具，且都做过实测。

### 快速开始

```bash
git clone https://github.com/Aston-x/codex-model-switcher.git
cd codex-model-switcher
bash install.sh            # 用 bash 调，不需要可执行位
```

```powershell
git clone https://github.com/Aston-x/codex-model-switcher.git
cd codex-model-switcher
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

没有 git 也能装 —— 直接下载 zip 解压即可：

```bash
# https://github.com/Aston-x/codex-model-switcher/archive/refs/heads/main.zip
unzip codex-model-switcher-main.zip && cd codex-model-switcher-main && bash install.sh
```

**直连 GitHub 不通时走镜像**（国内常见，代理开没开都能拉）：

```bash
git clone https://ghproxy.net/https://github.com/Aston-x/codex-model-switcher.git
# 或
git clone https://gh-proxy.com/https://github.com/Aston-x/codex-model-switcher.git
```

安装脚本：检查 Python 3.11+ → 装 `mcp` → 注册 marketplace 并安装插件 → 跑 `doctor.py --fix`。

装完**完全退出并重开 Codex 桌面版**，然后在对话里说「用 deepseek」即可。

手动安装：

```toml
# ~/.codex/config.toml
[marketplaces.codex-model-switcher]
source_type = "local"
source = '<仓库目录>'

[plugins."model-switcher@codex-model-switcher"]
enabled = true
```

### 拉不到 / 跑不起来

| 症状 | 原因与解法 |
|---|---|
| `Failed to connect to github.com:443 over proxy 127.0.0.1` | 这台机器的 git 配了本地代理但**代理没在跑**。先起代理；或临时绕过：`git -c http.proxy= -c https.proxy= clone ...` |
| `Recv failure: Connection was reset` | 直连 GitHub 被重置。**走镜像**（见上面的 clone 命令），或用 zip 下载 |
| `Permission denied: ./install.sh` | 仓库里的可执行位在 Windows 上 checkout 会丢。**用 `bash install.sh`**（不需要可执行位） |
| `bad interpreter: /usr/bin/env bash^M` | 脚本被转成了 CRLF。`git config core.autocrlf false` 后重新 clone，或直接用 zip |
| `找不到 Python 3.11+` | 需要 3.11+（要 `tomllib`）。装完再跑一次 |
| `pip 装不上 mcp` | `externally-managed-environment`：建虚拟环境，或 `pip install --user mcp` |
| `找不到 codex 命令` | codex 不在 PATH。手动执行脚本里提示的两条 `codex plugin ...` 命令 |

排查代理配置：

```bash
git config --global --get http.proxy     # 看有没有写死本地代理
```

### 接入密钥：走剪贴板，别贴进对话

密钥**不要**作为聊天明文或 MCP 工具参数传递（会进会话记录与工具日志）。

```python
setup_preset_from_clipboard("deepseek")   # 从剪贴板读 key，用完立刻清空
```

流程：供应商控制台复制 key → 只对 agent 说「从剪贴板更新 DEEPSEEK_API_KEY」→
agent 读本机剪贴板直接写入 `config.toml` → 随即清空剪贴板 →
输出只给长度与连通性验证，**不回显 key**。

如果已经把 key 贴进对话里了，请**去供应商控制台吊销并重签**。

### doctor —— 自动识别环境并修

```bash
python doctor.py            # 只体检
python doctor.py --fix      # 体检 + 自动修
python doctor.py --json     # 机器可读
```

| 检查项 | 能修什么 |
|---|---|
| Python 版本 | 报缺 3.11+（需要 `tomllib`） |
| `mcp` 包 | `pip install mcp` |
| `.mcp.json` 平台 | 按当前系统换 Windows / POSIX 版本 |
| `config.toml` 可解析 | 报 TOML 语法错 |
| `model_context_window` | 字符串 → 整数（字符串会让 Codex 起不来） |
| `model` / `model_provider` | 配对修正（2013 的直接原因） |
| `model_catalog_json` | 缺失时建目录并在 config.toml 登记 |
| provider 的 key 方式 | 提示 env 模式在桌面版读不到，应改 inline |
| 插件是否装进缓存 | 版本不一致时提示重装 |
| 开机自启 | Windows Run 键 / macOS LaunchAgent / Linux systemd user |
| 守护进程是否在跑 | 按锁文件判活 |

> ⚠️ doctor 改 `config.toml` 时，Windows 路径一律用 **TOML 字面量字符串**（单引号），
> 写完还会**回读校验**——不会把你的配置改坏。

### 工具一览（19 个）

| 工具 | 作用 |
|---|---|
| `list_providers` | 列出已配置的 provider、每个可选模型、当前默认模型 |
| `switch_model(provider_id, model)` | 切换默认模型 + provider |
| `add_provider(...)` | 新增/更新自定义 provider |
| `delete_provider(provider_id)` | 删除自定义 provider |
| `setup_preset(preset, api_key, ...)` | 一键接入（key 建议走剪贴板版） |
| `setup_preset_from_clipboard(preset, ...)` | 一键接入，**密钥从剪贴板读并随即清空** |
| `set_api_key(env_key_name, api_key, ...)` | 单独写 key（`inline` / `env`） |
| `set_api_key_from_clipboard(env_key_name, ...)` | 单独写 key，**从剪贴板读并随即清空** |
| `discover_models(base_url, ...)` | 拉厂商 `/v1/models` 拿**真实**在售模型与档位 |
| `check_provider_deprecations(provider_id, models)` | 查**厂商官网的下线公告** |
| `check_provider_protocol(base_url, provider_id)` | 探测厂商支不支持 Responses 协议；有密钥时直接实测 |
| `test_provider(provider_id, model)` | 真发一次请求，验证"接上了到底能不能用" |
| `register_model_in_catalog(slug, ...)` | 把模型登记进桌面版模型目录 |
| `list_catalog_models(include_hidden)` | 列出模型目录（含已隐藏的） |
| `set_model_visibility(slug, visible)` | **显示 / 隐藏**单个模型（不删除条目） |
| `sync_provider_models(provider_id, keep)` | 以厂商在售列表对齐目录；`keep` 自选留哪些 |
| `prune_stale_models(dry_run)` | 找出并隐藏**已经没人提供**的模型 |
| `fix_provider_alignment(dry_run)` | 修正 model 与 model_provider 的错配 |
| `list_model_capabilities(model)` | 查模型**真实支持**的档位与上下文 |
| `set_reasoning_effort(effort, model)` | 设置推理等级（会校验档位） |
| `set_context_window(context_window, model)` | 设置上下文长度（会校验上限） |

preset：`deepseek / minimax / kimi / glm / mimo / siliconflow / dashscope / openrouter`

### 接厂商前先查下线公告

`/v1/models` 只说明"**现在还能调到**"，不说明"**厂商已经宣布要下线**"。

实测（2026-10-25）：MiniMax 官网已把 **M2 / M2.1 / M2.5 / M2.7 全系列**标记「已下线」，
但它的 `/v1/models` **仍然返回这 9 个 id**——只靠 `/v1/models` 会把它们全当成在售。

`setup_preset` 会自动核查官网公告，已下线的模型名**不会**写进 provider 块、
**不会**登记进模型目录。

⚠️ **「查不到」不等于「已下线」**：没配官网在售页、又拿不到 `/v1/models` 时，
工具给「无法判定」并**保留模型不动**。

### 自选哪些模型留在列表里

目录条目的 `visibility` 只有两个合法值：`"list"`（出现在选择器里）、
`"hide"`（不出现，但**条目与已登记的档位/上下文都还在**）。

```python
set_model_visibility("gpt-6-astra", False)   # 从选择器里摘掉
sync_provider_models("minimax", keep="MiniMax-M3.1-Flash,MiniMax-M3")
prune_stale_models(dry_run=True)              # 找出没人提供的模型
```

真相来源：桌面版实时清单 `~/.codex/models_cache.json` + 各 provider 的 `/v1/models`。
模型重新上架时会自动放回列表。

### 哪些厂商能直连

**Codex 0.163+ 只认 `wire_api = "responses"`**，所以能不能直连取决于厂商有没有实现
`POST {base}/responses`：

| 厂商 | `/v1/responses` | 能否直连 |
|---|---|---|
| DeepSeek | 存在 | ✅ 实测可用 |
| MiniMax | 存在 | ✅ 实测可用 |
| 小米 MiMo | 存在 | ✅ |
| OpenRouter | 存在 | ✅ 467 个模型，聚合首选 |
| 阿里百炼 DashScope | 存在 | ✅ |
| 硅基流动 SiliconFlow | **404** | ❌ 需 Responses 兼容代理 |
| 智谱 GLM / Moonshot | 401（统一鉴权，测不出） | ❌ 官方只提供 Chat Completions，需代理 |

⚠️ **无密钥探测会骗人**：这类厂商对**任何路径**都先返回同一个 401，看着像"端点存在"，
其实证明不了。**别把 401 当成"支持"**——有密钥时 `check_provider_protocol` 会直接实测。

### 推理等级与上下文长度要按真实值填

不同模型支持的档位不一样，别写死：

- MiniMax M3.1 Flash：`low / medium / high / xhigh / max`（默认 max），上下文 1,000,000
- MiniMax M3 / M2.x：`minimal / low / medium / high`
- DeepSeek V4：`low / high / max`（`/v1/models` 直接返回 `effort.supported_levels`）

取值顺序：① `discover_models` 的厂商返回值 → ② 厂商官方文档 → ③ `list_model_capabilities`。
档位登记进 `model_catalog_json` 后，模型选择器里才会显示这些档位。

### 跨平台

| | Windows | macOS | Linux |
|---|---|---|---|
| MCP server 启动 | `cmd.exe` → `.cmd` | `bash` → `.sh` | `bash` → `.sh` |
| 用户级环境变量 | `setx` | `launchctl setenv` + shell rc | `~/.config/environment.d` + `~/.profile` |
| 剪贴板 | Win32 API | `pbpaste` | `wl-paste` / `xclip` / `xsel` |
| 进程枚举 | Win32 API（零子进程） | `ps` | `/proc` |
| 开机自启 | HKCU Run 键 | LaunchAgent | systemd user / XDG autostart |
| 静默启动 | `pythonw.exe` | `setsid` 脱离终端 | `setsid` 脱离终端 |

### 开发与测试

```bash
python tests/smoke_test.py                                   # 全新临时环境跑全链路
MODEL_SWITCHER_TEST_KEY=sk-xxx python tests/smoke_test.py   # 带真 key（含真实请求）
```

冒烟测试会构造一台"刚装好 Codex 的机器"，跑：doctor 体检 → `--fix` → MCP 工具 →
接入 provider → **真发一次请求** → 隐藏/放回模型 → 按在售列表对齐 → 下线公告核查。

### 目录结构

```
.
├── .agents/plugins/marketplace.json   # marketplace 清单（仓库根即 marketplace 根）
├── .codex-plugin/plugin.json          # 插件清单
├── .mcp.json / .mcp.windows.json / .mcp.unix.json
├── doctor.py                          # 环境体检 + 自动修复
├── platform_compat.py                 # 跨平台适配层（含剪贴板）
├── server.py                          # 21 个 MCP 工具
├── install.sh / install.ps1           # 一键安装
├── scripts/                           # 守护进程 + 双平台启动器
├── skills/model-switcher/             # 给 agent 看的使用说明
├── tests/smoke_test.py                # 全新环境冒烟测试
└── vendor/                            # codex-token (MIT) + PATCHES.md 补丁台账
```

> ⚠️ **Windows 的 `.cmd` 必须是纯 ASCII + CRLF**：`cmd.exe` 用控制台代码页（936/GBK）读
> 无 BOM 的 UTF-8，CJK 注释会破坏块解析，MCP server 根本起不来。
> 详见 [`scripts/launch_model_switcher.cmd`](scripts/launch_model_switcher.cmd) 内的注释。

> ⚠️ **vendor 不是原样的**：`vendor/switcher.py` 在上游基础上打了补丁，
> 台账见 [`vendor/PATCHES.md`](vendor/PATCHES.md)。

### 和其它项目的关系

| 项目 | 形态 | 差异 |
|---|---|---|
| [SakuraByteCore/codexmate](https://github.com/SakuraByteCore/codexmate) | CLI + Web UI | 面更广（多 agent / 技能市场），但不是 Codex 插件形态 |
| [hereww/codextools](https://github.com/hereww/codextools) | Go 桌面控制台 | 靠 CDP 注入 ChatGPT 界面；**仓库无 LICENSE 文件**，不可再分发 |
| [lizhelang/codexbar](https://github.com/lizhelang/codexbar) | macOS 菜单栏 | 只做 macOS |
| [haixin-ye/Codex_provider_change](https://github.com/haixin-ye/Codex_provider_change) | 桌面应用 | 只修"历史会话不可见" |

本项目补的是**另外半边**：provider 与 model 的**配对**（2013 报错）、协议兼容性、
厂商下线公告核查、模型目录可见性管理。形态坚持**纯插件**：不注入、不代理、不改客户端。

### 致谢

- [codex-token](https://github.com/winwiny/codex-token)（原 `Forever5201/codex-token`）——
  切换逻辑的底层实现（MIT，`vendor/LICENSE.codex-token` 保留原始版权声明）

**License** MIT

---

<a id="english"></a>

## English

```
use deepseek, read the key from the clipboard without echoing it 
set reasoning effort to max
can I use this provider?
which models are deprecated?
```

### The four bugs it fixes

| Symptom | Root cause |
|---|---|
| `unknown model 'xxx'` (2013) | The desktop app writes the **model name but not the provider** |
| `Missing environment variable: XXX` | The desktop app is an MSIX package — it can't read env vars written by `setx` |
| Protocol errors when switching vendors | Since Codex 0.163 only `wire_api = "responses"` is supported |
| Dead entries in the model picker | The picker reads a local catalog that still lists retired models |

Each has a dedicated tool, and all of them were verified against real systems.

### Quick start

```bash
git clone https://github.com/Aston-x/codex-model-switcher.git
cd codex-model-switcher
bash install.sh            # use bash — no executable bit needed
```

```powershell
git clone https://github.com/Aston-x/codex-model-switcher.git
cd codex-model-switcher
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

No git needed either — download the zip and unpack it:

```bash
# https://github.com/Aston-x/codex-model-switcher/archive/refs/heads/main.zip
unzip codex-model-switcher-main.zip && cd codex-model-switcher-main && bash install.sh
```

**If GitHub is unreachable, use a mirror** (works with or without a local proxy):

```bash
git clone https://ghproxy.net/https://github.com/Aston-x/codex-model-switcher.git
# or
git clone https://gh-proxy.com/https://github.com/Aston-x/codex-model-switcher.git
```

The installer checks Python 3.11+, installs `mcp`, registers the marketplace,
installs the plugin, then runs `doctor.py --fix`.

**Fully quit and reopen** the Codex desktop app afterwards, then just say
"use deepseek" in a conversation.

Manual install:

```toml
# ~/.codex/config.toml
[marketplaces.codex-model-switcher]
source_type = "local"
source = '<repo directory>'

[plugins."model-switcher@codex-model-switcher"]
enabled = true
```

### Can't clone / can't run it

| Symptom | Cause and fix |
|---|---|
| `Failed to connect to github.com:443 over proxy 127.0.0.1` | git is configured for a local proxy that **isn't running**. Start it, or bypass: `git -c http.proxy= -c https.proxy= clone ...` |
| `Recv failure: Connection was reset` | Direct connections to GitHub get reset. **Use a mirror** (clone commands above), or the zip download |
| `Permission denied: ./install.sh` | The executable bit is lost when checking out on Windows. **Use `bash install.sh`** — no executable bit needed |
| `bad interpreter: /usr/bin/env bash^M` | The script got CRLF line endings. `git config core.autocrlf false` and re-clone, or use the zip |
| `Python 3.11+ not found` | 3.11+ is required (for `tomllib`). Install it and run again |
| `pip can't install mcp` | `externally-managed-environment`: create a venv, or `pip install --user mcp` |
| `codex command not found` | codex isn't on PATH — run the two `codex plugin ...` commands the script prints |

Check the proxy setting with:

```bash
git config --global --get http.proxy
```

### Passing keys: use the clipboard, never chat

Never pass an API key as chat text or as an MCP tool argument — it lands in the
conversation log and the tool log.

```python
setup_preset_from_clipboard("deepseek")   # reads the clipboard, wipes it right after
```

Flow: copy the key at the vendor's console → tell the agent only
"update DEEPSEEK_API_KEY from the clipboard" → the agent reads the local
clipboard, writes it straight into `config.toml`, then **wipes the clipboard** →
the output only reports length and connectivity, **never the key itself**.

If you already pasted a key into the conversation, **revoke it and issue a new
one** at the vendor's console.

### doctor — detect and repair the environment

```bash
python doctor.py            # diagnose only
python doctor.py --fix      # diagnose + repair
python doctor.py --json     # machine-readable
```

| Check | What it can fix |
|---|---|
| Python version | Reports a missing 3.11+ (needed for `tomllib`) |
| `mcp` package | `pip install mcp` |
| `.mcp.json` platform | Swaps in the Windows or POSIX variant |
| `config.toml` parses | Reports the TOML syntax error |
| `model_context_window` | String → integer (a string stops Codex from starting) |
| `model` / `model_provider` | Fixes the mismatch (the direct cause of 2013) |
| `model_catalog_json` | Creates it and registers it in `config.toml` |
| Provider key storage | Flags `env` mode, which the desktop app can't read |
| Plugin installed in cache | Reports a version mismatch |
| Autostart | Windows Run key / macOS LaunchAgent / Linux systemd user |
| Guard running | Detected via lock files |

> ⚠️ When doctor edits `config.toml`, Windows paths are written as **TOML literal
> strings** (single quotes) and the file is **re-parsed afterwards** — your config
> is never left broken.

### Tools (21)

| Tool | What it does |
|---|---|
| `list_providers` | Lists configured providers, their models, and the active default |
| `switch_model(provider_id, model)` | Switches the default model + provider |
| `add_provider(...)` | Adds / updates a custom provider |
| `delete_provider(provider_id)` | Removes a custom provider |
| `setup_preset(preset, api_key, ...)` | One-shot onboarding (prefer the clipboard variant) |
| `setup_preset_from_clipboard(preset, ...)` | One-shot onboarding, **key read from the clipboard and wiped right after** |
| `set_api_key(env_key_name, api_key, ...)` | Writes a single key (`inline` / `env`) |
| `set_api_key_from_clipboard(env_key_name, ...)` | Writes a single key **from the clipboard, wiped right after** |
| `discover_models(base_url, ...)` | Pulls the vendor's **real** `/v1/models` list and effort levels |
| `check_provider_deprecations(provider_id, models)` | Checks the vendor's **deprecation notices** |
| `check_provider_protocol(base_url, provider_id)` | Probes Responses-protocol support; with a key it sends a real request |
| `test_provider(provider_id, model)` | Sends a real request — "is it actually usable?" |
| `register_model_in_catalog(slug, ...)` | Registers a model in the desktop catalog |
| `list_catalog_models(include_hidden)` | Lists the catalog including hidden entries |
| `set_model_visibility(slug, visible)` | **Shows / hides** a model (entry is never deleted) |
| `sync_provider_models(provider_id, keep)` | Aligns the catalog with the live list; `keep` chooses which stay |
| `prune_stale_models(dry_run)` | Finds and hides models nobody serves anymore |
| `fix_provider_alignment(dry_run)` | Fixes model / provider mismatches |
| `list_model_capabilities(model)` | Real effort levels and context window of a model |
| `set_reasoning_effort(effort, model)` | Sets the effort level (validated) |
| `set_context_window(context_window, model)` | Sets the context window (validated) |

Presets: `deepseek / minimax / kimi / glm / mimo / siliconflow / dashscope / openrouter`

### Check deprecation notices before onboarding a vendor

`/v1/models` only tells you what still **works**, not what the vendor has
**announced it is retiring**.

Measured on 2026-10-25: MiniMax's docs already mark the whole **M2 / M2.1 / M2.5 /
M2.7** family as retired, yet its `/v1/models` **still returns those 9 ids** — relying
on `/v1/models` alone treats them all as live.

`setup_preset` checks the vendor's notices automatically; retired model names are
never written into the provider block nor registered in the catalog.

⚠️ **"Not found" ≠ "retired"**: when the vendor has no lineup page configured and
`/v1/models` is unreachable, the tool reports **"cannot determine"** and **leaves
the models alone**.

### Choosing which models stay in the picker

Each catalog entry has a `visibility` field with exactly two legal values:
`"list"` (shown in the picker) and `"hide"` (not shown — but the entry and its
registered effort levels / context window are all still there).

```python
set_model_visibility("gpt-6-astra", False)   # pull it out of the picker
sync_provider_models("minimax", keep="MiniMax-M3.1-Flash,MiniMax-M3")
prune_stale_models(dry_run=True)              # find models nobody serves
```

Sources of truth: the desktop app's live list `~/.codex/models_cache.json` plus each
provider's `/v1/models`. Models come back automatically when a vendor re-lists them.

### Which vendors can be used directly

Since Codex 0.163 only `wire_api = "responses"` works, so direct use depends on
whether the vendor implements `POST {base}/responses`:

| Vendor | `/v1/responses` | Direct? |
|---|---|---|
| DeepSeek | exists | ✅ verified |
| MiniMax | exists | ✅ verified |
| Xiaomi MiMo | exists | ✅ |
| OpenRouter | exists | ✅ 467 models, best aggregator |
| Alibaba DashScope | exists | ✅ |
| SiliconFlow | **404** | ❌ needs a Responses-compatible proxy |
| Zhipu GLM / Moonshot | 401 (blanket auth, undetectable) | ❌ chat-completions only, needs a proxy |

⚠️ **Keyless probing lies**: such vendors return the same 401 for *any* path, which
looks like "endpoint exists" but proves nothing. **Never read 401 as "supported"** —
with a key, `check_provider_protocol` sends a real request and settles it.

### Reasoning levels and context window must be real

Effort levels differ per model — never hardcode them:

- MiniMax M3.1 Flash: `low / medium / high / xhigh / max` (default max), 1,000,000 ctx
- MiniMax M3 / M2.x: `minimal / low / medium / high`
- DeepSeek V4: `low / high / max` (returned directly by `/v1/models`)

Order of truth: ① `discover_models` → ② vendor docs → ③ `list_model_capabilities`.
Levels only appear in the picker once registered in `model_catalog_json`.

### Cross-platform

| | Windows | macOS | Linux |
|---|---|---|---|
| MCP server launch | `cmd.exe` → `.cmd` | `bash` → `.sh` | `bash` → `.sh` |
| User env vars | `setx` | `launchctl setenv` + shell rc | `~/.config/environment.d` + `~/.profile` |
| Clipboard | Win32 API | `pbpaste` | `wl-paste` / `xclip` / `xsel` |
| Process enumeration | Win32 API (no child processes) | `ps` | `/proc` |
| Autostart | HKCU Run key | LaunchAgent | systemd user / XDG autostart |
| Silent start | `pythonw.exe` | `setsid` | `setsid` |

### Development and testing

```bash
python tests/smoke_test.py                                   # full chain in a fresh temp env
MODEL_SWITCHER_TEST_KEY=sk-xxx python tests/smoke_test.py   # with a real key (real requests)
```

The smoke test builds a "brand-new Codex machine" (empty `CODEX_HOME` + a minimal
`config.toml`) and runs: doctor → `--fix` → MCP tools → onboarding → **a real
request** → hide/show a model → align with the live list → deprecation check.

> ⚠️ **The Windows `.cmd` must be pure ASCII with CRLF**: `cmd.exe` reads BOM-less
> UTF-8 with the console code page (936/GBK here), and CJK comments break the block
> parser, so the MCP server never starts at all. See the comments inside
> [`scripts/launch_model_switcher.cmd`](scripts/launch_model_switcher.cmd).

> ⚠️ **vendor is patched, not pristine**: see [`vendor/PATCHES.md`](vendor/PATCHES.md).

### Related projects

| Project | Shape | Difference |
|---|---|---|
| [SakuraByteCore/codexmate](https://github.com/SakuraByteCore/codexmate) | CLI + Web UI | Broader (multi-agent, skills market), but not a Codex plugin |
| [hereww/codextools](https://github.com/hereww/codextools) | Go desktop console | Injects into the ChatGPT UI over CDP; **no LICENSE file** — not redistributable |
| [lizhelang/codexbar](https://github.com/lizhelang/codexbar) | macOS menu bar | macOS only |
| [haixin-ye/Codex_provider_change](https://github.com/haixin-ye/Codex_provider_change) | Desktop app | Only restores invisible history |

This project covers the **other half**: model ↔ provider pairing (the 2013 error),
protocol compatibility, vendor deprecation notices, and catalog visibility.
It stays a **pure plugin**: no injection, no proxy, no client patching.

### Credits

- [codex-token](https://github.com/winwiny/codex-token) (formerly `Forever5201/codex-token`) —
  the underlying switch logic (MIT; original copyright kept in
  `vendor/LICENSE.codex-token`)

**License** MIT
