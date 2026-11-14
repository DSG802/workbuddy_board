"""Android 入口（Chaquopy 内嵌 CPython）。

把 app/ 的本地服务跑在 127.0.0.1，窗口由 Java 侧 WebView 负责。
由 Java 后台线程调用，返回端口号字符串。
"""

import os
import socket
import sys
import threading
import time
from pathlib import Path

_STARTED = False
_PORT = 0
_BOOT_LOCK = threading.Lock()


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _prepare_env(files_dir: Path, port: int) -> None:
    state = files_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (files_dir / "login").mkdir(parents=True, exist_ok=True)

    os.environ["STATE_DIR"] = str(state)
    os.environ["TOKEN_FILE"] = str(state / "token")
    os.environ["LOGIN_DIR"] = str(files_dir / "login")
    os.environ["PET_DIR"] = str(files_dir / "pets")
    os.environ["PORT"] = str(port)
    os.environ["BIND_ADDR"] = "127.0.0.1"
    # 设备号落盘到 state/desktop.dev，清掉 cookie 也不换号；不能开 WB_DESKTOP=1（会连带桌面专属行为）。
    os.environ.setdefault("WB_DEV_FILE", "1")
    os.environ.setdefault("API_BASE", "https://www.codebuddy.cn")
    os.environ.setdefault("API_V2", "0")
    os.environ.setdefault("MULTIUSER", "1")
    os.environ.setdefault("RUN_ON_START", "1")
    os.environ.setdefault("ATTEMPT_HOURS", "6,9,12,15,18,21")
    os.environ.setdefault("TRAVEL_ENABLE", "1")
    os.environ.setdefault("TZ_OFFSET", "8")
    os.environ.setdefault("ARCHIVE_MODE", "0")


class _InlineWorker:
    """顶替 subprocess.Popen 的扫码 worker。

    Chaquopy 只有 libpython、没有解释器可执行文件，扫码改为在线程里跑。
    checkin 只用 poll / wait / terminate / kill 四个接口。
    """

    def __init__(self, target):
        self._rc = None
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(target,), daemon=True)
        self._thread.start()

    def _run(self, target):
        try:
            target()
        except Exception:  # noqa: BLE001
            import traceback

            traceback.print_exc()
        finally:
            self._rc = 0
            self._done.set()

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        self._done.wait(timeout)
        return self._rc

    def terminate(self):
        # 线程杀不掉，置位让 checkin 认为它已结束。
        self._rc = 0
        self._done.set()

    def kill(self):
        self.terminate()


def _patch_login_worker(checkin) -> None:
    import subprocess as _sp

    import login

    if getattr(_sp, "_wb_patched", False):
        return

    def _is_login_worker(cmd) -> bool:
        """两种形态：

        * 冻结态（桌面 App）：[exe, "--login-worker", sid]
        * 开发态 / Android：[sys.executable, "-c", "import sys,login;..."]

        Android 上 sys.frozen 为假，checkin 会走 -c 那条，必须拦下：
        真去 fork+exec 的话，ART 子进程会被 abort（Fatal signal 6）。
        """
        if not isinstance(cmd, (list, tuple)):
            return False
        for a in cmd:
            if isinstance(a, str) and ("--login-worker" in a or "login.run_once_blocking" in a):
                return True
        return False

    def patched_popen(cmd, cwd=None, env=None, stdout=None, stderr=None, **kw):
        if not _is_login_worker(cmd):
            # Android 上 fork 出来没 exec 的子进程会被 ART abort（Fatal signal 6），改抛 Python 异常。
            raise OSError("Android 运行时不支持子进程：%r" % (cmd,))

        run_env = dict(env or os.environ)
        sid_dir = run_env.get("LOGIN_DIR") or os.environ.get("LOGIN_DIR")

        def run():
            old = {k: os.environ.get(k) for k in ("LOGIN_DIR", "TOKEN_FILE")}
            try:
                os.environ["LOGIN_DIR"] = run_env.get("LOGIN_DIR", "")
                os.environ["TOKEN_FILE"] = run_env.get("TOKEN_FILE", "")
                # login 的这几个路径在 import 时固化，按会话目录重新绑定。
                login.LOGIN_DIR = sid_dir
                login.STATE_FILE = os.path.join(sid_dir, "state.json")
                login.QR_FILE = os.path.join(sid_dir, "qr.png")
                login.COOKIE_FILE = os.path.join(sid_dir, "cookies.json")
                ok = bool(login.run_once_blocking())
                print(f"[android] 扫码登录结束 ok={ok}", flush=True)
            except Exception:  # noqa: BLE001
                import traceback

                traceback.print_exc()
            finally:
                for k, v in old.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

        return _InlineWorker(run)

    _sp.Popen = patched_popen
    _sp._wb_patched = True
    checkin.subprocess = _sp


def start(files_dir: str) -> str:
    """起服务，返回 127.0.0.1 上的端口。重复调用直接返回已有端口。"""
    global _STARTED, _PORT

    with _BOOT_LOCK:
        if _STARTED:
            return str(_PORT)

        files = Path(files_dir)
        port = _free_port()
        _PORT = port
        _prepare_env(files, port)

        # http.server 绑定时会 getfqdn 反查，Android 上又慢又常失败，直接短路。
        socket.getfqdn = lambda *a, **k: "127.0.0.1"  # type: ignore[assignment]

        import checkin  # noqa: E402  依赖上面的环境变量

        _patch_login_worker(checkin)

        threading.Thread(target=checkin.main, name="wb-service", daemon=True).start()
        _STARTED = True

    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                print(f"[android] 本地服务就绪 http://127.0.0.1:{port}/", flush=True)
                return str(port)
        except OSError:
            time.sleep(0.15)
    raise RuntimeError(f"本地服务没能在 127.0.0.1:{port} 起来，见 logcat 里的 wb-service 输出")
