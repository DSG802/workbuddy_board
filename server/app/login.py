"""扫码登录（无头 Chromium + Playwright）。

  1. 每次新建 profile：旧 profile 带着会话，打开 /login 会直接跳走、不出码
  2. 打开官方登录页，从微信 iframe 读二维码图片地址并下载（截图法在 headless 首访常截到空白）
  3. 轮询页面 URL，离开 /login 视为登录完成
  4. 导出 codebuddy.cn 的 cookie 到 state/session.json

状态机写在 /tmp/wb-login/state.json，前端轮询 /api/login/state。
扫码全程在服务端浏览器里完成，客户端只负责显示二维码和等结果。
"""

from __future__ import annotations

import base64
import hashlib
import http.cookiejar
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import session

LOGIN_DIR = os.environ.get("LOGIN_DIR", "/tmp/wb-login")
# 个人版入口必须 platform=workbuddy&state=0；裸 /login/ 会 302 到企业版 admin，扫出的 cookie 调账单接口 401。
LOGIN_URL = os.environ.get(
    "LOGIN_URL", "https://www.codebuddy.cn/login/?platform=workbuddy&state=0")
MAX_IDLE = int(os.environ.get("MAX_IDLE", "600"))
QR_REFRESH = int(os.environ.get("QR_REFRESH", "240"))  # 微信码约 4 分钟失效
QR_WAIT = int(os.environ.get("QR_WAIT", "40"))
# 等二维码出码的上限（实测稳定出码 10~25 秒）。
MAX_QR_ATTEMPT = int(os.environ.get("MAX_QR_ATTEMPT", "45"))

# 纯 API 登录链（按官方登录 SPA 的 js 实现）。
KC_BASE = "https://www.codebuddy.cn/auth-v2/realms/copilot"
# 官方回调路径（js 常量 ar="/login/signin-callback"）。
CALLBACK_URI = "https://www.codebuddy.cn/login/signin-callback"
LP_POLL = "https://lp.open.weixin.qq.com/connect/l/qrconnect"
# 登录链必须用真实浏览器 UA：HeadlessChrome 会返回 OK 但不种 session/session_2。
# 登录成功后的 /billing 等 API 仍须用 session.UA（HeadlessChrome）。
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36")

os.makedirs(LOGIN_DIR, exist_ok=True)

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()

# 用户在别处（微信内官方页 / 桌面客户端）用同一 state 登录时，轮询 /v2/plugin/auth/token?state= 取令牌。
_plugin_evt = threading.Event()
_watch_lock = threading.Lock()
_watch_states: set[str] = set()

STATE_FILE = os.path.join(LOGIN_DIR, "state.json")
QR_FILE = os.path.join(LOGIN_DIR, "qr.png")
COOKIE_FILE = os.path.join(LOGIN_DIR, "cookies.json")

_state = {"phase": "idle", "message": "未开始", "ver": 0, "ts": int(time.time() * 1000)}



def _set(**kw) -> None:
    _state.update(kw)
    _state["ts"] = int(time.time() * 1000)
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_state, f)
    except Exception:
        pass


def read_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return dict(_state)


def alive() -> bool:
    global _thread
    return _thread is not None and _thread.is_alive()


def qr_png() -> bytes | None:
    try:
        return open(QR_FILE, "rb").read()
    except Exception:
        return None



def _http_get(url: str, timeout: int = 30, referer: str | None = None) -> bytes | None:
    """下载二维码原图；直连，不走环境代理。"""
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        hdr = {"User-Agent": session.UA,
               "Referer": referer or "https://open.weixin.qq.com/"}
        req = urllib.request.Request(url, headers=hdr)
        with op.open(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def _log(msg: str) -> None:
    print(f"[login {time.strftime('%H:%M:%S')}] {msg}", flush=True)



def _qr_frame(page):
    """返回微信二维码 iframe（只比对 URL，不 evaluate，避免被未就绪的 frame 阻塞）。"""
    for fr in page.frames:
        if "qrconnect" in (fr.url or ""):
            return fr
    return None


def _frame_qr_src(fr, tries: int = 6) -> str:
    """在 iframe 里取二维码原图地址。iframe 刚插进来时 img 还没渲染，要小等几次。"""
    for _ in range(tries):
        try:
            src = fr.evaluate(
                "() => (Array.from(document.images).map(i => i.src || '')"
                ".find(s => s.includes('/connect/qrcode/'))) || ''"
            )
            if src:
                return src
        except Exception:
            pass
        time.sleep(1.0)
    return ""


def _decode_qr(data: bytes) -> str | None:
    """用 zbar 真实解码一次。解不出来的一律不算二维码（空白块/图标区都挡在这）。"""
    try:
        import io

        from PIL import Image
        from pyzbar.pyzbar import decode
        img = Image.open(io.BytesIO(data))
        # 先试原图，失败再放大提识别率。
        for im in (img, img.resize((img.width * 2, img.height * 2), Image.LANCZOS)):
            res = decode(im.convert("L"))
            if res:
                return res[0].data.decode("utf-8", "replace")
    except ImportError:
        return "SKIP"  # 没装解码库时退回放行（老镜像）
    except Exception:
        return None
    return None


def _save_qr_bytes(data: bytes) -> bool:
    try:
        with open(QR_FILE, "wb") as f:
            f.write(data)
    except Exception:
        return False
    _set(ver=_state["ver"] + 1, qr_at=int(time.time() * 1000))
    _log(f"二维码已更新（{len(data)}B）ver={_state['ver']}")
    return True


# 登录页有多个版本在灰度：有的把微信 iframe 挂成独立 frame，有的只在内联 iframe 里渲染。
QR_SELECTORS = (
    ".wb-login-v3-qrcode__frame",
    ".wb-login-v3-qrcode",
    'iframe[title="wechat-qrcode"]',
    'iframe[src*="qrconnect"]',
)


def _grab_qr(page, last: dict) -> bool:
    # 原图
    fr = _qr_frame(page)
    if fr:
        src = _frame_qr_src(fr)
        if src and src != last.get("src"):
            data = _http_get(src)
            if data and len(data) > 500 and _decode_qr(data):
                last["src"] = src
                last["hash"] = ""
                return _save_qr_bytes(data)
    # 按候选区域截图逐个解码验证：各版页面几何不同，坐标不可猜。
    candidates = []
    for sel in QR_SELECTORS:
        el = page.query_selector(sel)
        if not el:
            continue
        try:
            box = el.bounding_box()
        except Exception:
            continue
        if not box or box["width"] < 100:
            continue
        w, h = box["width"], box["height"]
        side = min(w, h)
        candidates += [
            (sel, {**box, "width": w, "height": h}),                       # 整框
            (sel, {"x": box["x"], "y": box["y"], "width": side, "height": side}),  # 顶对齐正方形
            (sel, {"x": box["x"], "y": box["y"] + max(0, (h - side) / 2),
                   "width": side, "height": side}),                        # 居中正方形
            (sel, {"x": box["x"], "y": box["y"], "width": w, "height": max(150, h * 0.68)}),
        ]
    for sel, clip in candidates:
        try:
            data = page.screenshot(clip=clip, type="png")
        except Exception:
            continue
        if not data or len(data) < 3000:
            continue
        if _decode_qr(data):
            return _save_qr_bytes(data)
    return False


def _ensure_qr(page, last: dict) -> bool:
    """反复加载登录页直到二维码出来。这个页面不是每次都渲染二维码，只能靠重试。"""
    for attempt in range(1, 5):
        if _stop.is_set():
            return False
        _set(phase="loading",
             message="正在打开官方登录页…" if attempt == 1 else f"正在重试（第 {attempt} 次）…")
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        deadline = time.time() + MAX_QR_ATTEMPT
        while time.time() < deadline:
            time.sleep(1.5)
            if _stop.is_set():
                return False
            try:
                if _grab_qr(page, last):
                    return True
            except Exception:
                continue
        _log(f"第 {attempt} 次未拿到二维码，重试")
    return False


def _run() -> None:
    try:
        if _run_api():
            return
    except Exception as e:  # noqa: BLE001
        _log(f"API 引擎异常，转浏览器兜底：{type(e).__name__}: {e}")
    _run_browser()



def _api_opener() -> tuple[urllib.request.OpenerDirector, http.cookiejar.CookieJar]:
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.ProxyHandler({}))  # 直连，不走环境代理
    return op, cj


