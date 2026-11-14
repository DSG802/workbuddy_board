#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 每日签到 + 积分看板（纯标准库）。

签到在 CHECKIN_WINDOW（默认 09:00–12:00）内按随机时刻表尝试，幂等，领到即停；
派猫首次落在 TRAVEL_WINDOW，之后只在 TRAVEL_DAY_WINDOW 内继续。

看板：剩余积分 / 今日是否已领 / 连续天数 / 领取日历 / 历史。

接口（POST，Authorization: Bearer <accessToken>）：
  /billing/meter/checkin-activity-status      活动状态（今日是否已领、连续天数、奖励）
  /billing/meter/checkin-status               简易状态（降级用）
  /billing/meter/daily-checkin                执行签到
  /billing/meter/get-user-resource-summary    积分余额（Packages[].CycleRemainCapacity）

状态落 state/status.json + state/history.jsonl。令牌不进日志，只记 sha256 前 8 位。

环境变量：TOKEN_FILE TOKEN API_BASE API_V2 X_USER_ID TZ_OFFSET RUN_ON_START
  CHECKIN_WINDOW(9-12) CHECKIN_TRIES(4) TRAVEL_WINDOW(9-12) TRAVEL_DAY_WINDOW(9-22)
  PORT STATE_DIR PUSH_SECRET WEBHOOK_URL BALANCE_MINUTES
"""

import base64
import hashlib
import hmac
import json
import os
import random
import secrets
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import uuid
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import login       # noqa: E402  扫码登录编排
import login_page  # noqa: E402  未登录时的扫码页
import session     # noqa: E402  cookie 会话
import store       # noqa: E402  SQLite 持久层（账户/会话/流水/快照）

BASE = os.environ.get("API_BASE", "https://copilot.tencent.com").rstrip("/")
PREFER_V2 = os.environ.get("API_V2", "0") == "1"
TOKEN_FILE = Path(os.environ.get("TOKEN_FILE", "/state/token"))
STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))
STATUS_FILE = STATE_DIR / "status.json"
HISTORY_FILE = STATE_DIR / "history.jsonl"
X_USER_ID = os.environ.get("X_USER_ID", "").strip()
PUSH_SECRET = os.environ.get("PUSH_SECRET", "").strip()
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
RUN_ON_START = os.environ.get("RUN_ON_START", "1") == "1"
BALANCE_MINUTES = int(os.environ.get("BALANCE_MINUTES", "30"))
PORT = int(os.environ.get("PORT", "8080"))
# 桌面客户端只监听回环。
BIND_ADDR = os.environ.get("BIND_ADDR", "0.0.0.0")
# 桌面端允许用本机账户接回会话；服务器多用户模式必须为 0，否则谁打开都变成第一个账户。
DESKTOP_MODE = os.environ.get("WB_DESKTOP") == "1"
# 桌面端和安卓都要固定设备号，但安卓不能开 DESKTOP_MODE（会连带会话接回等桌面行为），单独一个开关。
DEV_FILE_MODE = DESKTOP_MODE or os.environ.get("WB_DEV_FILE") == "1"
TZ = timezone(timedelta(hours=float(os.environ.get("TZ_OFFSET", "8"))))
ATTEMPT_HOURS = sorted(
    {int(x) for x in os.environ.get("ATTEMPT_HOURS", "6,9,12,15,18,21").split(",") if x.strip().isdigit()}
)
HTTP_TIMEOUT = 20


def _parse_win(raw: str, default: str) -> tuple:
    """解析 "9-12" 这种小时区间（含起点、不含终点）。"""
    stack = [str(raw or ""), default]
    while stack:
        cand = stack.pop(0).replace("－", "-").replace("~", "-").replace("—", "-").strip()
        try:
            lo_s, hi_s = cand.split("-")
            lo, hi = int(lo_s), int(hi_s)
            if 0 <= lo < hi <= 24:
                return lo, hi
        except Exception:  # noqa: BLE001
            continue
    return 9, 12


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, default))))
    except Exception:  # noqa: BLE001
        return default


CHECKIN_WINDOW = _parse_win(os.environ.get("CHECKIN_WINDOW"), "9-12")        # 自动签到时段
BUILD_TAG = os.environ.get("BUILD_TAG", "2026-11-13.2205")   # 页面右下角显示，用来核对装的是哪版
CHECKIN_TRIES = _env_int("CHECKIN_TRIES", 4, 1, 8)                            # 时段内随机尝试几次
TRAVEL_WINDOW = _parse_win(os.environ.get("TRAVEL_WINDOW"), "9-12")          # 当天第一次派猫
TRAVEL_DAY_WINDOW = _parse_win(os.environ.get("TRAVEL_DAY_WINDOW"), "9-22")  # 白天窗口：多次旅行 / 领奖

# 签到与派猫只在 09:00–12:00 窗口内触发，窗口外不发请求。
# 旅行状态机以服务端 state 为准。
TRAVEL_ENABLE = os.environ.get("TRAVEL_ENABLE", "1") == "1"
# 存档模式只展示不写入：同一账号挂两个实例会互相抢派遣与令牌续期。
ARCHIVE_MODE = os.environ.get("ARCHIVE_MODE", "0") == "1"
TRAVEL_MINUTES = int(os.environ.get("TRAVEL_MINUTES", "10"))
# 目的地：1 咖啡馆 / 2 商场店铺
TRAVEL_LOCATION_ID = int(os.environ.get("TRAVEL_LOCATION_ID", "1"))


def _in_window(win: tuple, t: datetime = None) -> bool:
    """t 是否落在 [lo, hi) 小时内（按本地时区的钟点算）。"""
    t = t or now()
    lo, hi = win
    return lo <= t.hour + t.minute / 60.0 < hi


def _at_minutes(day: str, minutes: int) -> datetime:
    d = datetime.strptime(day, "%Y-%m-%d").date()
    return datetime(d.year, d.month, d.day, tzinfo=TZ) + timedelta(minutes=int(minutes))


def plan_slots(day: str, key: str = "", n: int = 1, win: tuple = None) -> list:
    """在时间窗里随机撒 n 个时刻。种子固定为「日期+账号+窗口」，同一天重启结果不变。"""
    lo, hi = win or CHECKIN_WINDOW
    n = max(1, int(n))
    rng = random.Random("wb-plan-v2|%s|%s|%d-%d|%d" % (day, key, lo, hi, n))
    step = (hi - lo) * 60.0 / n
    out = []
    for i in range(n):
        a, b = lo * 60 + i * step, lo * 60 + (i + 1) * step
        m = rng.uniform(a + 3, b - 3) if step > 8 else (a + b) / 2
        out.append(_at_minutes(day, max(lo * 60, min(hi * 60 - 1, int(round(m))))))
    return out


def checkin_plan(day: str, key: str = "") -> list:
    """某天某个号的自动签到时刻表（默认 4 个，铺在 09:00–12:00 里）。"""
    return plan_slots(day, "checkin|%s" % key, CHECKIN_TRIES, CHECKIN_WINDOW)


def travel_first_at(day: str, key: str = "") -> datetime:
    """当天第一次派猫的随机时刻（落在 TRAVEL_WINDOW 内）。"""
    return plan_slots(day, "travel|%s" % key, 1, TRAVEL_WINDOW)[0]


def travel_may_act(action: str, t: datetime = None, key: str = "") -> bool:
    """派猫 / 领奖的时段闸门：depart 必须过当天随机点，claim 只在白天窗口内。"""
    t = t or now()
    if not _in_window(TRAVEL_DAY_WINDOW, t):
        return False
    if action == "depart":
        return t >= travel_first_at(t.strftime("%Y-%m-%d"), key)
    return True


def travel_pace_seconds() -> int:
    """两次派遣之间随机间隔 6–32 分钟。"""
    return random.randint(6 * 60, 32 * 60)

# 接口只回 TCACA_code_xxx，这里映射成可读包名。
PACKAGE_LABELS = {
    "TCACA_code_007": "平台赠送包",
    "TCACA_code_008": "体验版基础积分",
    "TCACA_code_030": "拉新权益包",
}
PACKAGE_HINTS = {
    "TCACA_code_007": "签到 / 旅行 / 活动发放都进这里",
    "TCACA_code_008": "每月 1 号 00:00 发 500，月底清零不结转",
    "TCACA_code_030": "邀请好友奖励（一次性，2027-04 到期）",
}


def pkg_label(code):
    code = str(code or "")
    for k, v in PACKAGE_LABELS.items():
        if code.startswith(k):
            return v
    return code or "-"


def pkg_hint(code):
    code = str(code or "")
    for k, v in PACKAGE_HINTS.items():
        if code.startswith(k):
            return v
    return ""
# UA 必须带 Mozilla + Electron 段，否则余额接口 403 code 10085。
UA = os.environ.get(
    "UA",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "WorkBuddy/5.8.3 Chrome/138.0.0.0 Electron/37.10.3 Safari/537.36",
)

# 两种认证：扫码 cookie（UA 必须 HeadlessChrome）> Bearer（UA 必须客户端 UA），混用必 401。
# 认证头与 UA 成对下发，见 _apply_auth()。


def has_credential():
    return bool(session.header()) or bool(read_token()[0])


def read_credential():
    """返回 (凭据, 来源)；凭据为空表示未登录。"""
    if session.header():
        return "cookie", "state/session.json (扫码)"
    return read_token()


def credential_fp(cred):
    if session.header():
        return session.fingerprint() or "cookie"
    return token_fp(cred)


def credential_expiry(cred):
    e = session.expiry()
    if e:
        try:
            return datetime.fromtimestamp(int(e), tz=TZ).isoformat(timespec="seconds")
        except Exception:  # noqa: BLE001
            pass
    return token_expiry(cred)


def _apply_auth(req, token):
    """按凭据类型下发认证头与匹配的 UA：有真令牌一律 Bearer，不走 cookie。"""
    tok = "" if token in (None, "", "cookie") else str(token)
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
        req.add_header("User-Agent", UA)
        return
    hdr = session.header()
    if hdr:
        req.add_header("Cookie", hdr)
        req.add_header("User-Agent", session.UA)
    else:
        req.add_header("Authorization", "Bearer ")
        req.add_header("User-Agent", UA)


def jwt_payload(token):
    """解析 JWT 载荷（不验签，仅用于展示账户昵称）。失败返回 {}。"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        pad = "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except Exception:  # noqa: BLE001
        return {}


def account_info():
    """看板右上角的账户摘要：Bearer 解 JWT 载荷取昵称，扫码 cookie 用会话指纹代替。"""
    cred, src = read_credential()
    if not cred:
        return None
    exp = credential_expiry(cred)
    if session.header():  # 扫码会话路径
        return {"kind": "cookie", "name": "扫码会话", "sub": None,
                "fp": session.fingerprint() or "-", "expiry": exp, "source": src}
    p = jwt_payload(cred)
    nick = (p.get("nickname") or "").strip()
    phone = (p.get("preferred_username") or "").strip()
    name = nick or phone or "已登录"
    return {"kind": "bearer", "name": name, "sub": phone if nick and phone != nick else None,
            "fp": token_fp(cred), "expiry": exp, "source": src}

_lock = threading.Lock()
_state = {
    "started_at": None,
    "last_run": None,
    "last_code": None,
    "last_msg": "",
    "last_credit": None,
    "streak_days": None,
    "today_done": False,
    "today_date": None,
    "today_credit": None,
    "attempts_today": 0,
    "slots_done": [],
    "token_fp": None,
    "token_exp": None,
    "api_path": None,
    # 积分看板
    "balance": None,
    "balance_total": None,
    "balance_at": None,
    "packages": [],
    "is_paid": None,
    "plan": None,
    "activity_active": None,
    "daily_credit": None,
    "next_streak_day": None,
    "streak_bonus_credit": None,
    "checkin_dates": [],
    # Buddy 旅行
    "travel_state": None,
    "travel_location": None,
    "travel_arrive_at": None,
    "travel_reward": None,
    "travel_daily_limit": None,
    "travel_last_check": None,
    "travel_record_id": None,
    "travel_action": None,
    # Buddy 图鉴
    "buddies": [],
    "buddy_count": 0,
    "buddy_at": None,
}


def now():
    return datetime.now(TZ)


def fdt(v, fallback="—"):
    """ISO 时间 → `YYYY-MM-DD HH:MM:SS`（年月日时分秒，去掉时区后缀）。"""
    if not v:
        return fallback
    t = str(v).strip().replace("T", " ")
    if t.endswith("Z"):
        t = t[:-1]
    if "+" in t[10:]:
        t = t[: t.index("+", 10)]
    t = t.strip()
    return t[:19] if len(t) >= 19 else t


def log(msg):
    print(f"[{now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def read_token():
    try:
        if TOKEN_FILE.exists():
            t = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if t:
                return t, str(TOKEN_FILE)
    except Exception as e:  # noqa: BLE001
        log(f"读取令牌文件失败：{e}")
    t = os.environ.get("TOKEN", "").strip()
    return (t, "env:TOKEN") if t else (None, None)


def token_fp(token):
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def jwt_exp(token):
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        pad = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
        exp = payload.get("exp")
        if isinstance(exp, (int, float)):
            return datetime.fromtimestamp(int(exp), tz=TZ).isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001
        pass
    return None


def token_expiry(token):
    e = jwt_exp(token)
    if e:
        return e
    p = TOKEN_FILE.parent / "token.exp"
    try:
        if p.exists():
            return p.read_text(encoding="utf-8").strip() or None
    except Exception:  # noqa: BLE001
        pass
    return None


def api_post(path, token, payload=None):
    """POST，返回 (http_code, json|None, raw)。404 时自动换 /v2/ 前缀再试一次。"""
    alt = (
        path.replace("/billing/", "/v2/billing/", 1)
        if "/v2/" not in path
        else path.replace("/v2/billing/", "/billing/", 1)
    )
    variants = [alt, path] if PREFER_V2 else [path, alt]

    last = (0, None, "")
    for p in variants:
        req = urllib.request.Request(BASE + p, data=json.dumps(payload or {}).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        _apply_auth(req, token)
        # 客户端每个请求都带 X-Request-ID（uuid 去横线），缺了不影响签到但会被风控降级
        req.add_header("X-Request-ID", uuid.uuid4().hex)
        if X_USER_ID:
            req.add_header("X-User-Id", X_USER_ID)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                raw = r.read().decode("utf-8", "replace")
                code = r.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace") if e.fp else ""
            code = e.code
        except Exception as e:  # noqa: BLE001
            last = (0, None, f"{type(e).__name__}: {e}")
            continue
        try:
            data = json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001
            data = None
        last = (code, data, raw)
        _state["api_path"] = p
        if code == 404:
            continue
        return last
    return last


def api_get(path, token):
    """GET 版请求，返回 (http_code, json|None, raw)。风控头必须与 POST 一致。"""
    req = urllib.request.Request(BASE + path, method="GET")
    req.add_header("Accept", "application/json")
    _apply_auth(req, token)
    req.add_header("X-Request-ID", uuid.uuid4().hex)
    if X_USER_ID:
        req.add_header("X-User-Id", X_USER_ID)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            raw = r.read().decode("utf-8", "replace")
            code = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        code = e.code
    except Exception as e:  # noqa: BLE001
        return (0, None, f"{type(e).__name__}: {e}")
    try:
        data = json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        data = None
    return (code, data, raw)


def _retry(fn, tries=3, backoff=5):
    r = (0, None, "")
    for i in range(tries):
        r = fn()
        code = r[0]
        if code != 0 and code < 500:
            return r
        if i < tries - 1:
            log(f"  网络/服务端异常（code={code}），{backoff * (i + 1)}s 后重试")
            time.sleep(backoff * (i + 1))
    return r


def notify(text):
    if not WEBHOOK_URL:
        return
    try:
        req = urllib.request.Request(
            WEBHOOK_URL, data=json.dumps({"text": f"[WorkBuddy 签到] {text}"}).encode(), method="POST"
        )
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # noqa: BLE001
        log(f"通知发送失败：{e}")


def _num(v):
    try:
        return int(float(v))
    except Exception:  # noqa: BLE001
        return 0


def fetch_accounts_bearer(tok):
    """GET /console/accounts：昵称实时，用来纠正 JWT 里固化的旧名字。返回 (ok, accounts 或错误串)。"""
    req = urllib.request.Request(BASE + "/console/accounts", method="GET")
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "*/*")
    req.add_header("Referer", BASE + "/home/")
    req.add_header("Origin", BASE)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"[:120]
    if not isinstance(j, dict) or j.get("code") != 0:
        return False, f"code={j.get('code') if isinstance(j, dict) else '?'}"
    return True, (j.get("data") or {}).get("accounts") or []


def sync_profile(u, reason: str = "") -> str | None:
    """把实时昵称同步进 u['account']，返回最新名字（拿不到返回 None）。"""
    tok = (u or {}).get("token") or ""
    if not tok:
        return None
    ok, accts = fetch_accounts_bearer(tok)
    if not ok or not isinstance(accts, list) or not accts:
        return None
    cur = next((a for a in accts if str(a.get("uid") or "") == str(u.get("uid") or "")), None)
    if cur is None:
        cur = next((a for a in accts if a.get("lastLogin")), None)
    if not cur:
        return None
    acct = u.setdefault("account", {})
    nick = str(cur.get("nickname") or "").strip()
    if nick and nick != acct.get("name"):
        old = acct.get("name") or "（空）"
        acct["name"] = nick
        log(f"[{u['uid'][:8]}] 昵称已同步：{old} → {nick}" + (f"（{reason}）" if reason else ""))
    return nick or None


def passive_checkin_event(u, credit) -> None:
    """其他端先签到时被动发现：当天没有签到流水就补一条 kind=passive。"""
    if not u or not u.get("uid"):
        return
    today = now().strftime("%Y-%m-%d")
    if u.get("_ck_ev_date") == today:
        return
    for h in reversed(u.get("hist") or []):
        if str(h.get("ts") or "").startswith(today) and h.get("kind") in ("success", "passive"):
            u["_ck_ev_date"] = today
            return
    u["_ck_ev_date"] = today
    try:
        user_hist_add(u, {"ts": now().isoformat(timespec="seconds"), "reason": "被动发现",
                          "ok": True, "kind": "passive", "credit": int(credit or 0),
                          "msg": f"今日已签到（其他端完成，入账 {int(credit or 0)} 积分）"})
    except Exception:  # noqa: BLE001
        log(traceback.format_exc()[:300])


def fetch_balance(token):
    """剩余积分：get-user-resource-summary 的 Packages 里 CycleRemainCapacity 求和。
    明细存 state/balance_raw.json 便于核对。
    """
    code, data, raw = _retry(lambda: api_post("/billing/meter/get-user-resource-summary", token))
    if code in (401, 403):
        return {"error": f"HTTP {code}", "auth": True}
    if code != 200:
        return {"error": f"HTTP {code} {raw[:120]}"}
    if not isinstance(data, dict) or data.get("code") != 0:
        return {"error": f"code={data.get('code') if isinstance(data, dict) else '?'} {str(data)[:120]}"}
    d = data.get("data") or {}
    pkgs = d.get("Packages") or []

    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "balance_raw.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:  # noqa: BLE001
        log(f"余额原始响应落盘失败：{e}")

    items = []
    for p in pkgs:
        if not isinstance(p, dict):
            continue
        items.append(
            {
                "code": p.get("PackageCode") or "",
                "name": p.get("PackageName") or p.get("PackageCode") or "",
                "remain": _num(p.get("CycleRemainCapacity")),
                "total": _num(p.get("CycleTotalCapacity")),
                "unit": p.get("CapacityUnit") or p.get("Unit") or p.get("MeterUnit") or "",
                "start": p.get("CycleStartTime") or p.get("CycleStart") or "",
                "end": p.get("CycleEndTime") or p.get("CycleEnd") or p.get("ExpireTime") or "",
            }
        )

    return {
        "balance": sum(i["remain"] for i in items),
        "total": sum(i["total"] for i in items),
        "is_paid": bool(d.get("IsPaidUser")),
        "plan": d.get("SubscriptionPackageName") or "",
        "packages": items,
    }


def fetch_activity(token):
    """活动状态（字段更全）：active、today_checked_in、streak_days、today_credit…"""
    code, data, raw = _retry(lambda: api_post("/billing/meter/checkin-activity-status", token))
    if code in (401, 403):
        return {"error": f"HTTP {code}", "auth": True}
    if code != 200 or not isinstance(data, dict) or data.get("code") != 0 or not data.get("data"):
        return {"error": f"HTTP {code} {raw[:120]}"}
    d = data["data"]
    return {
        "active": bool(d.get("active")),
        "today_checked_in": bool(d.get("today_checked_in")),
        "streak_days": _num(d.get("streak_days")),
        "daily_credit": _num(d.get("daily_credit")),
        "today_credit": _num(d.get("today_credit")),
        "is_streak_day": bool(d.get("is_streak_day")),
        "next_streak_day": _num(d.get("next_streak_day")),
        "streak_bonus_days": _num(d.get("streak_bonus_days")),
        "streak_bonus_credit": _num(d.get("streak_bonus_credit")),
        "total_credits": _num(d.get("total_credits")),
        "checkin_dates": d.get("checkin_dates") or [],
    }


def fetch_buddies(token):
    """已拥有的 Buddy 列表。GET 接口，用 POST 会 404。"""
    code, data, raw = _retry(lambda: api_get("/activity/growth/buddy/list", token))
    if code in (401, 403):
        return {"error": f"HTTP {code}"}
    if code != 200 or not isinstance(data, dict) or data.get("code") != 0:
        return {"error": f"HTTP {code} {str(raw)[:120]}"}
    d = data.get("data") or {}
    out = []
    for b in d.get("buddies") or []:
        out.append({
            "name": b.get("name") or "",
            "rarity": (b.get("rarity") or "").upper(),
            "current": bool(b.get("current_buddy")),
            "instance_id": b.get("instance_id"),
            # 素材地址留给自动补图集：anim 优先，static 兜底。
            "anim": b.get("full_animated_url") or "",
            "static": b.get("base_static_url") or b.get("thumbnail_url") or "",
        })
    return {"buddies": out, "count": d.get("count") or len(out)}


def refresh_board(reason=""):
    """只刷新余额 + 活动状态，不触发签到。"""
    token, _ = read_token()
    if not token:
        return
    with _lock:
        _state["token_fp"] = token_fp(token)
        _state["token_exp"] = token_expiry(token)
    b = fetch_balance(token)
    a = fetch_activity(token)
    bd = fetch_buddies(token)
    with _lock:
        if "balance" in b:
            _state["balance"] = b["balance"]
            _state["balance_total"] = b["total"]
            _state["packages"] = b.get("packages") or []
            _state["is_paid"] = b["is_paid"]
            _state["plan"] = b["plan"]
            _state["balance_at"] = now().isoformat(timespec="seconds")
        elif "error" in b:
            log(f"余额查询失败：{b['error']}")
        if "active" in a:
            _state["activity_active"] = a["active"]
            _state["daily_credit"] = a["daily_credit"]
            _state["next_streak_day"] = a["next_streak_day"]
            _state["streak_bonus_credit"] = a["streak_bonus_credit"]
            if a["streak_days"]:
                _state["streak_days"] = a["streak_days"]
            if a["checkin_dates"]:
                _state["checkin_dates"] = a["checkin_dates"]
            if a["today_checked_in"]:
                _state["today_done"] = True
                _state["today_credit"] = a["today_credit"] or a["daily_credit"]
        elif "error" in a:
            log(f"活动状态查询失败：{a['error']}")
        if "buddies" in bd:
            _state["buddies"] = bd["buddies"]
            _state["buddy_count"] = bd["count"]
            _state["buddy_at"] = now().isoformat(timespec="seconds")
        elif "error" in bd:
            log(f"Buddy 列表查询失败：{bd['error']}")
    save_status()
    # 缺图集的后台补，抽帧慢，不阻塞状态接口。
    ensure_pets_missing(_state.get("buddies") or [])



def travel_tick(reason=""):
    """旅行巡检，动作由服务端 state 驱动：arrived 领奖 / idle 派遣 / traveling 只记到达时间。"""
    if not TRAVEL_ENABLE:
        return None
    # 白天窗口外不查旅行状态。
    if not _in_window(TRAVEL_DAY_WINDOW):
        with _lock:
            _state["travel_action"] = "夜间休眠（%02d:00–%02d:00 才活动）" % TRAVEL_DAY_WINDOW
        return None
    token, _ = read_token()
    if not token:
        return None

    code, data, raw = _retry(lambda: api_get("/activity/growth/buddy/travel/status", token))
    if code in (401, 403):
        log("  旅行：令牌失效，跳过")
        return None
    if code != 200 or not isinstance(data, dict) or data.get("code") != 0:
        log(f"  旅行状态查询失败：HTTP {code} {str(raw)[:120]}")
        return None

    d = data.get("data") or {}
    state = d.get("state") or "unknown"
    loc = d.get("location") or {}
    with _lock:
        _state["travel_state"] = state
        _state["travel_daily_limit"] = bool(d.get("daily_limit_reached"))
        _state["travel_last_check"] = now().isoformat(timespec="seconds")
        if loc.get("name"):
            _state["travel_location"] = loc["name"]
        if d.get("arrive_at"):
            _state["travel_arrive_at"] = datetime.fromtimestamp(
                int(d["arrive_at"]), tz=TZ
            ).isoformat(timespec="seconds")
        if d.get("record_id"):
            _state["travel_record_id"] = int(d["record_id"])
        if d.get("reward_credit"):
            _state["travel_reward"] = _num(d["reward_credit"])

    try:
        if state == "arrived":
            if not travel_may_act("claim", key="owner"):
                with _lock:
                    _state["travel_action"] = "已到家，等白天时段再收"
                return state
            rid = _state.get("travel_record_id")
            body = {"record_id": rid} if rid else {}
            c2, d2, r2 = _retry(
                lambda: api_post("/activity/growth/buddy/travel/claim", token, body)
            )
            # 有的后端要求带 record_id，有的只认空 body，被拒回退一次。
            if not (c2 == 200 and isinstance(d2, dict) and d2.get("code") == 0) and rid:
                log("  旅行：带 record_id 领取被拒，回退空 body 重试")
                c2, d2, r2 = _retry(
                    lambda: api_post("/activity/growth/buddy/travel/claim", token, {})
                )
            got = _num(((d2 or {}).get("data") or {}).get("reward_credit")) if isinstance(d2, dict) else 0
            if c2 == 200 and isinstance(d2, dict) and d2.get("code") == 0:
                with _lock:
                    _state["travel_reward"] = got or _state.get("travel_reward")
                    _state["travel_action"] = f"领取 +{got or 0} 分"
                    _state["travel_state"] = "idle"
                    loc_name = _state.get("travel_location") or "咖啡馆"
                log(f"  旅行：领取成功 +{got or 0} 分（{reason or '巡检'}）")
                # 收益记进 history.jsonl，看板「最近领取」读这里。
                append_history({
                    "ts": now().isoformat(timespec="seconds"),
                    "reason": reason or "旅行巡检",
                    "ok": True,
                    "kind": "travel",
                    "credit": int(got or 0),
                    "msg": f"旅行归来（{loc_name}），领取 {int(got or 0)} 积分",
                })
                refresh_board("旅行收益入账")
                with _lock:
                    # 两次派遣之间随机等待。
                    _state["travel_next_depart"] = time.time() + travel_pace_seconds()
            else:
                log(f"  旅行：领取失败 {c2} {str(r2)[:140]}")
        elif state == "idle":
            if _state.get("travel_daily_limit"):
                log("  旅行：今日已达派遣上限")
                return None
            if not travel_may_act("depart", key="owner"):
                with _lock:
                    _state["travel_action"] = "等待 %02d:00–%02d:00 时段派猫" % TRAVEL_WINDOW
                return state
            if time.time() < float(_state.get("travel_next_depart") or 0):
                with _lock:
                    _state["travel_action"] = "空闲，随机歇一会儿再派"
                return state
            c3, d3, r3 = _retry(
                lambda: api_post(
                    "/activity/growth/buddy/travel/depart", token, {"location_id": TRAVEL_LOCATION_ID}
                )
            )
            if c3 == 200 and isinstance(d3, dict) and d3.get("code") == 0:
                dd = d3.get("data") or {}
                arrive = dd.get("arrive_at")
                with _lock:
                    _state["travel_state"] = "traveling"
                    _state["travel_record_id"] = dd.get("record_id")
                    _state["travel_location"] = (dd.get("location") or {}).get("name")
                    _state["travel_action"] = "已派遣"
                    _state["travel_next_depart"] = time.time() + travel_pace_seconds()
                    if arrive:
                        _state["travel_arrive_at"] = datetime.fromtimestamp(
                            int(arrive), tz=TZ
                        ).isoformat(timespec="seconds")
                log(
                    "  旅行：已派出 → "
                    + str(_state.get("travel_location") or TRAVEL_LOCATION_ID)
                    + "，预计 "
                    + fdt(_state.get("travel_arrive_at"))
                    + " 回来"
                )
            else:
                log(f"  旅行：派遣失败 {c3} {str(r3)[:140]}")
        else:
            msg = f"  旅行：{state}"
            if state == "traveling":
                msg += f"，预计 {fdt(_state.get('travel_arrive_at'))} 回来"
            log(msg)
    finally:
        save_status()
    return state


def run_once(reason=""):
    ts = now()
    with _lock:
        token, source = read_credential()
    if not token:
        return finish(
            {"ok": False, "kind": "no_token", "msg": "尚未登录（请扫码登录，或放入 state/token）"}, reason
        )

    fp = credential_fp(token)
    exp = credential_expiry(token)
    log(f"开始签到 attempt（{reason or '手动'}），令牌指纹 {fp}，来源 {source}" + (f"，到期 {exp}" if exp else ""))
    with _lock:
        if _state["token_fp"] != fp:
            _state["token_fp"] = fp
            _state["token_exp"] = exp

    if exp:  # 到期提醒（剩 5 天内每次尝试都提示）
        try:
            d = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=TZ)
            if (d - ts).days <= 5:
                log(f"⚠️ 令牌将于 {exp} 到期（剩 {(d - ts).days} 天），请更换 state/token")
        except Exception:  # noqa: BLE001
            pass

    # 1) 活动状态优先（字段全），失败降级 checkin-status
    a = fetch_activity(token)
    if a.get("auth"):
        r = {"ok": False, "kind": "auth", "msg": f"令牌失效或无权限（{a['error']}），请同步新令牌"}
        finish(r, reason)
        notify(r["msg"])
        return r

    if "active" in a:
        with _lock:
            _state["activity_active"] = a["active"]
            _state["daily_credit"] = a["daily_credit"]
            _state["next_streak_day"] = a["next_streak_day"]
            _state["streak_bonus_credit"] = a["streak_bonus_credit"]
            if a["checkin_dates"]:
                _state["checkin_dates"] = a["checkin_dates"]
        if a["today_checked_in"]:
            with _lock:
                _state["today_done"] = True
                _state["today_credit"] = a["today_credit"] or a["daily_credit"]
            finish(
                {
                    "ok": True,
                    "kind": "already",
                    "msg": f"今日已签到（连续第 {a['streak_days']} 天，今日 {a['today_credit'] or a['daily_credit']} 积分）",
                },
                reason,
            )
            refresh_board("已签后刷新余额")
            return {"ok": True, "kind": "already"}
    else:
        log(f"活动状态接口不可用（{a.get('error')}），降级 checkin-status")
        code, data, raw = _retry(lambda: api_post("/billing/meter/checkin-status", token))
        if code in (401, 403):
            r = {"ok": False, "kind": "auth", "msg": f"令牌失效或无权限（HTTP {code}），请同步新令牌"}
            finish(r, reason)
            notify(r["msg"])
            return r
        if code != 200:
            return finish({"ok": False, "kind": "http", "msg": f"查询签到状态失败（HTTP {code}）{raw[:160]}"}, reason)
        if isinstance(data, dict) and (data.get("data") or {}).get("today_checked_in"):
            with _lock:
                _state["today_done"] = True
            finish({"ok": True, "kind": "already", "msg": "今日已签到（状态接口），无需重复领取"}, reason)
            refresh_board("已签后刷新余额")
            return {"ok": True, "kind": "already"}

    # 2) 执行签到
    code2, data2, raw2 = _retry(lambda: api_post("/billing/meter/daily-checkin", token))
    if code2 in (401, 403):
        r = {"ok": False, "kind": "auth", "msg": f"令牌失效或无权限（HTTP {code2}），请同步新令牌"}
        finish(r, reason)
        notify(r["msg"])
        return r

    api_code, msg, d = None, "", {}
    if isinstance(data2, dict):
        api_code = data2.get("code")
        msg = str(data2.get("msg") or data2.get("message") or "")
        d = data2.get("data") or {}
    if api_code is None and code2 != 200:
        return finish({"ok": False, "kind": "http", "msg": f"签到请求失败（HTTP {code2}）{raw2[:160]}"}, reason)

    if api_code == 0:
        credit = _num(d.get("credit"))
        streak = _num(d.get("streak_days"))
        with _lock:
            _state["today_credit"] = credit
            if streak:
                _state["streak_days"] = streak
        r = {
            "ok": True,
            "kind": "success",
            "credit": credit,
            "streak_days": streak,
            "msg": f"签到成功，领取 {credit} 积分，连续第 {streak} 天",
        }
    elif api_code == 10001:
        r = {"ok": True, "kind": "already", "msg": "今日已签到（接口返回 10001），幂等跳过"}
    else:
        r = {
            "ok": False,
            "kind": "api",
            "api_code": api_code,
            "http": code2,
            "msg": f"接口拒绝：code={api_code} msg={msg or raw2[:160]}",
        }
    finish(r, reason)
    if not r["ok"]:
        notify(r["msg"])
    refresh_board("签后刷新余额")
    return r


