#!/usr/bin/env python3
"""WorkBuddy 看板 · 桌面客户端入口。

复用 app/（纯标准库），进程内起一个只听 127.0.0.1 的 HTTP 服务，pywebview 开原生窗口指过去。
状态落用户目录，卸载 App 不动数据：
  macOS  ~/Library/Application Support/WorkBuddyBoard/
  Win    %APPDATA%\\WorkBuddyBoard\\
单实例：SQLite 是 WAL，两个进程同写会 SQLITE_BUSY，第二次启动提示后退出。
"""

import os
import socket
import sqlite3  # noqa: F401  显式引用：store.py 是运行时按路径加载的，PyInstaller 静态分析扫不到
import sys
import threading
import time
from pathlib import Path

APP_NAME = "WorkBuddyBoard"
WINDOW_TITLE = "WorkBuddy 看板"


def resource_root() -> Path:
    """打包后资源在 sys._MEIPASS，开发时就是本文件所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def data_root() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / APP_NAME
    (base / "state").mkdir(parents=True, exist_ok=True)
    return base


def free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


RES = resource_root()
DATA = data_root()
# 随机端口，冒烟测试时用 WB_PORT 固定。
PORT = int(os.environ.get("WB_PORT") or 0) or free_port()


_LOCK_HANDLE = None
_OWNS_PORT_FILE = False   # 只有持锁的那个进程才该删 port 文件（否则会删掉别人的）


def acquire_single_instance() -> bool:
    """两个窗口会各起一个进程抢同一份 SQLite，第二次启动直接挡住。"""
    global _LOCK_HANDLE
    path = DATA / ".running"
    try:
        # 用 a+ 不用 w：另一个实例在跑时不清空它写的 pid。
        f = open(path, "a+")
    except OSError:
        return True  # 锁文件写不了时放行，由主流程报错
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _LOCK_HANDLE = f
    try:
        f.write(str(os.getpid()))
        f.flush()
    except OSError:
        pass
    return True


def warn_already_running() -> None:
    """锁被占又探不到端口：另一个进程卡在启动阶段。用 Popen 起 osascript，run 会一直等用户点按钮。"""
    msg = "WorkBuddy 看板已经在运行了。\n\n请先切到那个窗口；如果找不到，可以在终端执行：\nkillall WorkBuddyBoard"
    if sys.platform == "darwin":
        try:
            import subprocess

            subprocess.Popen(
                ["osascript", "-e",
                 f'display dialog "{msg}" buttons {{"好"}} default button 1 with title "{WINDOW_TITLE}"'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return
        except Exception:  # noqa: BLE001
            pass
    print(msg, file=sys.stderr)


def _port_file() -> Path:
    return DATA / "port"


def probe_port(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def existing_port(wait: float = 8.0) -> int:
    """已有实例的端口。刚启动几秒内还没写文件，需要重试几次。"""
    end = time.time() + wait
    while True:
        try:
            port = int(_port_file().read_text().strip())
        except (OSError, ValueError):
            port = 0
        if port and probe_port(port):
            return port
        if time.time() >= end:
            return 0
        time.sleep(0.25)


def activate_existing() -> bool:
    """把已在跑的窗口叫到前面。用 osascript activate，open -a 可能再拉起一个进程。"""
    if sys.platform != "darwin":
        return False
    try:
        import subprocess

        r = subprocess.run(
            ["osascript", "-e", f'tell application "{WINDOW_TITLE}" to activate'],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def prepare_env(worker: bool = False) -> None:
    """必须在 import checkin 之前设好：这些是模块级常量。worker=True 时不覆盖父进程给的 LOGIN_DIR / TOKEN_FILE。"""
    os.environ["STATE_DIR"] = str(DATA / "state")
    if not worker:
        os.environ["TOKEN_FILE"] = str(DATA / "state" / "token")
        os.environ["LOGIN_DIR"] = str(DATA / "login")
    os.environ["PET_DIR"] = str(RES / "pets")
    os.environ["PORT"] = str(PORT)
    os.environ["BIND_ADDR"] = "127.0.0.1"          # 只监听回环
    os.environ["WB_DESKTOP"] = "1"                 # 单机版：允许用本机账户自动接回会话
    os.environ.setdefault("API_BASE", "https://www.codebuddy.cn")
    os.environ.setdefault("API_V2", "0")
    os.environ.setdefault("MULTIUSER", "1")
    os.environ.setdefault("RUN_ON_START", "1")
    os.environ.setdefault("ATTEMPT_HOURS", "6,9,12,15,18,21")
    os.environ.setdefault("TRAVEL_ENABLE", "1")
    os.environ.setdefault("TZ_OFFSET", "8")
    # 单机版不需要「存档只读」
    os.environ.setdefault("ARCHIVE_MODE", "0")


class _Tee:
    """同时写原流和日志文件。print 只走 write/flush。"""

    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
            except Exception:  # noqa: BLE001
                pass

    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:  # noqa: BLE001
                pass


def fix_stdio() -> None:
    """双击启动时没有控制台，sys.stdout 可能是 None，log() 里的 print(flush=True) 会抛 AttributeError。"""
    out, err = sys.stdout, sys.stderr
    log_file = DATA / "app.log"
    # 超 4 MB 轮转，只留一份上一轮（app.log 里含 traceback，长期运行会一直长）。
    try:
        if log_file.stat().st_size > 4 * 1024 * 1024:
            log_file.replace(DATA / "app.log.1")
    except OSError:
        pass
    stream = None
    try:
        stream = open(log_file, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        pass
    if stream is None and out is None:
        import io

        stream = io.StringIO()
    sys.stdout = _Tee(out, stream)
    sys.stderr = _Tee(err, stream)


def patch_getfqdn() -> None:
    """http.server 绑定时会 socket.getfqdn(host)，无网络环境会卡住（端口不监听、窗口白屏），本地直接短路。"""
    socket.getfqdn = lambda *a, **k: "127.0.0.1"  # type: ignore[assignment]


def wait_ready(port: int, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def run_login_worker() -> int:
    """--login-worker <sid>：扫码子进程入口，只跑一轮登录。LOGIN_DIR / TOKEN_FILE 由父进程指到会话目录。"""
    # 窗口化程序里 sys.stdout 可能是 None，日志一律 tee 到 app.log。
    if sys.stdout is None:
        try:
            sys.stdout = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8", errors="replace")
        except OSError:
            sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        try:
            sys.stderr = os.fdopen(os.dup(2), "w", buffering=1, encoding="utf-8", errors="replace")
        except OSError:
            sys.stderr = sys.stdout

    if not os.environ.get("LOGIN_DIR"):
        print("--login-worker 缺少 LOGIN_DIR，父进程没传环境变量", file=sys.stderr, flush=True)
        return 2

    prepare_env(worker=True)
    patch_getfqdn()
    for p in (str(RES), str(RES / "app")):
        if p not in sys.path:
            sys.path.insert(0, p)

    import login  # noqa: E402

    print(f"登录 worker 启动 pid={os.getpid()} LOGIN_DIR={os.environ['LOGIN_DIR']}", flush=True)
    try:
        ok = bool(login.run_once_blocking())
    except Exception:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        return 1
    print(f"登录 worker 结束 ok={ok}", flush=True)
    return 0 if ok else 1


def open_window(port: int) -> int:
    """开原生窗口。关窗即整进程退出，否则留下 Dock 有图标、锁被占的僵尸实例。"""
    import webview

    win = webview.create_window(
        WINDOW_TITLE,
        f"http://127.0.0.1:{port}/",
        width=480,
        height=920,
        min_size=(390, 620),
        # 与页面底色一致，避免整页重载时闪白。
        background_color="#05070e",
    )

    def _bye():
        try:
            # 第二实例没有锁，不该删别人写的 port 文件。
            if _OWNS_PORT_FILE:
                _port_file().unlink()
        except OSError:
            pass
        os._exit(0)

    win.events.closed += _bye
    # private_mode 默认 True，WKWebView 用临时存储，关窗即丢会话 cookie（下次打开回扫码页、设备号也变），
    # 所以改成持久存储，登录状态跟这台机器走。
    try:
        webview.start(private_mode=False, storage_path=str(DATA / "webview"))
    except TypeError:
        # 老版本 pywebview 不认 storage_path，退回只开持久模式。
        webview.start(private_mode=False)
    return 0


def main() -> int:
    if "--login-worker" in sys.argv:
        return run_login_worker()

    if not acquire_single_instance():
        # 已有实例在跑：叫到前面来，不弹阻塞对话框。
        port = existing_port()
        if port and activate_existing():
            print(f"已在运行，已激活原窗口（127.0.0.1:{port}）", flush=True)
            return 0
        if port:
            # 激活失败时退化成第二个窗口，共用同一份服务。
            print(f"已在运行，新开一个窗口指向 127.0.0.1:{port}", flush=True)
            return open_window(port)
        warn_already_running()
        return 1

    prepare_env()
    fix_stdio()
    patch_getfqdn()

    # app/ 里的模块靠 sys.path 加载。
    for p in (str(RES), str(RES / "app")):
        if p not in sys.path:
            sys.path.insert(0, p)

    import checkin  # noqa: E402  （依赖上面的环境变量）

    def boot() -> None:
        try:
            checkin.main()
        except Exception:  # noqa: BLE001
            import traceback

            traceback.print_exc()

    threading.Thread(target=boot, name="wb-service", daemon=True).start()

    if not wait_ready(PORT):
        print(f"本地服务没能在 127.0.0.1:{PORT} 起来", file=sys.stderr)
        return 1

    # 让第二次启动能认出这个实例（登录子进程也靠它判断父进程活着）。
    global _OWNS_PORT_FILE
    try:
        _port_file().write_text(str(PORT))
        _OWNS_PORT_FILE = True
    except OSError:
        pass

    # 冒烟用：只起服务不建窗口。
    if "--service-only" in sys.argv:
        print(f"READY http://127.0.0.1:{PORT}/", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0

    return open_window(PORT)


if __name__ == "__main__":
    sys.exit(main())
