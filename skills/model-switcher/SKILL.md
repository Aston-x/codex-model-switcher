---
name: model-switcher
description: "管理 Codex 的模型来源与运行参数：填 API key、一键接入 DeepSeek/MiniMax/Kimi/智谱GLM/硅基流动/百炼/OpenRouter、切换当前模型、修正 provider 错配、设置推理等级与上下文长度、探测厂商协议并实测连通性、自选哪些模型留在选择器里、清理已下架模型。当用户说『填一下 API key』『接上 Kimi/DeepSeek/GLM/MiniMax/OpenRouter』『换成 XX 模型』『现在用的什么模型』『有哪些模型可选』『推理等级调到 xhigh』『上下文调到 1M』『换个模型后报错』『这家能不能接』『把下架模型清掉』『只留这几个模型』时使用。"
---

# Codex 模型切换器

管理 Codex 的**模型来源**与**运行参数**。底层复用现成项目
**codex-token**（`winwiny/codex-token`，原 `Forever5201/codex-token`）的 `switcher.py`，
本插件做 MCP 包装，
并补上它没有的几项能力：一键接入预设、自动发现真实模型列表、
探测厂商协议并实测连通性、自选哪些模型留在选择器里、清理下架模型、
修正 provider 错配、按模型真实档位设置推理等级与上下文长度。

## 用户怎么说 → 调哪个工具

| 用户的话 | 调这个 |
|---|---|
| 「用 Kimi，key 是 sk-xxx」「接一下智谱 GLM」 | `setup_preset(preset, api_key)` |
| 「把 MOONSHOT_API_KEY 设成 sk-xxx」 | `set_api_key(env_key_name, api_key, storage_mode, provider_id)` |
| 「换成 deepseek-v4-pro」 | `switch_model("deepseek", "deepseek-v4-pro")` |
| 「现在用的什么模型」 | `list_providers()` |
| 「Kimi 有哪些模型」 | `discover_models(base_url, env_key=...)` |
| 「这家能不能接」「支持吗」「会不会协议报错」 | `check_provider_protocol(base_url, provider_id=...)` |
| 「接上了真的能用吗」「发个请求试试」 | `test_provider(provider_id, model)` |
| **「换个模型后报错」「unknown model」** | **`fix_provider_alignment()`** —— 见下方「换模型报错的根因」 |
| 「推理等级调到 xhigh」「用 max 档」 | `set_reasoning_effort(effort, model)` |
| 「上下文调到 1M」 | `set_context_window(context_window, model)` |
| 「这个模型有哪些档位 / 上下文多大」 | `list_model_capabilities(model)` |
| 「模型列表太乱了」「把下架的摘掉」 | `prune_stale_models(dry_run)` |
| 「只留这几个模型」「其他的别显示」 | `sync_provider_models(provider_id, keep="a,b")` |
| 「把这个模型藏起来 / 放回来」 | `set_model_visibility(slug, visible)` |

### `setup_preset` —— 最常用的一步到位

preset 取值：**deepseek / minimax / kimi / glm / siliconflow / moonshot / dashscope / openrouter**

它一次做完五件事：
1. `setx` 写用户级环境变量（供 CLI 使用）
2. 写 `[model_providers.<id>]` 块，**默认 `storage_mode="inline"`**（key 内联进 config.toml）
3. **拉厂商 `/v1/models` 拿真实在售列表**（预设里的模型名只是兜底，厂商上下架很频繁）
4. 按真实在售列表登记到桌面版模型目录 `model_catalog_json`
   （不做这步，桌面版会把顶层 `model` 写回别的模型 → provider/model 组合非法 → 请求必挂），
   并把该 provider 下**已被厂商下架**的模型从选择器里隐藏
5. 设为默认

**做完必须提醒用户：完全退出并重开 Codex 桌面版。**
**然后建议再跑一次 `test_provider(provider_id, model)` 确认真能发请求。**

## ⚠️ 换模型报错的根因（2026-10-21 实测定位）

有两个**互相独立**的坑，都会表现为「换了模型就报错」：

### 坑 1：桌面版读不到用户级环境变量 → `Missing environment variable: XXX`

Codex 桌面版是 **MSIX 应用**，由 shell 激活，继承的是 **explorer.exe 的环境快照**。
explorer 登录后不再刷新，`setx` 写入的变量**传不进已在运行的桌面版进程**。
实测：explorer 启动于 10/18，`setx` 写于 10/21，桌面版进程里两个 key 全是 `<无>`。

⇒ **桌面版必须用 `storage_mode="inline"`**（key 明文写进 config.toml 的
`experimental_bearer_token`）。CLI 场景 `env` / `inline` 都行。
⇒ 若已经用了 `env` 模式且桌面版报这个错，调用
`set_api_key(env_key_name, api_key, storage_mode="inline", provider_id=...)` 改过来。

### 坑 2：桌面版换模型只写 `model`、不写 `model_provider` → `unknown model 'xxx' (2013)`

桌面版模型选择器**只改模型名**，provider 沿用旧值。全局状态里模型配置只有
`{model, reasoningEffort, serviceTier}`，**结构上就没有 provider 字段**；
桌面版**也不改 config.toml**（它只写自己的全局状态 + 线程记录），
自己的 app-server 同样不重读 config.toml 的 provider。