def append_history(rec):
    """追加一条记录：history.jsonl + SQLite events。"""
    ok = False
    try:
        with HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        ok = True
    except Exception as e:  # noqa: BLE001
        log(f"写历史失败：{e}")
    try:
        tok, _ = read_token()
        uid = (jwt_payload(tok) or {}).get("sub") or ""
        if uid:
            store.add_event(uid, rec)   # 站长账户也能在 /api/history 里看到流水
    except Exception:  # noqa: BLE001
        pass
    return ok


def finish(result, reason):
    ts = now()
    date = ts.strftime("%Y-%m-%d")
    with _lock:
        if _state["today_date"] != date:
            _state["today_date"] = date
            _state["today_done"] = False
            _state["attempts_today"] = 0
        _state["attempts_today"] += 1
        _state["last_run"] = ts.isoformat(timespec="seconds")
        _state["last_code"] = result.get("api_code", result.get("http"))
        _state["last_msg"] = result.get("msg", "")
        _state["last_credit"] = result.get("credit")
        if result.get("streak_days") is not None:
            _state["streak_days"] = result["streak_days"]
        if result.get("ok") and result.get("kind") in ("success", "already"):
            _state["today_done"] = True
    append_history(
        {
            "ts": ts.isoformat(timespec="seconds"),
            "reason": reason,
            "ok": result.get("ok"),
            "kind": result.get("kind"),
            "credit": result.get("credit"),
            "msg": result.get("msg"),
        }
    )
    save_status()
    log(("[OK ] " if result.get("ok") else "[ERR] ") + str(result.get("msg")))
    return result


def snapshot():
    with _lock:
        s = dict(_state)
    s["attempt_hours"] = ATTEMPT_HOURS
    s["checkin_window"] = "%02d:00–%02d:00" % CHECKIN_WINDOW
    s["travel_window"] = "%02d:00–%02d:00" % TRAVEL_WINDOW
    s["travel_day_window"] = "%02d:00–%02d:00" % TRAVEL_DAY_WINDOW
    s["next_slot"] = next_slot()
    s["token_file"] = str(TOKEN_FILE)
    s["travel_enabled"] = TRAVEL_ENABLE
    s["account"] = account_info()
    s["updated_at"] = now().isoformat(timespec="seconds")
    return s


