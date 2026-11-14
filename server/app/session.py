"""扫码登录得到的 cookie 会话（纯标准库）。

两条认证路径打的都是 /billing/meter/*：
  1. Bearer token   桌面客户端的 accessToken（JWT，7 天），UA 必须是客户端 UA（WorkBuddy/x Electron/y）
  2. Cookie 会话    扫码后的 session + session_2（7 天），UA 必须是 HeadlessChrome

本模块只管第 2 条，第 1 条由 checkin.py 的 state/token 负责。

约束：必需 cookie 只有 session + session_2，缺一即 401；总长 < 8KB（网关 APISIX
large_client_header_buffers 8k）；session 值形如 `<sid>|<过期时间戳>|<签名>`。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

REQUIRED = ("session", "session_2")

# UA 必须如实声明 HeadlessChrome：Chrome/Safari/curl/Python-urllib 全 401。
UA = os.environ.get(
    "COOKIE_UA",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) HeadlessChrome/158.0.0.0 Safari/537.36",
)

STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))
SESSION_FILE = STATE_DIR / "session.json"

_cache: dict = {"mtime": None, "header": "", "cookies": {}}


def save(cookies: dict, meta: dict | None = None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "cookies": {k: v for k, v in cookies.items() if k in REQUIRED},
        "saved_at": int(time.time()),
        "meta": meta or {},
    }
    tmp = str(SESSION_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SESSION_FILE)
    try:
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        pass
    _cache["mtime"] = None  # mtime 变了就重读


def load() -> dict | None:
    try:
        d = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    ck = d.get("cookies") or {}
    if not (ck.get("session") and ck.get("session_2")):
        return None
    return d


def clear() -> None:
    try:
        SESSION_FILE.unlink()
    except FileNotFoundError:
        pass
    _cache["mtime"] = None


def header() -> str:
    """拼 Cookie 头；无会话返回空串。带 mtime 缓存，避免每次请求都读盘。"""
    try:
        m = SESSION_FILE.stat().st_mtime
    except OSError:
        return ""
    if m != _cache["mtime"]:
        d = load()
        ck = (d or {}).get("cookies") or {}
        _cache["header"] = "; ".join(f"{k}={ck[k]}" for k in REQUIRED if ck.get(k))
        _cache["cookies"] = ck
        _cache["mtime"] = m
    return _cache["header"]


def expiry() -> int | None:
    ck = _cache["cookies"] or ((load() or {}).get("cookies") or {})
    seg = (ck.get("session") or "").split("|")
    if len(seg) >= 2:
        try:
            return int(seg[1])
        except ValueError:
            return None
    return None


def fingerprint() -> str:
    ck = _cache["cookies"] or ((load() or {}).get("cookies") or {})
    raw = ck.get("session") or ""
    return raw.split("|")[0][:12] if raw else ""


def describe() -> dict:
    """给看板/状态页用的一次性摘要。"""
    d = load()
    if not d:
        return {"ok": False}
    exp = expiry()
    return {
        "ok": True,
        "saved_at": d.get("saved_at"),
        "expires_at": exp,
        "expired": bool(exp and exp < time.time()),
        "fingerprint": fingerprint(),
        "meta": d.get("meta") or {},
    }