def _api_req(op, url: str, data: bytes | None = None, referer: str | None = None,
             timeout: int = 40, origin: str = "https://www.codebuddy.cn") -> str:
    hdr = {"User-Agent": BROWSER_UA,
           "Accept": "application/json, text/plain, */*",
           "Referer": referer or f"{origin}/login/",
           "Origin": origin}
    if data is not None:
        hdr["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdr)
    try:
        with op.open(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        _log(f"HTTP {e.code} {url.split('?')[0]}：{body[:300]}")
        raise


def _api_req_full(op, url: str, data: bytes | None = None, referer: str | None = None,
                  timeout: int = 40, ctype: str | None = None,
                  origin: str = "https://www.codebuddy.cn"):
    """同 _api_req，但返回 (http_code, 响应头, 正文)，用于查 Set-Cookie。"""
    hdr = {"User-Agent": BROWSER_UA,
           "Accept": "text/html,application/json,*/*",
           "Referer": referer or f"{origin}/login/",
           "Origin": origin}
    if data is not None:
        hdr["Content-Type"] = ctype or "application/json"
    req = urllib.request.Request(url, data=data, headers=hdr)
    try:
        with op.open(req, timeout=timeout) as r:
            return r.status, r.headers, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode("utf-8", "replace")


def _new_qr_api(op, state: str = "", redirect_uri: str = CALLBACK_URI) -> tuple[str, str, str]:
    """发起一轮登录，返回 (session_state, uuid, 状态消息)。失败返回 None。
    platform 恒为 web；桌面模式多带 state，redirect_uri 换成 workbuddy.cn 域 signin-callback。
    """
    plat = "web"
    q = urllib.parse.quote(redirect_uri, safe="")
    extra = f"&state={urllib.parse.quote(state, safe='')}" if state else ""
    org = "https://www.workbuddy.cn" if state else "https://www.codebuddy.cn"
    # 官方桌面页走 workbuddy.cn 域 auth-v2（appid 白名单 + state 透传）。
    kc = f"{org}/auth-v2/realms/copilot"
    d = json.loads(_api_req(op, f"{kc}/signin/wechat/login?platform={plat}"
                                f"&redirect_uri={q}{extra}", origin=org))
    ss, qurl = d["session_state"], d["qrcode_url"]
    html = _api_req(op, qurl)
    m = (re.search(r'uuid=([A-Za-z0-9_=-]+)', html)
         or re.search(r'"uuid"\s*:\s*"([^"]+)"', html))
    if not m:
        _log(f"qrconnect 页无 uuid（{len(html)}B）")
        return None
    return ss, m.group(1), qurl


def _poll_wx(op, uuid: str) -> tuple[str, str]:
    """微信长轮询，返回 (errcode, wx_code)。单次阻塞约 25s。"""
    txt = _api_req(op, f"{LP_POLL}?uuid={uuid}&_={int(time.time() * 1000)}",
                   referer="https://open.weixin.qq.com/")
    ec = re.search(r"wx_errcode=(\d+)", txt)
    cd = re.search(r"wx_code=['\"]?([A-Za-z0-9_-]*)", txt)
    return (ec.group(1) if ec else "?"), (cd.group(1) if cd else "")


KC_TOKEN_URL = "https://www.workbuddy.cn/auth/realms/copilot/protocol/openid-connect/token"
WB_HOST = "www.workbuddy.cn"
WB_PLUGIN = f"https://{WB_HOST}/v2/plugin"



def _fetch_desktop_state(op) -> tuple[str, str] | None:
    """官方桌面登录第一步：POST /v2/plugin/auth/state → (state, authUrl)。prefixPath=/plugin。"""
    req = urllib.request.Request(f"{WB_PLUGIN}/auth/state?platform=workbuddy",
                                 data=b"{}", method="POST")
    req.add_header("User-Agent", CLIENT_UA)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    for h in ("X-No-Authorization", "X-No-User-Id", "X-No-Enterprise-Id",
              "X-No-Department-Info"):
        req.add_header(h, "true")
    try:
        with op.open(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        _log(f"auth/state 失败：{getattr(e, 'code', e)}")
        return None
    data = d.get("data") or {}
    st, url = data.get("state") or "", data.get("authUrl") or ""
    if d.get("code") == 0 and st and url:
        _log(f"官方桌面 state 领取成功：{st[:8]}… authUrl={url[:80]}")
        return st, url
    _log(f"auth/state 响应异常：{json.dumps(d, ensure_ascii=False)[:200]}")
    return None


def _console_get(op, path: str, state: str = "") -> tuple[int, str]:
    """打 /console/* 接口（官方 LoginSuccess 页那两个 GET 用的就是这套头）。"""
    host = WB_HOST
    url = f"https://{host}{path}"
    if state:
        url += ("&" if "?" in url else "?") + "state=" + urllib.parse.quote(state, safe="")
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "X-Domain": host,
        "Referer": f"https://{host}/login/started",
        "Origin": f"https://{host}",
    })
    try:
        with op.open(req, timeout=25) as r:
            return r.status, r.read().decode("utf-8", "replace")[:400]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def _console_bind(op, state: str) -> bool:
    """把浏览器登录会话绑定到客户端 state（官方 /login/started 页的收尾）。
    先 GET /console/validate/refresh-token，再 GET /console/auth/login?platform=&state=&domain=。
    缺这两步时 /v2/plugin/auth/token?state= 恒返回 11217 login ing...
    """
    host = WB_HOST
    st, body = _console_get(op, "/console/validate/refresh-token")
    _log(f"validate/refresh-token HTTP {st}：{body[:160]}")
    st2, body2 = _console_get(
        op, f"/console/auth/login?platform=workbuddy&domain={host}", state=state)
    _log(f"console/auth/login HTTP {st2}：{body2[:200]}")
    return 200 <= st2 < 300


def _poll_plugin_token(op, state: str, timeout: int = 90) -> bool:
    """官方桌面登录收尾：轮询 GET /v2/plugin/auth/token?state= 收 JWT，挂起时返回 11217。"""
    _set(phase="scanned", message="已确认，正在提取登录令牌…")
    deadline = time.time() + timeout
    n = 0
    while time.time() < deadline and not _stop.is_set():
        req = urllib.request.Request(f"{WB_PLUGIN}/auth/token?state="
                                     + urllib.parse.quote(state, safe=""))
        req.add_header("User-Agent", CLIENT_UA)
        req.add_header("Accept", "application/json")
        req.add_header("X-No-Authorization", "true")
        req.add_header("X-No-User-Id", "true")
        try:
            with op.open(req, timeout=15) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            _log(f"token 轮询异常：{getattr(e, 'code', e)}")
            time.sleep(2.5)
            continue
        n += 1
        data = d.get("data") or {}
        if d.get("code") == 0 and (data.get("accessToken") or data.get("access_token")):
            if _accept_tokens(data):
                _log(f"官方桌面通道登录成功 ✅（轮询 {n} 次）")
                return True
            return False
        if n == 1 or n % 10 == 0:
            _log(f"token 轮询第 {n} 次：code={d.get('code')} {d.get('msg','')[:60]}")
            if n == 1:
                _log(f"  原始响应：{json.dumps(d, ensure_ascii=False)[:300]}")
        time.sleep(2.5)
    _log(f"token 轮询 {timeout}s 超时（共 {n} 次）")
    return False


def _accept_tokens(data: dict) -> bool:
    """plugin/auth/token 返回体 → 落盘。成功返回 True。"""
    tok = data.get("accessToken") or data.get("access_token") or ""
    if not tok:
        return False
    if not _validate_token(tok):
        _log("拿到 token 但余额接口校验未通过")
        return False
    _adopt_token(tok)
    rt = data.get("refreshToken") or ""
    if rt:
        try:
            target = os.path.join(
                os.path.dirname(os.environ.get("TOKEN_FILE", "/state/token")),
                "refresh_token")
            with open(target, "w", encoding="utf-8") as fp:
                fp.write(rt)
            os.chmod(target, 0o600)
            _log(f"refreshToken 已落盘（{len(rt)}B）；官方为机密客户端，"
                 "该 token 仅作留存，续期仍以重新登录为准")
        except OSError as e:
            _log(f"refreshToken 落盘失败：{e}")
    _set(phase="done", message="登录成功")
    _plugin_evt.set()
    return True


def _watch_plugin_token(state: str, seconds: int = 900) -> None:
    """后台盯住某个 state 等令牌就绪：微信内登录的 code 不会回到我们的轮询队列，只会挂到 state 上。"""
    op, _ = _api_opener()
    t0 = time.time()
    n = 0
    while time.time() - t0 < seconds:
        if _stop.is_set() or _plugin_evt.is_set():
            return
        n += 1
        try:
            req = urllib.request.Request(
                f"{WB_PLUGIN}/auth/token?state=" + urllib.parse.quote(state, safe=""))
            req.add_header("User-Agent", CLIENT_UA)
            req.add_header("Accept", "application/json")
            req.add_header("X-No-Authorization", "true")
            req.add_header("X-No-User-Id", "true")
            with op.open(req, timeout=15) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            if n % 20 == 1:
                _log(f"state 监视异常：{getattr(e, 'code', e)}")
            time.sleep(3)
            continue
        data = d.get("data") or {}
        if d.get("code") == 0 and _accept_tokens(data):
            _log(f"state 监视：登录完成 ✅（第 {n} 次轮询，state={state[:8]}…）")
            return
        if n == 1 or n % 40 == 0:
            _log(f"state 监视 {n} 次：code={d.get('code')} {str(d.get('msg'))[:40]}")
        time.sleep(2.5)


def _ensure_watch(state: str) -> None:
    """为 state 起一个后台监视线程（同一 state 只起一次）。"""
    if not state or _plugin_evt.is_set():
        return
    with _watch_lock:
        if state in _watch_states:
            return
        _watch_states.add(state)
    threading.Thread(target=_watch_plugin_token, args=(state,), daemon=True,
                     name=f"plugwatch-{state[:8]}").start()
    _log(f"已挂上 state 监视：{state[:8]}…（微信内官方页登录后自动收牌）")


def _try_wxide(op, wx_code: str) -> bool:
    """官方微信登录通道：POST /v2/auth/token/wxide?tmpCode=<微信临时码>，成功直接返回 accessToken/refreshToken。
    网页扫码拿到的 wx_code 同为微信 OAuth 临时码，可直接复用。
    """
    url = ("https://www.workbuddy.cn/v2/auth/token/wxide?tmpCode="
           + urllib.parse.quote(wx_code, safe=""))
    req = urllib.request.Request(url, data=b"{}", method="POST")
    req.add_header("User-Agent", CLIENT_UA)
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    try:
        with op.open(req, timeout=20) as r:
            txt = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        _log(f"wxide 交换 HTTP {e.code}：{txt[:300]}")
        return False
    except Exception as e:  # noqa: BLE001
        _log(f"wxide 交换异常：{e}")
        return False
    _log(f"wxide 交换响应：{txt[:300]}")
    try:
        d = json.loads(txt)
    except ValueError:
        return False
    data = d.get("data") or {}
    tok = data.get("accessToken") or ""
    if not tok:
        return False
    if not _validate_token(tok):
        _log("wxide 拿到 token 但余额接口校验未通过")
        return False
    _adopt_token(tok)
    rt = data.get("refreshToken") or ""
    if rt:
        try:
            target = os.path.join(os.path.dirname(os.environ.get("TOKEN_FILE", "/state/token")),
                                  "refresh_token")
            with open(target, "w", encoding="utf-8") as fp:
                fp.write(rt)
            os.chmod(target, 0o600)
            _log(f"refreshToken 已落盘 {target}（{len(rt)}B），后续可用 X-Refresh-Token 无限续期")
        except OSError as e:
            _log(f"refreshToken 落盘失败：{e}")
    _set(phase="done", message="登录成功")
    _log("wxide 通道登录成功 ✅（官方桌面客户端同款）")
    return True


def _try_token_exchange(op, auth_code: str) -> bool:
    """拿 finalize 的授权码去 Keycloak 换 JWT。逐个试 client_id，成功返回 True（授权码一次性，顺序关键）。"""
    for cid in ("console", "workbuddy", "codebuddy", "copilot"):
        body = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": CALLBACK_URI,
            "client_id": cid,
        }).encode()
        st, hdrs, txt = _api_req_full(op, KC_TOKEN_URL, data=body,
                                      referer="https://www.codebuddy.cn/",
                                      ctype="application/x-www-form-urlencoded")
        try:
            d = json.loads(txt)
        except Exception:  # noqa: BLE001
            d = {}
        if isinstance(d, dict) and d.get("access_token"):
            _log(f"token 交换成功（client_id={cid}），落盘 JWT")
            tok = d["access_token"]
            tokdir = os.path.dirname(COOKIE_FILE)
            tokpath = os.path.join(tokdir, "token")
            with open(tokpath, "w", encoding="utf-8") as fp:
                fp.write(tok)
            try:
                os.chmod(tokpath, 0o600)
            except OSError:
                pass
            if _validate_token(tok):
                _adopt_token(tok)
                _set(phase="done", message="登录成功（JWT 令牌）")
                return True
            _log("JWT 校验未通过，继续尝试其他路径")
            return False
        _log(f"token 交换({cid}) HTTP {st}：{txt[:160]}")
        err = d.get("error") if isinstance(d, dict) else None
        if err == "invalid_client":
            continue  # client_id 不对，码可能还没烧，换下一个 client_id
        break  # invalid_grant 等：授权码已作废，没必要再试
    return False