def save_status():
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATUS_FILE.write_text(json.dumps(snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log(f"写入状态文件失败：{e}")


def history(limit=12):
    out = []
    try:
        if HISTORY_FILE.exists():
            lines = HISTORY_FILE.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-limit:]
            for ln in lines:
                try:
                    out.append(json.loads(ln))
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    return list(reversed(out))


def next_slot(key: str = ""):
    """下一次自动签到的时刻：当天剩余的计划点，都过了就是明天的第一个。"""
    t = now()
    for at in checkin_plan(t.strftime("%Y-%m-%d"), key):
        if at > t:
            return at.isoformat(timespec="seconds")
    nxt = (t + timedelta(days=1)).strftime("%Y-%m-%d")
    return checkin_plan(nxt, key)[0].isoformat(timespec="seconds")


def scheduler():
    if RUN_ON_START:
        time.sleep(5)
        try:
            # 启动自检也受时段限制。
            if not load_today_done() and (_in_window(CHECKIN_WINDOW) or _in_window(TRAVEL_DAY_WINDOW)):
                run_once("启动自检")
            else:
                refresh_board("启动刷新")
        except Exception:  # noqa: BLE001
            log(traceback.format_exc())
    last_balance = time.time()
    last_travel = 0.0
    last_renew = 0.0
    last_users = 0.0
    while True:
        try:
            t = now()
            date = t.strftime("%Y-%m-%d")
            with _lock:
                if _state["today_date"] != date:
                    _state["today_date"] = date
                    _state["today_done"] = False
                    _state["attempts_today"] = 0
                    _state["slots_done"] = []
                    _state["catchup_tries"] = 0
                    _state["catchup_last"] = 0
                done = _state["today_done"]
                slots_done = list(_state["slots_done"])
            if not done and not ARCHIVE_MODE:
                # 按当天随机时刻表触发，跑过的时间点标记掉，避免同一时刻重跑。
                plan = checkin_plan(date, "owner")
                done_set = {str(x) for x in slots_done}
                passed = [i for i, at in enumerate(plan) if t >= at and str(i) not in done_set]
                reason = None
                if passed:
                    newest = passed[-1]
                    with _lock:
                        _state["slots_done"] = sorted(done_set | {str(i) for i in range(newest + 1)})
                        done_set = set(_state["slots_done"])
                    if _in_window(CHECKIN_WINDOW):
                        reason = "定时 %s" % plan[newest].strftime("%H:%M")
                        if (t - plan[newest]) > timedelta(minutes=30):
                            reason = "错过时段补签"
                if reason is None and t >= plan[-1] + timedelta(minutes=30) \
                        and _in_window(TRAVEL_DAY_WINDOW) and "catchup" not in done_set \
                        and int(_state.get("catchup_tries") or 0) < 3 \
                        and (time.time() - float(_state.get("catchup_last") or 0)) > 600:
                    # 白天窗口内补一次，失败最多重试 3 次。
                    with _lock:
                        _state["slots_done"] = sorted(done_set | {"catchup"})
                        _state["catchup_tries"] = int(_state.get("catchup_tries") or 0) + 1
                        _state["catchup_last"] = time.time()
                    reason = "错过时段补签"
                if reason:
                    _res = run_once(reason)
                    if reason == "错过时段补签" and not (_res or {}).get("ok"):
                        with _lock:
                            _state["slots_done"] = [x for x in _state["slots_done"] if x != "catchup"]
            if TRAVEL_ENABLE and time.time() - last_travel > TRAVEL_MINUTES * 60:
                last_travel = time.time()
                travel_tick("周期巡检")
            # 访客账户的刷新与签到走独立线程 users_scheduler()，避免被主循环卡住。
            if time.time() - last_balance > BALANCE_MINUTES * 60:
                last_balance = time.time()
                refresh_board("周期刷新余额")
            # 每 6 小时探测一次续期，失败只记日志。
            if not ARCHIVE_MODE and time.time() - last_renew > 6 * 3600:
                last_renew = time.time()
                try:
                    login.auto_renew_if_needed()
                except Exception:  # noqa: BLE001
                    log(traceback.format_exc())
            # 令牌变化后立即重拉数据。
            _tok, _ = read_token()
            _fp = token_fp(_tok) if _tok else ""
            if _fp and _fp != _state.get("token_fp"):
                log(f"检测到令牌变更（{_state.get('token_fp')} → {_fp}），立即刷新看板")
                with _lock:
                    _state["token_fp"] = _fp
                threading.Thread(target=refresh_board, args=("令牌变更",),
                                 daemon=True).start()
        except Exception:  # noqa: BLE001
            log(traceback.format_exc())
        time.sleep(20)


def load_today_done():
    try:
        if STATUS_FILE.exists():
            d = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            return bool(d.get("today_done")) and d.get("today_date") == now().strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        pass
    return False


# 宠物雪碧图：12 帧横排，单帧 96px；服务端由 nginx 从 /pets/ 出图，桌面端由应用自己服务。
# 源 APNG 2.7~7MB 不能直传，抽帧后约 150KB；文件名用 ASCII（中文名在 nginx alias 下 404）。
PET_FILES = {
    "量子喵": "/pets/pet-liangzi.png",
    "暴富喵": "/pets/pet-baofu.png",
    "龙焰喵": "/pets/pet-longyan.png",
    "设计喵": "/pets/pet-shejimiao.png",
}
PET_DIR = os.environ.get("PET_DIR", "/pets")
PET_SHEET_FRAMES = 12          # 与 CSS 里 background-size:--sz*12 绑定
PET_SHEET_PX = 96              # 单帧边长（CSS 里按 --sz 缩放）
RARITY_RANK = {"UR": 4, "SSR": 3, "SR": 2, "R": 1, "": 0}


def _pet_auto_path(name):
    h = hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
    return os.path.join(PET_DIR, f"pet-auto-{h}.png")


def pet_url(name):
    """宠物 → 图集 URL；没有图返回空串。"""
    if not name:
        return ""
    p = PET_FILES.get(name)
    if p:
        return p
    f = _pet_auto_path(name)
    try:
        if os.path.exists(f) and os.path.getsize(f) > 2000:
            return "/pets/" + os.path.basename(f)
    except OSError:
        pass
    return ""


def pets_missing(buddies):
    """有名字但还没有任何图集的宠物。"""
    return [b for b in (buddies or []) if (b.get("name") or "") and not pet_url(b["name"])]


def ensure_pets_missing(buddies, tag=""):
    """缺图的宠物排一轮后台抽帧。全局引擎与多用户账户两条路都要调。"""
    try:
        now_ts = time.time()
        # 失败过的冷却一小时再试。
        missing = [b for b in pets_missing(buddies)
                   if now_ts - _PET_FAIL.get(b["name"], 0) > _PET_FAIL_TTL]
        if not missing:
            return 0
        log(f"{tag}宠物图集：{len(missing)} 只缺图（{', '.join(b['name'] for b in missing[:6])}），后台补图")
        threading.Thread(target=_build_pets_bg, args=(missing,), daemon=True).start()
        return len(missing)
    except Exception:  # noqa: BLE001
        log(traceback.format_exc())
        return 0

# 官方素材只有 idle APNG，没有迈步帧；走路效果由路面滚动 + 颠簸 + 缩放合成。
# 这段是普通字符串，CSS 花括号不用双写。
PET_CSS = r"""
/* Buddy 舞台 */
.petrow{display:flex;gap:8px;align-items:flex-end;margin:9px auto 0;max-width:620px}
.track{position:relative;flex:1 1 auto;min-width:0;height:112px;border-radius:11px;overflow:hidden;
  perspective:88px;perspective-origin:50% 18%;
  background:
    radial-gradient(120% 58% at 50% 0%,rgba(140,170,220,.20),transparent 62%),
    linear-gradient(180deg,rgba(128,138,160,.03) 0%,rgba(128,138,160,.17) 100%)}
/* 远处雾化：用主题遮罩色 */
.track::after{content:"";position:absolute;left:0;right:0;top:0;height:26%;pointer-events:none;
  background:linear-gradient(180deg,var(--scrim),transparent)}
/* 路面：梯形收窄造透视，横纹向下滚；中灰配色，深浅主题都有对比度 */
.road{position:absolute;left:-22%;right:-22%;bottom:0;height:100%;
  clip-path:polygon(42% 0,58% 0,120% 100%,-20% 100%);
  background:linear-gradient(180deg,rgba(100,108,130,.24) 0%,rgba(88,96,118,.54) 38%,rgba(74,82,102,.70) 100%)}
/* 远段：密横纹（透视上线距小） */
.road::before{content:"";position:absolute;left:0;right:0;top:0;height:46%;
  background:repeating-linear-gradient(180deg,rgba(214,220,236,.38) 0 1px,transparent 1px 11px);
  animation:roadFar .82s linear infinite}
/* 近段：疏横纹 */
.road::after{content:"";position:absolute;left:0;right:0;top:46%;bottom:0;
  background:repeating-linear-gradient(180deg,rgba(214,220,236,.34) 0 2px,transparent 2px 30px);
  animation:roadNear .82s linear infinite}
/* 中线虚线：窄梯形，随透视变宽 */
.lane{position:absolute;left:0;right:0;top:0;bottom:0;
  clip-path:polygon(49.7% 0,50.3% 0,51.2% 100%,48.8% 100%);
  background:repeating-linear-gradient(180deg,rgba(244,206,110,.9) 0 11px,transparent 11px 30px);
  animation:laneRun .82s linear infinite}
.petshadow{position:absolute;left:50%;bottom:3px;width:52px;height:9px;margin-left:-26px;border-radius:50%;
  background:radial-gradient(50% 50% at 50% 50%,rgba(0,0,0,.28),rgba(0,0,0,0) 66%)}
.runner{position:absolute;left:50%;bottom:4px;width:96px;height:96px;margin-left:-48px;
  background-repeat:no-repeat;background-size:1152px 96px;background-position:0 0;
  transform-origin:50% 100%;
  animation:petFrames 2.4s steps(12) infinite,breath 3.6s ease-in-out infinite}
.track.idle .road,.track.idle .road::before,
.track.idle .road::after,.track.idle .lane{animation-play-state:paused}
.track.idle .road{opacity:.55}
/* 旅行中：帧动画提速 + 上下颠簸 + 前后缩放，阴影同步 */
.track.run .runner{animation:petFrames 1.12s steps(12) infinite,stepBob .32s ease-in-out infinite alternate}
.track.run .petshadow{animation:shBob .32s ease-in-out infinite alternate}
.track.enter .runner{animation:enterIn .5s ease-out}
.pettag{position:absolute;left:9px;top:7px;max-width:58%;font-size:10px;color:var(--fg3);
  letter-spacing:.4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tvtip{font-size:10.5px;line-height:1.65;color:var(--fg3);opacity:.78;margin-top:6px}
/* 看家的宠物：方框必须与雪碧图帧等比（--sz × --sz，background-size: --sz*12），
   否则一个框里会露出 1.5 帧。--sz 由服务端按只数算好，整列高度等于马路 112px。 */
.sitters{display:flex;flex-direction:column;gap:2px;justify-content:flex-end;
  flex:0 0 auto}
.sitter{position:relative;width:var(--sz,36px);height:var(--sz,36px);padding:0;border:0;cursor:pointer;
  border-radius:calc(var(--sz,36px) * .28);
  background-color:rgba(128,128,128,.10);background-repeat:no-repeat;
  background-size:calc(var(--sz,36px) * 12) var(--sz,36px);background-position:0 0;
  transform-origin:50% 100%;
  -webkit-tap-highlight-color:transparent;transition:opacity .3s,filter .3s}
.sitter.asleep{opacity:.72;filter:saturate(.5) brightness(.95);
  animation:sleepBreath 4.2s ease-in-out infinite}
.sitter.awake{opacity:1;filter:none;animation:none}
/* 没有图集的宠物：占位显示（稀有度底色 + 名字首字），图补好后整页刷新换成真图 */
.runner.ph,.sitter.ph{background-image:none!important;
  background-color:rgba(255,208,117,.13);
  box-shadow:inset 0 0 0 1px rgba(255,208,117,.24)}
.runner.ph::after,.sitter.ph::after{content:attr(data-ini);position:absolute;inset:0;
  display:flex;align-items:center;justify-content:center;pointer-events:none;
  font-weight:600;color:var(--fg2);letter-spacing:0;
  font-size:calc(var(--sz,36px) * .46)}
.runner.ph::after{font-size:30px}
.runner.ph{animation:breath 3.6s ease-in-out infinite!important}
.track.run .runner.ph{animation:stepBob .32s ease-in-out infinite alternate!important}
.sitter.hop{animation:hop .44s ease-out both}
.zzz{position:absolute;right:-1px;top:-2px;width:20px;height:16px;pointer-events:none}
.zzz i{position:absolute;right:0;bottom:0;font-style:normal;font-weight:600;
  font-size:calc(var(--sz,36px) * .25);color:var(--fg2);opacity:0}
.sitter.asleep .zzz i{animation:zfloat 2.6s ease-in-out infinite}
.zzz i:nth-child(2){right:calc(var(--sz,36px) * .14);font-size:calc(var(--sz,36px) * .222);animation-delay:.85s}
.zzz i:nth-child(3){right:calc(var(--sz,36px) * .28);font-size:calc(var(--sz,36px) * .194);animation-delay:1.7s}
@keyframes petFrames{from{background-position:0 0}to{background-position:-1152px 0}}
/* UR 滑板皮肤：素材自带板面，走滑行，只留 1.2px 重心微浮 */
@keyframes stepBob{from{transform:translateY(0)}to{transform:translateY(-1.2px)}}
@keyframes breath{0%,100%{transform:translateY(0) scale(1)}50%{transform:translateY(-1px) scale(1.012)}}
@keyframes shBob{from{transform:scale(1);opacity:.5}to{transform:scale(.96);opacity:.38}}
@keyframes dust{from{transform:scale(.72);opacity:.30}to{transform:scale(1.2);opacity:0}}
@keyframes roadFar{from{background-position:0 0}to{background-position:0 8px}}
@keyframes roadNear{from{background-position:0 0}to{background-position:0 22px}}
@keyframes laneRun{from{background-position:0 0}to{background-position:0 26px}}
@keyframes enterIn{from{opacity:0}to{opacity:1}}
@keyframes sleepBreath{0%,100%{transform:scale(.93) translateY(3px)}
  50%{transform:scale(.96) translateY(1px)}}
@keyframes hop{0%{transform:translateY(0) scale(.94)}38%{transform:translateY(-9px) scale(1.06)}
  100%{transform:translateY(0) scale(1)}}
@keyframes zfloat{0%{opacity:0;transform:translateY(0) scale(.7)}
  25%{opacity:.9}100%{opacity:0;transform:translateY(-12px) scale(1.05)}}
@media (prefers-reduced-motion:reduce){
  .runner,.petshadow,.petshadow::after,.road::before,.road::after,.lane,.sitter,.zzz i{animation:none!important}
}
"""

# 点击只切换看板本地视图，不动服务端 current_buddy（没有切换接口）。
# 选择存 sessionStorage：整页刷新不丢，重开回到服务端那只。
PET_JS = r"""
(function(){
  var box=document.getElementById('petrow'),
      node=document.getElementById('petdata');
  if(!box||!node)return;
  var data;
  try{data=JSON.parse(node.textContent||'{}')}catch(e){return}
  if(!data.list||!data.list.length)return;

  var KEY='wb-runner',
      QKEY='wb-queue',
      track=document.getElementById('track'),
      runner=document.getElementById('runner'),
      sitters=document.getElementById('sitters'),
      tag=document.getElementById('pettag');

  function find(n){
    for(var i=0;i<data.list.length;i++){if(data.list[i].n===n)return data.list[i]}
    return null;
  }
  var running=data.run||data.list[0].n;
  try{
    var memo=sessionStorage.getItem(KEY);
    if(memo&&find(memo))running=memo;   /* 刷新后接着上次的选择 */
  }catch(e){}
  var came=null;   /* 刚被换下来、还醒着的那只 */

  /* 排队顺序：旅行中的在马路，其余在右侧；点某只则它上路、原跑者排队尾，队列存 sessionStorage */
  var order=[];
  for(var i=0;i<data.list.length;i++)order.push(data.list[i].n);
  try{
    var q=sessionStorage.getItem(QKEY);
    if(q){
      var arr=JSON.parse(q),ok=arr.length===order.length;
      for(var i=0;i<arr.length;i++){if(!find(arr[i]))ok=false}
      if(ok)order=arr;
    }
  }catch(e){}
  order=norm(order);          /* 去重 + 补齐（旧 sessionStorage 里可能存了重复项） */
  function norm(a){
    var out=[],seen={},i;
    for(i=0;i<a.length;i++){if(find(a[i])&&!seen[a[i]]){seen[a[i]]=1;out.push(a[i])}}
    for(i=0;i<data.list.length;i++){if(!seen[data.list[i].n]){out.push(data.list[i].n);seen[data.list[i].n]=1}}
    return out;
  }
  function saveQ(){try{sessionStorage.setItem(QKEY,JSON.stringify(order))}catch(e){}}

  function ini(n){return (n||'?').charAt(0)}

  function paint(){
    var r=find(running)||data.list[0];
    runner.style.display='';
    /* 没有图集就先挂占位（首字），图补好后整页刷新自动换上 */
    if(r.i){runner.style.backgroundImage='url("'+r.i+'")';
            runner.classList.remove('ph');runner.removeAttribute('data-ini')}
    else{runner.style.backgroundImage='';runner.classList.add('ph');
         runner.setAttribute('data-ini',ini(r.n))}
    /* 与服务端 current_buddy 无关，点选只改本地视图 */
    if(tag)tag.textContent=(r.n||'')+(r.r?' · '+r.r:'');

    while(sitters.firstChild)sitters.removeChild(sitters.firstChild);
    var shown=0;
    for(var i=0;i<order.length;i++){
      if(shown>=3)break;               /* 右侧最多显示 3 只 */
      var x=find(order[i]);
      if(!x||x.n===running)continue;   /* 没图也要排进来，只是显示占位 */
      shown++;
      var b=document.createElement('button');
      b.className=(came===x.n?'sitter awake':'sitter asleep');
      b.setAttribute('data-n',x.n);
      b.title=x.n+'：点一下让 TA 去跑';
      if(x.i){b.style.backgroundImage='url("'+x.i+'")'}
      else{b.className+=' ph';b.setAttribute('data-ini',ini(x.n))}
      var z=document.createElement('span');
      z.className='zzz';
      z.innerHTML='<i>z</i><i>z</i><i>z</i>';
      b.appendChild(z);
      sitters.appendChild(b);
    }
    sitters.style.display=sitters.children.length?'':'none';
  }

  sitters.addEventListener('click',function(ev){
    var b=ev.target;
    while(b&&b!==sitters&&!b.classList.contains('sitter'))b=b.parentNode;
    if(!b||b===sitters)return;
    var n=b.getAttribute('data-n');
    if(!n||n===running)return;
    b.classList.remove('asleep');
    b.classList.add('awake','hop');     /* 先爬起来 */
    var prev=running;
    setTimeout(function(){
      /* 轮换：被点中的上路，原跑者排队尾，其余前移 */
      var idx=order.indexOf(n);
      if(idx>-1)order.splice(idx,1);
      for(var j=order.length-1;j>=0;j--){if(order[j]===prev)order.splice(j,1)}
      if(prev)order.push(prev);
      order=norm(order);
      saveQ();
      running=n;came=prev;
      try{sessionStorage.setItem(KEY,n)}catch(e){}
      paint();
      track.classList.add('enter');
      setTimeout(function(){track.classList.remove('enter')},560);
      /* 换下来的那只醒几秒再睡 */
      setTimeout(function(){came=null;paint()},2800);
    },240);
  });

  paint();
})();
"""


# 自解 APNG 抽帧：拆块 → 解压反滤波 → 按 fcTL 合成 → 抽 12 帧 → 裁边 → 缩放 → 重编码。
# 不依赖 Pillow/ffmpeg；失败只表示这只没有立绘，不影响签到。
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PET_BUILD_LOCK = threading.Lock()
# 失败的宠物记时间戳，冷却一小时再试。
_PET_FAIL = {}
_PET_FAIL_TTL = 3600


def _png_chunks(blob):
    i, n = 8, len(blob)
    while i + 8 <= n:
        ln = struct.unpack(">I", blob[i:i + 4])[0]
        yield blob[i + 4:i + 8], blob[i + 8:i + 8 + ln]
        i += 12 + ln


def _unfilter(raw, w, h, bpp):
    stride = w * bpp
    out = bytearray(h * stride)
    prev = bytearray(stride)
    pos = 0
    for y in range(h):
        ft = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        elif ft != 0:
            raise ValueError(f"未知的 PNG filter {ft}")
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return out


def _to_rgba(buf, w, h, ct):
    if ct == 6:
        return bytes(buf)
    n = w * h
    out = bytearray(n * 4)
    if ct == 2:
        for i in range(n):
            out[4 * i:4 * i + 3] = buf[3 * i:3 * i + 3]
            out[4 * i + 3] = 255
    elif ct == 0:
        for i in range(n):
            v = buf[i]
            out[4 * i] = out[4 * i + 1] = out[4 * i + 2] = v
            out[4 * i + 3] = 255
    elif ct == 4:
        for i in range(n):
            v = buf[2 * i]
            out[4 * i] = out[4 * i + 1] = out[4 * i + 2] = v
            out[4 * i + 3] = buf[2 * i + 1]
    else:
        raise ValueError(f"不支持的 PNG 色彩类型 {ct}（索引色图集请手工处理）")
    return bytes(out)


def _paint_frame(canvas, px, W, H, fw, fh, fx, fy, blend):
    for j in range(fh):
        cy = fy + j
        if cy < 0 or cy >= H:
            continue
        span = min(fw, W - fx)
        if span <= 0:
            continue
        if fx < 0:
            span += fx
            if span <= 0:
                continue
        src = j * fw * 4
        dst = (cy * W + fx) * 4
        if blend == 0:                      # SOURCE：直接覆盖（含透明像素）
            canvas[dst:dst + span * 4] = px[src:src + span * 4]
        else:                               # OVER：按 alpha 叠在上面
            for k in range(span):
                s = src + k * 4
                d = dst + k * 4
                sa = px[s + 3]
                if sa == 255:
                    canvas[d:d + 4] = px[s:s + 4]
                elif sa:
                    da = canvas[d + 3]
                    oa = sa + da * (255 - sa) // 255
                    for c in range(3):
                        canvas[d + c] = (px[s + c] * sa + canvas[d + c] * da * (255 - sa) // 255) // oa
                    canvas[d + 3] = oa


def png_frames(blob, sample=PET_SHEET_FRAMES):
    """解 PNG/APNG，返回 (w, h, [(idx, rgba_bytes), ...])；只保留抽样帧以省内存。"""
    if not blob.startswith(_PNG_SIG):
        raise ValueError("不是 PNG")
    ihdr, plan, cur, buf = None, [], None, bytearray()
    for typ, data in _png_chunks(blob):
        if typ == b"IHDR":
            w, h, bd, ct, _cm, _fm, im = struct.unpack(">IIBBBBB", data)
            ihdr = (w, h, bd, ct, im)
        elif typ == b"fcTL":
            if cur is not None or buf:
                plan.append((cur, bytes(buf)))
                buf = bytearray()
            seq, fw, fh, fx, fy, dn, dd, dop, bop = struct.unpack(">IIIIIHHBB", data)
            cur = {"w": fw, "h": fh, "x": fx, "y": fy, "dispose": dop, "blend": bop}
        elif typ == b"IDAT":
            buf += data
        elif typ == b"fdAT":
            buf += data[4:]
        elif typ == b"IEND":
            break
    plan.append((cur, bytes(buf)))
    if not ihdr:
        raise ValueError("缺 IHDR")
    w, h, bd, ct, im = ihdr
    if bd != 8 or im != 0:
        raise ValueError(f"只支持 8bit 非隔行 PNG（got depth={bd} interlace={im}）")
    bpp = {6: 4, 2: 3, 0: 1, 4: 2}[ct]

    n = len(plan)
    idx = [round(i * n / sample) % n for i in range(sample)] if n > sample else list(range(n))
    want = set(idx)

    canvas = bytearray(w * h * 4)
    out = []
    for i, (f, raw) in enumerate(plan):
        fw = f["w"] if f else w
        fh = f["h"] if f else h
        fx = f["x"] if f else 0
        fy = f["y"] if f else 0
        before = bytes(canvas) if (f and f["dispose"] == 2) else None
        px = _to_rgba(_unfilter(zlib.decompress(raw), fw, fh, bpp), fw, fh, ct)
        _paint_frame(canvas, px, w, h, fw, fh, fx, fy, f["blend"] if f else 0)
        if i in want:
            out.append((i, bytes(canvas)))
        if f and f["dispose"] == 1:          # 画完擦掉自己这块
            for j in range(fh):
                cy = fy + j
                if 0 <= cy < h:
                    d = (cy * w + fx) * 4
                    canvas[d:d + min(fw, w - fx) * 4] = b"\x00" * (min(fw, w - fx) * 4)
        elif before is not None:             # dispose=PREVIOUS：回到画之前
            canvas = bytearray(before)
    return w, h, out, n


def _crop_scale(px, w, h, box, px_out):
    """裁到 box 后按面积平均缩放进 px_out×px_out（等比、居中、贴底）。"""
    x0, y0, x1, y1 = box
    cw, ch = x1 - x0, y1 - y0
    s = min(px_out / cw, px_out / ch)
    dw, dh = max(1, round(cw * s)), max(1, round(ch * s))
    ox, oy = (px_out - dw) // 2, px_out - dh        # 底部对齐
    out = bytearray(px_out * px_out * 4)
    for dy in range(dh):
        sy0 = y0 + dy * ch // dh
        sy1 = max(sy0 + 1, y0 + (dy + 1) * ch // dh)
        for dx in range(dw):
            sx0 = x0 + dx * cw // dw
            sx1 = max(sx0 + 1, x0 + (dx + 1) * cw // dw)
            r = g = b = a = cnt = 0
            for y in range(sy0, sy1):
                base = (y * w + sx0) * 4
                for x in range(sx1 - sx0):
                    i = base + x * 4
                    al = px[i + 3]
                    if al:
                        r += px[i] * al
                        g += px[i + 1] * al
                        b += px[i + 2] * al
                        a += al
                    cnt += 1
            if a:
                o = ((oy + dy) * px_out + ox + dx) * 4
                out[o] = r // a
                out[o + 1] = g // a
                out[o + 2] = b // a
                out[o + 3] = a // cnt
    return bytes(out)


def _png_encode(w, h, rgba):
    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (_PNG_SIG
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def build_pet_sheet(blob):
    """APNG/PNG → 1152×96 横排图集（12 帧）。"""
    w, h, frames, total = png_frames(blob)
    if not frames:
        raise ValueError("没有可用帧")
    # 并集包围盒：整段动画里出现过的不透明区域，避免裁掉动作
    x0 = y0 = 10 ** 9
    x1 = y1 = -1
    for _i, px in frames:
        for y in range(h):
            row = px[y * w * 4:(y + 1) * w * 4]
            if not any(row[3::4]):
                continue
            if y < y0:
                y0 = y
            y1 = y + 1
            for x in range(w):
                if row[x * 4 + 3]:
                    if x < x0:
                        x0 = x
                    if x > x1:
                        x1 = x
    if x1 < 0:
        raise ValueError("整段动画全透明")
    box = (x0, y0, x1 + 1, y1 + 1)
    slots = []
    for k in range(PET_SHEET_FRAMES):
        px = frames[min(k, len(frames) - 1)][1]      # 帧数不足就重复最后一帧
        slots.append(_crop_scale(px, w, h, box, PET_SHEET_PX))
    # 横向拼图必须按行交错，不能把每格整块拼接。
    row = PET_SHEET_PX * 4
    sheet = bytearray()
    for y in range(PET_SHEET_PX):
        for sl in slots:
            sheet += sl[y * row:(y + 1) * row]
    return _png_encode(PET_SHEET_FRAMES * PET_SHEET_PX, PET_SHEET_PX, bytes(sheet))


def _http_get(url, max_bytes=12 * 1024 * 1024, timeout=60):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 WorkBuddy-Dashboard",
        "Accept": "image/png,image/*,*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("素材过大")
    return data


def ensure_pet_sprite(buddy):
    """给一只没有图集的宠物现抽一张。返回 True 表示本次生成了。"""
    name = buddy.get("name") or ""
    if not name or PET_FILES.get(name):
        return False
    dst = _pet_auto_path(name)
    if os.path.exists(dst) and os.path.getsize(dst) > 2000:
        return False
    urls = [u for u in (buddy.get("anim"), buddy.get("static")) if u]
    if not urls:
        return False
    for u in urls:
        try:
            sheet = build_pet_sheet(_http_get(u))
        except Exception as e:  # noqa: BLE001
            log(f"  宠物图集：{name} 用 {u.rsplit('/', 1)[-1]} 生成失败（{e}），换下一个源")
            continue
        tmp = dst + ".tmp"
        os.makedirs(PET_DIR, exist_ok=True)
        with open(tmp, "wb") as fh:
            fh.write(sheet)
        os.replace(tmp, dst)          # 原子替换，nginx 不会读到半个文件
        _PET_FAIL.pop(name, None)
        log(f"  宠物图集：{name} 自动生成 {len(sheet) // 1024}KB → {dst}")
        return True
    _PET_FAIL[name] = time.time()
    log(f"  宠物图集：{name} 所有素材都失败，先不上舞台（用首字占位，1 小时后重试）")
    return False


def _build_pets_bg(buddies):
    """后台补图：一次最多三只，失败不影响主流程。"""
    if not _PET_BUILD_LOCK.acquire(blocking=False):
        return
    try:
        for b in buddies[:3]:
            try:
                ensure_pet_sprite(b)
            except Exception:  # noqa: BLE001
                log(traceback.format_exc())
    finally:
        _PET_BUILD_LOCK.release()


def render_html(s, hist):
    done = s.get("today_done")
    no_token = not s.get("token_fp")
    balance = s.get("balance")
    bal_txt = "—" if balance is None else f"{balance:,}"
    bal_cls = "" if balance is not None else " dim"
    total = s.get("balance_total")
    pkgs = s.get("packages") or []
    if total:
        bal_sub = f"总容量 {total:,}"
        if len(pkgs) > 1:
            bal_sub += f" · {len(pkgs)} 个资源包合计"
    else:
        bal_sub = "未配置令牌" if no_token else "等待首次查询"
    streak = s.get("streak_days") or 0
    next_day = s.get("next_streak_day")
    bonus = s.get("streak_bonus_credit")
    next_bonus = f"第 {next_day} 天 +{bonus}" if next_day and bonus else "—"
    today_credit = s.get("today_credit")
    daily = s.get("daily_credit") or 0

    if done:
        pill_cls, pill_txt = "ok", "今日已自动领取"
    elif no_token:
        pill_cls, pill_txt = "warn", "未配置令牌"
    else:
        pill_cls, pill_txt = "warn", "今日尚未领取"

    acct = s.get("account") or {}
    acct_name = acct.get("name") or "已登录"
    acct_sub = f"（{mask_phone(acct['sub'])}）" if acct.get("sub") else ""
    acct_exp = ""
    if acct.get("expiry"):
        try:
            acct_exp = " · 凭据到期 " + str(acct["expiry"])[:16].replace("T", " ")
        except Exception:  # noqa: BLE001
            acct_exp = ""
    acct_fp = acct.get("fp") or "-"
    acct_src = acct.get("source") or "-"

    dates = set()
    for d0 in s.get("checkin_dates") or []:
        dates.add(str(d0)[:10])
    for h in hist:
        if h.get("ok") and h.get("kind") in ("success", "already") and h.get("ts"):
            dates.add(str(h["ts"])[:10])

    chips = []
    today = now().date()
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        got = d.isoformat() in dates
        chips.append(
            f'<div class="chip{" on" if got else ""}"><b>{d.day}</b><span>{d.month}月</span>'
            f'<i>{"✓" if got else ""}</i></div>'
        )

    rows = []
    for h in hist:
        ok = bool(h.get("ok"))
        kind = h.get("kind") or ""
        ts = fdt(h.get("ts"))
        credit = f"+{h.get('credit')}" if h.get("credit") else ""
        if kind == "travel":
            dot = '<span class="dot t">✦</span>'
            tag = '<span class="tg">旅行</span>'
        else:
            dot = '<span class="dot g">✓</span>' if ok else '<span class="dot r">✕</span>'
            tag = ""
        rows.append(
            f'<tr><td class="mono">{ts}</td>'
            f'<td>{dot}{tag}<span class="msg">{_esc(h.get("msg") or "-")}</span></td>'
            f'<td class="mono right">{credit}</td></tr>'
        )
    if not rows:
        rows.append('<tr><td colspan="3" class="empty">暂无记录（写入令牌后自动开始）</td></tr>')

    pkg_rows = []
    for p in pkgs:
        code = str(p.get("code") or "-")
        unit = p.get("unit") or "分"
        hint = pkg_hint(code)
        hint_html = f'<div class="phint">{hint}</div>' if hint else ""
        pkg_rows.append(
            f'<tr><td><div class="pname">{_esc(pkg_label(code))}</div>'
            f'<div class="pcode">{_esc(code)}</div>{hint_html}</td>'
            f'<td class="num">{p.get("remain", 0):,} {unit}</td>'
            f'<td class="num" style="color:var(--fg2)">{p.get("total", 0):,}</td></tr>'
        )
    pkg_block = ""
    if pkg_rows:
        pkg_block = (
            '<div class="block"><h2>积分明细（按资源包）</h2>'
            '<p class="pkg-note">上方「剩余积分」= 下列各包剩余之和。'
            '若与客户端显示不一致，多半是接口里同时含订阅套餐包与赠送/活动包，计量口径不同。'
            '体验版的基础积分每月 1 号重新发放 500，上月未用完的不结转。</p>'
            '<table><thead><tr><th>资源包</th><th class="r">剩余</th>'
            '<th class="r">周期总量</th></tr></thead>'
            f'<tbody>{"".join(pkg_rows)}</tbody></table></div>'
        )

    exp = s.get("token_exp") or "—"
    exp_days = None
    if exp and exp != "—":
        try:
            _secs = (datetime.fromisoformat(exp.replace("Z", "+00:00")) - now()).total_seconds()
            left = int(_secs // 86400)          # 已过期给负数（向下取整，-0.4 天 = -1）
            if _secs >= 0 and _secs % 86400:    # 未过期向上取整：剩 3.9 天显示 4 天
                left += 1
            exp_days = left
            exp = f"{exp[:10]}（剩 {left} 天）"
        except Exception:  # noqa: BLE001
            pass

    # 剩 7 天以内才提示换新令牌。
    if exp_days is not None and exp_days < 0:
        renew_html = ('<div class="notice bad"><b>登录凭据已过期</b>'
                      '<span>重新登录一次就能恢复，历史积分与流水都还在库里。</span>'
                      '<a class="nbtn" href="/login">重新登录</a></div>')
    elif exp_days is not None and exp_days <= 7:
        renew_html = (f'<div class="notice"><b>凭据还剩 {exp_days} 天</b>'
                      '<span>剩 7 天以内才会提示换新：重新登录一次即可续 30 天。</span>'
                      '<a class="nbtn" href="/login">重新登录</a></div>')
    else:
        renew_html = ""

    today_show = today_credit if today_credit is not None else (daily if done else 0)

    # 今日入账 = 签到 + 旅行（kind=travel）。
    _d = today.isoformat()
    travel_today = sum(
        int(h.get("credit") or 0)
        for h in hist
        if h.get("ok") and h.get("kind") == "travel" and str(h.get("ts"))[:10] == _d
    )
    _ck = today_show or 0

    tv_state = s.get("travel_state")
    if not TRAVEL_ENABLE:
        tv_cls, tv_txt, tv_sub = "", "未开启", "TRAVEL_ENABLE=0"
    elif tv_state == "arrived":
        tv_cls, tv_txt, tv_sub = "ok", "可领取", "下一轮自动领"
    elif tv_state == "traveling":
        tv_cls, tv_txt = "warn", "旅行中"
        _at = s.get("travel_arrive_at")
        # 地点名来自服务端，进 HTML 前转义。
        tv_sub = (_esc(s.get("travel_location") or "途中")
                  + (" · " + fdt(_at)[11:16] + " 回" if _at else ""))
    elif tv_state == "idle":
        rw = s.get("travel_reward")
        if s.get("travel_daily_limit"):
            tv_cls, tv_txt = "", "今日已派完"
            tv_sub = (f"上次 +{rw} 分 · 明日 00:00 后再派") if rw else "明日 00:00 后再派"
        else:
            tv_cls, tv_txt = "warn", "待派遣"
            tv_sub = (f"上次 +{rw} 分 · 下一轮自动派") if rw else "下一轮自动派"
    else:
        tv_cls, tv_txt, tv_sub = "", "—", "尚未巡检"

    buddies = s.get("buddies") or []
    cur_pet = next((b for b in buddies if b.get("current")), None)
    if cur_pet is None and buddies:
        # 服务端没标 current 时取最稀有的一只。
        cur_pet = max(buddies, key=lambda b: RARITY_RANK.get(b.get("rarity") or "", 0))
    pet_name = (cur_pet or {}).get("name") or ""
    # pet_url 是模块级函数，不能当局部变量名。
    pet_rarity = ((cur_pet or {}).get("rarity") or "").lower()

    if buddies:
        bd_txt = f"{len(buddies)}<small>只</small>"
        _cnt = {k: sum(1 for b in buddies if (b.get("rarity") or "") == k) for k in ("UR", "SSR", "SR", "R")}
        bd_sub = " · ".join(f"{k} {v}" for k, v in _cnt.items() if v) or "—"
        _sorted = sorted(buddies, key=lambda b: -RARITY_RANK.get(b.get("rarity") or "", 0))
        bd_tags = "".join(
            '<span class="rb r{0}{1}">{2}</span>'.format(
                _cls(b.get("rarity") or ""),
                " cur" if b.get("current") else "",
                _esc(b.get("name") or "?"),
            )
            for b in _sorted
        )
    else:
        bd_txt, bd_sub, bd_tags = "—", "等待首次查询", ""

    # 舞台显示全部宠物，没有图集的用首字占位。
    _fig = [b for b in buddies if (b.get("name") or "")]
    _fig.sort(key=lambda b: (-RARITY_RANK.get(b.get("rarity") or "", 0), b.get("name") or ""))
    _run = next((b for b in _fig if b.get("name") == pet_name), (_fig[0] if _fig else None))
    if _run:
        _rname = _run.get("name") or ""
        # 不能叫 _cls：会遮住模块级 _cls()。
        _run_cls = "run" if tv_state == "traveling" else "idle"
        _sit_names = [b.get("name") or "" for b in _fig if b.get("name") != _rname][:3]
        _n = max(1, len(_sit_names))
        # 整列高度恒等于马路 112px：n*sz + (n-1)*2 = 112 → 3 只 36px / 2 只 55px
        _sz = min(56, (112 - 2 * (_n - 1)) // _n)

        def _ini(nm):
            """占位用：名字首字（去掉颜色/品种后缀噪音）。"""
            nm = (nm or "").strip()
            return nm[0] if nm else "?"

        def _sitter_html(n0):
            img = pet_url(n0)
            n_e = _esc(n0)          # 昵称来自服务端，进 HTML/属性前必须转义
            i_e = _esc(_ini(n0))
            if img:
                return ('<button class="sitter asleep" data-n="{0}" title="{0}：点一下让 TA 去跑"'
                        ' style="background-image:url({1})">'
                        '<span class="zzz"><i>z</i><i>z</i><i>z</i></span></button>').format(n_e, img)
            # 没图：占位方块，照样可点、可轮换
            return ('<button class="sitter asleep ph" data-n="{0}" data-ini="{2}"'
                    ' title="{0}：图集还在生成，点一下让 TA 去跑">'
                    '<span class="zzz"><i>z</i><i>z</i><i>z</i></span></button>').format(n_e, "", i_e)

        _sit = "".join(_sitter_html(n0) for n0 in _sit_names)
        _rimg = pet_url(_rname)
        if _rimg:
            _runner = ('<div class="runner" id="runner"'
                       f' style="background-image:url({_rimg})"></div>')
        else:
            _runner = (f'<div class="runner ph" id="runner" data-ini="{_esc(_ini(_rname))}"></div>')
        pet_html = (
            '<div class="petrow" id="petrow">'
            f'<div class="track {_run_cls}" id="track">'
            '<div class="road"><i class="lane"></i></div><div class="petshadow"></div>'
            f'{_runner}'
            f'<div class="pettag" id="pettag">{_esc(_rname)} · {_esc(_run.get("rarity") or "")}</div>'
            '</div>'
            f'<div class="sitters" id="sitters" style="--sz:{_sz}px">{_sit}</div>'
            '</div>'
        )
        _json = json.dumps(
            {
                "run": _rname,
                "list": [
                    {"n": b.get("name") or "",
                     "i": pet_url(b.get("name") or ""),
                     "r": b.get("rarity") or ""}
                    for b in _fig
                ],
            },
            ensure_ascii=False,
        ).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        pet_html += f'<script id="petdata" type="application/json">{_json}</script>'
    else:
        pet_html = ""

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WorkBuddy 积分看板</title>
<meta name="color-scheme" content="dark light">
<meta http-equiv="refresh" content="60">
<script>(function(){{try{{var t=localStorage.getItem('wb-theme');if(t==='light'||t==='dark'){{document.documentElement.setAttribute('data-theme',t)}}}}catch(e){{}}}})();</script>
<style>
*{{box-sizing:border-box}}
/* 关掉移动端点击高亮：不跟圆角，会盖出一个方块 */
*{{-webkit-tap-highlight-color:transparent}}
button,a,.acct,.aitem,.aadd{{-webkit-tap-highlight-color:transparent;-webkit-touch-callout:none}}
/* 键盘聚焦仍要看得见：outline 会自动跟随 border-radius，不会变成方块 */
:focus{{outline:none}}
:focus-visible{{outline:2px solid rgba(91,140,255,.62);outline-offset:2px}}
html,body{{min-height:100%}}

/* 主题令牌（默认深色） */
:root{{
  color-scheme:dark;
  --bg:#05070e;
  --bg-glow:radial-gradient(120% 80% at 50% -10%,#111a33 0%,transparent 60%);
  --fg:rgba(255,255,255,.94);
  --fg2:rgba(255,255,255,.60);
  --fg3:rgba(255,255,255,.42);
  --panel-a:rgba(255,255,255,.14);
  --panel-b:rgba(255,255,255,.05);
  /* 玻璃釉面：左上角高光 */
  --gloss:rgba(255,255,255,.14);
  --inner:rgba(2,6,23,.30);
  /* 浮层（下拉/气泡）：压在内容之上，底色比卡片实 */
  --drop-a:rgba(26,33,51,.78);
  --drop-b:rgba(13,18,30,.66);
  /* 兜底实心底：WKWebView 里 backdrop-filter 常失效，玻璃层下垫一层接近不透明的底 */
  --drop-solid:rgba(13,18,30,.975);
  --hero1:rgba(255,255,255,.155);
  --hero2:rgba(255,255,255,.052);
  --stroke:rgba(255,255,255,.20);
  --edge:rgba(255,255,255,.42);
  --hair:rgba(255,255,255,.07);
  --shadow:0 18px 46px rgba(2,6,23,.42);
  --hero-glow:rgba(110,140,255,.36);
  --num-glow:0 2px 26px rgba(150,185,255,.5);
  --tbtn-bg:rgba(255,255,255,.10);
  --blob-op:.50;
  --blob-blur:70px;
  --scrim:rgba(5,7,14,.52);
  --blob1:#5b5bf5;--blob2:#06b6d4;--blob3:#d946ef;--blob4:#2563eb;
  --blob-mode:screen;
  --grain:.055;
  --ok:#34d399;--warn:#fbbf24;--bad:#f87171;
  --ok-bg:rgba(52,211,153,.16);--ok-bd:rgba(52,211,153,.42);
  --dot-fg:#07101d;
  --tv-fg:#c7d2fe;--tv-bg:rgba(99,102,241,.22);--tv-bd:rgba(129,140,248,.45);
  --link:#93c5fd;
  --shadow-none:none;
  /* 品牌标记（深色主题）：深蓝底 + 金色 W */
  --logo-bg:#121a2e;--logo-w:#f2c76b;--logo-coin:#f2c76b;
  --logo-hole:rgba(122,74,12,.42);--logo-edge:transparent;
}}

/* 浅色令牌 */
@media (prefers-color-scheme: light){{
  :root:not([data-theme="dark"]){{
    color-scheme:light;
    --bg:#e7ecf7;
    --bg-glow:radial-gradient(120% 80% at 50% -10%,#ffffff 0%,transparent 62%);
    --fg:#0b1220;
    --fg2:rgba(11,18,32,.62);
    --fg3:rgba(11,18,32,.45);
    --panel-a:rgba(255,255,255,.56);
    --panel-b:rgba(255,255,255,.26);
    --gloss:rgba(255,255,255,.92);
    --inner:rgba(15,23,42,.06);
    --drop-a:rgba(255,255,255,.72);
    --drop-b:rgba(226,238,255,.40);
    --drop-solid:rgba(250,252,255,.975);
    --hero1:rgba(255,255,255,.68);
    --hero2:rgba(255,255,255,.34);
    --stroke:rgba(255,255,255,.78);
    --edge:rgba(255,255,255,1);
    --hair:rgba(11,18,32,.09);
    --shadow:0 18px 44px rgba(15,23,42,.13);
    --hero-glow:rgba(110,140,255,.2);
    --num-glow:none;
    --tbtn-bg:rgba(255,255,255,.62);
    --blob-op:.56;
    --blob-blur:70px;
    --scrim:rgba(255,255,255,.50);
    --grain:.03;
    --ok:#059669;
    --warn:#b45309;
    --bad:#dc2626;
    --ok-bg:rgba(16,185,129,.13);
    --ok-bd:rgba(16,185,129,.36);
    --dot-fg:#ffffff;
    --tv-fg:#4338ca;--tv-bg:rgba(99,102,241,.12);--tv-bd:rgba(99,102,241,.30);
    --link:#2563eb;
  /* 品牌标记（浅色主题）：白底 + 深藏青 W，金点保留 */
  --logo-bg:#ffffff;--logo-w:#14203a;--logo-coin:#dfa14a;
  --logo-hole:rgba(122,74,12,.5);--logo-edge:rgba(11,18,32,.10);
    --blob1:#6366f1;--blob2:#06b6d4;--blob3:#8b5cf6;--blob4:#3b82f6;
    --blob-mode:normal;
    }}
}}
:root[data-theme="light"]{{
  color-scheme:light;
  --bg:#e7ecf7;
  --bg-glow:radial-gradient(120% 80% at 50% -10%,#ffffff 0%,transparent 62%);
  --fg:#0b1220;
  --fg2:rgba(11,18,32,.62);
  --fg3:rgba(11,18,32,.45);
  --panel-a:rgba(255,255,255,.56);
  --panel-b:rgba(255,255,255,.26);
  --gloss:rgba(255,255,255,.92);
  --inner:rgba(15,23,42,.06);
  --drop-a:rgba(255,255,255,.72);
  --drop-b:rgba(226,238,255,.40);
  --drop-solid:rgba(250,252,255,.975);
  --hero1:rgba(255,255,255,.68);
  --hero2:rgba(255,255,255,.34);
  --stroke:rgba(255,255,255,.78);
  --edge:rgba(255,255,255,1);
  --hair:rgba(11,18,32,.09);
  --shadow:0 18px 44px rgba(15,23,42,.13);
  --hero-glow:rgba(110,140,255,.2);
  --num-glow:none;
  --tbtn-bg:rgba(255,255,255,.62);
  --blob-op:.56;
  --blob-blur:70px;
  --scrim:rgba(255,255,255,.50);
  --grain:.03;
  --ok:#059669;
  --warn:#b45309;
  --bad:#dc2626;
  --ok-bg:rgba(16,185,129,.13);
  --ok-bd:rgba(16,185,129,.36);
  --dot-fg:#ffffff;
  --tv-fg:#4338ca;--tv-bg:rgba(99,102,241,.12);--tv-bd:rgba(99,102,241,.30);
  --link:#2563eb;
  /* 品牌标记（浅色主题）：白底 + 深藏青 W，金点保留 */
  --logo-bg:#ffffff;--logo-w:#14203a;--logo-coin:#dfa14a;
  --logo-hole:rgba(122,74,12,.5);--logo-edge:rgba(11,18,32,.10);
  --blob1:#6366f1;--blob2:#06b6d4;--blob3:#8b5cf6;--blob4:#3b82f6;
  --blob-mode:normal;}}

/* 页面 */
body{{margin:0;padding:32px 16px 48px;background:var(--bg);color:var(--fg);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB",sans-serif;
-webkit-font-smoothing:antialiased;
transition:background .35s ease,color .35s ease}}
body::before{{content:"";position:fixed;inset:0;z-index:0;background:var(--bg-glow);pointer-events:none}}

/* 极光光斑 */
/* 定位用视口百分比 + vmax 尺寸上限，避免页面变高时整层被推出视口 */
/* 层级走正向（0/1/2/3）：html 有兜底底色，负 z-index 会被根背景盖掉 */
.bg{{position:fixed;inset:0;z-index:1;pointer-events:none;overflow:hidden;
filter:blur(var(--blob-blur)) saturate(155%);opacity:var(--blob-op);transition:opacity .35s ease}}
.bg::after{{content:"";position:absolute;inset:0;z-index:2;background:var(--scrim)}}
.bg i{{position:absolute;z-index:1;display:block;border-radius:50%;mix-blend-mode:var(--blob-mode)}}
.bg i:nth-child(1){{width:min(66vmax,860px);height:min(66vmax,860px);left:-13%;top:-20%;background:var(--blob1);animation:d1 22s ease-in-out infinite}}
.bg i:nth-child(2){{width:min(54vmax,720px);height:min(54vmax,720px);right:-15%;top:-8%;background:var(--blob2);animation:d2 26s ease-in-out infinite}}
.bg i:nth-child(3){{width:min(60vmax,780px);height:min(60vmax,780px);left:6%;bottom:-30%;background:var(--blob3);animation:d3 30s ease-in-out infinite}}
.bg i:nth-child(4){{width:min(40vmax,540px);height:min(40vmax,540px);right:-4%;bottom:-2%;background:var(--blob4);animation:d4 24s ease-in-out infinite}}
@keyframes d1{{0%,100%{{transform:translate3d(0,0,0) scale(1)}}50%{{transform:translate3d(7vw,5vh,0) scale(1.16)}}}}
@keyframes d2{{0%,100%{{transform:translate3d(0,0,0) scale(1)}}50%{{transform:translate3d(-6vw,7vh,0) scale(1.12)}}}}
@keyframes d3{{0%,100%{{transform:translate3d(0,0,0) scale(1)}}50%{{transform:translate3d(5vw,-6vh,0) scale(1.14)}}}}
@keyframes d4{{0%,100%{{transform:translate3d(0,0,0) scale(1)}}50%{{transform:translate3d(-5vw,-5vh,0) scale(1.18)}}}}
@media(prefers-reduced-motion:reduce){{.bg i{{animation:none}}}}

/* 磨砂颗粒 */
.grain{{position:fixed;inset:0;z-index:2;pointer-events:none;opacity:var(--grain);
background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)'/%3E%3C/svg%3E")}}

.wrap{{position:relative;z-index:3;max-width:860px;margin:0 auto}}

/* 玻璃基材 */
/* 玻璃质感：左上高光（--gloss）+ 背景模糊（backdrop）+ 顶亮线底内影（厚度）。 */
.hero,.card,.block,.chip{{
background:
  radial-gradient(130% 105% at 6% -14%,var(--gloss),transparent 60%),
  linear-gradient(150deg,var(--panel-a),var(--panel-b));
border:1px solid var(--stroke);border-radius:18px;
-webkit-backdrop-filter:blur(26px) saturate(145%) brightness(1.03);
backdrop-filter:blur(26px) saturate(145%) brightness(1.03);
box-shadow:var(--shadow),
  inset 0 1px 0 var(--edge),
  inset 0 -22px 36px -28px var(--inner),
  inset 0 0 40px -18px var(--gloss);
transition:background .35s ease,border-color .35s ease,box-shadow .35s ease,transform .25s ease}}

.top{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px;flex-wrap:wrap}}
h1{{font-size:19px;margin:0;font-weight:650;letter-spacing:.2px;display:flex;align-items:center;gap:9px}}
.wbm{{flex:none;border-radius:7px}}
.tools{{display:flex;align-items:center;gap:9px}}
.tbtn{{cursor:pointer;font-family:inherit;font-size:12.5px;font-weight:600;color:var(--fg2);
padding:7px 13px;border-radius:999px;background:var(--tbtn-bg);border:1px solid var(--stroke);
-webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);
box-shadow:inset 0 1px 0 var(--edge);transition:.2s}}
.tbtn:hover{{color:var(--fg);border-color:var(--edge);filter:brightness(1.08)}}
.tbtn:active{{filter:brightness(.94)}}

.pill{{display:inline-flex;align-items:center;gap:7px;padding:7px 13px;border-radius:999px;font-size:13px;
font-weight:600;border:1px solid;transition:.35s}}
.pill.ok{{color:var(--ok);background:var(--ok-bg);border-color:var(--ok-bd)}}
.pill.warn{{color:var(--warn);background:rgba(251,191,36,.14);border-color:rgba(251,191,36,.38)}}
.pill i{{width:7px;height:7px;border-radius:50%;background:currentColor;
box-shadow:0 0 10px currentColor;display:inline-block}}

.hero{{position:relative;overflow:hidden;border-radius:22px;padding:26px 28px;
display:flex;align-items:flex-end;justify-content:space-between;gap:16px;
background:linear-gradient(145deg,var(--hero1),var(--hero2) 55%,var(--hero1))}}
.hero::before{{content:"";position:absolute;inset:0;pointer-events:none;
background:radial-gradient(120% 90% at 15% 0%,var(--edge),transparent 55%);opacity:.5}}
.hero::after{{content:"";position:absolute;left:-30%;right:-30%;bottom:-70%;height:120%;pointer-events:none;
background:radial-gradient(50% 50% at 50% 50%,var(--hero-glow),transparent 70%)}}
.hero>div{{position:relative;z-index:1}}
.hero .label{{font-size:13px;color:var(--fg2);margin-bottom:8px;letter-spacing:.3px}}
.hero .num{{font-size:52px;font-weight:700;line-height:1;letter-spacing:-1.5px;
font-variant-numeric:tabular-nums;text-shadow:var(--num-glow)}}
.hero .num.dim{{font-size:34px;color:var(--fg2);letter-spacing:0;text-shadow:none}}
.hero .num.dim small{{font-size:13px}}
.hero .num small{{font-size:15px;font-weight:500;color:var(--fg2);margin-left:7px;text-shadow:none}}
.hero .sub{{font-size:12px;color:var(--fg3);margin-top:12px}}
.hero .right{{text-align:right;font-size:12px;color:var(--fg2);line-height:2;
white-space:nowrap;flex:0 0 auto}}
.hero>div:first-child{{flex:1 1 auto;min-width:0}}
.hero .right b{{color:var(--fg);font-weight:600}}

.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(136px,1fr));gap:12px;margin-top:14px}}
/* 宠物舞台（样式见 PET_CSS） */
{PET_CSS}
/* 图鉴独占一行，避免在窄容器里折成 5+1。 */
.card.span{{grid-column:1/-1}}
.dexrow{{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-top:2px}}
.dexrow .v{{margin:0}}
/* 图鉴小标签 */
.rbrow{{margin-top:6px;line-height:1}}
.rb{{display:inline-block;font-size:10px;padding:2px 6px;border-radius:6px;margin:3px 4px 0 0;
  background:rgba(128,128,128,.14);color:var(--fg3);white-space:nowrap}}
.rb.rur{{background:rgba(214,158,46,.18);color:#c99a2e}}
.rb.rssr{{background:rgba(150,110,220,.18);color:#8f6fd0}}
.rb.rsr{{background:rgba(70,140,220,.16);color:#4a86c8}}
.rb.rcur{{box-shadow:inset 0 0 0 1px currentColor}}
.card{{padding:16px 17px}}
.card:hover{{transform:translateY(-2px)}}
.card .k{{font-size:12px;color:var(--fg3);margin-bottom:9px}}
.card .v{{font-size:21px;font-weight:650;font-variant-numeric:tabular-nums;color:var(--fg)}}
.card .v small{{font-size:12px;color:var(--fg2);font-weight:500;margin-left:3px}}
.card .v.state{{font-size:18px}}
.card .v.state.ok{{color:var(--ok)}}
.card .v.state.warn{{color:var(--warn)}}

.block{{padding:18px 19px;margin-top:14px}}
.block h2{{font-size:12px;margin:0 0 13px;color:var(--fg2);font-weight:600;letter-spacing:.6px;text-transform:uppercase}}

.cal{{display:grid;grid-template-columns:repeat(14,1fr);gap:6px}}
.chip{{position:relative;border-radius:11px;padding:8px 0 6px;text-align:center;color:var(--fg3);
background:rgba(127,127,127,.10);border-color:var(--hair);box-shadow:none}}
.chip b{{display:block;font-size:13px;font-weight:600}}
.chip span{{display:block;font-size:9px;opacity:.85}}
.chip i{{position:absolute;top:-6px;right:-3px;font-size:10px;color:var(--ok);font-style:normal}}
.chip.on{{background:var(--ok-bg);border-color:var(--ok-bd);color:var(--ok);
box-shadow:0 0 20px var(--ok-bg),inset 0 1px 0 var(--edge)}}

table{{width:100%;border-collapse:collapse;font-size:13px}}
td{{padding:9px 4px;border-bottom:1px solid var(--hair);vertical-align:middle}}
tr:last-child td{{border-bottom:none}}
.mono{{font-variant-numeric:tabular-nums;color:var(--fg3);white-space:nowrap;width:150px;font-size:12.5px;letter-spacing:-.1px}}
td.right{{text-align:right;color:var(--ok);font-weight:600;width:56px}}
.pkg-note{{margin:0 0 12px;font-size:12px;line-height:1.7;color:var(--fg3)}}
td.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--ok);font-weight:600}}
thead th{{font-size:11px;color:var(--fg3);font-weight:600;text-align:left;letter-spacing:.4px;padding-bottom:7px}}
thead th.r{{text-align:right}}
.msg{{color:var(--fg)}}
.dot{{display:inline-block;width:16px;height:16px;line-height:16px;text-align:center;border-radius:50%;
font-size:10px;margin-right:7px;color:var(--dot-fg);font-weight:700}}
.dot.g{{background:var(--ok);box-shadow:0 0 12px var(--ok-bg)}}
.dot.r{{background:var(--bad);box-shadow:0 0 12px rgba(248,113,113,.4)}}
.dot.t{{background:linear-gradient(135deg,#8b5cf6,#38bdf8);box-shadow:0 0 12px rgba(139,92,246,.45)}}
.tg{{display:inline-block;font-size:10px;line-height:1;padding:3px 6px;border-radius:6px;margin-right:6px;
background:var(--tv-bg);color:var(--tv-fg);border:1px solid var(--tv-bd);vertical-align:1px;font-weight:600}}
.pname{{font-weight:600;color:var(--fg)}}
.pcode{{font-size:11px;color:var(--fg3);font-variant-numeric:tabular-nums;letter-spacing:-.1px;margin-top:1px}}
.phint{{font-size:11px;color:var(--fg3);opacity:.82;margin-top:3px;line-height:1.5}}
.empty{{color:var(--fg3);text-align:center;padding:20px}}
.foot{{margin-top:16px;font-size:12px;color:var(--fg3);display:flex;justify-content:space-between;
gap:12px;flex-wrap:wrap}}
.clock{{font-variant-numeric:tabular-nums;color:var(--fg2);font-weight:600;letter-spacing:.2px}}
.acct{{font-size:12px;color:var(--fg2);background:rgba(127,127,127,.10);border:1px solid var(--hair);
border-radius:999px;padding:5px 12px;white-space:nowrap;width:216px;max-width:216px;overflow:hidden;
font-weight:500;position:relative;font-family:inherit;display:flex;align-items:center;gap:6px;
text-align:left;cursor:pointer;-webkit-backdrop-filter:blur(14px) saturate(150%);
backdrop-filter:blur(14px) saturate(150%);
transition:background .2s ease,border-color .2s ease}}
.acct:hover{{background:rgba(127,127,127,.16)}}
.acct:active{{background:rgba(127,127,127,.22)}}
.acct-in{{display:block;white-space:nowrap;overflow:hidden;will-change:transform;flex:1 1 auto;min-width:0}}
.acct-ar{{flex:none;font-size:10px;opacity:.7;line-height:1}}
/* 内容超宽才滚动（JS 加 .scroll 并给出 --shift），来回滑而不是跳回开头 */
.acct.scroll .acct-in{{animation:acctSlide 14s ease-in-out infinite alternate}}
@keyframes acctSlide{{from{{transform:translateX(0)}}to{{transform:translateX(var(--shift,0px))}}}}
@media(prefers-reduced-motion:reduce){{.acct.scroll .acct-in{{animation:none}}}}

/* 账户下拉：挂在账户按钮正下方 */
.acctwrap{{position:relative;flex:none;z-index:56}}
.acct.open{{background:rgba(47,107,255,.14);border-color:rgba(47,107,255,.45)}}
/* 玻璃浮层：磨砂 + 折射底层，不能做成实心白块。 */
.adrop{{position:absolute;top:calc(100% + 8px);right:0;z-index:61;width:324px;max-width:88vw;
background-color:var(--drop-solid);
background-image:linear-gradient(155deg,var(--drop-a),var(--drop-b));
-webkit-backdrop-filter:blur(32px) saturate(200%);backdrop-filter:blur(32px) saturate(200%);
border:1px solid var(--stroke);border-radius:16px;padding:8px;overflow:hidden;
box-shadow:0 22px 48px rgba(2,6,23,.30),0 10px 34px -16px rgba(72,112,255,.42),
inset 0 1px 0 var(--edge);
/* 展开/收起用 transition（JS 加 .in / .out），transform-origin 放右上角 */
transform-origin:top right;opacity:0;transform:translateY(-9px);
/* 只动 translateY/opacity：Safari 里给 backdrop-filter 元素加 scale 容易露出直角方块 */
transition:opacity .2s cubic-bezier(.22,.68,.32,1),transform .26s cubic-bezier(.22,.68,.32,1);
}}
.adrop.in{{opacity:1;transform:none}}
.adrop.out{{opacity:0;transform:translateY(-6px);transition-duration:.15s}}
/* 顶部反光，体现玻璃厚度 */
.adrop::before{{content:"";position:absolute;left:0;right:0;top:0;height:48%;pointer-events:none;
background:linear-gradient(180deg,var(--edge),transparent);opacity:.30;border-radius:16px 16px 0 0;
z-index:0}}
/* 内容压在反光层之上 */
.adrop>*{{position:relative;z-index:1}}
@keyframes dropIn{{from{{opacity:0;transform:translateY(-7px) scale(.985)}}
to{{opacity:1;transform:none}}}}
@media(prefers-reduced-motion:reduce){{.adrop{{transition:opacity .01s,transform .01s}}}}
/* 一屏 5 行（行高约 53px），第 6 行露一条边提示还能上下滑 */
.alist{{max-height:276px;overflow-y:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch;
scrollbar-width:thin;scrollbar-color:rgba(127,127,127,.55) transparent}}
/* 常显细滚动条：macOS 默认会隐藏，滑动入口看不见 */
.alist::-webkit-scrollbar{{width:5px}}
.alist::-webkit-scrollbar-thumb{{background:rgba(127,127,127,.5);border-radius:9px}}
.alist::-webkit-scrollbar-track{{background:transparent}}
/* 还有更多账户时，列表底部一道极淡内阴影，提示可以往下滑 */
.adrop.more .alist{{box-shadow:inset 0 -9px 9px -9px rgba(0,0,0,.55)}}
.aitem{{display:flex;align-items:center;gap:10px;width:100%;padding:10px 11px;font-family:inherit;
text-align:left;color:var(--fg);background:transparent;border:0;border-radius:10px;cursor:pointer}}
.aitem+.aitem{{margin-top:1px}}
.aitem.on{{background:rgba(47,107,255,.10)}}
.aitem:active{{background:rgba(47,107,255,.16)}}
.aitem .ai{{flex:1 1 auto;min-width:0}}
.aitem .an{{font-size:13.5px;font-weight:600;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis}}
.aitem .ap{{font-size:11.5px;color:var(--fg3);margin-top:2px;font-variant-numeric:tabular-nums}}
.aitem .ab{{margin-left:auto;text-align:right;flex:none}}
.aitem .ab b{{font-size:14px;font-variant-numeric:tabular-nums}}
.aitem .ab i{{display:block;font-style:normal;font-size:10.5px;color:var(--fg3);margin-top:2px}}
/* 「当前」贴在昵称后面：右侧要留给余额和退出按钮，行尾文字一多就会挤出可视区 */
.aitem .ck{{display:inline-block;margin-left:6px;padding:1px 5px;border-radius:5px;
font-size:10px;font-weight:700;color:#2f6bff;background:rgba(47,107,255,.13);
vertical-align:1px}}
/* 每行右侧的「退出」：次级红按钮。 */
.aitem .aout{{flex:none;margin-left:6px;padding:3px 7px;border-radius:7px;font-size:10.5px;
line-height:1.5;color:#e5484d;border:1px solid rgba(229,72,77,.34);white-space:nowrap;
transition:background .16s,border-color .16s,opacity .16s}}
.aitem .aout:hover{{background:rgba(229,72,77,.14);border-color:rgba(229,72,77,.62)}}
.aitem .aout:active{{background:rgba(229,72,77,.22)}}
.aitem .aout.busy{{opacity:.55}}
/* 聚合账户：下拉第一项，和下面的单号明显区分开 */
.aitem.agg{{margin-bottom:6px;border:1px solid rgba(120,160,255,.30);border-radius:12px;
background:linear-gradient(135deg,rgba(91,140,255,.17),rgba(139,92,246,.10));
box-shadow:inset 0 1px 0 rgba(255,255,255,.16),0 6px 18px -12px rgba(72,112,255,.7)}}
.aitem.agg:hover{{background:linear-gradient(135deg,rgba(91,140,255,.24),rgba(139,92,246,.14))}}
.aitem.agg .an{{color:#7ea6ff;letter-spacing:.2px}}
.aitem.agg .ai .ap{{color:var(--fg2)}}
.aitem.agg .ab b{{color:#5b8cff}}
:root[data-theme="light"] .aitem.agg .an{{color:#2f5fe0}}
@media (prefers-color-scheme: light){{
  :root:not([data-theme="dark"]) .aitem.agg .an{{color:#2f5fe0}}
}}
/* 底部一排：左「添加账户」占满剩余宽度，右「全部退出」按内容宽度靠右 */
.arow{{display:flex;align-items:stretch;gap:8px;margin-top:10px}}
.aadd{{flex:1 1 auto;min-width:0;padding:11px;border-radius:12px;font-family:inherit;font-size:13px;
font-weight:600;color:#fff;border:0;cursor:pointer;
background:linear-gradient(135deg,rgba(61,116,255,.94),rgba(123,92,255,.94));
box-shadow:0 8px 20px rgba(61,116,255,.28),inset 0 1px 0 rgba(255,255,255,.34);
-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);transition:.2s}}
.aadd:hover{{filter:brightness(1.06)}}
.aadd:active{{filter:brightness(.94)}}
.atip{{margin-top:8px;font-size:11.5px;color:var(--fg3);text-align:center;padding:2px 0}}
.atip:empty{{display:none}}
/* 「全部退出」与「添加账户」同一排，右侧细边框红按钮；≥2 个账户时才出现 */
.aoutall{{flex:none;padding:0 14px;border-radius:12px;font-family:inherit;font-size:12.5px;
font-weight:600;color:#e5484d;cursor:pointer;white-space:nowrap;
border:1px solid rgba(229,72,77,.42);background:rgba(229,72,77,.07);
transition:background .18s,border-color .18s,color .18s,opacity .18s}}
.aoutall:hover{{color:#ff5b60;border-color:rgba(229,72,77,.72);background:rgba(229,72,77,.15)}}
.aoutall:active{{opacity:.6}}
.aoutall[disabled]{{opacity:.5;cursor:default}}
.aoutall[hidden]{{display:none}}
.atip.err{{color:#e5484d}}
.notice{{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:0 0 14px;padding:12px 16px;
border-radius:14px;border:1px solid rgba(255,159,64,.42);background:rgba(255,159,64,.12);
font-size:13px;line-height:1.65}}
.notice.bad{{border-color:rgba(229,72,77,.45);background:rgba(229,72,77,.12)}}
.notice b{{font-weight:700;white-space:nowrap}}
.notice span{{color:var(--fg2);flex:1 1 240px}}
.notice .nbtn{{flex:none;padding:6px 14px;border-radius:9px;background:#2f6bff;color:#fff;
font-size:12px;font-weight:600;white-space:nowrap}}
.notice .nbtn:hover{{text-decoration:none;filter:brightness(1.08)}}
a{{color:var(--link);text-decoration:none}}
a:hover{{text-decoration:underline}}

/* 老浏览器无 backdrop-filter：退化为更实的底，保证可读 */
@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){{
  .hero,.card,.block,.adrop{{background:var(--bg)}}
}}

/* 过渡动效 */
/* 页面进场：轻微上浮淡入 */
@keyframes pageIn{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:none}}}}
.wrap{{animation:pageIn .28s cubic-bezier(.22,.68,.32,1) both}}
/* 离场：只淡出内容，整个 html 透明会露出 WebView 底色 */
html{{background:var(--bg);transition:opacity .18s ease}}
html.wb-leaving{{opacity:1}}
html.wb-leaving body{{opacity:0;transition:opacity .15s ease}}
/* 重载期间盖同底色 + 提示 */
html.wb-leaving::after{{content:"正在切换…";position:fixed;inset:0;z-index:2147483000;
display:grid;place-items:center;background:var(--bg);color:var(--fg2);
font-size:13px;letter-spacing:.6px;animation:leaveIn .12s ease both}}
@keyframes leaveIn{{from{{opacity:0}}to{{opacity:1}}}}
/* 切主题时临时接管所有过渡：背景、文字、边框、阴影一起渐变 */
html.wb-theming,html.wb-theming *,html.wb-theming *::before,html.wb-theming *::after{{
transition:background-color .34s ease,background-image .34s ease,color .34s ease,
border-color .34s ease,box-shadow .34s ease,opacity .34s ease,fill .34s ease !important}}
@media(prefers-reduced-motion:reduce){{
  .wrap{{animation:none}}
  html{{transition:none}}
  html.wb-theming,html.wb-theming *,html.wb-theming *::before,html.wb-theming *::after{{transition:none !important}}
}}

@media(max-width:680px){{
  .grid{{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}
  .cal{{grid-template-columns:repeat(7,1fr)}}
  .hero{{flex-direction:column;align-items:flex-start;padding:18px 16px;gap:12px}}
  .hero .right{{text-align:left;line-height:1.9;white-space:normal;font-size:11.5px}}
  .hero .num{{font-size:38px;letter-spacing:-1px}}
  body{{padding:16px 10px 30px}}
  /* 顶部工具条：状态/账户各占一行，两个按钮并排等宽，避免出现半行空位 */
  .top{{flex-direction:column;align-items:stretch;gap:10px}}
  .top h1{{font-size:17px;line-height:1.3}}
  .tools{{width:100%;display:flex;flex-wrap:wrap;gap:8px}}
  .tools .pill{{order:1;flex:1 1 100%;justify-content:center}}
  /* 账户下拉：手机上一整行，面板贴着按钮下方铺满 */
  .tools .acctwrap{{order:2;flex:1 1 100%;width:100%}}
  .tools .acct{{width:100%;max-width:100%;justify-content:center}}
  .adrop{{left:0;right:0;width:auto;max-width:none}}
  .tools .tbtn{{order:3;flex:1 1 calc(50% - 4px);padding:9px 10px;text-align:center}}
  .notice{{flex-direction:column;align-items:stretch;gap:8px;padding:12px 14px}}
  .notice b{{white-space:normal}}
  .notice .nbtn{{text-align:center;padding:9px 14px}}
  .card{{padding:14px 14px}}
  .card .v{{font-size:19px}}
  .mono{{width:auto;font-size:11px;letter-spacing:-.2px}}
  table{{font-size:12px}}
  td{{padding:7px 3px}}
  .msg{{display:inline-block;word-break:break-word}}
  .chip{{padding:4px 2px}}
}}
@media(max-width:400px){{
  .grid{{grid-template-columns:1fr 1fr}}
  .hero .num{{font-size:32px}}
  .acct{{font-size:11px}}
}}
</style></head><body>
<div class="bg"><i></i><i></i><i></i><i></i></div><div class="grain"></div>
<div class="wrap">

{renew_html}

<div class="top">
  <h1>{logo_svg(26)}WorkBuddy 每日签到</h1>
  <div class="tools">
    <button id="tbtn" class="tbtn" type="button">跟随系统</button>
    <button id="sbtn" class="tbtn" type="button">⟳ 同步</button>
    <span class="pill {pill_cls}"><i></i>{pill_txt}</span>
    <div class="acctwrap" id="acctWrap">
      <button class="acct" id="acctBtn" type="button" title="点击切换或添加账户 · 凭据指纹 {_esc(acct_fp)}{_esc(acct_exp)} · 来源 {_esc(acct_src)}"><span class="acct-in">👤 {_esc(acct_name)}{_esc(acct_sub)}</span><span class="acct-ar">▾</span></button>
      <div class="adrop" id="adrop" hidden role="dialog" aria-label="切换账户">
        <div class="alist" id="alist"></div>
        <div class="arow">
          <button class="aadd" id="aadd" type="button">＋ 添加账户</button>
          <button class="aoutall" id="lbtn" type="button" title="退出本机全部账户" hidden>全部退出</button>
        </div>
        <div class="atip" id="atip"></div>
      </div>
    </div>
  </div>
</div>

<div class="hero">
  <div>
    <div class="label">当前剩余积分</div>
    <div class="num{bal_cls}">{bal_txt}<small>分</small></div>
    <div class="sub">{bal_sub}{' · ' + _esc(s['plan']) if s.get('plan') else ''}</div>
  </div>
  <div class="right">
    今日领取 <b>{_ck + travel_today if travel_today else today_show}</b> 分{'（签到 ' + str(_ck) + ' + 旅行 ' + str(travel_today) + '）' if travel_today else ''}<br>
    最近执行 {fdt(s.get('last_run'))}<br>
    下次尝试 {fdt(s.get('next_slot'))}<br>
    自动时段 {s.get('checkin_window') or ('%02d:00–%02d:00' % CHECKIN_WINDOW)}（随机）
  </div>
</div>

<div class="grid">
  <div class="card"><div class="k">今日状态</div><div class="v state {pill_cls}">{'已领取' if done else '未领取'}</div></div>
  <div class="card"><div class="k">连续签到</div><div class="v">{streak}<small>天</small></div></div>
  <div class="card"><div class="k">下次大奖</div><div class="v" style="font-size:16px">{next_bonus}</div></div>
  <div class="card"><div class="k">令牌到期</div><div class="v" style="font-size:14px">{exp}</div></div>
  <div class="card span"><div class="k">Buddy 旅行</div>
    <div class="dexrow"><div class="v state {tv_cls}">{tv_txt}</div>
      <div style="font-size:11px;color:var(--fg3)">{tv_sub}</div></div>
    {pet_html}
  </div>
  <div class="card span"><div class="k">Buddy 图鉴</div>
    <div class="dexrow"><div class="v">{bd_txt}</div><div class="rbrow">{bd_tags}</div></div>
    <div style="font-size:11px;color:var(--fg3);margin-top:6px">{bd_sub}</div></div>
</div>

<div class="block"><h2>近 14 天领取</h2><div class="cal">{''.join(chips)}</div></div>

{pkg_block}

<div class="block"><h2>最近领取记录</h2>
<p class="pkg-note">签到与 Buddy 旅行收益都在这里，按时间倒序。</p>
<table>{''.join(rows)}</table></div>

<div class="foot">
  <span><b id="clock" class="clock">--</b> · 更新 {fdt(s.get('updated_at'))} · 余额每 {BALANCE_MINUTES} 分钟刷新 · 页面 60 秒自动刷新 · 构建 {BUILD_TAG}</span>
</div>
</div>

<script>/* 软刷新：同步/切号后只换 .wrap 内容并重新 init，不整页 reload（会白闪） */
function wbSoftReload(){{
  var cur=document.querySelector('.wrap');
  if(!cur){{ location.reload(); return; }}
  var y=window.scrollY||0;
  fetch(location.pathname+location.search,{{cache:'no-store'}})
    .then(function(r){{ return r.text(); }})
    .then(function(html){{
      var doc=new DOMParser().parseFromString(html,'text/html');
      var next=doc.querySelector('.wrap');
      if(!next){{ location.reload(); return; }}
      cur.style.transition='opacity .15s ease, transform .15s ease';
      cur.style.opacity='0'; cur.style.transform='translateY(5px)';
      setTimeout(function(){{
        cur.innerHTML=next.innerHTML;
        wbBoot();
        window.scrollTo(0,y);
        void cur.offsetWidth;
        cur.style.opacity='1'; cur.style.transform='none';
      }},150);
    }})
    .catch(function(){{ location.reload(); }});
}}
function wbBoot(){{
(function(){{
  var root=document.documentElement,btn=document.getElementById('tbtn');
  if(!btn)return;
  var order=['auto','light','dark'];
  var label={{auto:'跟随系统',light:'浅色',dark:'深色'}};
  var icon={{auto:'\u25D0',light:'\u2600',dark:'\u263E'}};
  var mem=null; /* 沙箱预览里 localStorage 可能被禁，用内存兜底 */
  function get(){{
    if(mem)return mem;
    try{{var t=localStorage.getItem('wb-theme');return order.indexOf(t)<0?'auto':t}}catch(e){{return 'auto'}}
  }}
  function paint(anim){{
    if(anim){{
      root.classList.add('wb-theming');
      clearTimeout(paint.__t);
      paint.__t=setTimeout(function(){{root.classList.remove('wb-theming')}},380);
    }}
    var t=get();
    if(t==='auto'){{root.removeAttribute('data-theme')}}else{{root.setAttribute('data-theme',t)}}
    btn.textContent=icon[t]+' '+label[t];
    btn.title='主题：'+label[t]+'（点击切换 跟随系统 → 浅色 → 深色）';
  }}
  btn.addEventListener('click',function(){{
    var t=get(),n=order[(order.indexOf(t)+1)%3];
    mem=n;
    try{{localStorage.setItem('wb-theme',n)}}catch(e){{}}
    paint(true);
  }});
  paint(false);
}})();
(function(){{
  var el=document.getElementById('clock');
  if(!el)return;
  var wk=['日','一','二','三','四','五','六'];
  function p(n){{return n<10?'0'+n:''+n}}
  function tick(){{
    var d=new Date();
    el.textContent=d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '
      +p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds())+' 周'+wk[d.getDay()];
  }}
  tick();
  /* 软刷新会重跑 boot，老计时器不清理就会越堆越多 */
  if(window.__wbClock)clearInterval(window.__wbClock);
  window.__wbClock=setInterval(tick,1000);
}})();
(function(){{
  /* 账户框定宽：内容超宽才滚动（来回滑），否则保持静止 */
  var box=document.querySelector('.acct');
  if(!box)return;
  var inner=box.querySelector('.acct-in');
  if(!inner)return;
  function fit(){{
    box.classList.remove('scroll');
    inner.style.removeProperty('--shift');
    var over=(inner.scrollWidth||0)-(inner.clientWidth||0);
    if(over>6){{
      inner.style.setProperty('--shift','-'+over+'px');
      box.classList.add('scroll');
    }}
  }}
  fit();
  if(window.__wbFit)window.removeEventListener('resize',window.__wbFit);
  window.__wbFit=fit;
  window.addEventListener('resize',fit);
}})();
(function(){{
  var b=document.getElementById('lbtn');
  if(!b)return;
  b.addEventListener('click',function(){{
    if(b.disabled)return;
    // 全部退出：清掉本机所有账户的凭据与会话，后台也不再替它们签到
    if(!window.confirm('退出全部账户？\\n退出后所有账户都会停止自动签到，需要重新扫码才能登录。'))return;
    b.disabled=true;b.textContent='退出中…';
    fetch('/api/accounts/logout_all',{{method:'POST'}})
      .then(function(){{location.href='/login'}})
      .catch(function(){{b.disabled=false;b.textContent='全部退出';}});
  }});
}})();
(function(){{
  /* 手动同步：拉实时昵称 + 余额 / 宠物，完成后原地软刷新（不整页重载） */
  var b=document.getElementById('sbtn');
  if(!b)return;
  b.addEventListener('click',function(){{
    if(b.disabled)return;
    b.disabled=true;b.textContent='同步中…';
    fetch('/api/sync',{{method:'POST'}}).then(function(r){{return r.json()}})
      .then(function(r){{
        if(r&&r.ok===false){{ b.disabled=false;b.textContent='同步失败';b.title=(r&&r.msg)||'未知原因';setTimeout(function(){{b.textContent='⟳ 同步';}},1500); return; }}
        var st=(r&&r.state)||((r&&r.claimed)?'claimed':'already');
        if(st==='claimed'){{ b.textContent='已领 +'+(r.credit||0); }}
        else if(st==='failed'){{ b.textContent='领取失败'; b.title=(r&&r.msg)||''; }}
        else {{ b.textContent='已同步 ✓'; }}
        setTimeout(wbSoftReload,160);
      }})
      .catch(function(){{
        b.disabled=false;b.textContent='⟳ 同步';
      }});
  }});
}})();
(function(){{
  /* 账户下拉：点顶部账户 → 正下方展开列表（最多 5 个，超了滚动）/ 添加账户 */
  var wrap=document.getElementById('acctWrap'), btn=document.getElementById('acctBtn'),
      panel=document.getElementById('adrop'), list=document.getElementById('alist'),
      tip=document.getElementById('atip'), add=document.getElementById('aadd'),
      all=document.getElementById('lbtn');
  if(!btn||!panel)return;
  var closeT=null;
  /* 展开：先 display，下一帧再加 .in 触发 transition */
  function open(){{
    clearTimeout(closeT);
    panel.hidden=false;
    panel.classList.remove('out','in');
    void panel.offsetWidth;
    requestAnimationFrame(function(){{ panel.classList.add('in'); }});
    btn.classList.add('open'); load();
  }}
  /* 收起：先播 .out，过渡结束后再 hidden */
  function shut(now){{
    if(panel.hidden)return;
    btn.classList.remove('open');
    panel.classList.remove('in');
    if(now){{ panel.hidden=true; panel.classList.remove('out'); return; }}
    panel.classList.add('out');
    clearTimeout(closeT);
    closeT=setTimeout(function(){{ panel.hidden=true; panel.classList.remove('out'); }},190);
  }}
  /* 页面切换先淡出，避免整屏硬切 */
  function go(url){{
    document.documentElement.classList.add('wb-leaving');
    setTimeout(function(){{ location.href=url; }},150);
  }}
  function esc(v){{
    return String(v==null?'':v).replace(/[&<>"']/g,function(c){{
      return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c];
    }});
  }}
  function setTip(t,err){{ tip.textContent=t||''; tip.className='atip'+(err?' err':''); }}
  function paint(d){{
    var arr=(d&&d.list)||[], agg=d&&d.agg, head='';
    // 只有一个账户时不显示「全部退出」：那一行右侧的「退出」就够了，
    // 两个及以上才在列表最下面给一个统一退出的入口
    if(all) all.hidden = arr.length < 2;
    if(agg&&agg.n>1){{
      var ab=(agg.balance==null)?'—':Number(agg.balance).toLocaleString('zh-CN');
      head='<button class="aitem agg" type="button" data-agg="1">'
        +'<div class="ai"><div class="an">⧉ 全部账户</div>'
        +'<div class="ap">'+agg.n+' 个账户 · 今日已领 '+agg.done+'/'+agg.n+'</div></div>'
        +'<div class="ab"><b>'+ab+'</b><i>总积分</i></div></button>';
    }}
    if(!arr.length){{
      list.innerHTML=head+'<div class="atip" style="padding:16px 0">还没有账户</div>';
      return;
    }}
    list.innerHTML=head+arr.map(function(a){{
      var bal=(a.balance==null)?'—':Number(a.balance).toLocaleString('zh-CN');
      var days=(a.days==null||a.days==='')?'':(a.days>0?('剩 '+Math.round(a.days)+' 天'):'已过期');
      return '<button class="aitem'+(a.active?' on':'')+'" type="button" data-uid="'+esc(a.uid)+'"'
        +(a.active?' data-on="1"':'')+'>'
        +'<div class="ai"><div class="an">'+esc(a.name)
        +(a.active?'<span class="ck">当前</span>':'')+'</div>'
        +'<div class="ap">'+esc(a.phone||'')+'</div></div>'
        +'<div class="ab"><b>'+bal+'</b><i>'+days+'</i></div>'
        +'<span class="aout" data-out="'+esc(a.uid)+'" role="button" title="退出这个账户">退出</span>'
        +'</button>';
    }}).join('');
    // 超过 5 个：底部渐隐提示还能往下滑
    setTimeout(function(){{
      panel.classList.toggle('more', list.scrollHeight>list.clientHeight+2);
    }},0);
  }}
  function load(){{
    setTip(''); list.innerHTML='<div class="atip" style="padding:16px 0">加载中…</div>';
    fetch('/api/accounts',{{cache:'no-store'}}).then(function(r){{return r.json()}})
      .then(paint).catch(function(){{ setTip('加载失败，稍后再试',true); }});
  }}
  btn.addEventListener('click',function(e){{
    e.stopPropagation();
    if(panel.hidden){{ open(); }}else{{ shut(); }}
  }});
  // 点面板外任意处关闭（面板在 wrap 内，点它自己不会触发）
  // 挂在 document 上：软刷新会重跑 boot，先摘掉上一轮的监听，避免一次点击触发两遍
  if(window.__wbDocClick)document.removeEventListener('click',window.__wbDocClick);
  if(window.__wbDocKey)document.removeEventListener('keydown',window.__wbDocKey);
  window.__wbDocClick=function(e){{
    if(!panel.hidden && !wrap.contains(e.target)) shut();
  }};
  window.__wbDocKey=function(e){{
    if(e.key==='Escape'&&!panel.hidden){{ shut(); btn.focus(); }}
  }};
  document.addEventListener('click',window.__wbDocClick);
  document.addEventListener('keydown',window.__wbDocKey);
  /* 退出单个账户：确认 → 删凭据 → 软刷新；退光回登录页 */
  function outOne(uid,node){{
    if(!uid||!node||node.classList.contains('busy'))return;
    var row=node.closest?node.closest('.aitem'):null;
    var nm=row?row.querySelector('.an'):null;
    var label=(nm&&nm.textContent)||'这个账户';
    if(!window.confirm('退出 '+label+'？\\n退出后这个账户不再自动签到，重新登录要再扫一次码。'))return;
    node.classList.add('busy'); node.textContent='退出中…';
    fetch('/api/accounts/logout',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{uid:uid}})}}).then(function(r){{return r.json()}})
      .then(function(r){{
        if(!r||!r.ok){{
          node.classList.remove('busy'); node.textContent='退出';
          setTip((r&&r.msg)||'退出失败',true); return;
        }}
        if(typeof r.remain==='number'&&r.remain<=0){{ location.href='/login'; return; }}
        shut(true); wbSoftReload();
      }}).catch(function(){{
        node.classList.remove('busy'); node.textContent='退出';
        setTip('退出失败，检查网络',true);
      }});
  }}
  list.addEventListener('click',function(e){{
    var it=e.target.closest?e.target.closest('.aitem'):null;
    if(!it)return;
    var ob=e.target.closest?e.target.closest('.aout'):null;
    if(ob){{ outOne(ob.getAttribute('data-out'), ob); return; }}
    if(it.getAttribute('data-agg')){{ shut(true); go('/?view=all'); return; }}
    if(it.getAttribute('data-on')){{ shut(); return; }}
    var uid=it.getAttribute('data-uid');
    setTip('正在切换…');
    fetch('/api/accounts/switch',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{uid:uid}})}}).then(function(r){{return r.json()}})
      .then(function(r){{
        if(r&&r.ok){{
          shut(true); wbSoftReload(); return;
        }}
        setTip((r&&r.msg)||'切换失败',true);
      }}).catch(function(){{ setTip('切换失败，检查网络',true); }});
  }});
  add.addEventListener('click',function(){{
    add.disabled=true; setTip('正在打开登录…');
    fetch('/api/accounts/add',{{method:'POST'}}).then(function(r){{return r.json()}})
      .then(function(){{ location.href='/login?add=1'; }})
      .catch(function(){{ add.disabled=false; setTip('打开失败，稍后再试',true); }});
  }});
}})();
{PET_JS}
}}  /* ← wbBoot 结束 */
wbBoot();
</script>
</body></html>"""


# 每个浏览器一个 wb_sid：sid → uid → state/users/<uid>.json，各自持有令牌与快照。
# 站长账户仍由全局 state/token 引擎调度，这里跳过它。
MULTIUSER = os.environ.get("MULTIUSER", "1") != "0"
USERS_DIR = STATE_DIR / "users"
SESS_FILE = STATE_DIR / "sessions.json"
LOGIN_ROOT = STATE_DIR / "login"
DB_FILE = STATE_DIR / "dashboard.db"
SID_COOKIE = "wb_sid"
SID_TTL = 90 * 86400
DEV_COOKIE = "wb_dev"      # 设备标记：这台浏览器登录过哪些账户（多账户切换用）
DEV_TTL = 90 * 86400
MAX_ACCTS_SHOW = 5         # 前端抽屉里一屏显示 5 个，超了滚动

_sess_lock = threading.Lock()
_worker_lock = threading.Lock()
_workers: dict = {}

# 账户 / 会话 / 流水 / 快照存 SQLite。
try:
    store.init(str(DB_FILE))
    _mig = store.migrate_json(USERS_DIR, SESS_FILE)
    if not _mig.get("skip"):
        log(f"SQLite 已启用：{DB_FILE}（迁移 {_mig}）")
    else:
        st0 = store.stats()
        log(f"SQLite 已就绪：{DB_FILE}（账户 {st0['accounts']} / 会话 {st0['sessions']} "
            f"/ 流水 {st0['events']} / 快照 {st0['snapshots']}）")
    # 老流水从 history.jsonl 补进 events，幂等。
    _t0, _ = read_token()
    _sub0 = (jwt_payload(_t0) or {}).get("sub") if _t0 else ""
    if _sub0:
        _n = store.backfill_events(_sub0, HISTORY_FILE)
        if _n:
            log(f"历史流水已导入 SQLite：{_n} 条（{_sub0[:8]}）")
except Exception:  # noqa: BLE001
    log(f"SQLite 初始化失败，回退不可用：{traceback.format_exc()[:400]}")
    raise


def _read_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _write_json(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    try:
        os.chmod(p, 0o600)   # 含 token，不留 644
    except OSError:
        pass


def sessions() -> dict:
    """sid → {uid, at}（兼容旧调用；真实存储在 SQLite）。"""
    return store.all_sessions()


def bind_session(sid: str, uid: str) -> None:
    store.put_session(sid, uid, SID_TTL)


def forget_session(sid: str) -> None:
    # 只断会话，账户与历史保留。
    store.del_session(sid)


def _sid_ok(s: str) -> bool:
    """sid 会拼进文件路径，必须严格限定成 32 位十六进制，防目录穿越。"""
    return len(s) == 32 and all(c in "0123456789abcdef" for c in s)


def sid_of(headers) -> str:
    raw = headers.get("Cookie") or ""
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == SID_COOKIE and v and _sid_ok(v):
            return v
    return ""


def dev_of(headers) -> str:
    """设备（浏览器）标记：多账户列表挂在它下面。"""
    raw = headers.get("Cookie") or ""
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == DEV_COOKIE and v and _sid_ok(v):
            return v
    return ""


def uid_of(sid: str) -> str:
    if not sid:
        return ""
    return store.session_uid(sid)


def new_sid() -> str:
    return secrets.token_hex(16)


def _nologin_file() -> Path:
    return STATE_DIR / ".nologin"


def mark_nologin(on: bool) -> None:
    """记下用户主动退出：有标记就不再自动接回会话，扫码成功后清掉。"""
    if not DESKTOP_MODE:
        return
    f = _nologin_file()
    try:
        if on:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(now().isoformat(timespec="seconds"), encoding="utf-8")
        elif f.exists():
            f.unlink()
    except OSError:
        log("写退出标记失败：" + traceback.format_exc()[:200])


def ensure_dev(raw: str) -> str:
    """设备标识：DEV_FILE_MODE 下固定落盘（桌面 / 安卓），否则沿用 cookie 或新发一个。"""
    if not DEV_FILE_MODE:
        return raw or secrets.token_hex(16)
    f = STATE_DIR / "desktop.dev"
    d = ""
    try:
        d = f.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if not _sid_ok(d):
        d = secrets.token_hex(16)
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(d, encoding="utf-8")
            os.chmod(f, 0o600)
        except OSError:
            pass
    adopt_local_accounts(d)
    return d


_DEV_ADOPTED = False


def adopt_local_accounts(dev: str) -> None:
    """桌面端把本机库里的账户都挂到这个 dev 下（抽屉列表按 dev 过滤）。"""
    global _DEV_ADOPTED
    if _DEV_ADOPTED:
        return
    _DEV_ADOPTED = True
    try:
        for uid in store.account_uids():
            store.put_device(dev, uid)
    except Exception:
        log("绑定本机账户失败：" + traceback.format_exc()[:200])


def desktop_resume(sid: str, dev: str) -> str:
    """桌面端用本机账户接回会话，仅在 WB_DESKTOP=1 且没主动退出时生效。"""
    if not DESKTOP_MODE or _nologin_file().exists():
        return ""
    uids = store.device_uids(dev) if dev else []
    if not uids:
        # 设备号换过时退回本机全部账户，取最近更新的那个。
        uids = store.account_uids()
    for uid in uids:
        u = user_load(uid)
        if u and u.get("token"):
            bind_session(sid, uid)
            if dev:
                try:
                    store.put_device(dev, uid)
                except Exception:  # noqa: BLE001
                    pass
            log(f"桌面端自动接回会话：{uid[:8]}")
            return uid
    return ""


# wb_keep：uid + HMAC 签名，HttpOnly。令牌剩 7 天以上直接接回会话，否则走登录页。
# 主动退出会一并清掉。
KEEP_COOKIE = "wb_keep"
KEEP_TTL = 90 * 86400
KEEP_MIN_DAYS = 7
KEEP_KEY_FILE = STATE_DIR / "keep.key"
_keep_sk = b""
_keep_lock = threading.Lock()


def _keep_secret() -> bytes:
    global _keep_sk
    if _keep_sk:
        return _keep_sk
    with _keep_lock:
        if _keep_sk:
            return _keep_sk
        try:
            b = KEEP_KEY_FILE.read_bytes().strip()
            if len(b) >= 32:
                _keep_sk = b
                return _keep_sk
        except OSError:
            pass
        b = secrets.token_hex(32).encode()
        try:
            KEEP_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            KEEP_KEY_FILE.write_bytes(b)
            os.chmod(KEEP_KEY_FILE, 0o600)
        except OSError:
            log(f"写 {KEEP_KEY_FILE} 失败（免登录恢复重启后会失效）：{traceback.format_exc()[:200]}")
        _keep_sk = b
        return _keep_sk


def keep_sign(uid: str) -> str:
    mac = hmac.new(_keep_secret(), uid.encode(), hashlib.sha256).hexdigest()[:40]
    return f"{uid}.{mac}"


def keep_uid(headers) -> str:
    """从 cookie 里取回上次登录的 uid（签名不对一律当没有）。"""
    raw = headers.get("Cookie") or ""
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k != KEEP_COOKIE or not v:
            continue
        uid, _, mac = v.partition(".")
        if not uid or not mac:
            return ""
        want = hmac.new(_keep_secret(), uid.encode(), hashlib.sha256).hexdigest()[:40]
        return uid if hmac.compare_digest(mac, want) else ""
    return ""


def token_days_left(tok: str) -> float:
    """令牌还剩多少天；解不出来返回 -1（当作不可用）。"""
    p = jwt_payload(tok) or {}
    exp = p.get("exp")
    if not isinstance(exp, (int, float)):
        return -1.0
    return (datetime.fromtimestamp(int(exp), tz=TZ) - now()).total_seconds() / 86400


def revive_from_keep(headers, sid: str):
    """wb_sid 丢了但本机还记得账户：令牌够用就直接接回会话，不用重新收短信。"""
    uid = keep_uid(headers)
    if not uid:
        return None
    u = user_load(uid)
    if not u or not u.get("token"):
        return None
    left = token_days_left(u["token"])
    if left <= KEEP_MIN_DAYS:
        log(f"免登录恢复跳过：{uid[:8]} 令牌只剩 {max(left, 0):.1f} 天，需要重新登录换新令牌")
        return None
    bind_session(sid, uid)
    log(f"用上次的凭据直接进入：{sid[:8]} → {uid[:8]}（令牌剩 {left:.1f} 天）")
    return u


def user_path(uid: str) -> Path:
    """老 JSON 路径，仅迁移期参考（现在账户都在 SQLite 里）。"""
    return USERS_DIR / f"{uid}.json"


def user_load(uid: str):
    return store.get_account(uid) if uid else None


def user_save(u: dict) -> None:
    u["updated_at"] = now().isoformat(timespec="seconds")
    store.put_account(u)


def user_new(tok: str, source: str = "扫码登录") -> dict:
    p = jwt_payload(tok)
    uid = p.get("sub") or hashlib.sha1(tok.encode()).hexdigest()[:16]
    nick = (p.get("nickname") or "").strip()
    phone = (p.get("preferred_username") or "").strip()
    fp = token_fp(tok)
    exp = token_expiry(tok)
    return {
        "uid": uid,
        "token": tok,
        "token_fp": fp,
        "token_exp": exp,
        "account": {
            "name": nick or phone or "已登录",
            "sub": phone if nick and phone != nick else None,
            "fp": fp, "expiry": exp, "source": source, "kind": "bearer",
        },
        "snap": {}, "hist": [], "created": int(time.time()),
        "today_date": "", "today_done": False, "slots_done": [],
    }


def user_hist_add(u: dict, item: dict, cap: int = 300) -> None:
    h = u.setdefault("hist", [])
    h.append(item)
    if len(h) > cap:
        del h[: len(h) - cap]
    try:
        store.add_event(u.get("uid") or "", item)   # 同时进 events 表，切账户也不丢
    except Exception:  # noqa: BLE001
        log(traceback.format_exc()[:300])


def user_refresh(u, reason: str = "") -> bool:
    """拉余额 / 活动 / 宠物，写进该用户自己的快照。"""
    tok = (u or {}).get("token") or ""
    if not tok:
        return False
    # 同步实时昵称，改名后不用重新登录。
    try:
        sync_profile(u, reason)
    except Exception:  # noqa: BLE001
        pass
    s = u.setdefault("snap", {})
    b = fetch_balance(tok)
    a = fetch_activity(tok)
    bd = fetch_buddies(tok)
    if "balance" in b:
        s.update({
            "balance": b["balance"], "balance_total": b["total"],
            "packages": b.get("packages") or [], "is_paid": b["is_paid"],
            "plan": b["plan"], "balance_at": now().isoformat(timespec="seconds"),
        })
    elif "error" in b:
        log(f"[{u['uid'][:8]}] 余额查询失败：{b['error']}")
    if "active" in a:
        s["daily_credit"] = a["daily_credit"]
        s["next_streak_day"] = a["next_streak_day"]
        s["streak_bonus_credit"] = a["streak_bonus_credit"]
        if a["streak_days"]:
            s["streak_days"] = a["streak_days"]
        if a["checkin_dates"]:
            s["checkin_dates"] = a["checkin_dates"]
        if a["today_checked_in"]:
            s["today_done"] = True
            s["today_credit"] = a["today_credit"] or a["daily_credit"]
            u["today_done"] = True
            u["today_date"] = now().strftime("%Y-%m-%d")
            # 其他端签到后补一条被动流水。
            passive_checkin_event(u, s["today_credit"])
        else:
            # today_done 以服务端 today_checked_in 为唯一真值，双向写。
            if s.get("today_done") or s.get("today_credit"):
                s["today_done"] = False
                s["today_credit"] = 0
            if u.get("today_date") != now().strftime("%Y-%m-%d"):
                u["today_done"] = False
    elif "error" in a:
        log(f"[{u['uid'][:8]}] 活动查询失败：{a['error']}")
    if "buddies" in bd:
        s["buddies"] = bd["buddies"]
        s["buddy_count"] = bd["count"]
        s["buddy_at"] = now().isoformat(timespec="seconds")
        # 访客账户同样要补图集。
        ensure_pets_missing(bd["buddies"], tag=f"[{u['uid'][:8]}] ")
    elif "error" in bd:
        log(f"[{u['uid'][:8]}] Buddy 查询失败：{bd['error']}")
    s["token_fp"] = u.get("token_fp")
    s["token_exp"] = u.get("token_exp")
    s["account"] = u.get("account") or {}
    s["updated_at"] = now().isoformat(timespec="seconds")
    try:
        if s.get("balance") is not None:
            store.add_snapshot(u["uid"], s)   # 积分曲线用，库内每账户留最近 2000 条
    except Exception:  # noqa: BLE001
        log(traceback.format_exc()[:300])
    user_save(u)
    return True


def user_checkin(u, reason: str = "定时") -> bool:
    """给单个用户签到（不碰全局 state）。"""
    tok = u.get("token") or ""
    if not tok:
        return False
    a = fetch_activity(tok)
    if a.get("auth"):
        log(f"  [{u['uid'][:8]}] 令牌失效，跳过签到")
        return False
    if a.get("today_checked_in"):
        u["today_done"] = True
        u["today_date"] = now().strftime("%Y-%m-%d")
        user_refresh(u, "已签后刷新")
        return True
    code, data, raw = _retry(lambda: api_post("/billing/meter/daily-checkin", tok))
    d = data if isinstance(data, dict) else {}
    ok = code == 200 and d.get("code") == 0
    credit = _num((d.get("data") or {}).get("credit") or (d.get("data") or {}).get("today_credit"))
    u["last_run"] = now().isoformat(timespec="seconds")
    if ok:
        u["today_done"] = True
        u["today_date"] = now().strftime("%Y-%m-%d")
        user_hist_add(u, {"ts": now().isoformat(timespec="seconds"), "reason": reason,
                          "ok": True, "kind": "success", "credit": int(credit or 0),
                          "msg": f"签到成功{('，领取 %d 积分' % credit) if credit else ''}"})
        log(f"  [{u['uid'][:8]}] 签到成功{' +%d' % credit if credit else ''}（{reason}）")
    else:
        msg = str(d.get("msg") or f"HTTP {code} {raw[:100]}")
        user_hist_add(u, {"ts": now().isoformat(timespec="seconds"), "reason": reason,
                          "ok": False, "kind": "fail", "msg": msg})
        log(f"  [{u['uid'][:8]}] 签到失败：{msg}")
    user_refresh(u, "签到后刷新")
    return ok


def _claim_credit(u: dict) -> int:
    """这次刚领到多少分：取今天最新一条 success 流水的 credit。"""
    today = now().strftime("%Y-%m-%d")
    for h in reversed(u.get("hist") or []):
        h = h or {}
        ts = str(h.get("ts") or "")
        if ts and ts[:10] != today:
            break        # 已翻到昨天，昨天的成功不算今天
        if h.get("kind") == "success" and h.get("ok") is not False:
            n = _num(h.get("credit"))
            if n:
                return int(n)
    return int((u.get("snap") or {}).get("today_credit") or 0)


def checkin_due(u: dict, t: datetime = None, key: str = "") -> bool:
    """这个号今天该不该自动签到：按当天随机时刻表，错过则在白天窗口内补一次。"""
    t = t or now()
    day = t.strftime("%Y-%m-%d")
    if u.get("today_date") != day:
        u["today_date"] = day
        u["today_done"] = False
        u["slots_done"] = []
        u.pop("catchup", None)
        u.pop("catchup_tries", None)
    plan = checkin_plan(day, key)
    done = {str(x) for x in (u.get("slots_done") or [])}
    passed = [i for i, at in enumerate(plan) if t >= at and str(i) not in done]
    if passed:
        if _in_window(CHECKIN_WINDOW, t):
            u["slots_done"] = sorted(done | {str(i) for i in passed})
            return True
        # 出窗口的点直接标记，交给白天补签。
        u["slots_done"] = sorted(done | {str(i) for i in passed})
        done = set(u["slots_done"])
    if t >= plan[-1] + timedelta(minutes=30) and _in_window(TRAVEL_DAY_WINDOW, t) \
            and u.get("catchup") != day and int(u.get("catchup_tries") or 0) < 3:
        # 补签失败最多重试 3 次，由 last_tick 的 600 秒节流兜着。
        u["catchup"] = day
        u["catchup_tries"] = int(u.get("catchup_tries") or 0) + 1
        return True
    return False


def user_travel(u, reason: str = "") -> None:
    """单个用户的旅行巡检：到站领奖 / 空闲派遣，只在白天窗口内。"""
    if not TRAVEL_ENABLE:
        return
    tok = u.get("token") or ""
    if not tok:
        return
    uid = str(u.get("uid") or "")
    if not _in_window(TRAVEL_DAY_WINDOW):
        return          # 夜间不领奖、不派遣，也不查状态
    code, data, raw = _retry(lambda: api_get("/activity/growth/buddy/travel/status", tok))
    if code != 200 or not isinstance(data, dict) or data.get("code") != 0:
        return
    d = data.get("data") or {}
    s = u.setdefault("snap", {})
    st = d.get("state") or "unknown"
    loc = (d.get("location") or {}).get("name")
    s["travel_state"] = st
    s["travel_daily_limit"] = bool(d.get("daily_limit_reached"))
    if loc:
        s["travel_location"] = loc
    if d.get("arrive_at"):
        s["travel_arrive_at"] = datetime.fromtimestamp(int(d["arrive_at"]), tz=TZ).isoformat(timespec="seconds")
    if st == "arrived":
        if not travel_may_act("claim", key=uid):
            s["travel_action"] = "已到家，等白天时段再收"
            user_save(u)
            return
        rid = d.get("record_id")
        body = {"record_id": int(rid)} if rid else {}
        c2, d2, _ = _retry(lambda: api_post("/activity/growth/buddy/travel/claim", tok, body))
        got = _num(((d2 or {}).get("data") or {}).get("reward_credit")) if isinstance(d2, dict) else 0
        if c2 == 200 and isinstance(d2, dict) and d2.get("code") == 0:
            s["travel_state"] = "idle"
            s["travel_reward"] = got
            s["travel_action"] = f"领取 +{got} 分"
            u["travel_next_depart"] = time.time() + travel_pace_seconds()
            user_hist_add(u, {"ts": now().isoformat(timespec="seconds"), "reason": reason or "旅行",
                              "ok": True, "kind": "travel", "credit": int(got or 0),
                              "msg": f"旅行归来（{loc or '咖啡馆'}），领取 {int(got or 0)} 积分"})
            log(f"  [{u['uid'][:8]}] 旅行领取 +{got} 分")
    elif st == "idle" and not s.get("travel_daily_limit"):
        if not travel_may_act("depart", key=uid):
            s["travel_action"] = "等待 %02d:00–%02d:00 时段派猫" % TRAVEL_WINDOW
            user_save(u)
            return
        if time.time() < float(u.get("travel_next_depart") or 0):
            s["travel_action"] = "空闲，随机歇一会儿再派"
            user_save(u)
            return
        if not _in_window(TRAVEL_DAY_WINDOW):
            s["travel_action"] = "夜里不出门（%02d:00–%02d:00 才活动）" % TRAVEL_DAY_WINDOW
            user_save(u)
            return
        c3, d3, _ = _retry(lambda: api_post("/activity/growth/buddy/travel/depart", tok,
                                            {"location_id": TRAVEL_LOCATION_ID}))
        if c3 == 200 and isinstance(d3, dict) and d3.get("code") == 0:
            dd = d3.get("data") or {}
            s["travel_state"] = "traveling"
            s["travel_action"] = "已派遣"
            u["travel_next_depart"] = time.time() + travel_pace_seconds()
            if dd.get("arrive_at"):
                s["travel_arrive_at"] = datetime.fromtimestamp(
                    int(dd["arrive_at"]), tz=TZ).isoformat(timespec="seconds")
            if (dd.get("location") or {}).get("name"):
                s["travel_location"] = dd["location"]["name"]
    user_save(u)


def _owner_sub() -> str:
    tok, _ = read_token()
    return (jwt_payload(tok) or {}).get("sub") if tok else ""


def user_snapshot(u: dict) -> dict:
    """拼出 render_html 需要的状态字典（字段与全局 _state 对齐）。"""
    s = dict(u.get("snap") or {})
    if _owner_sub() and u.get("uid") == _owner_sub():
        for k in ("travel_state", "travel_location", "travel_reward", "travel_arrive_at",
                  "travel_action", "travel_daily_limit",
                  "streak_days", "daily_credit", "next_streak_day", "streak_bonus_credit",
                  "checkin_dates", "last_run"):
            v = _state.get(k)
            if v not in (None, "", []):
                s[k] = v
        # today_done 只认今天的全局状态，否则旧值会覆盖进来。
        if _state.get("today_date") == now().strftime("%Y-%m-%d"):
            s["today_done"] = bool(_state.get("today_done"))
            s["today_credit"] = _state.get("today_credit")
    s.setdefault("plan", "")
    s.setdefault("packages", [])
    s.setdefault("buddies", [])
    s.setdefault("checkin_dates", [])
    s.setdefault("token_fp", u.get("token_fp"))
    s.setdefault("token_exp", u.get("token_exp"))
    s.setdefault("account", u.get("account") or {})
    s.setdefault("today_done", bool(u.get("today_done")))
    s.setdefault("travel_state", "idle")
    s.setdefault("next_slot", next_slot(str(u.get("uid") or "")))
    s.setdefault("last_run", u.get("last_run"))
    s.setdefault("updated_at", u.get("updated_at"))
    return s


def _acct_brief(u: dict, cur: str) -> dict:
    """单个账户的摘要（聚合视图与下拉共用）。"""
    acct = u.get("account") or {}
    snap = u.get("snap") or {}
    today = now().strftime("%Y-%m-%d")
    try:
        ev = store.list_events(u["uid"], 40)
    except Exception:  # noqa: BLE001
        ev = []
    if not ev:
        ev = list(reversed((u.get("hist") or [])[-40:]))
    day_in = sum(int(h.get("credit") or 0) for h in ev
                 if h.get("ok") and str(h.get("ts"))[:10] == today)
    return {
        "uid": u["uid"],
        "name": " ".join(str(acct.get("name") or "").split()) or "已登录",
        "phone": mask_phone(acct.get("sub") or ""),
        "balance": snap.get("balance"),
        "today_done": bool(u.get("today_done")) and str(u.get("today_date") or "") == today,
        "today_in": day_in,
        "streak": snap.get("streak_days") or u.get("streak_days") or 0,
        "pets": len(snap.get("buddies") or []),
        "travel": snap.get("travel_state") or "",
        "travel_reward": snap.get("travel_reward"),
        "days": round(token_days_left(u.get("token") or ""), 1),
        "updated_at": snap.get("updated_at") or "",
        "active": u["uid"] == cur,
        "events": ev[:8],
    }


def agg_uids(dev: str, sid: str) -> list:
    """这台设备见过的账户（当前会话那个永远在第一位）。"""
    cur = uid_of(sid)
    uids = store.device_uids(dev)
    if cur and cur not in uids:
        uids.insert(0, cur)
    return uids[:30]


def build_agg(dev: str, sid: str) -> dict:
    """聚合账户：把本机登录过的全部账户合成一份快照（总积分 + 各自的今日状态）。"""
    today = now().strftime("%Y-%m-%d")
    _ = today
    items, feed = [], []
    for uid in agg_uids(dev, sid):
        u = user_load(uid)
        if not u:
            continue
        it = _acct_brief(u, uid_of(sid))
        for h in it.pop("events", []):
            feed.append({"name": it["name"], "uid": it["uid"], **h})
        items.append(it)
    feed.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    bals = [i["balance"] for i in items if i["balance"] is not None]
    days = [i["days"] for i in items if i["days"] is not None]
    return {
        "n": len(items),
        "balance_total": sum(int(b) for b in bals) if bals else None,
        "balance_missing": len(items) - len(bals),
        "done_n": sum(1 for i in items if i["today_done"]),
        "today_in": sum(i["today_in"] for i in items),
        "pets": sum(i["pets"] for i in items),
        "streak_max": max([int(i["streak"] or 0) for i in items] or [0]),
        "days_min": min(days) if days else None,
        "accounts": items,
        "feed": feed[:15],
        "current": uid_of(sid),
        "updated_at": now().isoformat(timespec="seconds"),
    }


def accounts_list(dev: str, sid: str) -> dict:
    """这台设备登录过的账户列表（抽屉里用）。当前会话那个永远排第一。"""
    cur = uid_of(sid)
    uids = store.device_uids(dev)
    if cur and cur not in uids:
        uids.insert(0, cur)
    out = []
    for uid in uids[:30]:
        u = user_load(uid)
        if not u:
            continue
        acct = u.get("account") or {}
        snap = u.get("snap") or {}
        out.append({
            "uid": uid,
            "name": " ".join(str(acct.get("name") or "").split()) or "已登录",
            "phone": mask_phone(acct.get("sub") or ""),
            "balance": snap.get("balance"),
            "done": bool(u.get("today_done"))
                    and str(u.get("today_date") or "") == now().strftime("%Y-%m-%d"),
            "days": round(token_days_left(u.get("token") or ""), 1),
            "active": uid == cur,
        })
    # 当前账户排最前，其余按最近使用
    out.sort(key=lambda x: (not x["active"],))
    bals = [x["balance"] for x in out if x["balance"] is not None]
    return {"current": cur, "list": out, "max_show": MAX_ACCTS_SHOW,
            "agg": {"n": len(out),
                    "balance": sum(int(b) for b in bals) if bals else None,
                    "done": sum(1 for x in out if x["done"])}}


def logout_account(uid: str) -> dict:
    """退出一个账户：删凭据、断会话、解除设备关联，流水保留。"""
    uid = str(uid or "").strip()
    if not uid or len(uid) > 128 or "/" in uid or "\\" in uid:
        return {"ok": False, "msg": "账户标识无效"}
    owner = False
    try:
        tok, _src = read_token()
        if tok and (jwt_payload(tok) or {}).get("sub") == uid:
            owner = True
            if TOKEN_FILE.exists():
                bak = TOKEN_FILE.with_name(f"token.bak-{now().strftime('%Y%m%d-%H%M%S')}")
                TOKEN_FILE.replace(bak)
                log(f"退出主账户 {uid[:8]}：令牌已归档为 {bak.name}")
    except Exception:  # noqa: BLE001
        log("归档主账户令牌失败：" + traceback.format_exc()[:300])
    store.del_account(uid)
    log(f"退出账户：{uid[:8]}{'（主账户）' if owner else ''}")
    return {"ok": True, "uid": uid, "owner": owner}


AGG_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
/* 移动端点击高亮块关掉，聚焦态用跟随圆角的 outline */
*{-webkit-tap-highlight-color:transparent}
button,a,.row,.tbtn{-webkit-tap-highlight-color:transparent;-webkit-touch-callout:none}
:focus{outline:none}
:focus-visible{outline:2px solid rgba(91,140,255,.62);outline-offset:2px}
html,body{min-height:100%}
:root{
  color-scheme:dark;
  --bg:#05070e;
  --fg:rgba(255,255,255,.94);
  --fg2:rgba(255,255,255,.60);
  --fg3:rgba(255,255,255,.42);
  --panel-a:rgba(255,255,255,.14);
  --panel-b:rgba(255,255,255,.05);
  --gloss:rgba(255,255,255,.14);
  --inner:rgba(2,6,23,.30);
  --hero1:rgba(255,255,255,.155);
  --hero2:rgba(255,255,255,.052);
  --edge:rgba(255,255,255,.24);
  --shadow:0 18px 44px rgba(0,0,0,.46);
  --ok:#39d98a;--warn:#ffb020;--bad:#ff5c6c;
  --link:#93c5fd;
  --blob1:#6366f1;--blob2:#06b6d4;--blob3:#8b5cf6;--blob4:#3b82f6;
  --blob-op:.55;--blob-blur:78px;--num-glow:0 0 34px rgba(120,160,255,.35);
  --logo-bg:#121a2e;--logo-w:#f2c76b;--logo-coin:#f2c76b;
  --logo-hole:rgba(122,74,12,.42);--logo-edge:transparent;
}
@media (prefers-color-scheme: light){
  :root:not([data-theme="dark"]){
    color-scheme:light;
    --bg:#e7ecf7;--fg:#0b1220;--fg2:rgba(11,18,32,.62);--fg3:rgba(11,18,32,.45);
    --panel-a:rgba(255,255,255,.56);--panel-b:rgba(255,255,255,.26);
    --gloss:rgba(255,255,255,.92);--inner:rgba(15,23,42,.06);
    --hero1:rgba(255,255,255,.68);--hero2:rgba(255,255,255,.34);
    --edge:rgba(255,255,255,.92);--shadow:0 18px 44px rgba(15,23,42,.13);
    --ok:#059669;--warn:#b45309;--bad:#dc2626;--link:#2563eb;
    --blob-op:.30;--num-glow:none;
    --logo-bg:#ffffff;--logo-w:#14203a;--logo-coin:#dfa14a;
    --logo-hole:rgba(122,74,12,.5);--logo-edge:rgba(11,18,32,.10);
  }
}
:root[data-theme="light"]{
  color-scheme:light;
  --bg:#e7ecf7;--fg:#0b1220;--fg2:rgba(11,18,32,.62);--fg3:rgba(11,18,32,.45);
  --panel-a:rgba(255,255,255,.56);--panel-b:rgba(255,255,255,.26);
  --gloss:rgba(255,255,255,.92);--inner:rgba(15,23,42,.06);
  --hero1:rgba(255,255,255,.68);--hero2:rgba(255,255,255,.34);
  --edge:rgba(255,255,255,.92);--shadow:0 18px 44px rgba(15,23,42,.13);
  --ok:#059669;--warn:#b45309;--bad:#dc2626;--link:#2563eb;
  --blob-op:.30;--num-glow:none;
  --logo-bg:#ffffff;--logo-w:#14203a;--logo-coin:#dfa14a;
  --logo-hole:rgba(122,74,12,.5);--logo-edge:rgba(11,18,32,.10);
}
body{
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  color:var(--fg);background:var(--bg);padding:22px 14px 48px;
  -webkit-font-smoothing:antialiased;
}
.bg{position:fixed;inset:0;overflow:hidden;pointer-events:none;z-index:0}
.bg i{position:absolute;border-radius:50%;filter:blur(var(--blob-blur));opacity:var(--blob-op)}
.bg i:nth-child(1){width:44vw;height:44vw;left:-12vw;top:-14vw;background:var(--blob1)}
.bg i:nth-child(2){width:36vw;height:36vw;right:-10vw;top:2vh;background:var(--blob2)}
.bg i:nth-child(3){width:40vw;height:40vw;left:18vw;bottom:-16vw;background:var(--blob3)}
.bg i:nth-child(4){width:30vw;height:30vw;right:6vw;bottom:-6vw;background:var(--blob4)}
.wrap{position:relative;z-index:1;max-width:900px;margin:0 auto}
.top{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:16px}
.wbm{flex:none;border-radius:6px}
h1{font-size:17px;font-weight:700;letter-spacing:.2px;display:flex;align-items:center;gap:8px}
h1 .dotx{width:8px;height:8px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 4px rgba(57,217,138,.16)}
.tools{display:flex;gap:8px;align-items:center}
.tbtn{padding:8px 13px;border-radius:11px;border:1px solid var(--edge);background:var(--panel-a);
  color:var(--fg);font:inherit;font-size:12.5px;cursor:pointer;backdrop-filter:blur(16px);text-decoration:none;
  transition:background .2s ease,border-color .2s ease,transform .16s ease}
.tbtn:hover{border-color:rgba(120,160,255,.5);filter:brightness(1.08)}
.tbtn:active{filter:brightness(.94)}
.pill{display:inline-flex;align-items:center;gap:6px;padding:6px 11px;border-radius:999px;
  font-size:12px;color:var(--fg2);border:1px solid var(--edge);background:var(--panel-b)}
.pill i{width:6px;height:6px;border-radius:50%;background:var(--ok)}
.hero{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;flex-wrap:wrap;
  padding:22px 22px 18px;border-radius:22px;margin-bottom:14px;
  background:radial-gradient(125% 100% at 8% -14%,var(--gloss),transparent 58%),
    linear-gradient(160deg,var(--hero1),var(--hero2));
  border:1px solid var(--edge);backdrop-filter:blur(26px) saturate(160%) brightness(1.03);
  -webkit-backdrop-filter:blur(26px) saturate(160%) brightness(1.03);
  box-shadow:var(--shadow),inset 0 1px 0 var(--edge),inset 0 -22px 36px -28px var(--inner)}
.k{font-size:12px;color:var(--fg2);letter-spacing:.5px}
.num{font-size:48px;font-weight:800;letter-spacing:-1.6px;line-height:1.06;margin-top:4px;
  font-variant-numeric:tabular-nums;text-shadow:var(--num-glow)}
.num small{font-size:16px;font-weight:600;letter-spacing:0;color:var(--fg2);margin-left:6px}
.sub{font-size:12.5px;color:var(--fg2);margin-top:8px}
.sub b{color:var(--ok);font-weight:700}
.side{text-align:right;font-size:12.5px;color:var(--fg2);line-height:2}
.side b{color:var(--fg);font-weight:600}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:14px}
.card{padding:14px 14px 12px;border-radius:16px;
  background:radial-gradient(130% 110% at 6% -14%,var(--gloss),transparent 60%),
    linear-gradient(160deg,var(--panel-a),var(--panel-b));
  border:1px solid var(--edge);backdrop-filter:blur(18px) saturate(150%);-webkit-backdrop-filter:blur(18px) saturate(150%);
  box-shadow:inset 0 1px 0 var(--edge),0 10px 26px rgba(2,6,23,.22)}
.card .v{font-size:22px;font-weight:750;font-variant-numeric:tabular-nums;margin-top:2px}
.card .s{font-size:11px;color:var(--fg3);margin-top:2px}
.block{border-radius:20px;padding:6px 6px 8px;margin-bottom:14px;
  background:radial-gradient(130% 100% at 6% -12%,var(--gloss),transparent 58%),
    linear-gradient(160deg,var(--panel-a),var(--panel-b));
  border:1px solid var(--edge);backdrop-filter:blur(20px) saturate(150%);-webkit-backdrop-filter:blur(20px) saturate(150%);
  box-shadow:inset 0 1px 0 var(--edge),inset 0 -22px 36px -28px var(--inner)}
.block h2{font-size:12.5px;font-weight:600;color:var(--fg2);letter-spacing:.6px;padding:12px 14px 8px}
.row{display:flex;align-items:center;gap:12px;width:100%;padding:11px 12px;border-radius:14px;
  border:1px solid transparent;background:transparent;color:inherit;font:inherit;text-align:left;cursor:pointer;
  transition:background .2s ease,border-color .2s ease,transform .16s ease}
.row+.row{margin-top:2px}
.row:hover{background:var(--panel-b);border-color:var(--edge)}
.row.busy{opacity:.55;pointer-events:none}
.av{flex:none;width:38px;height:38px;border-radius:12px;display:grid;place-items:center;font-weight:700;
  font-size:15px;color:#fff;background:linear-gradient(145deg,#5b8cff,#8b5cf6);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.4)}
.mi{flex:1 1 auto;min-width:0}
.mi b{display:block;font-size:14px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mi i{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
  font-style:normal;font-size:11.5px;color:var(--fg3);margin-top:2px;line-height:1.5}
/* 合并流水：手机上一行放不下 4 列，改成「账户 · 时间 » 事件 » 积分」的两行卡片 */
.feed{padding:0 8px 8px}
.frow{padding:9px 6px;border-top:1px solid var(--edge)}
.frow:first-child{border-top:0}
.fh{display:flex;align-items:baseline;gap:8px;font-size:11px;color:var(--fg3)}
.fh b{color:var(--fg2);font-weight:600;font-size:11.5px;max-width:46%;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.fh em{margin-left:auto;font-style:normal;font-variant-numeric:tabular-nums;color:var(--ok);font-weight:650}
.fh em.bad{color:var(--bad)}
.fm{font-size:12.5px;margin-top:2px;word-break:break-word}
.rt{flex:none;text-align:right}
.rt b{display:block;font-size:15px;font-variant-numeric:tabular-nums}
.rt i{display:block;font-style:normal;font-size:11px;margin-top:3px;color:var(--fg3)}
.rt i.ok{color:var(--ok)}
.rt i.warn{color:var(--warn)}
.badge{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:999px;font-size:10.5px;
  border:1px solid var(--edge);color:var(--fg2)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{font-size:11px;color:var(--fg3);font-weight:500;text-align:left;padding:4px 12px}
td{padding:8px 12px;border-top:1px solid var(--edge);vertical-align:top}
td.who{white-space:nowrap;color:var(--fg2)}
td.r{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.msg{color:var(--fg);word-break:break-word}
.tip{padding:0 14px;font-size:12px;color:var(--bad);min-height:0}
.tip:empty{display:none}
.empty{padding:26px 14px;text-align:center;color:var(--fg3);font-size:12.5px}
/* 过渡动效 */
@keyframes pageIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.wrap{animation:pageIn .28s cubic-bezier(.22,.68,.32,1) both}
/* 只淡出内容，整个 html 透明会露出 WebView 底色 */
html{background:var(--bg);transition:opacity .18s ease}
html.wb-leaving{opacity:1}
html.wb-leaving body{opacity:0;transition:opacity .15s ease}
html.wb-leaving::after{content:"正在切换…";position:fixed;inset:0;z-index:2147483000;
  display:grid;place-items:center;background:var(--bg);color:var(--fg2);
  font-size:13px;letter-spacing:.6px;animation:leaveIn .12s ease both}
@keyframes leaveIn{from{opacity:0}to{opacity:1}}
/* 切主题时统一接管过渡，背景/文字/边框/阴影一起渐变 */
html.wb-theming,html.wb-theming *,html.wb-theming *::before,html.wb-theming *::after{
  transition:background-color .34s ease,background-image .34s ease,color .34s ease,
  border-color .34s ease,box-shadow .34s ease,opacity .34s ease,fill .34s ease !important}
@media(prefers-reduced-motion:reduce){
  .wrap{animation:none}
  html{transition:none}
  html.wb-theming,html.wb-theming *,html.wb-theming *::before,html.wb-theming *::after{transition:none !important}
}
@media(max-width:680px){
  body{padding:16px 10px 36px}
  .hero{padding:18px 16px 16px;border-radius:18px}
  .num{font-size:40px}
  .side{text-align:left;line-height:1.95}
  .grid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .top{flex-wrap:wrap}
  /* 三个控件在手机上排不开：账户数独占一行，两个按钮各占一半 */
  .tools{width:100%;flex-wrap:wrap}
  .pill{flex:1 1 100%;justify-content:center;order:1}
  .tbtn{flex:1 1 0;text-align:center;order:2}
  .mi b{font-size:13.5px}
  td,th{padding:7px 8px}
  td.who{white-space:normal;max-width:76px}
}
"""

AGG_JS = """
/* 软刷新：同步后只换 .wrap 内容并重新 init，不做整页 reload（避免白闪/黑屏一下） */
function aggSoftReload(){
  var cur=document.querySelector('.wrap');
  if(!cur){ location.reload(); return; }
  var y=window.scrollY||0;
  fetch(location.pathname+location.search,{cache:'no-store'})
    .then(function(r){ return r.text(); })
    .then(function(html){
      var doc=new DOMParser().parseFromString(html,'text/html');
      var next=doc.querySelector('.wrap');
      if(!next){ location.reload(); return; }
      cur.style.transition='opacity .15s ease, transform .15s ease';
      cur.style.opacity='0'; cur.style.transform='translateY(5px)';
      setTimeout(function(){
        cur.innerHTML=next.innerHTML;
        aggBoot();
        window.scrollTo(0,y);
        void cur.offsetWidth;
        cur.style.opacity='1'; cur.style.transform='none';
      },150);
    })
    .catch(function(){ location.reload(); });
}
function aggBoot(){
(function(){
  /* 主题：跟随系统 → 浅色 → 深色，和单号看板共用同一个 localStorage key */
  var root=document.documentElement, tbtn=document.getElementById('tbtn');
  if(tbtn){
    var order=['auto','light','dark'];
    var label={auto:'跟随系统',light:'浅色',dark:'深色'};
    var icon={auto:'\u25D0',light:'\u2600',dark:'\u263E'};
    var mem=null, animT=null;
    function get(){
      if(mem) return mem;
      try{var t=localStorage.getItem('wb-theme'); return order.indexOf(t)<0?'auto':t}catch(e){return 'auto'}
    }
    function paint(anim){
      if(anim){
        root.classList.add('wb-theming');
        clearTimeout(animT);
        animT=setTimeout(function(){root.classList.remove('wb-theming')},380);
      }
      var t=get();
      if(t==='auto'){root.removeAttribute('data-theme')}else{root.setAttribute('data-theme',t)}
      tbtn.textContent=icon[t]+' '+label[t];
      tbtn.title='主题：'+label[t]+'（点击切换 跟随系统 → 浅色 → 深色）';
    }
    tbtn.addEventListener('click',function(){
      var t=get(), n=order[(order.indexOf(t)+1)%3];
      mem=n;
      try{localStorage.setItem('wb-theme',n)}catch(e){}
      paint(true);
    });
    paint(false);
  }

  /* 页面跳转先淡出再走，避免整屏硬切 */
  function go(url){
    root.classList.add('wb-leaving');
    setTimeout(function(){ location.href=url; },150);
  }
  var single=document.getElementById('tosingle');
  if(single){
    single.addEventListener('click',function(e){
      e.preventDefault(); go(single.getAttribute('href')||'/');
    });
  }

  /* 手动同步：一次刷本机全部账户的实时昵称 + 余额 / 宠物 */
  var sbtn=document.getElementById('sbtn');
  if(sbtn){
    sbtn.addEventListener('click',function(){
      if(sbtn.disabled)return;
      sbtn.disabled=true; sbtn.textContent='同步中…';
      fetch('/api/sync',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({agg:true})}).then(function(r){return r.json()})
        .then(function(r){
          if(r&&r.ok===false){ sbtn.disabled=false; sbtn.textContent='同步失败';
            sbtn.title=(r&&r.msg)||'未知原因';
            var tp=document.getElementById('tip'); if(tp) tp.textContent=(r&&r.msg)||'同步失败';
            setTimeout(function(){ sbtn.textContent='⟳ 同步'; },1500); return; }
          if(r.claimed){ sbtn.textContent='已领 '+r.claimed+' 个号'; }
          else if(r.failed){ sbtn.textContent='有 '+r.failed+' 个号失败'; sbtn.title=(r&&r.msg)||''; }
          else { sbtn.textContent='已同步 ✓'; }
          setTimeout(aggSoftReload,160);
        })
        .catch(function(){ sbtn.disabled=false; sbtn.textContent='⟳ 同步'; });
    });
  }

  var tip=document.getElementById('tip');
  function say(t){ if(!tip)return; tip.textContent=t||''; }
  document.querySelectorAll('.row[data-uid]').forEach(function(b){
    b.addEventListener('click',function(){
      var uid=b.getAttribute('data-uid');
      if(!uid)return;
      b.classList.add('busy'); say('');
      fetch('/api/accounts/switch',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({uid:uid})}).then(function(r){return r.json()})
        .then(function(r){
          if(r&&r.ok){ go('/'); return; }
          b.classList.remove('busy'); say((r&&r.msg)||'切换失败');
        }).catch(function(){ b.classList.remove('busy'); say('切换失败，检查网络'); });
    });
  });
  // 每 60s 静默重载（meta refresh 已兜底，这里给「切回前台」用）
  // 软刷新会重跑 aggBoot，先摘掉上一轮的监听
  if(window.__wbVis) document.removeEventListener('visibilitychange',window.__wbVis);
  window.__wbVis=function(){
    if(document.visibilityState==='visible'){
      var t=Date.now();
      if(t-(window.__wbLoaded||t)>60000) location.reload();
    }
  };
  document.addEventListener('visibilitychange',window.__wbVis);
  window.__wbLoaded=Date.now();
})();
}
aggBoot();
"""


def render_agg_html(agg: dict) -> str:
    """聚合看板：总积分 + 各账户今日状态 + 合并流水。"""
    n = agg.get("n") or 0
    tot = agg.get("balance_total")
    tot_txt = "—" if tot is None else f"{int(tot):,}"
    done_n = agg.get("done_n") or 0
    today_in = agg.get("today_in") or 0
    pets = agg.get("pets") or 0
    streak = agg.get("streak_max") or 0
    dmin = agg.get("days_min")
    dmin_txt = "—" if dmin is None else (f"{int(dmin)} 天" if dmin > 0 else "已过期")
    upd = str(agg.get("updated_at") or "")[11:16] or "—"

    top3 = sorted(agg.get("accounts") or [],
                  key=lambda x: -(int(x.get("balance") or 0)))[:3]

    rows = []
    for a in agg.get("accounts") or []:
        bal = a.get("balance")
        bal_txt = "—" if bal is None else f"{int(bal):,}"
        if bal is None:
            bal_cls = "warn"
            day_txt = "等待首次查询"
        elif a.get("today_done"):
            bal_cls = "ok"
            day_txt = f"今日已领 +{a.get('today_in') or 0}"
        else:
            bal_cls = "warn"
            day_txt = f"今日未领" + (f" · 已入账 +{a['today_in']}" if a.get("today_in") else "")
        meta = [a.get("phone") or ""]
        if a.get("streak"):
            meta.append(f"连续 {a['streak']} 天")
        if a.get("pets"):
            meta.append(f"宠物 {a['pets']} 只")
        if a.get("days") is not None:
            meta.append("令牌剩 %d 天" % round(a["days"]) if a["days"] > 0 else "令牌已过期")
        name = a.get("name") or "已登录"
        rows.append(
            '<button class="row" type="button" data-uid="{uid}" title="切到这个账户">'
            '<span class="av">{ini}</span>'
            '<span class="mi"><b>{name}{cur}</b><i>{meta}</i></span>'
            '<span class="rt"><b>{bal}</b><i class="{cls}">{day}</i></span></button>'.format(
                uid=_esc(a.get("uid") or ""),
                ini=_esc(name[:1] or "?"),
                name=_esc(name),
                cur=' <span class="badge">当前</span>' if a.get("active") else "",
                meta=_esc(" · ".join(x for x in meta if x)),
                bal=bal_txt,
                cls=bal_cls,
                day=_esc(day_txt),
            )
        )
    if not rows:
        rows.append('<div class="empty">这台设备还没有登录过账户</div>')

    feed_rows = []
    for h in agg.get("feed") or []:
        ts = str(h.get("ts") or "")
        when = ts[5:16].replace("T", " ")
        credit = f"+{h.get('credit')}" if h.get("credit") else ""
        em_cls = "" if h.get("ok") else " class=\"bad\""
        feed_rows.append(
            '<div class="frow"><div class="fh"><b>{name}</b><span>{when}</span>'
            '<em{cls}>{credit}</em></div><div class="fm">{msg}</div></div>'.format(
                name=_esc(h.get("name") or ""),
                when=_esc(when),
                cls=em_cls,
                credit=_esc(credit),
                msg=_esc(h.get("msg") or "-"),
            )
        )
    if not feed_rows:
        feed_rows.append('<div class="empty">暂无记录</div>')

    top_line = " · ".join(f"{_esc(x.get('name'))} {int(x.get('balance') or 0):,}" for x in top3) or "—"
    left_txt = ("全部账户今天都领过了" if n and done_n >= n else
                (f"还有 {n - done_n} 个账户今天没领" if n else "还没有账户"))
    rows_html = "".join(rows)
    feed_html = "".join(feed_rows)

    # CSS / JS 用常量拼接，不放 f-string 或 .format（含花括号）。
    body = f'''<div class="bg"><i></i><i></i><i></i><i></i></div>
<div class="wrap">
  <div class="top"><h1>{logo_svg(24)}<span class="dotx"></span>聚合看板</h1>
    <div class="tools"><span class="pill"><i></i>{n} 个账户</span>
      <button class="tbtn" id="tbtn" type="button">跟随系统</button>
      <button class="tbtn" id="sbtn" type="button">⟳ 同步</button>
      <a class="tbtn" id="tosingle" href="/">单号看板</a></div>
  </div>

  <div class="hero">
    <div class="left">
      <div class="k">总积分</div>
      <div class="num">{tot_txt}<small>分</small></div>
      <div class="sub">今日已领 <b>{done_n}/{n}</b>　·　今日入账 +{today_in}　·　{n} 个账户合计</div>
    </div>
    <div class="side">
      <div><b>连续</b> 最长 {streak} 天</div>
      <div><b>宠物</b> 共 {pets} 只</div>
      <div><b>令牌</b> 最近到期 {dmin_txt}</div>
      <div><b>更新</b> {upd}</div>
    </div>
  </div>

  <div class="grid">
    <div class="card"><div class="k">今日已领</div><div class="v">{done_n}/{n}</div>
      <div class="s">{left_txt}</div></div>
    <div class="card"><div class="k">今日入账</div><div class="v">+{today_in}</div>
      <div class="s">签到 + 旅行合计</div></div>
    <div class="card"><div class="k">宠物总数</div><div class="v">{pets}</div>
      <div class="s">所有账户之和</div></div>
    <div class="card"><div class="k">积分前三</div>
      <div class="v" style="font-size:12.5px;line-height:1.7;font-weight:600">{top_line}</div></div>
  </div>

  <div class="block"><h2>账户明细（点一行切过去）</h2>{rows_html}</div>

  <div class="block"><h2>合并流水</h2><div class="feed">{feed_html}</div></div>

  <div class="tip" id="tip"></div>
  <div style="text-align:center;font-size:12px;opacity:.55;margin:18px 0 0">更新 {upd} · 页面 60 秒自动刷新 · 构建 {BUILD_TAG}</div>
</div>'''

    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>聚合看板 · WorkBuddy 积分</title>'
            '<meta name="color-scheme" content="dark light">'
            '<meta http-equiv="refresh" content="60">'
            '<script>(function(){try{var t=localStorage.getItem("wb-theme");'
            'if(t==="light"||t==="dark"){document.documentElement.setAttribute("data-theme",t)}}catch(e){}})();</script>'
            '<style>' + AGG_CSS + '</style></head><body>'
            + body + '<script>' + AGG_JS + '</script></body></html>')


def logo_svg(px: int = 26) -> str:
    """品牌标记（W 字标 + 右上角金点），颜色走 CSS 变量。"""
    return (
        f'<svg class="wbm" viewBox="0 0 1024 1024" width="{px}" height="{px}" aria-hidden="true">'
        '<rect width="1024" height="1024" rx="230" fill="var(--logo-bg,#121a2e)"/>'
        '<rect x="8" y="8" width="1008" height="1008" rx="222" fill="none" '
        'stroke="var(--logo-edge,transparent)" stroke-width="16"/>'
        '<path d="M252 320 L382 762 L512 492 L642 762 L772 320" fill="none" '
        'stroke="var(--logo-w,#f2c76b)" stroke-width="126" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        '<circle cx="838" cy="182" r="82" fill="var(--logo-coin,#f2c76b)"/>'
        '<circle cx="838" cy="182" r="42" fill="none" stroke="var(--logo-hole,rgba(122,74,12,.42))" '
        'stroke-width="19"/>'
        '</svg>'
    )


def mask_phone(v) -> str:
    """手机号脱敏：13800138000 → 138xxxx8000。非纯数字（unionid 之类）原样返回。"""
    s = str(v or "").strip()
    if len(s) < 8 or not s.isdigit():
        return s
    return s[:3] + "x" * (len(s) - 7) + s[-4:]


def _esc(v) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


def _cls(v) -> str:
    """拼 CSS class 用：只留字母数字和 -_，服务端给的稀有度等字段可能是任意串。"""
    return "".join(c for c in str(v or "").lower() if c.isalnum() or c in "-_")



def _sid_dir(sid: str) -> Path:
    d = LOGIN_ROOT / sid
    d.mkdir(parents=True, exist_ok=True)
    return d


def session_login_state(sid: str) -> dict:
    d = _sid_dir(sid)
    st = _read_json(d / "state.json", {}) or {}
    if not st:
        st = {"phase": "idle", "message": "未开始", "ver": 0, "ts": int(time.time() * 1000)}
    # 子进程拿到凭据即绑定会话，不必等前端 /api/login/sync。
    if (d / "token").exists() and not (d / "bound").exists() \
            and not (d / "bind_err").exists():
        try:
            fd = os.open(d / "binding", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            mine = True
        except OSError:
            mine = False
        if mine:
            try:
                r = session_login_sync(sid)
                if r.get("ok"):
                    (d / "bound").write_text(str(r.get("uid") or ""), encoding="utf-8")
                    st["phase"] = "done"
                    st["message"] = "登录成功"
                else:
                    log(f"会话 {sid[:8]} 自动绑定失败：{r.get('msg')}")
                    (d / "bind_err").write_text(str(r.get("msg") or "")[:200],
                                                encoding="utf-8")
            except Exception:  # noqa: BLE001
                log(traceback.format_exc())
            finally:
                try:
                    (d / "binding").unlink()
                except OSError:
                    pass
    elif (d / "bound").exists():
        st["phase"] = "done"
    with _worker_lock:
        p = _workers.get(sid)
        st["alive"] = bool(p is not None and p.poll() is None)
    if st["alive"] and st.get("phase") in ("idle", None):
        st["phase"] = "loading"
    return st


def session_login_qr(sid: str):
    try:
        return (_sid_dir(sid) / "qr.png").read_bytes()
    except OSError:
        return None


def session_login_start(sid: str, force: bool = False) -> dict:
    d = _sid_dir(sid)
    with _worker_lock:
        p = _workers.get(sid)
        alive = bool(p is not None and p.poll() is None)
        st = _read_json(d / "state.json", {}) or {}
        if alive and st.get("phase") in ("loading", "waiting", "scanned") and not force:
            return {"ok": True, "msg": "登录已在进行中"}
        # 清理退出的 worker 并限制并发。
        for k, v in list(_workers.items()):
            if v.poll() is not None:
                _workers.pop(k, None)
        if not alive and len(_workers) >= 8:
            return {"ok": False, "msg": "当前同时登录的人较多，请稍后再试"}
        if alive:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
        for n in ("state.json", "qr.png", "cookies.json", "token", "refresh_token",
                  "bound", "bind_err", "binding"):
            try:
                (d / n).unlink()
            except OSError:
                pass
        env = dict(os.environ)
        env["LOGIN_DIR"] = str(d)
        env["TOKEN_FILE"] = str(d / "token")
        if getattr(sys, "frozen", False):
            # 冻结态 sys.executable 是 App 本体，`python -c` 会被 PyInstaller 忽略（等于再开一个看板）。
            # 所以改走自定义 argv，由 desktop.py 分流到 --login-worker。
            cmd = [sys.executable, "--login-worker", sid]
        else:
            cmd = [sys.executable, "-c",
                   "import sys,login;sys.exit(0 if login.run_once_blocking() else 1)"]
        try:
            # 子进程日志落到会话目录，排障时直接看 <sid>/login.log
            logf = (d / "login.log").open("ab")
            _workers[sid] = subprocess.Popen(cmd, cwd=_HERE, env=env,
                                             stdout=logf, stderr=subprocess.STDOUT)
            logf.close()  # 子进程已继承 fd，父进程关掉不影响
        except Exception as e:  # noqa: BLE001
            log(f"启动登录子进程失败：{e}")
            return {"ok": False, "msg": f"启动登录失败：{e}"}
    log(f"会话 {sid[:8]} 开始扫码登录")
    return {"ok": True, "msg": "已刷新，正在获取二维码" if force else "已发起登录"}


def session_login_sync(sid: str) -> dict:
    """把子进程拿到的 token 绑定成该会话的用户。"""
    d = _sid_dir(sid)
    try:
        tok = (d / "token").read_text(encoding="utf-8").strip()
    except OSError:
        return {"ok": False, "msg": "还没有取到登录凭据"}
    if not tok or not jwt_payload(tok):
        return {"ok": False, "msg": "还没有取到登录凭据"}
    b = fetch_balance(tok)
    if b.get("auth"):
        return {"ok": False, "msg": f"凭据校验失败：{b.get('error')}"}
    u = user_new(tok)
    old = user_load(u["uid"])
    if old:
        # 换新令牌，保留该账户已有数据。
        for k in ("created", "hist", "snap", "slots_done", "today_done", "today_date",
                  "last_run", "today_credit", "streak_days", "checkin_dates",
                  "last_tick", "refresh_token"):
            v = old.get(k)
            if v not in (None, "", [], {}):
                u[k] = v
    try:
        rt = (d / "refresh_token").read_text(encoding="utf-8").strip()
        if rt:
            u["refresh_token"] = rt
    except OSError:
        pass
    # 从旧记录接回的 today_done 先按自然日清一次。
    _today = now().strftime("%Y-%m-%d")
    if u.get("today_date") != _today:
        u["today_done"] = False
        u["today_date"] = _today
        u["slots_done"] = []
    user_refresh(u, "登录完成")
    # 登录成功补领一次今日签到，守时段，已领过不重复提交。
    if not u.get("today_done"):
        try:
            if _in_window(CHECKIN_WINDOW) or _in_window(TRAVEL_DAY_WINDOW):
                user_checkin(u, "登录补领")
            else:
                log(f"  [{u['uid'][:8]}] 登录时间在自动时段外，今天先不自动签到"
                    f"（下个点 {fdt(next_slot(u['uid']))}）")
        except Exception:  # noqa: BLE001
            log(traceback.format_exc()[:300])
    try:
        user_travel(u, "登录后")
    except Exception:  # noqa: BLE001
        log(traceback.format_exc())
    bind_session(sid, u["uid"])
    mark_nologin(False)      # 重新登录成功，撤掉「主动退出」标记，恢复自动接回
    log(f"会话 {sid[:8]} 登录成功：{u['account'].get('name')}（{u['uid'][:8]}）")
    return {"ok": True, "msg": "已登录", "uid": u["uid"], "name": u["account"].get("name")}


def users_tick() -> None:
    """定时给所有访客账户刷新 + 签到 + 旅行（站长账户交给全局引擎）。"""
    if not MULTIUSER:
        return
    # 清理 6 小时前的扫码临时目录。
    try:
        cut = time.time() - 6 * 3600
        for d in LOGIN_ROOT.glob("*"):
            if d.is_dir() and d.stat().st_mtime < cut:
                shutil.rmtree(d, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
    tok, _ = read_token()
    owner_sub = (jwt_payload(tok) or {}).get("sub") if tok else ""
    t = now()
    date = t.strftime("%Y-%m-%d")
    for uid in store.account_uids():
        try:
            u = store.get_account(uid)
            if not u or not u.get("token"):
                continue
            # 站长账户只刷新，签到与旅行交给全局引擎。
            is_owner = bool(owner_sub) and u.get("uid") == owner_sub
            exp = u.get("token_exp")
            if exp:
                try:
                    if datetime.fromisoformat(str(exp).replace("Z", "+00:00")) <= t:
                        continue  # 凭据过期：等他自己重新扫码
                except Exception:  # noqa: BLE001
                    pass
            if u.get("today_date") != date:
                u["today_date"] = date
                u["today_done"] = False
                u["slots_done"] = []
                u.pop("catchup", None)
                u.pop("catchup_tries", None)
                # 跨天重置立刻落盘，否则下一轮从库里读回旧值。
                user_save(u)
            if time.time() - float(u.get("last_tick") or 0) < 600:
                continue
            u["last_tick"] = time.time()
            user_refresh(u, "周期刷新")   # 站长也刷，保证看板积分不陈旧
            if ARCHIVE_MODE:
                user_save(u)
                continue          # 存档模式：只读刷新，不派猫也不补签
            if not is_owner:
                user_travel(u, "周期巡检")
            if not u.get("today_done") and checkin_due(u, t, uid):
                # 按该账户自己的随机时刻表。
                u["last_ck"] = time.time()
                if not user_checkin(u, "定时领取") and u.get("catchup") == date:
                    # 失败清掉 catchup 标记，下一轮再试。
                    u.pop("catchup", None)
            user_save(u)
        except Exception:  # noqa: BLE001
            log(f"访客巡检异常（{uid[:8]}）：{traceback.format_exc()[:300]}")


def users_scheduler() -> None:
    """访客账户的独立巡检线程（每 60 秒一轮），避免被主循环卡住。"""
    if not MULTIUSER:
        return
    time.sleep(15)
    while True:
        try:
            users_tick()
        except Exception:  # noqa: BLE001
            log("访客巡检线程异常：" + traceback.format_exc()[:300])
        time.sleep(60)


class Handler(BaseHTTPRequestHandler):
    server_version = "wb-checkin"

    def log_message(self, fmt, *args):
        pass

    def _serve_pet(self, name, ck=None):
        """只出 PET_DIR 下的 png，挡掉 .. 之类的路径穿越。"""
        fn = os.path.basename(str(name or ""))
        if not fn.lower().endswith(".png"):
            return self._send(404, b"", "text/plain", cookies=ck)
        try:
            data = (Path(PET_DIR) / fn).read_bytes()
        except OSError:
            return self._send(404, b"", "text/plain", cookies=ck)
        return self._send(200, data, "image/png", cookies=ck)

    def _send(self, code, body, ctype="application/json; charset=utf-8", cookies=None):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        try:
            self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError):
            pass   # 手机跳转/关页面时客户端先断，属正常，不记栈

    @staticmethod
    def _sid_cookie(sid: str) -> str:
        return (f"{SID_COOKIE}={sid}; Path=/; Max-Age={SID_TTL}; "
                "HttpOnly; SameSite=Lax")

    @staticmethod
    def _keep_cookie(uid: str) -> str:
        return (f"{KEEP_COOKIE}={keep_sign(uid)}; Path=/; Max-Age={KEEP_TTL}; "
                "HttpOnly; SameSite=Lax")

    @staticmethod
    def _dev_cookie(dev: str) -> str:
        return (f"{DEV_COOKIE}={dev}; Path=/; Max-Age={DEV_TTL}; "
                "HttpOnly; SameSite=Lax")

    @staticmethod
    def _drop_cookies() -> list:
        return [f"{SID_COOKIE}=; Path=/; Max-Age=0",
                f"{KEEP_COOKIE}=; Path=/; Max-Age=0"]

    def _redirect(self, loc, cookies=None):
        self.send_response(302)
        self.send_header("Location", loc)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.end_headers()

    @staticmethod
    def _user(sid: str):
        uid = uid_of(sid)
        return user_load(uid) if uid else None

    def do_GET(self):
        p = "/" + self.path.split("?")[0].strip("/")
        adding = "add=1" in (self.path.split("?", 1)[1] if "?" in self.path else "")
        sid = sid_of(self.headers)
        ck = []
        dev = dev_of(self.headers)
        # 设备号不一致时要重发 cookie。
        dev_fixed = ensure_dev(dev)
        if dev_fixed != dev:
            dev = dev_fixed
            ck.append(self._dev_cookie(dev))
        if not sid:
            sid = new_sid()
            ck.append(self._sid_cookie(sid))
        # 添加账户时不能用上次的凭据把自己接回去。
        if not adding and not self._user(sid) and not p.startswith("/api/login/"):
            # 会话没了但本机记得账户：令牌剩 7 天以上直接接回。
            revive_from_keep(self.headers, sid)
            # 桌面端没有 wb_keep 时用本机账户兜底。
            if not self._user(sid) and DESKTOP_MODE and not p.startswith("/api/"):
                if desktop_resume(sid, dev):
                    ck.append(self._keep_cookie(uid_of(sid)))
        if p.startswith("/pets/"):
            # 桌面版没有 nginx，/pets/ 由应用自己服务。
            return self._serve_pet(p[6:], ck)
        if p.endswith("/health"):
            return self._send(200, json.dumps({"ok": True, "ts": now().isoformat(timespec="seconds")}))
        if p == "/api/login/state":
            st = session_login_state(sid)
            return self._send(200, json.dumps(st), cookies=ck)
        if p == "/api/login/qr.png":
            b = session_login_qr(sid)
            if b:
                return self._send(200, b, "image/png", cookies=ck)
            return self._send(404, b"", "image/png", cookies=ck)
        if p == "/api/status":
            u = self._user(sid)
            if not u:
                return self._send(401, json.dumps({"error": "未登录"}), cookies=ck)
            return self._send(200, json.dumps(user_snapshot(u), ensure_ascii=False, indent=2), cookies=ck)
        if p == "/api/history":
            u = self._user(sid)
            if not u:
                return self._send(401, json.dumps({"error": "未登录"}), cookies=ck)
            # 读 events 表，无条数上限，切账户也还在。
            hist = store.list_events(u["uid"], 60) or list(reversed((u.get("hist") or [])[-60:]))
            return self._send(200, json.dumps(hist, ensure_ascii=False, indent=2), cookies=ck)
        if p == "/api/accounts":
            return self._send(200, json.dumps(accounts_list(dev, sid), ensure_ascii=False),
                              cookies=ck)
        if p == "/api/store/stats":
            return self._send(200, json.dumps(store.stats(), ensure_ascii=False), cookies=ck)
        if p == "/api/balance":
            u = self._user(sid)
            if not u:
                return self._send(401, json.dumps({"error": "未登录"}), cookies=ck)
            return self._send(200, json.dumps((u.get("snap") or {}), ensure_ascii=False, indent=2), cookies=ck)
        if p == "/api/logout":
            forget_session(sid)
            return self._send(200, json.dumps({"ok": True}), cookies=self._drop_cookies())
        if p in ("/login", "/login/"):
            u0 = self._user(sid)
            # 令牌剩 7 天以内（含过期）才进登录页。
            if u0 and token_days_left(u0.get("token") or "") > KEEP_MIN_DAYS:
                return self._redirect("/", cookies=ck)
            return self._send(200, login_page.PAGE, "text/html; charset=utf-8", cookies=ck)
        if p == "/":
            u = self._user(sid)
            if not u:
                return self._send(200, login_page.PAGE, "text/html; charset=utf-8", cookies=ck)
            if not keep_uid(self.headers):
                # 老会话补发 wb_keep。
                ck = ck + [self._keep_cookie(u["uid"])]
            try:
                store.put_device(dev, u["uid"])   # 老会话也补进多账户列表
            except Exception:  # noqa: BLE001
                log(traceback.format_exc()[:300])
            # /?view=all：把这台设备登录过的账户合成一页。
            if "view=all" in (self.path.split("?", 1)[1] if "?" in self.path else ""):
                try:
                    return self._send(200, render_agg_html(build_agg(dev, sid)),
                                      "text/html; charset=utf-8", cookies=ck)
                except Exception:  # noqa: BLE001
                    log("聚合页渲染失败：" + traceback.format_exc()[:400])
            snap = u.get("snap") or {}
            stale = snap.get("balance") is None or not snap.get("buddies")
            at = snap.get("updated_at") or snap.get("balance_at")
            if at:
                try:
                    stale = stale or (now() - datetime.fromisoformat(str(at))).total_seconds() > 300
                except Exception:  # noqa: BLE001
                    stale = True
            if stale:
                try:
                    user_refresh(u, "打开看板")
                except Exception:  # noqa: BLE001
                    log(traceback.format_exc())
            hist = store.list_events(u["uid"], 18) or list(reversed((u.get("hist") or [])[-18:]))
            return self._send(200, render_html(user_snapshot(u), hist),
                              "text/html; charset=utf-8", cookies=ck)
        return self._send(404, json.dumps({"error": "not found"}))

    def _login_post(self, p):
        sid = sid_of(self.headers)
        ck = []
        raw_dev = dev_of(self.headers)
        # 设备号统一走 ensure_dev()，否则 cookie 一丢就多造一个 dev。
        dev = ensure_dev(raw_dev)
        if dev != raw_dev:
            ck.append(self._dev_cookie(dev))
        if not sid:
            sid = new_sid()
            ck.append(self._sid_cookie(sid))
        if p == "/api/login/start":
            return self._send(200, json.dumps(session_login_start(sid), ensure_ascii=False), cookies=ck)
        if p == "/api/login/restart":
            return self._send(200, json.dumps(session_login_start(sid, force=True), ensure_ascii=False), cookies=ck)
        if p == "/api/login/sync":
            r = session_login_sync(sid)
            if r.get("ok") and r.get("uid"):
                # 记录这台设备登录过的账户。
                try:
                    store.put_device(dev, r["uid"])
                except Exception:  # noqa: BLE001
                    log(traceback.format_exc()[:300])
                ck.append(self._keep_cookie(r["uid"]))
            return self._send(200, json.dumps(r, ensure_ascii=False), cookies=ck)
        if p in ("/api/login/phone/send", "/api/login/phone/verify"):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}")
            except Exception:  # noqa: BLE001
                body = {}
            d = _sid_dir(sid)
            if p.endswith("/send"):
                r = login.phone_begin(str(d), str(body.get("phone") or ""),
                                      str(body.get("ticket") or ""),
                                      str(body.get("randstr") or ""))
            else:
                r = login.phone_verify(str(d), str(body.get("code") or ""))
            return self._send(200, json.dumps(r, ensure_ascii=False), cookies=ck)
        if p == "/api/login/logout":
            forget_session(sid)
            return self._send(200, json.dumps({"ok": True}), cookies=self._drop_cookies())
        return self._send(404, json.dumps({"error": "not found"}))

    def _accounts_post(self, p):
        """多账户：切换 / 添加。都不走 PUSH_SECRET（和登录接口同级）。"""
        sid = sid_of(self.headers) or new_sid()
        # 设备号走 ensure_dev()，否则 device_has 一律判 403。
        dev = ensure_dev(dev_of(self.headers))
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}") if n else {}
        except Exception:  # noqa: BLE001
            body = {}
        if p == "/api/accounts/logout":
            uid = str(body.get("uid") or "")
            if not uid:
                return self._send(400, json.dumps({"ok": False, "msg": "缺少账户标识"},
                                                  ensure_ascii=False))
            cur = uid_of(sid)
            if uid != cur and not store.device_has(dev, uid):
                return self._send(403, json.dumps({"ok": False, "msg": "这个账户不在这台设备的列表里"},
                                                  ensure_ascii=False))
            r = logout_account(uid)
            if not r.get("ok"):
                return self._send(404, json.dumps(r, ensure_ascii=False))
            remain = store.device_uids(dev)
            ck2 = []
            if uid == cur:
                # 退掉当前账户后自动切到下一个。
                if remain:
                    bind_session(sid, remain[0])
                    ck2 = [self._sid_cookie(sid), self._keep_cookie(remain[0])]
                else:
                    ck2 = self._drop_cookies() + [self._dev_cookie(dev)]
                    mark_nologin(True)   # 全部退出后停在扫码页，不再自动接回
            r["remain"] = len(remain)
            r["cur"] = remain[0] if (uid == cur and remain) else cur
            return self._send(200, json.dumps(r, ensure_ascii=False), cookies=ck2)
        if p == "/api/accounts/logout_all":
            uids = list(store.account_uids())
            cur = uid_of(sid)
            if cur and cur not in uids:
                uids.append(cur)
            n = 0
            for uid in uids:
                if logout_account(uid).get("ok"):
                    n += 1
            log(f"全部退出：清了 {n} 个账户（设备 {dev[:8]}）")
            mark_nologin(True)
            return self._send(200, json.dumps({"ok": True, "n": n, "remain": 0},
                                              ensure_ascii=False),
                              cookies=self._drop_cookies() + [self._dev_cookie(dev)])
        if p == "/api/accounts/switch":
            uid = str(body.get("uid") or "")
            if not store.device_has(dev, uid):
                return self._send(403, json.dumps({"ok": False, "msg": "这个账户不在这台设备的列表里"},
                                                  ensure_ascii=False))
            u = user_load(uid)
            if not u or not u.get("token"):
                return self._send(404, json.dumps({"ok": False, "msg": "账户数据不见了，请重新登录"},
                                                  ensure_ascii=False))
            left = token_days_left(u["token"])
            if left <= 0:
                return self._send(409, json.dumps({"ok": False, "msg": "该账户凭据已过期，需要重新登录",
                                                   "uid": uid}, ensure_ascii=False))
            # 切换后同步实时昵称。
            try:
                if sync_profile(u, "切换账户"):
                    user_save(u)
            except Exception:  # noqa: BLE001
                pass
            bind_session(sid, uid)
            store.put_device(dev, uid)
            log(f"切换账户：{sid[:8]} → {uid[:8]}（令牌剩 {left:.1f} 天）")
            return self._send(200, json.dumps({"ok": True, "uid": uid}, ensure_ascii=False),
                              cookies=[self._sid_cookie(sid), self._keep_cookie(uid),
                                       self._dev_cookie(dev)])
        if p == "/api/accounts/add":
            # 新开会话登录，旧账户留在设备列表里。
            forget_session(sid)
            new = new_sid()
            log(f"添加账户：换新会话 {new[:8]}（设备 {dev[:8]}）")
            return self._send(200, json.dumps({"ok": True}, ensure_ascii=False),
                              cookies=[self._sid_cookie(new), self._dev_cookie(dev)])
        return self._send(404, json.dumps({"error": "not found"}))

    @staticmethod
    def _manual_claim(u: dict) -> str:
        """点「同步」时领今日积分：claimed / already / failed 三态。
        手动触发不受 09:00–12:00 自动窗口限制；user_checkin 本身幂等。
        """
        today = now().strftime("%Y-%m-%d")
        if u.get("today_done") and u.get("today_date") == today:
            return "already"        # 今天已领，跳过接口
        u["last_ck"] = time.time()
        _n0 = len(u.get("hist") or [])
        if not user_checkin(u, "手动同步"):
            return "failed"
        # 判据看调用后新增流水里有没有「手动同步」，不能看 hist[-1]（user_checkin 之后还会 refresh）。
        for h in (u.get("hist") or [])[_n0:]:
            h = h or {}
            if h.get("kind") == "success" and str(h.get("reason") or "") == "手动同步":
                return "claimed"
        return "already"

    def _sync_post(self, sid):
        """手动同步：实时昵称 + 余额 / 宠物刷新，并领取今日积分。agg=true 时同步本机全部账户。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}") if n else {}
        except Exception:  # noqa: BLE001
            body = {}
        if body.get("agg"):
            # 设备号与 do_GET 一样走 ensure_dev()。
            dev = ensure_dev(dev_of(self.headers))
            uids = store.device_uids(dev) if dev else []
            if not uids:
                # cookie 没带 wb_dev 时退回会话绑定的账户。
                su = self._user(sid)
                if su and su.get("uid"):
                    uids = [su["uid"]]
            done = 0
            claimed = 0
            already = 0
            failed = 0
            credit = 0
            _today = now().strftime("%Y-%m-%d")
            for uid in uids:
                u = user_load(uid)
                if not u or not u.get("token"):
                    continue
                try:
                    # 已领的号才需要先刷，未领的交给 user_checkin 自带刷新。
                    if u.get("today_done") and u.get("today_date") == _today:
                        user_refresh(u, "手动同步")
                    st = self._manual_claim(u)
                    if st == "claimed":
                        claimed += 1
                        credit += _claim_credit(u)
                    elif st == "already":
                        already += 1
                    else:
                        failed += 1
                    user_save(u)
                    done += 1
                except Exception:  # noqa: BLE001
                    failed += 1
                    log(f"手动同步异常（{uid[:8]}）：{traceback.format_exc()[:200]}")
            if not done:
                msg = "没有可同步的账户"
            elif claimed:
                msg = f"已同步 {done} 个账户，新领 {claimed} 个号 +{credit}"
                if failed:
                    msg += f"（{failed} 个失败）"
            elif failed:
                msg = f"已同步 {done} 个账户，{failed} 个领取失败（看流水里的原因，多为令牌过期）"
            else:
                msg = f"已同步 {done} 个账户（今日积分均已在账）"
            return self._send(200, json.dumps(
                {"ok": done > 0, "n": done, "claimed": claimed, "already": already,
                 "failed": failed, "credit": credit, "msg": msg},
                ensure_ascii=False))
        u = self._user(sid)
        if not u or not u.get("token"):
            return self._send(404, json.dumps({"ok": False, "msg": "还没有登录账户"}, ensure_ascii=False))
        st = "failed"
        try:
            # 同上：只有已领的号需要先刷。
            if u.get("today_done") and u.get("today_date") == now().strftime("%Y-%m-%d"):
                user_refresh(u, "手动同步")
            st = self._manual_claim(u)
            user_save(u)
        except Exception:  # noqa: BLE001
            log(f"手动同步异常（{u['uid'][:8]}）：{traceback.format_exc()[:200]}")
            return self._send(500, json.dumps({"ok": False, "msg": "同步失败，稍后再试"}, ensure_ascii=False))
        # 只有真的领到才回传 credit。
        credit = _claim_credit(u) if st == "claimed" else 0
        msg = {"claimed": f"已领 +{credit}",
               "already": "今日积分已在账",
               "failed": "领取失败（多为令牌过期，看流水里的原因）"}[st]
        return self._send(200, json.dumps(
            {"ok": True, "name": (u.get("account") or {}).get("name"),
             "claimed": st == "claimed", "state": st, "credit": credit, "msg": msg},
            ensure_ascii=False))

    def do_POST(self):
        p = "/" + self.path.split("?")[0].strip("/")
        # 扫码登录接口由 nginx 层口令保护，不走 PUSH_SECRET。
        if p.startswith("/api/login/"):
            return self._login_post(p)
        if p.startswith("/api/accounts/"):
            return self._accounts_post(p)
        if p == "/api/sync":
            return self._sync_post(sid_of(self.headers))
        if not PUSH_SECRET or self.headers.get("X-Auth", "") != PUSH_SECRET:
            return self._send(403, json.dumps({"error": "forbidden"}))
        if p.endswith("/token"):
            n = int(self.headers.get("Content-Length") or 0)
            t = self.rfile.read(n).decode("utf-8", "replace").strip()
            if not t:
                return self._send(400, json.dumps({"error": "empty token"}))
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_FILE.write_text(t, encoding="utf-8")
            os.chmod(TOKEN_FILE, 0o600)
            log(f"已接收新令牌，指纹 {token_fp(t)}")
            return self._send(200, json.dumps({"ok": True, "fp": token_fp(t)}))
        if p.endswith("/run"):
            return self._send(200, json.dumps(run_once("API 触发"), ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))


def serve():
    srv = ThreadingHTTPServer((BIND_ADDR, PORT), Handler)
    log(f"看板监听 {BIND_ADDR}:{PORT}（/ 看板、/api/status、/api/history、/health）")
    srv.serve_forever()


def main():
    _state["started_at"] = now().isoformat(timespec="seconds")
    log(f"WorkBuddy 签到守护启动｜时区 UTC{os.environ.get('TZ_OFFSET', '8')}"
        f"｜签到时段 %02d:00–%02d:00（%d 次随机）｜派猫 %02d:00–%02d:00 起、%02d:00 收工"
        % (CHECKIN_WINDOW + (CHECKIN_TRIES,) + TRAVEL_WINDOW + (TRAVEL_DAY_WINDOW[1],)))
    token, src = read_credential()
    if token:
        _state["token_fp"] = credential_fp(token)
        _state["token_exp"] = credential_expiry(token)
        _state["auth_kind"] = "cookie" if session.header() else "bearer"
        log(f"已加载凭据（{_state['auth_kind']}，指纹 {_state['token_fp']}，来源 {src}）")
    else:
        log("⚠️ 尚未登录：等待扫码登录，或把令牌放进 state/token")
    try:
        if STATUS_FILE.exists():
            old = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            for k in (
                "today_done", "today_date", "streak_days", "last_run", "last_msg", "today_credit",
                "balance", "balance_total", "balance_at", "packages", "is_paid", "plan",
                "activity_active", "daily_credit", "next_streak_day", "streak_bonus_credit", "checkin_dates",
                # 容器重启后恢复旅行状态。
                "travel_state", "travel_location", "travel_arrive_at", "travel_reward",
                "travel_daily_limit", "travel_record_id", "travel_action",
                "buddies", "buddy_count", "buddy_at",
                # 恢复已跑过的计划点。
                "slots_done",
            ):
                if k in old:
                    _state[k] = old[k]
    except Exception:  # noqa: BLE001
        pass

    if MULTIUSER:
        USERS_DIR.mkdir(parents=True, exist_ok=True)
        LOGIN_ROOT.mkdir(parents=True, exist_ok=True)
        _keep_secret()          # 会话丢了靠它免登录接回来（提前生成）
        log(f"多用户模式已启用：会话目录 {LOGIN_ROOT}，账户目录 {USERS_DIR}"
            f"｜免登录恢复：令牌剩 >{KEEP_MIN_DAYS} 天直接进看板")
    if os.environ.get("ONCE") == "1":
        run_once("ONCE 模式")
        return
    threading.Thread(target=serve, daemon=True).start()
    threading.Thread(target=users_scheduler, daemon=True).start()
    scheduler()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
