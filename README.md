# codex-model-switcher

**Codex 模型切换器** —— 纯插件形态（MCP + Skill）的模型来源与运行参数管理工具。

不注入桌面版、不起代理、不改客户端二进制。

在 Codex 对话里直接说「用 deepseek，key 是 sk-xxx」「换成 XX 模型」
「推理等级调到 max」「这家能不能接」「哪些模型下线了」「把下架模型清掉」就能完成。

---

## 为什么需要它

Codex 桌面版的模型管理有**四个结构性的坑**，都不是"配置写错"能解释的：

| # | 现象 | 根因 |
|---|---|---|
| 1 | `unknown model 'xxx'`（code 2013） | 桌面版模型选择器**只写模型名、不写 provider**。全局状态里模型配置只有 `{model, reasoningEffort, serviceTier}`，**结构上就没有 provider 字段** |
| 2 | `Missing environment variable: XXX` | 桌面版是 **MSIX 应用**，继承 explorer.exe 的环境快照，`setx` 写进去传不进已运行的进程 |
| 3 | 换了厂商就报协议错 | Codex 0.163 起只认 `wire_api = "responses"`（旧的 `"chat"` 已被移除），厂商必须有 `POST {base}/responses` |
| 4 | 模型列表里有一堆点不动 | 桌面版选择器读**本地目录**，厂商下架了模型但目录还留着 |

这四件事本插件都有对应的工具，而且**都做过实测**，不是照抄文档猜的。

## 快速开始

### macOS / Linux

```bash
git clone https://github.com/Aston-x/codex-model-switcher.git
cd codex-model-switcher
./install.sh
```

### Windows

```powershell
git clone https://github.com/Aston-x/codex-model-switcher.git
cd codex-model-switcher
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

安装脚本会做四件事：检查 Python 3.11+ → 装 `mcp` 依赖 →
注册 Codex 本地 marketplace 并安装插件 → 跑 `doctor.py --fix` 自动体检 + 修环境。

装完**完全退出并重开 Codex 桌面版**，然后在对话里说：

```
用 deepseek，key 是 sk-xxx
```

### 手动安装

```toml
# ~/.codex/config.toml
[marketplaces.codex-model-switcher]
source_type = "local"
source = '<仓库目录>'

