# 服务端（Docker + 反代）

把 `app/` 跑成多用户 Web 服务：扫码 / 手机号登录、定时签到、宠物旅行，每人一个会话，按会话取数。桌面版和 Android 是同一份 `app/` 的另外两个入口。

```
浏览器 ──▶ nginx（TLS）──▶ workbuddy-new:8080（容器内，不对外开端口）
                             ├── /state   SQLite + 会话 + 令牌（600）
                             └── 无头 Chromium（镜像自带，跑扫码）
```

## 目录

```
server/
├── Dockerfile                    # playwright/python 基座 + chromium，装 pyzbar / pillow
├── docker-compose.yaml           # 单服务，不映射端口，接外部网络
├── .env.example                  # 复制成 .env 再改
├── app/                          # 从 ../app 同步
├── tools/                        # CDP 版扫码脚本（Playwright 版出问题时的兜底）
└── nginx/dashboard.conf.example  # 反代示例
```

## 启动

```bash
cd server
cp .env.example .env      # 至少改 PUSH_SECRET，不填则不启用推送
docker compose up -d --build
docker compose logs -f --tail=50
curl -s localhost:8080/health
```

容器不映射端口，由同网络的 nginx 反代。本地看效果时临时加 `ports: ["8080:8080"]`，不要留在生产配置里。

`server/app/` 是 `../app/` 的副本，允许有服务端专属差异（多用户调度、存档模式）。改核心逻辑时两边都要同步，并保证 `android/app/src/main/python/*.py` 与 `app/*.py` 逐字节一致。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `8080` | 容器内监听端口 |
| `BIND_ADDR` | `0.0.0.0` | 容器内必须 0.0.0.0；桌面版固定 127.0.0.1 |
| `STATE_DIR` | `/state` | SQLite、会话、令牌的落盘目录，要持久化 |
| `TOKEN_FILE` | `/state/token` | 站长账户的令牌文件 |
| `LOGIN_DIR` | `/tmp/wb-login` | 扫码中间产物，可丢 |
| `PET_DIR` | `/pets` | 宠物雪碧图目录 |
| `MULTIUSER` | `1` | 访客各登自己的账户；`0` = 只有站长单账户 |
| `ARCHIVE_MODE` | `0` | 只读镜像：展示但不签到、不派猫、不续期 |
| `CHECKIN_WINDOW` | `9-12` | 自动签到的随机时段 |
| `CHECKIN_TRIES` | `4` | 时段内随机尝试次数 |
| `TRAVEL_WINDOW` | `9-12` | 当天第一次派猫的时段 |
| `TRAVEL_DAY_WINDOW` | `9-22` | 白天窗口：多次旅行与领奖 |
| `ATTEMPT_HOURS` | 空 | 旧版整点策略，已被上面四个取代 |
| `BALANCE_MINUTES` | `30` | 余额轮询间隔 |
| `TRAVEL_MINUTES` | `10` | 旅行状态轮询间隔 |
| `TRAVEL_ENABLE` | `1` | 是否派遣 / 领取旅行 |
| `PUSH_SECRET` + `WEBHOOK_URL` | 空 | 可选推送：签到 / 旅行结果回调 |
| `TZ` | `Asia/Shanghai` | |

## 存储

全部在 `STATE_DIR/dashboard.db`（标准库 `sqlite3`，WAL，文件 600）：

| 表 | 用途 |
|---|---|
| `accounts` | uid → 整份账户状态（令牌 / 快照 / 流水 / 签到进度） |
| `sessions` | `wb_sid` → uid，90 天 TTL |
| `events` | 签到与旅行流水 |
| `snapshots` | 每次刷新一条积分快照，每账户保留最近 2000 条 |
| `devices` | `(dev, uid)` → 最后使用时间 |
| `meta` | 迁移标记 |

每线程一个连接（`threading.local`），`busy_timeout=8000`，后台巡检与 HTTP 线程并发写不会丢更新。

退出登录只删 `sessions` 里那一行，账户与历史保留。整目录备份：

```bash
docker compose exec dashboard python - <<'PY'
import sqlite3; sqlite3.connect('/state/dashboard.db').execute("VACUUM INTO '/state/backup.db'")
PY
docker compose cp dashboard:/state/backup.db ./backup-$(date +%F).db
```

## 登录链路

两种方式都在服务端走完，浏览器只显示二维码或表单。

1. 微信扫码：`/v2/plugin/auth/state` 取 `state` + `authUrl` → 官方 signin 页出码（`qrcode_url`）→ 长轮询 `lp.open.weixin.qq.com/connect/l/qrconnect` → `ticket` → `finalize` 换 `code` → `signin/session` 建浏览器会话 → `GET /console/auth/login?state=<插件state>`（把浏览器会话绑到客户端 state，缺这步恒报 `11217 login ing...`）→ `/v2/plugin/auth/token?state=` 取 `accessToken` / `refreshToken`。
2. 手机号：`signin/phone/login` → `phone/sms` → `phone/verify` → `finalize`，之后与扫码一致。风控命中时 `phone/sms` 返回 `captcha_required`，接口回 `{captcha:true}`，前端加载腾讯验证码再重发。

- 令牌 30 天，到期前 10 天页面顶部提示续期。
- 静默续期基本不可用：官方 `refresh_token` 属机密客户端，没有 `client_secret` 时授权服务器一律拒绝。
- 扫码在子进程里跑（靠 `LOGIN_DIR` / `TOKEN_FILE` 隔离），并发上限 8。

## 反代

`nginx/dashboard.conf.example` 可直接用，两点必须保留：

1. `resolver 127.0.0.11 valid=10s;` + `set $up 上游:端口;` + `proxy_pass http://$up;`。直接写容器名会让 nginx 在启动时解析上游，上游没起来就整机 `emerg`。
2. 扫码状态是 1.5 秒一次的轮询，`proxy_read_timeout` 不低于 30s；大响应关掉 `Accept-Encoding`，免得二次压缩。

容器连的外部网络要在 compose 的 `networks` 里声明，和 nginx 用同一个。

## 同一账号挂两个实例

签到幂等，旅行派遣和令牌续期不幂等。同一账号挂两处会出现一边派猫失败，或令牌被续期后另一边 401。

要并存就给只读那份设 `ARCHIVE_MODE=1` + `TRAVEL_ENABLE=0`。只保留一个写入实例最省事。