CLIENT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
             "WorkBuddy/5.8.3 Chrome/138.0.0.0 Electron/37.10.3 Safari/537.36")


def _validate_token(tok: str) -> bool:
    """JWT 用客户端 UA 实测余额接口（与 checkin.py 的 Bearer 路径同款）。"""
    req = urllib.request.Request(
        "https://www.codebuddy.cn/billing/meter/get-user-resource-summary",
        data=b"{}", method="POST",
        headers={"Authorization": f"Bearer {tok}", "User-Agent": CLIENT_UA,
                 "Accept": "application/json", "Content-Type": "application/json",
                 "Referer": "https://www.codebuddy.cn/home/",
                 "Origin": "https://www.codebuddy.cn"})
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(req, timeout=20) as r:
            ok = r.status == 200
    except Exception as e:  # noqa: BLE001
        _log(f"JWT 校验失败: {getattr(e, 'code', e)}")
        return False
    _log("JWT 校验通过：余额接口 200" if ok else "JWT 校验未通过")
    return ok


def _adopt_token(tok: str) -> None:
    """把 JWT 交给 checkin.py 的 token 兜底体系。"""
    # 轮询 state/token（TOKEN_FILE），写盘并触发一次看板刷新。
    target = os.environ.get("TOKEN_FILE", "/state/token")
    try:
        with open(target, "w", encoding="utf-8") as fp:
            fp.write(tok)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        left = token_remaining_days(tok)
        extra = f"，剩 {left:.1f} 天" if left is not None else ""
        _log(f"JWT 已写入 {target}{extra}")
        # 到期时间单独存一份，外部脚本不用解析 JWT。
        try:
            exp = _jwt_exp(tok)
            if exp:
                ep = os.path.join(os.path.dirname(target), "token.exp")
                with open(ep, "w", encoding="utf-8") as fp:
                    fp.write(str(exp) + "\n")
        except OSError:
            pass
    except OSError as e:
        _log(f"JWT 写盘失败 {e}，改写 cookies.json 供手工搬运")
    with open(COOKIE_FILE, "w", encoding="utf-8") as fp:
        json.dump({"cookies": {}, "token": tok, "at": int(time.time() * 1000)},
                  fp, indent=2)