[plugins."model-switcher@codex-model-switcher"]
enabled = true
```

## doctor —— 自动识别环境并修

换一台机器、换个系统、升级了 Codex，很多问题会冒出来。
`doctor.py` 会把环境体检一遍并**自动修好能修的**：

```bash
python doctor.py            # 只体检，列出问题（不改任何东西）
python doctor.py --fix      # 体检 + 自动修
python doctor.py --json     # 机器可读输出
```

它检查并修这些：

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
> 写完还会**回读校验**——绝不会把你的配置改坏。

## 工具一览（19 个）

| 工具 | 作用 |
|---|---|
| `list_providers` | 列出已配置的 provider、每个可选模型、当前生效的默认模型 |
| `switch_model(provider_id, model)` | 切换默认模型 + provider |
| `add_provider(...)` | 新增/更新自定义 provider |
| `delete_provider(provider_id)` | 删除自定义 provider |
| `setup_preset(preset, api_key, ...)` | 一键接入：写 key → 建 provider → **查厂商下线公告** → 拉厂商真实在售列表 → 登记模型目录 → 设为默认 |
| `set_api_key(env_key_name, api_key, storage_mode, provider_id)` | 单独写 key（`inline` 或 `env`） |
| `discover_models(base_url, ...)` | 拉厂商 `/v1/models` 拿**真实**在售模型与档位 |
| `check_provider_deprecations(provider_id, models)` | **查厂商官网的下线公告**——`/v1/models` 只说明"还能调到"，公告才说明"厂商已宣布要下线" |
| `check_provider_protocol(base_url, provider_id)` | 探测厂商支不支持 Responses 协议；**有密钥时直接真发一次请求**给确定结论 |
| `test_provider(provider_id, model)` | 用已配置的 key 真发一次请求，验证"接上了到底能不能用" |
| `register_model_in_catalog(slug, ...)` | 把模型登记进桌面版模型目录 |
| `list_catalog_models(include_hidden)` | 列出桌面版模型目录（含已隐藏的） |
| `set_model_visibility(slug, visible)` | **显示 / 隐藏**单个模型（不删除条目） |
| `sync_provider_models(provider_id, keep)` | 以厂商在售列表为准对齐目录；`keep` 自选留哪些 |
| `prune_stale_models(dry_run)` | 找出并隐藏**已经没人提供**的模型 |
| `fix_provider_alignment(dry_run)` | 修正 model 与 model_provider 的错配 |
| `list_model_capabilities(model)` | 查某模型**真实支持**的推理档位与上下文 |
| `set_reasoning_effort(effort, model)` | 设置推理等级（会校验档位） |
| `set_context_window(context_window, model)` | 设置上下文长度（会校验上限） |

preset 取值：`deepseek / minimax / kimi / glm / mimo / siliconflow / dashscope / openrouter`

## 接厂商前先查下线公告（重要）

`/v1/models` 只说明"**现在还能调到**"，不说明"**厂商已经宣布要下线**"。
这两件事会差好几周甚至几个月，差到用户手上就是"模型选择器里点了就挂"。

实测（2026-10-25）：MiniMax 官网已把 **M2 / M2.1 / M2.5 / M2.7 全系列**标记「已下线」，
但它的 `/v1/models` **仍然返回这 9 个 id** —— 只靠 `/v1/models` 会把它们全当成在售。

```python
check_provider_deprecations("minimax")
# 厂商在售列表（/v1/models，已取到）：MiniMax-M3.1-Flash, MiniMax-M3, MiniMax-M2.7, ...
# 官网公告点名已退役的模型（全部）：MiniMax-M2, MiniMax-M2.1, ..., MiniMax-M2.7-highspeed
#   - MiniMax-M3.1-Flash  在售
#   - MiniMax-M3          在售
```

`setup_preset` 会自动做这一步，已下线的模型名**不会**被写进 provider 块、
**不会**被登记进模型目录。

⚠️ **「查不到」不等于「已下线」**：没配官网在售页、又拿不到 `/v1/models` 时，
工具会给「无法判定」并**保留模型不动**（2026-10-25 修掉的误报——
旧逻辑会把这类厂商的**全部**模型误报成已下线，导致 `setup_preset` 剔光后直接抛错）。

## 自选哪些模型留在列表里

桌面版模型目录里每个条目的 `visibility` 字段只有两个合法值：

- `"list"` —— 出现在模型选择器里
- `"hide"` —— 不出现在选择器里，**但条目和已登记的档位/上下文都还在**

所以清理下架模型的方式是**隐藏**，不是删除：

```python
set_model_visibility("gpt-6-astra", False)   # 从选择器里摘掉
sync_provider_models("minimax", keep="MiniMax-M3.1-Flash,MiniMax-M3")
prune_stale_models(dry_run=True)              # 找出没人提供的模型
```

`prune_stale_models` 的真相来源有两个：

1. 桌面版自己下发的实时清单 `~/.codex/models_cache.json`（带 `etag`）
2. 各 provider 的 `/v1/models`（第三方模型的在售清单）

模型重新上架时，`sync_provider_models` 会自动把它放回列表，已登记的推理档位
与上下文长度不会丢。

## 哪些厂商能直连

**Codex 0.163+ 只认 `wire_api = "responses"`**（旧的 `"chat"` 已被移除），
所以能不能直连完全取决于厂商有没有实现 `POST {base}/responses`：

| 厂商 | `/v1/responses` | 能否直连 |
|---|---|---|
| DeepSeek | 存在 | ✅ 实测可用 |
| MiniMax | 存在 | ✅ 实测可用 |
| 小米 MiMo | 存在 | ✅（官方有 Codex 配置指南） |
| OpenRouter | 存在 | ✅ 467 个模型，聚合首选 |
| 阿里百炼 DashScope | 存在 | ✅ |
| 硅基流动 SiliconFlow | **404** | ❌ 需 Responses 兼容代理 |
| 智谱 GLM / Moonshot | 401（统一鉴权，测不出） | ❌ 官方只提供 Chat Completions，需代理 |

⚠️ **无密钥探测会骗人**：智谱 / Moonshot / MiniMax 这类厂商对**任何路径**
（连不存在的路径）都先返回同一个 401 错误体，看着像"端点存在"，其实证明不了。
**别把 401 当成"支持"** —— 有密钥时 `check_provider_protocol` 会直接实测给结论。

## 推理等级与上下文长度要按真实值填

**不要写死档位** —— 不同模型支持的档位不一样：

- MiniMax M3.1 Flash：`low / medium / high / xhigh / max`（默认 max），上下文 1,000,000
- MiniMax M3 / M2.x：`minimal / low / medium / high`
- DeepSeek V4：`low / high / max`（`/v1/models` 直接返回 `effort.supported_levels`）

拿真实值的顺序：

1. `discover_models(base_url, ...)` —— 厂商 `/v1/models` 有些会直接返回
   `effort.supported_levels` 和 `context_window`，**这是最权威的**
2. 厂商官方文档
3. `list_model_capabilities(model)` —— 看桌面版模型目录里已登记的值

档位登记到桌面版模型目录（`model_catalog_json`）后，**模型选择器里才会显示这些档位**。
不登记的后果：桌面版把顶层 `model` 写回别的模型 → provider/model 组合非法 → 请求必挂。

## 跨平台

| | Windows | macOS | Linux |
|---|---|---|---|
| MCP server 启动 | `cmd.exe` → `.cmd` | `bash` → `.sh` | `bash` → `.sh` |
| 用户级环境变量 | `setx` | `launchctl setenv` + shell rc | `~/.config/environment.d` + `~/.profile` |
| 进程枚举 | Win32 API（零子进程） | `ps` | `/proc` |
| 开机自启 | HKCU Run 键 | LaunchAgent | systemd user / XDG autostart |
| 静默启动 | `pythonw.exe` | `setsid` 脱离终端 | `setsid` 脱离终端 |

`.mcp.json` 按平台分两份模板（`.mcp.windows.json` / `.mcp.unix.json`），
`doctor.py` 会自动挑对应的那份写进插件目录。

## 开发与测试

```bash
# 在一个全新的临时 CODEX_HOME 里跑完整链路（不碰你的真实配置）
python tests/smoke_test.py

