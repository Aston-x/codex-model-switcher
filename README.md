# model-switcher

在 **Codex 桌面版 / CLI** 里直接管理模型来源与运行参数：接 API key、切模型、
修 provider 错配、探测厂商协议、实测连通性、按模型真实档位调推理等级与上下文长度，
以及**自选哪些模型留在模型选择器里**。

纯插件形态（MCP + Skill），**不注入桌面版、不起代理、不改客户端**。

底层复用 [codex-token](https://github.com/winwiny/codex-token)
的 `switcher.py`（MIT，已 vendor 进 `vendor/`），本插件做 MCP 包装，
并补上它没有的几项能力。

## 它解决什么问题

Codex 桌面版的模型管理有三个**结构性的坑**，都不是"配置写错"能解释的：

1. **换模型只写模型名、不写 provider** → `unknown model 'xxx' (2013)`
   桌面版模型选择器**只改模型名**，provider 沿用旧值；全局状态里模型配置只有
   `{model, reasoningEffort, serviceTier}`，**结构上就没有 provider 字段**。
2. **读不到用户级环境变量** → `Missing environment variable: XXX`
   桌面版是 MSIX 应用，继承 explorer.exe 的环境快照，`setx` 写进去传不进去。
3. **只能接实现了 Responses 协议的厂商** → 换厂商就报协议错
   Codex 0.163 起已移除 `wire_api = "chat"`，厂商必须有 `POST {base}/responses`。

另外，厂商上下架模型很频繁，而桌面版模型选择器读的是**本地目录**——
目录里留着已下架的模型，用户一点就报错。

这四件事本插件都做了对应的工具，并且**都做过实测**，不是照抄文档猜的。

## 安装

把 `plugins/model-switcher/` 放进你的 Codex 本地 marketplace，然后：

```toml
# ~/.codex/config.toml
[marketplaces.local-personal]
source_type = "local"
source = '<你的 marketplace 目录>'

[plugins."model-switcher@local-personal"]
enabled = true
```

重启 Codex 桌面版即可。

Python 3.11+（需要 `tomllib`），依赖 `mcp` 包。

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
| `check_provider_protocol(base_url, provider_id)` | 探测厂商支不支持 Responses 协议；有密钥时**直接真发一次请求**给确定结论 |
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
**别把 401 当成"支持"。**

不确定就先 `check_provider_protocol(base_url)`；
**有密钥时它会直接真发一次请求给确定结论**（传 `provider_id`）。

## 三个已知坑（都在工具里做了防护）

### 1. 桌面版读不到用户级环境变量

Codex 桌面版是 **MSIX 应用**，由 shell 激活，继承的是 **explorer.exe 的环境快照**。
explorer 登录后不再刷新，`setx` 写入的变量**传不进已在运行的桌面版进程**。
症状：请求报 `Missing environment variable: XXX`。

⇒ 桌面版请用 `storage_mode="inline"`（key 内联进 config.toml）。
CLI 场景 `env` / `inline` 都行。

### 2. 桌面版换模型只写 `model`、不写 `model_provider`

桌面版模型选择器**只改模型名**，provider 沿用旧值。全局状态里模型配置只有
`{model, reasoningEffort, serviceTier}`，**结构上就没有 provider 字段**；
桌面版也**不改 `config.toml`**（只写自己的全局状态 + 线程记录），
自己的 app-server 同样不重读 config.toml 的 provider。

换到别的 provider 的模型后，请求用**新模型名**打到**旧端点**：

```
invalid params, code: 2013, unknown model 'deepseek-flash'
```

**本插件的处理**：MCP server 启动后会跑一个后台守护线程，盯着全局状态里
「最近选中的模型」，一旦它与 `config.toml` 的 `model` 不一致，
就按归属表把 `model` + `model_provider` 一起改对 ——
**新建的会话**继承到的一定是配对正确的配置。

已在运行的会话仍带旧 provider（存在桌面版内存里），需要**重开**才生效。
`fix_provider_alignment()` 也能手动修（config.toml + `state_5.sqlite`），
但桌面版把 provider 缓存在内存里，**直接改数据库对已开着的会话无效**。

### 3. `model_context_window` 必须是整数

写成带引号的字符串会直接让 Codex 起不来：

```
Error loading config.toml: invalid type: string "1000000", expected i64
```

⇒ `set_context_window` 写的是不带引号的整数。

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
协议兼容性（能不能直连）、以及模型目录的可见性管理（哪些还活着）。
形态上坚持**纯插件**：不注入、不代理、不改客户端二进制。

## 开发者

```
plugins/model-switcher/
├── .codex-plugin/plugin.json   # 插件清单
├── .mcp.json                   # MCP server 配置
├── server.py                   # 工具实现（19 个 MCP 工具）
├── doctor.py                   # 环境体检 + 自动修复
├── platform_compat.py          # 跨平台适配层
├── LICENSE
├── scripts/
│   ├── guard.py                # provider 配对守护（跟随 Codex 生命周期）
│   ├── launch_guard.py         # 生命周期看护 + 开机自启
│   ├── launch_model_switcher.cmd   # MCP server 启动器（Windows）
│   └── launch_model_switcher.sh    # MCP server 启动器（POSIX）
├── skills/model-switcher/
│   ├── SKILL.md                # 给 agent 看的使用说明
│   └── agents/openai.yaml
└── vendor/
    ├── switcher.py             # 来自 codex-token (MIT)
    ├── LICENSE.codex-token     # 上游许可证全文（原封不动）
    └── PATCHES.md              # 本地补丁台账
```

`switcher.py` 的位置按以下顺序自动探测：
`CODEX_SWITCHER_HOME` 环境变量 → 插件目录 `vendor/` → 同级 `codex-token/` → 用户主目录 `codex-token/`。

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
补丁清单与许可证要求见 **[vendor/PATCHES.md](vendor/PATCHES.md)**。

## 致谢

- [codex-token](https://github.com/winwiny/codex-token)（原 `Forever5201/codex-token`）——
  切换逻辑的底层实现（MIT，`vendor/LICENSE.codex-token` 保留了原始版权声明）

## License

MIT