换了模型后，请求用**新模型名**打到**旧 provider 端点**：
选 deepseek-flash 但 provider 还是 minimax → MiniMax 端点不认识这个模型名 →
`invalid params, code: 2013, unknown model 'deepseek-flash'`。

实测证据（2026-10-21）：
| 来源 | model | provider | 结果 |
|---|---|---|---|
| CLI / `codex exec` | deepseek-v4-pro | deepseek | 正常 |
| 桌面版 UI 下拉框 | deepseek-flash | **minimax** | 报 2013 |

⇒ 插件的 MCP server 启动后会跑一个**后台守护线程**，盯着全局状态里
  「最近选中的模型」，一旦它与 config.toml 的 model 不一致，
  就按归属表把 `model` + `model_provider` 一起改对 ——
  这样**新建的会话**继承到的一定是配对正确的配置。
⇒ 已在运行的会话仍带旧 provider（存在桌面版内存里），
  那些会话要在桌面版里**重开**才生效。
⇒ 也可以手动调 `fix_provider_alignment()`：它会同时修 config.toml
  （影响新会话）和 state_5.sqlite 里的会话记录（影响已存在会话）。
  ⚠️ 但桌面版把 provider 缓存在内存里，
  **直接改数据库对已开着的会话无效**（实测改完仍报 2013）。

## 推理等级与上下文长度

- **档位必须按模型真实支持值给**，不能写死。`list_model_capabilities(model)` 列出
  某模型在桌面版模型目录里登记的 `supported_reasoning_levels` 与 `context_window`。
- 档位来源优先级：① 厂商 `/v1/models` 返回的 `effort.supported_levels`
  （DeepSeek 直接返回）→ ② 厂商官方文档（如 MiniMax M3.1 Flash 是
  low/medium/high/xhigh/max 五档）→ ③ 桌面版模型目录里已登记的值。
- `set_reasoning_effort` 会**校验档位**是否被该模型声明支持，不支持就报错并列出可用档位。
- `set_context_window` 会校验是否超出该模型的 `max_context_window`。
- ⚠️ `model_context_window` 在 config.toml 里是**整数**，写成带引号的字符串会直接导致
  `Error loading config.toml: invalid type: string "1000000", expected i64`。

## 关键事实（别再踩）

- **`wire_api` 只能是 `"responses"`**：Codex 0.163 起已移除 `"chat"`，填了直接报
  `is no longer supported`。
  ⇒ **能不能直连，取决于厂商有没有实现 `POST {base}/responses`**。实测结论：

  | 厂商 | `/v1/responses` | 能否直连 |
  |---|---|---|
  | DeepSeek | 存在 | ✅ 实测可用 |
  | MiniMax | 存在 | ✅ 实测可用 |
  | OpenRouter | 存在 | ✅（400+ 模型，聚合首选） |
  | 阿里百炼 DashScope | 存在 | ✅ |
  | 硅基流动 SiliconFlow | **404** | ❌ 只能走 Responses 兼容代理 |
  | 智谱 GLM / Moonshot | 401（统一鉴权，测不出） | ❌ 官方只提供 Chat Completions，需代理 |

  ⇒ 不确定就先 `check_provider_protocol(base_url)`；
    **有密钥时它会直接真发一次请求给确定结论**（`provider_id=...`）。
- **⚠️ 无密钥探测会骗人**：智谱 / Moonshot / MiniMax 这类厂商对**任何路径**
  （连不存在的路径）都先返回同一个 401 错误体，看着像「端点存在」，
  其实证明不了。**别把 401 当成「支持」**。
- **模型名不要照抄预设**：各家会改名、也会下架。先 `discover_models(base_url, env_key=...)`
  拉 `/v1/models` 拿**真实** id 再写。
  （实测踩过：第三方预设写 `deepseek-v4-flash`，官方实际是 `deepseek-flash`；
  Kimi 主力是 `kimi-k3` 而非 K2.7；`moonshot-v1-*` 系列 2026-08-31 下线。）
- **key 放哪**：桌面版必须 `inline`（见坑 1）；CLI 两种都行。
  `inline` 的代价是 key 明文躺在 config.toml 里（可能被分享/提交 git），
  需要兼顾时可两种都写：inline 保证桌面版可用，env 供 CLI 使用。
- **桌面版会抢写 config.toml 顶层 `model`**：它把 UI 里选中的模型写回去。
  目标模型不在 `model_catalog_json` 里 → 写回别的模型 → 请求必挂。
  所以任何新模型都要先 `register_model_in_catalog`。
- **模型目录里的 `visibility` 只有 `"list"` / `"hide"` 两个值**：
  `"hide"` 的条目不会出现在模型选择器里，但**条目和已登记的档位/上下文都还在**。
  ⇒ 想清理下架模型就隐藏，别删除（删了要重填档位；模型重新上架时能自动放回）。
  ⇒ `prune_stale_models()` 会拿「桌面版实时清单 `models_cache.json`」和
  「各厂商 `/v1/models`」当真相源，把没人提供的模型标出来。
- **桌面版不管上下文长度**：它的 `thread_settings` 里只有 model / provider /
  reasoning_effort，没有 context 字段。上下文只能靠 `model_context_window` 或
  桌面版的「自动上下文」开关（`composer-auto-context-enabled`）。
- **改 config.toml 后已开着的会话不会变**：模型存在数据库（`state_5.sqlite` 的
  `threads.model_provider`），且桌面版会缓存设置。新会话才生效。
- **不要回显 key 明文**：工具只返回长度。