def _jwt_exp(tok: str) -> int:
    """JWT 的 exp（epoch 秒），解析失败返回 0。"""
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return int(json.loads(base64.urlsafe_b64decode(p)).get("exp") or 0)
    except Exception:  # noqa: BLE001
        return 0


def _state_file(name: str) -> str:
    base = os.path.dirname(os.environ.get("TOKEN_FILE", "/state/token"))
    try:
        with open(os.path.join(base, name), encoding="utf-8") as fp:
            return fp.read().strip()
    except OSError:
        return ""


def token_remaining_days(tok: str = "") -> float | None:
    tok = tok or _state_file("token")
    if not tok:
        return None
    exp = _jwt_exp(tok)
    if not exp:
        return None
    return (exp - time.time()) / 86400.0


def try_silent_refresh() -> bool:
    """拿 refresh_token 静默换新 JWT。
    官方 refresh_token 属机密客户端，没有 client_secret 只会得到 unauthorized_client，这里只做能力探测。
    """
    rt = _state_file("refresh_token")
    if not rt:
        return False
    for cid in ("console", "account"):
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "client_id": cid,
            "refresh_token": rt,
        }).encode()
        req = urllib.request.Request(KC_TOKEN_URL, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("User-Agent", CLIENT_UA)
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:120]
            except Exception:  # noqa: BLE001
                pass
            _log(f"refresh_token 续期探测失败（client_id={cid}）："
                 f"{getattr(e, 'code', e)} {body}")
            continue
        tok = d.get("access_token") or ""
        if not tok:
            continue
        new_rt = d.get("refresh_token") or ""
        _adopt_token(tok)
        if new_rt:
            try:
                p = os.path.join(os.path.dirname(os.environ.get("TOKEN_FILE", "/state/token")),
                                 "refresh_token")
                with open(p, "w", encoding="utf-8") as fp:
                    fp.write(new_rt)
                os.chmod(p, 0o600)
            except OSError:
                pass
        _log(f"静默续期成功（client_id={cid}），新凭据已落盘")
        return True
    return False


def auto_renew_if_needed(threshold_days: float = 7.0) -> None:
    """后台调度用：快到期才动手，失败只记日志。"""
    left = token_remaining_days()
    if left is None:
        return
    if left > threshold_days:
        return
    _log(f"凭据剩 {left:.1f} 天（阈值 {threshold_days:.0f}），尝试静默续期…")
    if try_silent_refresh():
        return
    _log("静默续期不可用（官方机密客户端限制），到期前扫码一次即可重置 30 天")



