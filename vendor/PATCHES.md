# vendor/ 本地补丁说明

`vendor/switcher.py` 来自 **[codex-token](https://github.com/winwiny/codex-token)**
（仓库原名 `Forever5201/codex-token`），MIT License，原始版权声明见
`vendor/LICENSE.codex-token`。

本项目**没有**把上游原样搬进来 —— 在它基础上打了少量补丁，
目的是**不要把已下线的模型名写进用户的配置**。补丁清单如下：

| 位置 | 改动 | 原因 |
|---|---|---|
| `DEFAULT_PRESETS` → `deepseek` | `deepseek-v4-flash` → `deepseek-flash` | 官方 Models & Pricing 页写明 legacy 名 `deepseek-v4-flash` 与 `deepseek-v4-flash-vision-exp` 已 retired（仍能调用，但会被自动路由到 V4.1-Flash 并按 Flash 价计费） |
| `DEFAULT_PRESETS` → `kimi` | 去掉 `kimi-k2.5` 与 `moonshot-v1-128k`，加入 `kimi-k3` | moonshot-v1-* 与 kimi-k2.5 已于 2026-08-31 下线；kimi-k3 是 2026-07 发布的旗舰（1M 上下文） |
| `DEFAULT_PRESETS` → 新增 `mimo` | 小米 MiMo 预设 | `mimo-v2.5` / `mimo-v2.5-pro` 已于 2026-10-21 正式下线，只留 v2.6 系列 |
| `DEFAULT_PRESETS` → `glm` | 默认模型 `glm-4.6` → `glm-4.7`，去掉 `glm-5.1` / `glm-5.2` | GLM-5.1 / GLM-5.2 只在第三方聚合平台（SiliconFlow 的 `zai-org/*`）见过，bigmodel 官方文档未确认 |

## 许可证要求

MIT 允许修改与再分发，但必须保留原始版权与许可声明 ——
`vendor/LICENSE.codex-token` 是**原封不动**的上游许可证全文，
本文件额外说明本地补丁，两者共同构成本目录的许可声明。

## 上游更新后怎么办

本文件是补丁的**唯一台账**。从上游拉新版本覆盖 `vendor/switcher.py` 时：

1. 先看本文件的补丁表，把对应改动**重新应用**（不要靠 git 冲突自动合并）；
2. 跑 `python tests/smoke_test.py` 确认预设与工具链仍然正常；
3. 更新本文件里的「改动原因」——厂商模型上下架是持续变化的，
   写明**核对日期**，方便以后判断补丁是否过期。