# 带真 key 跑（含真实请求）
MODEL_SWITCHER_TEST_KEY=sk-xxx python tests/smoke_test.py
```

冒烟测试会构造一台"刚装好 Codex 的机器"（空 `CODEX_HOME` + 最简 `config.toml`），
然后跑：doctor 体检 → doctor --fix → MCP 工具 → 接入 provider → **真发一次请求**
→ 隐藏/放回模型 → 按厂商在售列表对齐 → **下线公告核查（含误报回归）**。

## 目录结构

```
.
├── .agents/plugins/marketplace.json   # marketplace 清单（仓库根即 marketplace 根）
├── .codex-plugin/plugin.json          # 插件清单
├── .mcp.json / .mcp.windows.json / .mcp.unix.json
├── LICENSE
├── README.md
├── doctor.py                          # 环境体检 + 自动修复
├── platform_compat.py                 # 跨平台适配层
├── server.py                          # 19 个 MCP 工具
├── install.sh / install.ps1           # 一键安装
├── scripts/
│   ├── guard.py                       # provider 配对守护（跟随 Codex 生命周期）
│   ├── launch_guard.py                # 生命周期看护
│   ├── launch_model_switcher.cmd      # MCP server 启动器（Windows）
│   └── launch_model_switcher.sh       # MCP server 启动器（POSIX）
├── skills/model-switcher/
│   ├── SKILL.md                       # 给 agent 看的使用说明
│   └── agents/openai.yaml
├── tests/smoke_test.py                # 全新环境冒烟测试
└── vendor/
    ├── switcher.py                    # 来自 codex-token (MIT)
    ├── LICENSE.codex-token            # 上游许可证全文（原封不动）
    └── PATCHES.md                     # 本地补丁台账
```

### ⚠️ Windows 的 `.cmd` 必须是纯 ASCII + CRLF

`scripts/launch_model_switcher.cmd` 里**不能有任何非 ASCII 字符**（注释也不行）。
原因：`cmd.exe` 用**控制台代码页**读无 BOM 的 UTF-8 文件，
本机代码页是 **936（GBK）** —— CJK 注释会被拆成乱码字节，
其中若含 `(`、`)`、`&&` 还会直接破坏块解析，**MCP server 根本起不来**
（表现为「插件装了但一个工具都没有」）。

同源的两个设置也要注意：

- `set "PYTHONUTF8=1"` / `set "PYTHONIOENCODING=utf-8"` ——
  Python 编码管道输出时用的是 ANSI 代码页，而 **MCP 协议要求 UTF-8**。
- 所有面向控制台的脚本（`doctor.py`、`tests/smoke_test.py`）都要先
  `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` ——
  中文 Windows 控制台是 GBK，打印 `✓` 这类字符会 `UnicodeEncodeError` 直接崩掉。

### vendor 不是原样的

`vendor/switcher.py` 在上游基础上打了补丁（把已下线的模型名从预设里去掉等）。
补丁清单与许可证要求见 **[vendor/PATCHES.md](vendor/PATCHES.md)** ——
MIT 允许修改，但必须保留原始版权声明，`vendor/LICENSE.codex-token` 是原封不动的上游全文。

## 和其它项目的关系

同类项目不少，但路线不同：

| 项目 | 形态 | 差异 |
|---|---|---|
| [SakuraByteCore/codexmate](https://github.com/SakuraByteCore/codexmate) | CLI + Web UI，聚合多 agent | 面更广（多 agent / 技能市场 / 任务编排），但不是 Codex 插件形态 |
| [hereww/codextools](https://github.com/hereww/codextools) | Go 桌面控制台 | 通过 CDP 注入 ChatGPT 界面改行为；**仓库无 LICENSE 文件**，不可再分发 |
| [lizhelang/codexbar](https://github.com/lizhelang/codexbar) | macOS 菜单栏 | 只做 macOS |
| [Roxy-ljy/codex-cc-switch-history-sync](https://github.com/Roxy-ljy/codex-cc-switch-history-sync) | PowerShell 脚本 | 只解决"切 provider 后历史会话不可见" |
| [haixin-ye/Codex_provider_change](https://github.com/haixin-ye/Codex_provider_change) | 桌面应用 | 同上，只修历史可见性 |

本项目补的是**另外半边**：provider 与 model 的**配对**（2013 报错）、
协议兼容性（能不能直连）、厂商下线公告核查、以及模型目录的可见性管理（哪些还活着）。
形态上坚持**纯插件**：不注入、不代理、不改客户端二进制。

## 致谢

- [codex-token](https://github.com/winwiny/codex-token)（原 `Forever5201/codex-token`）——
  切换逻辑的底层实现（MIT，`vendor/LICENSE.codex-token` 保留了原始版权声明）

## License

MIT