def _run_api() -> bool:
    """纯 API 扫码，无浏览器：signin/wechat/login → qrconnect 取 uuid → 长轮询拿 wx_code
    → signin/verify 换 ticket → signin/finalize 换 code → POST signin/session 种 cookie。
    """
    op, cj = _api_opener()
    started = time.time()
    while time.time() - started < MAX_IDLE:
        if _stop.is_set():
            return False
        if _plugin_evt.is_set():
            _log("已有 state 完成登录，结束本轮引擎")
            return True
        # 官方桌面模式：先领 state，领不到退回普通网页模式。
        # platform 恒 web，redirect_uri 用 workbuddy.cn 域 signin-callback。
        desktop = _fetch_desktop_state(op)
        if desktop:
            state, auth_url = desktop
            redir = "https://www.workbuddy.cn/login/signin-callback?platform=workbuddy"
        else:
            state, auth_url, redir = "", "", CALLBACK_URI
        _set(phase="loading", message="正在向微信申请二维码…", ver=0)
        got = _new_qr_api(op, state=state, redirect_uri=redir)
        if not got:
            time.sleep(2)
            continue
        ss, uuid, _ = got
        # 图片是 qrconnect 页的相对路径，主机是 open。
        data = _http_get(f"https://open.weixin.qq.com/connect/qrcode/{uuid}",
                         referer="https://open.weixin.qq.com/connect/qrconnect")
        if data and _decode_qr(data):
            _save_qr_bytes(data)
        else:
            _log(f"二维码下载失败或不可解码（{len(data or b'')}B）")
            time.sleep(2)
            continue
        # 二维码编码的是微信确认页地址，手机可顶层打开；确认后 wx_code 仍进该 uuid 队列。
        _set(phase="waiting", message="请用微信扫码，并在手机上点「确认登录」",
             qr_link=f"https://open.weixin.qq.com/connect/confirm?uuid={uuid}",
             auth_url=auth_url or "")
        _log(f"API 引擎出码 uuid={uuid[:8]}…")
        # 微信内只能走官方登录页，码不回我们的 uuid 队列；官方会把令牌挂到 state 上，并行盯 plugin/auth/token。
        _ensure_watch(state)

        # 长轮询等扫码确认（uuid 约 4-5 分钟过期）
        wx_code = ""
        deadline = time.time() + 270
        while time.time() < deadline and not _stop.is_set():
            if _plugin_evt.is_set():
                _log("state 监视已在别处完成登录，跳过本轮的码等待")
                return True
            try:
                ec, code = _poll_wx(op, uuid)
            except Exception as e:  # noqa: BLE001
                _log(f"长轮询异常 {e}")
                time.sleep(2)
                continue
            if ec == "405" and code:
                wx_code = code
                break
            if ec in ("402", "403"):  # 过期/作废 → 换新码
                _log(f"二维码已失效（{ec}），自动换新")
                _set(phase="waiting", message="二维码已过期，正在换新码…")
                break
            if ec == "404":
                _set(phase="scanned", message="已扫码，请在手机上点「确认登录」")
            time.sleep(1.2)
        if not wx_code:
            continue

        # verify → finalize（域 / redirect_uri / Origin 与官方桌面页一致）
        _set(phase="scanned", message="已确认，正在建立会话…")
        org = "https://www.workbuddy.cn" if state else "https://www.codebuddy.cn"
        kc = f"{org}/auth-v2/realms/copilot"
        v = json.loads(_api_req(
            op, f"{kc}/signin/verify?session_state={urllib.parse.quote(ss)}"
                f"&type=wechat&code={urllib.parse.quote(wx_code)}", origin=org))
        if v.get("status") != "ready_to_login" or not v.get("ticket"):
            _log(f"verify 未就绪：{json.dumps(v, ensure_ascii=False)[:200]}")
            return False
        ticket = v["ticket"]
        f = json.loads(_api_req(
            op, f"{kc}/signin/finalize?ticket={urllib.parse.quote(ticket)}"
                f"&response_type=code&session_state={urllib.parse.quote(ss)}"
                f"&redirect_uri={urllib.parse.quote(redir, safe='')}", origin=org))
        auth_code, iss = f.get("code"), f.get("iss", "")
        f_state, f_redir = f.get("state") or "", f.get("redirect_uri") or ""
        _log(f"finalize 响应：{json.dumps(f, ensure_ascii=False)[:420]}")
        if not auth_code:
            _log(f"finalize 无 code：{json.dumps(f, ensure_ascii=False)[:200]}")
            return False
        # finalize 返回的 redirect_uri / state 直接当 OAuth2 参数用。
        if f_redir:
            redir = f_redir
        if f_state:
            _log(f"finalize state={f_state}（与插件 state {'一致' if f_state == state else '不同'}）")

        # 官方链路：finalize 返回 code/state/redirect_uri → 回调页 POST signin/session
        # → GET /console/auth/login?state= → GET /v2/plugin/auth/token 取 accessToken。
        if state:
            sess_redir = redir
            if f_state and "state=" not in sess_redir:
                sess_redir += ("&" if "?" in sess_redir else "?") + \
                    "state=" + urllib.parse.quote(f_state, safe="")
            # 先模拟浏览器跳到回调页（服务端可能在此识别 code）
            sep = "&" if "?" in redir else "?"
            _api_req_full(op, f"{redir}{sep}code={urllib.parse.quote(auth_code)}"
                              f"&state={urllib.parse.quote(f_state or '', safe='')}"
                              f"&iss={urllib.parse.quote(iss or '', safe='')}",
                          referer=f"{org}/login/started")
            body = json.dumps({"code": auth_code, "issuer": iss,
                               "redirect_uri": sess_redir}).encode()
            st, hdrs, sess_resp = _api_req_full(
                op, f"{org}/auth-v2/realms/copilot/signin/session", data=body,
                referer=redir, origin="https://www.workbuddy.cn")
            _log(f"signin/session(桌面绑定) HTTP {st}：{sess_resp[:160]}")
            # 把 state 绑定到这次登录会话。
            _console_bind(op, state)
            if _poll_plugin_token(op, state):
                return True
            _log("桌面绑定轮询未拿到令牌，走兜底路径")

        # 次选：Keycloak 标准授权码换 JWT
        if _try_token_exchange(op, auth_code):
            return True

        # 兜底：signin/session 短会话（官方回调页 redirect_uri）
        body = json.dumps({"code": auth_code, "issuer": iss,
                           "redirect_uri": CALLBACK_URI}).encode()
        st, hdrs, sess_resp = _api_req_full(op, f"{KC_BASE}/signin/session", data=body,
                                            referer=CALLBACK_URI)
        sc = hdrs.get_all("Set-Cookie") or []
        _log(f"signin/session HTTP {st}，Set-Cookie {len(sc)} 条 "
             f"{[c.split(';')[0][:40] + '…' for c in sc]}")
        if not sess_resp:
            sess_resp = "(空)"
        _log(f"signin/session 响应：{sess_resp[:200]}")

        # 浏览器流程里 session/session_2 是跳转 signin-callback 时种的，补一发同源 GET
        jar = {c.name: c.value for c in cj if c.domain.endswith("codebuddy.cn")}
        if not (jar.get("session") and jar.get("session_2")):
            sep = "&" if "?" in CALLBACK_URI else "?"
            st2, hdrs2, _ = _api_req_full(
                op, f"{CALLBACK_URI}{sep}code={urllib.parse.quote(auth_code)}")
            sc2 = hdrs2.get_all("Set-Cookie") or []
            _log(f"signin-callback HTTP {st2}，Set-Cookie {len(sc2)} 条 "
                 f"{[c.split(';')[0][:40] + '…' for c in sc2]}")

        jar = {c.name: c.value for c in cj if c.domain.endswith("codebuddy.cn")}
        _log(f"API 引擎取到 cookie {sorted(jar)}，session={len(jar.get('session',''))}B "
             f"session_2={len(jar.get('session_2',''))}B")
        if jar.get("session") and jar.get("session_2") and _validate_cookie(jar):
            with open(COOKIE_FILE, "w", encoding="utf-8") as fp:
                json.dump({"cookies": jar, "at": int(time.time() * 1000)}, fp, indent=2)
            _adopt()
            return True
        _log("API 引擎会话校验未通过，转浏览器兜底")
        return False
    _set(phase="error", message="等待扫码超时，请刷新二维码")
    return True  # 超时不算引擎故障，不再进浏览器



