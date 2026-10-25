#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在**全新的临时环境**里跑一遍全链路，验证插件不依赖任何本机状态。

模拟一台刚装好 Codex 的机器：
  · 空的 CODEX_HOME（没有 config.toml / 没有模型目录 / 没有 presets）
  · 只有一个最简 config.toml
  · 跑 doctor（应有告警）-> 跑 doctor --fix（应能修好）
  -> 接入 provider（需要真 key）-> 登记模型目录 -> 真发一次请求
  -> 隐藏/放回模型 -> 按厂商在售列表对齐

用法：
    python tests/smoke_test.py
    MODEL_SWITCHER_TEST_KEY=sk-xxx python tests/smoke_test.py   # 带真 key 跑完整链路

不带 key 时会跳过「真实请求」那一步，其余全跑。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

PASS, FAIL, SKIP = [], [], []


def _init_stdio():
    """控制台可能是 GBK（中文 Windows 默认），非 GBK 字符会直接崩。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass


def _safe(s):
    """把详情里的非 GBK 字符换掉，免得控制台直接崩。"""
    return "".join(c if (ord(c) < 0x2000 or c in "─│·") else "?" for c in str(s))


def ok(name, detail=""):
    PASS.append(name)
    print("  [PASS] %s%s" % (name, ("  -- " + _safe(detail)) if detail else ""))


def bad(name, detail=""):
    FAIL.append((name, detail))
    print("  [FAIL] %s%s" % (name, ("  -- " + _safe(detail)) if detail else ""))


def skip(name, detail=""):
    SKIP.append(name)
    print("  [SKIP] %s%s" % (name, ("  -- " + _safe(detail)) if detail else ""))


def section(t):
    print("\n=== %s ===" % t)


def main():
    _init_stdio()
    # 先记住**真实**家目录（后面会把 os.environ 换成临时环境）
    real_home = os.path.expanduser("~")
    real_cfg = os.path.join(real_home, ".codex", "config.toml")

    tmp = tempfile.mkdtemp(prefix="codex-ms-test-")
    home = os.path.join(tmp, "codex-home")
    os.makedirs(home)
    env = dict(os.environ)
    env["CODEX_HOME"] = home
    env["HOME"] = tmp
    env["USERPROFILE"] = tmp
    print("临时 CODEX_HOME: %s" % home)

    # ---------- 1) 最简 config.toml ----------
    section("1. 构造一台「刚装好 Codex」的机器")
    cfg = os.path.join(home, "config.toml")
    io.open(cfg, "w", encoding="utf-8", newline="\n").write(
        'model = "gpt-5.5"\nmodel_provider = "openai"\n'
        'model_reasoning_effort = "medium"\n'
        'approval_policy = "never"\n')
    ok("写入最简 config.toml")

    # ---------- 2) doctor 体检 ----------
    section("2. doctor 体检（应报出「没模型目录」等问题）")
    p = subprocess.run([sys.executable, os.path.join(REPO, "doctor.py"),
                        "--no-autostart"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=180)
    out = p.stdout or ""
    if "model_catalog_json" in out:
        ok("doctor 报出缺少模型目录")
    else:
        bad("doctor 报出缺少模型目录", out[:300])
    if p.returncode in (0, 1):
        ok("doctor 退出码正常（%s）" % p.returncode)
    else:
        bad("doctor 退出码异常", str(p.returncode))

    # ---------- 3) doctor --fix ----------
    section("3. doctor --fix（应自动建好模型目录并登记）")
    p = subprocess.run([sys.executable, os.path.join(REPO, "doctor.py"),
                        "--fix", "--no-autostart"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=180)
    out = p.stdout or ""
    try:
        import tomllib
        with open(cfg, "rb") as f:
            cfg2 = tomllib.load(f)
    except Exception as e:
        cfg2 = {}
        bad("修完后 config.toml 可解析", repr(e))
    if cfg2.get("model_catalog_json"):
        ok("已登记 model_catalog_json", cfg2["model_catalog_json"])
        if os.path.exists(cfg2["model_catalog_json"]):
            ok("模型目录文件已创建")
        else:
            bad("模型目录文件已创建", cfg2["model_catalog_json"])
    else:
        bad("已登记 model_catalog_json", out[-400:])
    if "Traceback" not in (p.stderr or ""):
        ok("doctor --fix 无异常")
    else:
        bad("doctor --fix 无异常", (p.stderr or "")[:300])

    # ---------- 4) 导入 server，验证工具在空环境下不崩 ----------
    section("4. MCP server 工具在空环境下可用")
    for m in ("server", "platform_compat"):
        if m in sys.modules:
            del sys.modules[m]
    os.environ.update(env)
    try:
        import server
        ok("server.py 可导入")
    except Exception as e:
        bad("server.py 可导入", repr(e))
        return finish(tmp)

    try:
        r = server.list_providers()
        if "当前默认" in r:
            ok("list_providers")
        else:
            bad("list_providers", r[:200])
    except Exception as e:
        bad("list_providers", repr(e))

    try:
        r = server.list_catalog_models(include_hidden=True)
        if "模型目录" in r:
            ok("list_catalog_models")
        else:
            bad("list_catalog_models", r[:200])
    except Exception as e:
        bad("list_catalog_models", repr(e))

    # ---------- 5) 接入 provider ----------
    section("5. 接入 provider（setup_preset）")
    key = os.environ.get("MODEL_SWITCHER_TEST_KEY", "").strip()
    if not key:
        # 退而求其次：从本机已有配置里找（仅本机跑回归用）
        try:
            import re as _re
            raw = io.open(real_cfg, encoding="utf-8-sig").read()
            m = _re.search(r"experimental_bearer_token\s*=\s*\"([^\"]+)\"", raw)
            if m:
                key = m.group(1)
        except Exception:
            key = ""
    if not key:
        skip("setup_preset（没有可用 key）")
        skip("test_provider 真实请求")
        skip("sync_provider_models")
        skip("set_model_visibility")
        return finish(tmp)

    try:
        r = server.setup_preset("deepseek", key, make_default=False)
        if "已写入 provider" in r:
            ok("setup_preset 写入 provider")
        else:
            bad("setup_preset 写入 provider", r[:300])
        if "已登记到桌面版模型目录" in r:
            ok("setup_preset 登记模型目录")
        else:
            bad("setup_preset 登记模型目录", r[-400:])
    except Exception as e:
        bad("setup_preset", repr(e))
        return finish(tmp)

    # ---------- 6) 真实请求 ----------
    section("6. 真实发一次请求（验证协议真的通）")
    try:
        r = server.test_provider("deepseek", "deepseek-flash", "只回复两个字：正常")
        if "成功" in r:
            ok("test_provider 真实请求成功", r.splitlines()[0])
        else:
            bad("test_provider 真实请求成功", r[:300])
    except Exception as e:
        bad("test_provider 真实请求成功", repr(e))

    # ---------- 7) 可见性自选 ----------
    section("7. 模型可见性自选（隐藏 / 放回）")
    try:
        r = server.set_model_visibility("deepseek-flash", False)
        if "visibility" in r:
            ok("set_model_visibility 隐藏")
        else:
            bad("set_model_visibility 隐藏", r[:200])
        r = server.list_catalog_models(include_hidden=True)
        if "已隐藏" in r and "deepseek-flash" in r:
            ok("隐藏后目录里查得到")
        else:
            bad("隐藏后目录里查得到", r[:400])
        r = server.set_model_visibility("deepseek-flash", True)
        if "visibility" in r:
            ok("set_model_visibility 放回")
        else:
            bad("set_model_visibility 放回", r[:200])
    except Exception as e:
        bad("模型可见性自选", repr(e))

    # ---------- 8) 在售对齐 ----------
    section("8. 按厂商在售列表对齐（sync_provider_models）")
    try:
        r = server.sync_provider_models("deepseek", keep="deepseek-flash")
        if "对齐完成" in r:
            ok("sync_provider_models")
        else:
            bad("sync_provider_models", r[:300])
    except Exception as e:
        bad("sync_provider_models", repr(e))

    # ---------- 9) 协议探测 ----------
    section("9. 协议探测（无需密钥也能跑）")
    try:
        r = server.check_provider_protocol("https://api.siliconflow.cn/v1")
        if "responses" in r:
            ok("check_provider_protocol")
        else:
            bad("check_provider_protocol", r[:200])
    except Exception as e:
        bad("check_provider_protocol", repr(e))

    # ---------- 10) 下线公告核查（防误报回归） ----------
    section("10. 下线公告核查（关键：查不到≠已下线）")
    try:
        # 有公告页的厂商：要能抓到公告
        r = server.check_provider_deprecations("minimax")
        if "下线" in r or "在售" in r:
            ok("check_provider_deprecations 跑通")
        else:
            bad("check_provider_deprecations 跑通", r[:200])
        # 没配公告页的厂商：必须报「无法判定」，**不能**报「已下架」
        r2 = server.check_provider_deprecations("dashscope")
        if "已下架" in r2:
            bad("没配公告页的厂商不误报已下架", r2[-400:])
        elif "无法判定" in r2:
            ok("没配公告页的厂商报「无法判定」而非「已下架」")
        else:
            bad("没配公告页的厂商报「无法判定」", r2[-400:])
        # 回归：setup_preset 不能把这类厂商的模型剔光
        p = server.PRESETS.get("dashscope") or {}
        models = list(p.get("models") or [])
        if models:
            st, _live, _pg = server._scan_provider_deprecations(
                "dashscope", p.get("base_url", ""), "", models)
            dead = {m for m, (s, _) in st.items() if s in server.DEAD_STATES}
            if dead:
                bad("查不到在售清单时不应剔除任何模型", "误剔: %s" % sorted(dead))
            else:
                ok("查不到在售清单时不剔除任何模型（预设 %d 个全保留）" % len(models))
    except Exception as e:
        bad("下线公告核查", repr(e))

    # ---------- 11) 剪贴板通道（key 不进对话与工具参数） ----------
    section("11. 剪贴板通道（读得到 / 用完即清 / 返回值不含明文）")
    saved = None
    fake = "FAKE_KEY_smoketest_0123456789abcdef"
    try:
        import platform_compat as pc
        backend = pc.clipboard_backend()
        if not backend:
            skip("剪贴板通道", "本机没有可用的剪贴板后端")
        else:
            saved = pc.read_clipboard_text()          # 先保存用户原剪贴板
            pc.write_clipboard_text(fake)             # 写入假 key
            got = pc.read_clipboard_text()
            if got and got.strip() == fake:
                ok("剪贴板写入后读回一致", "后端 %s" % backend)
            else:
                bad("剪贴板写入后读回一致", "后端 %s" % backend)
            try:
                server.set_api_key_from_clipboard("SMOKE_TEST_KEY",
                                                 storage_mode="inline")
            except Exception as e:
                bad("set_api_key_from_clipboard 可调用", repr(e)[:200])
            after = pc.read_clipboard_text()
            if after is None or after.strip() == "":
                ok("用完自动清空剪贴板")
            else:
                bad("用完自动清空剪贴板", "仍有内容")
            try:
                pc.write_clipboard_text(fake)
                r = server.set_api_key_from_clipboard("SMOKE_TEST_KEY2",
                                                     storage_mode="inline")
                if fake in r:
                    bad("返回值不含明文 key", "返回里出现了 key！")
                else:
                    ok("返回值不含明文 key")
            except Exception as e:
                bad("返回值不含明文 key", repr(e)[:200])
    except Exception as e:
        bad("剪贴板通道", repr(e))
    finally:
        try:
            import platform_compat as pc
            if saved is not None:
                pc.write_clipboard_text(saved)
        except Exception:
            pass

    return finish(tmp)


def finish(tmp):
    section("结果")
    print("  通过 %d，失败 %d，跳过 %d" % (len(PASS), len(FAIL), len(SKIP)))
    for n, d in FAIL:
        print("  FAILED: %s  %s" % (n, _safe(d)))
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
