"""凭据与积分查询（纯标准库，无浏览器依赖）。

认证是 cookie 会话，不是 Bearer token：必需 cookie 只有 session + session_2，缺一即 401；
session 值形如 `<sid>|<过期时间戳>|<签名>`，有效期 7 天；总长必须 < 8KB（网关 APISIX
large_client_header_buffers），多带无关 cookie 会把必需项挤出去。

  POST /billing/meter/get-user-resource-summary  积分汇总
  GET  /console/accounts                          账号信息
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

BASE = os.environ.get("WB_BASE", "https://www.codebuddy.cn")
BILLING_URL = BASE + "/billing/meter/get-user-resource-summary"
ACCOUNTS_URL = BASE + "/console/accounts"

# UA 必须声明 HeadlessChrome，否则网关 401；声称真实浏览器但没有 JS 指纹同样被拒。
# WebView 环境有 JS 指纹，可保留原 UA。
UA = os.environ.get(
    "WB_UA",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) HeadlessChrome/158.0.0.0 Safari/537.36",
)

# 只发这两个，必需且充分。
REQUIRED = ("session", "session_2")

STATE_DIR = os.environ.get("STATE_DIR", os.path.join(os.path.dirname(__file__), "..", "state"))
SESSION_FILE = os.path.join(STATE_DIR, "session.json")


# 存储


def save_session(cookies: dict, meta: dict | None = None) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    payload = {
        "cookies": {k: v for k, v in cookies.items() if k in REQUIRED},
        "saved_at": int(time.time()),
        "meta": meta or {},
    }
    tmp = SESSION_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SESSION_FILE)
    try:
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        pass


def load_session() -> dict | None:
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return None
    if not d.get("cookies"):
        return None
    return d


def clear_session() -> None:
    try:
        os.remove(SESSION_FILE)
    except FileNotFoundError:
        pass


# cookie 工具


def cookie_header(cookies: dict) -> str:
    """按必需顺序拼 Cookie，超长直接截断保护（网关 8KB 上限）。"""
    parts = [f"{k}={cookies[k]}" for k in REQUIRED if cookies.get(k)]
    return "; ".join(parts)


def session_expiry(cookies: dict) -> int | None:
    """从 session 值 `<sid>|<ts>|<sig>` 里抠过期时间戳。"""
    raw = cookies.get("session") or ""
    seg = raw.split("|")
    if len(seg) >= 2:
        try:
            return int(seg[1])
        except ValueError:
            return None
    return None


def fingerprint(cookies: dict) -> str:
    raw = cookies.get("session") or ""
    return raw.split("|")[0][:12] if raw else ""


# HTTP


# 直连，忽略 HTTP_PROXY/HTTPS_PROXY：代理对 6KB 的 Cookie 头处理异常，
# 同样的 cookie 在 curl 下 200、在 urllib 下 401。需要代理时用 WB_USE_PROXY=1。
if os.environ.get("WB_USE_PROXY") == "1":
    _OPENER = urllib.request.build_opener()
else:
    _OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _call(url: str, cookies: dict, method: str = "POST", body: bytes = b"{}") -> tuple[int, str]:
    headers = {
        "Cookie": cookie_header(cookies),
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": BASE + "/home/",
        "Origin": BASE,
    }
    if method == "POST":
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body if method == "POST" else None, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=25) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def fetch_accounts(cookies: dict) -> dict:
    st, body = _call(ACCOUNTS_URL, cookies, "GET")
    if st != 200:
        return {"ok": False, "http": st, "msg": "cookie 失效或未登录"}
    try:
        j = json.loads(body)
    except Exception:
        return {"ok": False, "http": st, "msg": "响应不是 JSON"}
    accts = (j.get("data") or {}).get("accounts") or []
    cur = next((a for a in accts if a.get("lastLogin")), accts[0] if accts else None)
    if not cur:
        return {"ok": False, "http": st, "msg": "没有可用账号"}
    return {
        "ok": True,
        "uid": cur.get("uid"),
        "nickname": cur.get("nickname"),
        "uin": cur.get("uin"),
        "type": cur.get("type"),
    }


def fetch_credits(cookies: dict) -> dict:
    st, body = _call(BILLING_URL, cookies, "POST")
    if st != 200:
        return {"ok": False, "http": st, "msg": "cookie 失效或未登录" if st in (401, 302) else f"HTTP {st}"}
    try:
        j = json.loads(body)
    except Exception:
        return {"ok": False, "http": st, "msg": "响应不是 JSON"}
    if j.get("code") != 0:
        return {"ok": False, "http": st, "msg": j.get("msg") or "接口返回异常"}
    data = j.get("data") or {}
    pkgs = data.get("Packages") or []

    def f(x):
        try:
            return float(x or 0)
        except (TypeError, ValueError):
            return 0.0

    total = sum(f(p.get("CycleTotalCapacity")) for p in pkgs)
    left = sum(f(p.get("CycleRemainCapacity")) for p in pkgs)
    used = sum(f(p.get("CycleUsedCapacity")) for p in pkgs)

    groups = []
    for g in data.get("CreditGroups") or []:
        groups.append(
            {
                "kind": g.get("Kind"),
                "name": g.get("Name"),
                "total": f(g.get("CycleTotalCapacity")),
                "left": f(g.get("CycleRemainCapacity")),
                "used": f(g.get("CycleUsedCapacity")),
                "count": g.get("TotalCount") or 0,
            }
        )

    packages = []
    # 保持接口原序（通常按额度降序）
    for p in pkgs:
        packages.append(
            {
                "code": p.get("PackageCode") or "",
                "total": f(p.get("CycleTotalCapacity")),
                "left": f(p.get("CycleRemainCapacity")),
                "used": f(p.get("CycleUsedCapacity")),
                "count": p.get("TotalCount") or 0,
            }
        )

    return {
        "ok": True,
        "left": round(left, 2),
        "total": round(total, 2),
        "used": round(used, 2),
        "official_left": round(f(data.get("TotalCredits")), 2),
        "plan": data.get("SubscriptionPackageName") or "",
        "paid": bool(data.get("IsPaidUser")),
        "packages": len(pkgs),
        "package_list": packages,
        "groups": groups,
        "fetched_at": int(time.time()),
    }


def status() -> dict:
    """给前端用的一站式状态。"""
    sess = load_session()
    out: dict = {"logged_in": False}
    if not sess:
        return out
    cookies = sess["cookies"]
    exp = session_expiry(cookies)
    out.update(
        {
            "logged_in": True,
            "saved_at": sess.get("saved_at"),
            "expires_at": exp,
            "expired": bool(exp and exp < time.time()),
            "fingerprint": fingerprint(cookies),
            "meta": sess.get("meta") or {},
        }
    )
    acct = fetch_accounts(cookies)
    if acct.get("ok"):
        out["account"] = {k: acct[k] for k in ("uid", "nickname", "uin", "type") if acct.get(k)}
    credit = fetch_credits(cookies)
    if credit.get("ok"):
        out["credit"] = credit
    else:
        out["credit_error"] = credit.get("msg")
    return out