def _run_browser() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        _set(phase="error", message=f"容器缺少 playwright：{e}")
        _log(f"playwright 导入失败 {e}")
        return

    last = {"src": "", "hash": ""}
    started = time.time()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                      "--disable-software-rasterizer"],
            )
            ctx = browser.new_context(viewport={"width": 1200, "height": 900},
                                      locale="zh-CN", timezone_id="Asia/Shanghai",
                                      device_scale_factor=3)  # 3 倍像素密度，截图裁剪的二维码够清晰
            page = ctx.new_page()

            if not _ensure_qr(page, last):
                _set(phase="error", message="登录页未渲染出二维码，请重试")
                _log(f"未找到二维码：{page.url}")
                browser.close()
                return

            _set(phase="waiting", message="请用微信扫码，并在手机上点「确认登录」", ver=_state["ver"])

            # 等扫码完成
            done = False
            last_refresh = time.time()
            while time.time() - started < MAX_IDLE:
                if _stop.is_set():
                    _log("收到停止信号")
                    browser.close()
                    return
                time.sleep(1.5)
                try:
                    url = page.url or ""
                    body = page.inner_text("body") or ""
                except Exception:
                    continue
                if "/login" not in url:
                    done = True
                    _log(f"已离开登录页 → {url}")
                    break
                if any(k in body for k in ("登录成功", "正在进入", "进入工作台")):
                    done = True
                    _log("页面提示登录成功")
                    break
                # 二维码到期，重载换新码
                if time.time() - last_refresh > QR_REFRESH:
                    last_refresh = time.time()
                    _log("二维码到期，重新获取")
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                    last["src"] = ""
                    last["hash"] = ""
                    if not _ensure_qr(page, last):
                        _set(phase="error", message="二维码刷新失败，请点重试")
                        browser.close()
                        return
                    _set(phase="waiting", message="请用微信扫码，并在手机上点「确认登录」")

            if not done:
                _set(phase="timeout", message="等待扫码超时，请刷新二维码")
                _log("超时")
                browser.close()
                return

            _set(phase="scanned", message="已确认，正在提取登录凭据…", ver=_state["ver"])

            # 扫码确认后会话 cookie 要等后续跳转才逐步落地，所以持续导出并实测。
            # 落在团队选择页时跳回个人空间入口。
            jar, has, valid = {}, False, False
            tried_home = False
            settle_deadline = time.time() + 45
            while time.time() < settle_deadline:
                time.sleep(3)
                try:
                    url = page.url or ""
                except Exception:
                    continue
                jar = {}
                for c in ctx.cookies():
                    dom = (c.get("domain") or "").lstrip(".")
                    if dom.endswith("codebuddy.cn"):
                        jar[c["name"]] = c["value"]
                has = bool(jar.get("session") and jar.get("session_2"))
                if has and _validate_cookie(jar):
                    valid = True
                    _log(f"cookie 校验通过（当前页面 {url[:70]}）")
                    break
                # 落在团队选择页 → 回个人空间入口，触发个人版会话
                if not tried_home and "/admin" in url:
                    tried_home = True
                    try:
                        body = (page.inner_text("body") or "").replace("\n", " ")[:150]
                    except Exception:
                        body = "?"
                    _log(f"落到 {url[:70]} | 页面: {body}")
                    try:
                        page.goto("https://www.codebuddy.cn/home/",
                                  wait_until="domcontentloaded", timeout=45000)
                    except Exception:
                        pass
            if not has:
                _set(phase="error", message="登录成功但未取到完整凭据，请重试")
                browser.close()
                return
            with open(COOKIE_FILE, "w", encoding="utf-8") as f:
                json.dump({"cookies": jar, "at": int(time.time() * 1000)}, f, indent=2)
            _log(f"导出 cookie {len(jar)} 项，session={len(jar.get('session',''))}B "
                 f"session_2={len(jar.get('session_2',''))}B")
            browser.close()

            if valid:
                _adopt()
            else:
                _set(phase="error",
                     message="登录会话无效（落到了企业版控制台），请点「重新扫码」")
    except Exception as e:  # noqa: BLE001
        _set(phase="error", message=f"登录流程异常：{type(e).__name__}: {e}")
        _log(f"异常 {e}")


def _validate_cookie(jar: dict) -> bool:
    """导出后立刻实测一次：cookie 能调通余额接口才算登录成功，否则会把无效会话当成功收下。"""
    cookie = "; ".join(f"{k}={jar[k]}" for k in ("session", "session_2") if jar.get(k))
    req = urllib.request.Request(
        "https://www.codebuddy.cn/billing/meter/get-user-resource-summary",
        data=b"{}", method="POST",
        headers={"Cookie": cookie, "User-Agent": session.UA,
                 "Accept": "application/json", "Content-Type": "application/json",
                 "Referer": "https://www.codebuddy.cn/home/",
                 "Origin": "https://www.codebuddy.cn"})
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(req, timeout=20) as r:
            return r.status == 200
    except Exception as e:  # noqa: BLE001
        _log(f"cookie 校验失败: {getattr(e, 'code', e)}")
        return False


def _adopt() -> dict:
    """把导出的 cookie 落成会话；JWT 交换成功时这里只负责报「已生效」。"""
    try:
        d = json.loads(open(COOKIE_FILE, encoding="utf-8").read())
    except Exception:
        return {"ok": False, "msg": "还没有取到登录凭据"}
    if d.get("token"):
        return {"ok": True, "msg": "JWT 令牌已生效"}
    jar = d.get("cookies") or {}
    if not (jar.get("session") and jar.get("session_2")):
        return {"ok": False, "msg": "凭据不完整，请重新扫码"}
    session.save(jar, meta={"source": "qr-login", "via": "server-playwright"})
    _set(phase="done", message="登录成功", ver=_state["ver"])
    return {"ok": True, "msg": "已登录"}



def start(force: bool = False) -> dict:
    global _thread
    with _lock:
        cur = read_state()
        running = alive() and cur.get("phase") in ("loading", "waiting", "scanned")
        if running and not force:
            qr_at = cur.get("qr_at") or cur.get("ts") or 0
            if time.time() * 1000 - qr_at < 240_000:
                return {"ok": True, "msg": "登录已在进行中"}
            force = True
        if alive():
            _stop.set()
            if _thread:
                _thread.join(timeout=8)
            _stop.clear()
        for name in (STATE_FILE, QR_FILE, COOKIE_FILE):
            try:
                os.remove(name)
            except FileNotFoundError:
                pass
        # 新一轮登录重置 state 监视（旧线程会因 _plugin_evt 或超时退出）。
        _plugin_evt.clear()
        with _watch_lock:
            _watch_states.clear()
        _state.update({"phase": "loading", "message": "正在打开官方登录页…", "ver": 0})
        _set()
        _thread = threading.Thread(target=_run, daemon=True)
        _thread.start()
        return {"ok": True, "msg": "已刷新，正在获取二维码" if force else "已发起登录"}


def sync() -> dict:
    """兜底：扫码成功但等待线程没跟上时，从 cookies.json 恢复。"""
    return _adopt()


