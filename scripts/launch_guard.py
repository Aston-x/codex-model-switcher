# -*- coding: utf-8 -*-
"""拉起并看护 provider 守护进程 —— 让它**跟随 Codex 桌面版的生命周期**。

行为：
  1. 检测官方 Codex 桌面版是否在跑（零子进程、零窗口）
  2. 没在跑 -> 轮询等待（不占资源）
  3. 在跑 -> 启动 guard.py 子进程，并持续看护：
       · Codex 退出 -> 结束 guard
       · guard 自己意外退出 -> 重新拉起

完全静默：Windows 用 pythonw.exe 启动（无控制台窗口），
POSIX 用 setsid 脱离终端；不输出到 stdout/stderr。

跨平台：进程枚举 / 锁自愈 / 解释器定位 / codex CLI 定位
全部在 platform_compat.py 里，本文件只管生命周期。
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import platform_compat  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "guard.py")


def codex_running():
    return platform_compat.codex_desktop_running()


def _acquire_single_instance():
    """全局互斥锁：保证同一台机器只有一个 launcher 在跑。

    做法：创建一个带 PID 的锁文件，用 O_EXCL 保证原子性。
    已经有实例时直接退出（**静默**，不打印任何东西）。

    ⚠️ 进程被强杀（任务管理器结束进程 / 断电）时锁文件会残留，
       导致之后再也起不来。所以要检测**陈旧锁**：
       锁里的 PID 已经不存在 => 锁是残留的，删掉重建。
    """
    lock_path = os.path.join(os.path.dirname(HERE), "launch_guard.lock")
    for _attempt in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return True, lock_path
        except FileExistsError:
            if not _lock_is_stale(lock_path):
                return False, lock_path
            try:
                os.unlink(lock_path)      # 陈旧锁，清掉重建
            except Exception:
                return False, lock_path
        except Exception:
            return True, lock_path      # 建不了锁就别拦自己
    return True, lock_path


def _lock_is_stale(lock_path):
    """锁里的 PID 已经不存在 => 陈旧锁。"""
    try:
        with open(lock_path, "r") as f:
            pid = int((f.read() or "0").strip() or 0)
    except Exception:
        return True
    if pid <= 0:
        return True
    return not platform_compat.pid_alive(pid)


def _spawn_kwargs():
    """静默启动子进程的平台参数。"""
    kw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              stdin=subprocess.DEVNULL)
    if platform_compat.IS_WINDOWS:
        kw["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                               | getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        kw["start_new_session"] = True
    return kw


def main():
    if not os.path.exists(GUARD):
        return 1

    ok, lock_path = _acquire_single_instance()
    if not ok:
        return 0                    # 已有实例在跑，本进程静默退出
    try:
        _run()
    finally:
        try:
            os.unlink(lock_path)
        except Exception:
            pass


def _run():
    child = None
    cli = platform_compat.codex_cli_path()
    py = platform_compat.python_launcher()
    while True:
        if not codex_running():
            # Codex 没在跑 -> 结束守护，等它下次起来
            if child is not None:
                if child.poll() is None:
                    try:
                        child.terminate()
                        try:
                            child.wait(timeout=3)
                        except Exception:
                            try:
                                child.kill()
                            except Exception:
                                pass
                    except Exception:
                        pass
                child = None
            time.sleep(3)
            continue

        # Codex 在跑 -> 确保守护活着
        if child is None or child.poll() is not None:
            env = dict(os.environ)
            if cli:
                env["CODEX_CLI_PATH"] = cli
            env["CODEX_HOME"] = platform_compat.codex_home()
            try:
                child = subprocess.Popen([py, GUARD], env=env, **_spawn_kwargs())
            except Exception:
                try:
                    child = subprocess.Popen([sys.executable, GUARD], env=env,
                                             **_spawn_kwargs())
                except Exception:
                    child = None

        time.sleep(3)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