# 手机号登录：微信内置浏览器既不能长按识别二维码，也拿不到官方登录页的回调，只能走纯接口。
# GET /signin/phone/login → POST phone/sms → POST phone/verify → GET /signin/finalize
# → 回调页 POST signin/session → console/auth/login → /v2/plugin/auth/token
# 风控返回 captcha_required（腾讯防水墙）时，返回 appid 让前端弹验证码。

PHONE_TO = 1800


def _wjson(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(obj, fp, ensure_ascii=False)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _rjson(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp) or {}
    except (OSError, ValueError):
        return {}


def _post_json(op, url: str, obj: dict | None = None,
               origin: str = "https://www.workbuddy.cn", timeout: int = 40):
    """POST JSON，失败也把响应体解析出来（要读 error=captcha_required）。"""
    body = json.dumps(obj or {}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Referer": f"{origin}/login/",
        "Origin": origin,
    })
    try:
        with op.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            st = r.status
    except urllib.error.HTTPError as e:
        raw, st = e.read().decode("utf-8", "replace"), e.code
    except Exception as e:  # noqa: BLE001
        return 0, {"_err": f"{type(e).__name__}: {e}"}
    try:
        return st, json.loads(raw or "{}")
    except ValueError:
        return st, {"_raw": raw[:300]}


def _zh_err(d: dict, fallback: str) -> str:
    """官方错误是英文短句，翻成人话再给前端。"""
    desc = str(d.get("error_description") or d.get("error") or d.get("_raw") or "")
    low = desc.lower()
    if "sms code" in low or "invalid_sms_code" in low:
        return "验证码不对或已过期，请重新获取"
    if "too many" in low or "frequent" in low or "rate" in low or "cooldown" in low:
        return "操作太频繁，请稍后再试"
    if "captcha" in low:
        return "需要人机验证"
    if "phone" in low and "invalid" in low:
        return "手机号格式不对"
    if not desc or re.search(r"[A-Za-z]{5,}", desc):
        return fallback
    return desc


def phone_begin(workdir: str, phone: str, captcha_ticket: str = "",
                captcha_randstr: str = "") -> dict:
    """发短信验证码。workdir 是该浏览器会话的目录（并发安全）。"""
    digits = re.sub(r"\D", "", phone or "")
    if not re.fullmatch(r"1[3-9]\d{9}", digits):
        return {"ok": False, "msg": "请输入 11 位手机号"}
    op, _ = _api_opener()
    org = "https://www.workbuddy.cn"
    kc = f"{org}/auth-v2/realms/copilot"
    pf = os.path.join(workdir, "phone.json")
    ctx = _rjson(pf)
    fresh = (ctx.get("phone") == digits and ctx.get("ss")
             and time.time() - float(ctx.get("ts") or 0) < PHONE_TO)
    if fresh:
        _log(f"手机号登录：复用会话 {str(ctx.get('ss'))[:10]}…")
    else:
        st = _fetch_desktop_state(op)
        state = st[0] if st else ""
        redir = f"{org}/login/signin-callback?platform=workbuddy"
        q = urllib.parse.quote(redir, safe="")
        extra = f"&state={urllib.parse.quote(state, safe='')}" if state else ""
        try:
            d = json.loads(_api_req(
                op, f"{kc}/signin/phone/login?platform=web&redirect_uri={q}{extra}",
                origin=org))
        except Exception as e:  # noqa: BLE001
            _log(f"phone/login 失败：{e}")
            return {"ok": False, "msg": "连不上官方登录服务，请稍后再试"}
        ss = d.get("session_state") or ""
        if not ss:
            _log(f"phone/login 无 session_state：{json.dumps(d)[:200]}")
            return {"ok": False, "msg": "官方未返回登录会话，请稍后再试"}
        ctx = {"ss": ss, "state": state, "phone": digits, "ts": int(time.time()), "redir": redir}
        _log(f"手机号登录：新会话 {ss[:10]}… state={state[:8] or '-'}")
    body = {"session_state": ctx["ss"], "phone": "+86" + digits}
    if captcha_ticket:
        body["captcha_ticket"] = captcha_ticket
        body["captcha_randstr"] = captcha_randstr
    code, d = _post_json(op, f"{kc}/signin/phone/sms", body, origin=org)
    _log(f"phone/sms HTTP {code}：{json.dumps(d, ensure_ascii=False)[:220]}")
    if str(d.get("error")) == "captcha_required" or str(d.get("status")) == "captcha_required":
        return {"ok": False, "captcha": True,
                "appid": d.get("captchaAppId") or d.get("captcha_appid") or "",
                "msg": "需要人机验证"}
    if str(d.get("status")) in ("ok", "cooldown"):
        ctx["ts"] = int(time.time())
        _wjson(pf, ctx)
        ra = d.get("retry_after")
        return {"ok": True, "msg": "验证码已发送", "retry_after": ra if isinstance(ra, int) else 60}
    return {"ok": False, "msg": _zh_err(d, "发送失败，请稍后再试")}


def _phone_grab_token(op, state: str, workdir: str, timeout: int = 90) -> bool:
    """轮询插件令牌并落盘（与 _poll_plugin_token 同协议，但不碰全局 state）。"""
    deadline = time.time() + timeout
    n = 0
    while time.time() < deadline:
        n += 1
        try:
            req = urllib.request.Request(
                f"{WB_PLUGIN}/auth/token?state=" + urllib.parse.quote(state, safe=""))
            req.add_header("User-Agent", CLIENT_UA)
            req.add_header("Accept", "application/json")
            req.add_header("X-No-Authorization", "true")
            req.add_header("X-No-User-Id", "true")
            with op.open(req, timeout=15) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            if n % 8 == 1:
                _log(f"手机号令牌轮询异常：{getattr(e, 'code', e)}")
            time.sleep(2.5)
            continue
        data = d.get("data") or {}
        tok = data.get("accessToken") or data.get("access_token") or ""
        if d.get("code") == 0 and tok:
            if not _validate_token(tok):
                _log("手机号登录：拿到令牌但余额校验未通过")
                return False
            with open(os.path.join(workdir, "token"), "w", encoding="utf-8") as fp:
                fp.write(tok)
            try:
                os.chmod(os.path.join(workdir, "token"), 0o600)
                exp = _jwt_exp(tok)
                if exp:
                    with open(os.path.join(workdir, "token.exp"), "w", encoding="utf-8") as fp:
                        fp.write(str(exp) + "\n")
            except OSError:
                pass
            rt = data.get("refreshToken") or ""
            if rt:
                try:
                    with open(os.path.join(workdir, "refresh_token"), "w",
                              encoding="utf-8") as fp:
                        fp.write(rt)
                    os.chmod(os.path.join(workdir, "refresh_token"), 0o600)
                except OSError:
                    pass
            left = token_remaining_days(tok)
            _log(f"手机号登录成功 ✅（轮询 {n} 次，剩 {left:.1f} 天）" if left else
                 f"手机号登录成功 ✅（轮询 {n} 次）")
            _wjson(os.path.join(workdir, "state.json"),
                   {"phase": "done", "message": "登录成功", "ver": 0,
                    "ts": int(time.time() * 1000)})
            return True
        if n == 1 or n % 10 == 0:
            _log(f"手机号令牌轮询 {n} 次：code={d.get('code')} {str(d.get('msg'))[:40]}")
        time.sleep(2.5)
    _log(f"手机号令牌轮询 {timeout}s 超时（{n} 次）")
    return False


def phone_verify(workdir: str, sms_code: str) -> dict:
    """校验短信码 → 换码 → 绑定 → 取令牌，一步到位。"""
    code = re.sub(r"\D", "", sms_code or "")
    if len(code) != 6:
        return {"ok": False, "msg": "请输入 6 位短信验证码"}
    pf = os.path.join(workdir, "phone.json")
    ctx = _rjson(pf)
    if not ctx.get("ss"):
        return {"ok": False, "msg": "请先获取验证码"}
    if time.time() - float(ctx.get("ts") or 0) > PHONE_TO:
        return {"ok": False, "msg": "验证码已过期，请重新获取"}
    op, _ = _api_opener()
    org = "https://www.workbuddy.cn"
    kc = f"{org}/auth-v2/realms/copilot"
    st, d = _post_json(op, f"{kc}/signin/phone/verify", {
        "session_state": ctx["ss"], "phone": "+86" + str(ctx.get("phone")), "sms_code": code},
        origin=org)
    _log(f"phone/verify HTTP {st}：{json.dumps(d, ensure_ascii=False)[:220]}")
    if str(d.get("error")) == "captcha_required":
        return {"ok": False, "captcha": True,
                "appid": d.get("captchaAppId") or d.get("captcha_appid") or "",
                "msg": "需要人机验证"}
    status = str(d.get("status") or "")
    if status == "confirm_bind":
        return {"ok": False, "msg": "该手机号需二次绑定确认，请改用微信扫码登录"}
    if status == "oneid_required":
        return {"ok": False, "msg": "该账号属于企业统一登录，请改用微信扫码"}
    ticket = d.get("ticket") or ""
    if status != "ready_to_login" or not ticket:
        return {"ok": False, "msg": _zh_err(d, "验证码不对或已过期，请重新获取")}
    ctx["workdir"] = workdir
    return _phone_finish(op, ctx, ticket)


def _phone_finish(op, ctx: dict, ticket: str) -> dict:
    org = "https://www.workbuddy.cn"
    kc = f"{org}/auth-v2/realms/copilot"
    ss = ctx["ss"]
    state = ctx.get("state") or ""
    wd = ctx.get("workdir") or ""
    redir = ctx.get("redir") or f"{org}/login/signin-callback?platform=workbuddy"
    try:
        f = json.loads(_api_req(
            op, f"{kc}/signin/finalize?ticket={urllib.parse.quote(ticket)}"
                f"&response_type=code&session_state={urllib.parse.quote(ss)}"
                f"&redirect_uri={urllib.parse.quote(redir, safe='')}", origin=org))
    except Exception as e:  # noqa: BLE001
        _log(f"手机号 finalize 失败：{e}")
        return {"ok": False, "msg": "官方未完成授权，请重试"}
    _log(f"手机号 finalize：{json.dumps(f, ensure_ascii=False)[:320]}")
    auth_code, iss = f.get("code") or "", f.get("iss") or ""
    f_state = f.get("state") or ""
    if f.get("redirect_uri"):
        redir = f["redirect_uri"]
    if not auth_code:
        return {"ok": False, "msg": "官方没有返回登录码，请重试"}

    if state:
        sess_redir = redir
        if f_state and "state=" not in sess_redir:
            sess_redir += ("&" if "?" in sess_redir else "?") + \
                "state=" + urllib.parse.quote(f_state, safe="")
        sep = "&" if "?" in redir else "?"
        _api_req_full(op, f"{redir}{sep}code={urllib.parse.quote(auth_code)}"
                          f"&state={urllib.parse.quote(f_state, safe='')}"
                          f"&iss={urllib.parse.quote(iss, safe='')}",
                      referer=f"{org}/login/started")
        body = json.dumps({"code": auth_code, "issuer": iss,
                           "redirect_uri": sess_redir}).encode()
        st, _h, resp = _api_req_full(op, f"{kc}/signin/session", data=body,
                                     referer=redir, origin=org)
        _log(f"手机号 signin/session HTTP {st}：{resp[:160]}")
        _console_bind(op, state)
        if _phone_grab_token(op, state, wd):
            return {"ok": True, "msg": "登录成功"}
        _log("手机号登录：桌面绑定没拿到令牌，试标准换码")

    if _phone_exchange(op, auth_code, wd):
        return {"ok": True, "msg": "登录成功"}
    return {"ok": False, "msg": "登录已通过但没拿到令牌，请重试"}


def _phone_exchange(op, auth_code: str, workdir: str) -> bool:
    """兜底：授权码换 JWT，直接写本会话目录（不碰全局 token）。"""
    for cid in ("console", "workbuddy", "codebuddy", "copilot"):
        body = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": CALLBACK_URI,
            "client_id": cid,
        }).encode()
        st, _h, txt = _api_req_full(op, KC_TOKEN_URL, data=body,
                                    referer="https://www.codebuddy.cn/",
                                    ctype="application/x-www-form-urlencoded")
        try:
            d = json.loads(txt)
        except Exception:  # noqa: BLE001
            d = {}
        if isinstance(d, dict) and d.get("access_token"):
            tok = d["access_token"]
            if not _validate_token(tok):
                _log("手机号换码：JWT 校验未通过")
                return False
            with open(os.path.join(workdir, "token"), "w", encoding="utf-8") as fp:
                fp.write(tok)
            try:
                os.chmod(os.path.join(workdir, "token"), 0o600)
            except OSError:
                pass
            if d.get("refresh_token"):
                try:
                    with open(os.path.join(workdir, "refresh_token"), "w",
                              encoding="utf-8") as fp:
                        fp.write(d["refresh_token"])
                    os.chmod(os.path.join(workdir, "refresh_token"), 0o600)
                except OSError:
                    pass
            _log(f"手机号换码成功（client_id={cid}）")
            _wjson(os.path.join(workdir, "state.json"),
                   {"phase": "done", "message": "登录成功", "ver": 0,
                    "ts": int(time.time() * 1000)})
            return True
        err = d.get("error") if isinstance(d, dict) else None
        _log(f"手机号换码({cid}) HTTP {st}：{txt[:120]}")
        if err == "invalid_client":
            continue
        break
    return False


def run_once_blocking() -> bool:
    """多用户 worker 子进程入口：跑完一次 API 扫码后退出，各自用独立的 LOGIN_DIR / TOKEN_FILE。"""
    try:
        ok = bool(_run_api())
        if ok:
            return True
        # 引擎没拿到码，但 state 监视可能稍后才收到。
        if _plugin_evt.wait(60):
            _log("引擎退出后由 state 监视补收令牌 ✅")
            return True
        return False
    except Exception as e:  # noqa: BLE001
        _log(f"worker 异常退出：{e}")
        try:
            _set(phase="error", message=f"登录失败：{e}")
        except Exception:  # noqa: BLE001
            pass
        return False
