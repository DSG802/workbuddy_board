# WorkBuddyBoard

WorkBuddy 看板客户端：签到、积分余额、宠物旅行。本机运行，不依赖浏览器。

`app/` 是纯 Python 标准库 + SQLite 的同一份核心代码，四个入口共用：

| 形态 | 入口 | 产物 |
|---|---|---|
| macOS | `desktop.py` + pywebview（WKWebView） | `.app`，单架构，约 28 MB |
| Windows | `desktop.py` + pywebview（Edge WebView2） | `WorkBuddyBoard.exe` 目录版 |
| Android | Chaquopy 内嵌 CPython + WebView | APK（arm64），约 21 MB |
| 服务端 | `python checkin.py` + Docker | 多用户 Web 服务，见 [`server/README.md`](server/README.md) |

客户端在进程内起一个只监听 `127.0.0.1` 的 HTTP 服务，窗口指向它。数据全在本机。

## 开发态

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pywebview pyinstaller
.venv/bin/python desktop.py                 # 开窗口
.venv/bin/python desktop.py --service-only  # 只起服务，打印 READY 地址
```

冒烟：

```bash
WB_PORT=18082 .venv/bin/python desktop.py --service-only &
curl -s localhost:18082/health
curl -sI localhost:18082/pets/pet-baofu.png
```

## 打包

```bash
./build_mac.sh            # 本机架构
./build_mac.sh arm64      # Apple Silicon → dist-arm64/
./build_mac.sh x86_64     # Intel        → dist-x86_64/
```

打包结束会顺带出 dmg（安装包内是 app + Applications 快捷方式），不需要时加 `MAKE_DMG=0` 关掉。

两个 macOS 包都是单架构，不通用。PyInstaller 只打当前解释器能跑的架构，Intel 版需要一份 x86_64 的 Python 3.13 建独立环境（默认 `.venv-x64`），在 Apple Silicon 上靠 Rosetta 2 转译：

```bash
PY_X64=/path/to/x86_64/python3.13 ./build_mac.sh x86_64
```

交叉打 x86_64 时 `/usr/bin/lipo` 不可用（shim 会 dlopen 只有 arm64 切片的 `libxcrun`），`tools/lipo` 包了一层 `exec arch -arm64`，打之前放到 `PATH` 最前。

Windows 在 Windows 上跑 `build_win.bat`（Python 3.10+，Win10/11 自带 WebView2 运行时）。PyInstaller 不支持交叉编译。

## 数据目录

| 平台 | 路径 |
|---|---|
| macOS | `~/Library/Application Support/WorkBuddyBoard/` |
| Windows | `%APPDATA%\WorkBuddyBoard\` |
| Android | `/data/data/com.workbuddy.board/files/` |

```
WorkBuddyBoard/
├── app.log            # 运行日志
├── login/             # 扫码登录的临时现场
└── state/
    ├── dashboard.db   # SQLite：账户 / 会话 / 流水 / 快照
    ├── token          # 站长令牌
    ├── keep.key       # 免登录恢复的签名密钥
    └── status.json    # 看板状态快照
```

`state/` 里是明文令牌和会话，按 600 落盘。卸载 App 不删这个目录。

同一份 `state/` 只允许一个进程读写（WAL，锁文件 `.running`），第二次启动唤起已有窗口。

## 三种形态的差异

代码同源，数据不互通：桌面版和 Android 各有一份 SQLite，首次使用各登录一次。

同一账号不要同时挂服务端和客户端：两边都会派宠物旅行、都会续期令牌，旅行派遣会有一边失败。切换前先停掉另一边；服务端可开 `ARCHIVE_MODE=1` 做只读镜像。

## 实现要点

**登录**：两条路都在进程内完成，界面只显示二维码或表单。

- 微信扫码：`/v2/plugin/auth/state` 取 `state` → 官方 signin 页出码 → 长轮询 `qrconnect` 取 `code` → `signin/session` → `GET /console/auth/login?state=`（把浏览器会话绑到插件 state，缺这步恒报 `11217 login ing...`）→ `/v2/plugin/auth/token` 取令牌。
- 手机号：`phone/login` → `phone/sms` → `phone/verify` → `finalize`，之后与扫码一致。
- 令牌 30 天。`refresh_token` 属机密客户端，没有 `client_secret` 一律被拒，静默续期不可用；到期前 10 天提示重新扫码。
- 网关两个约束：cookie 只发 `session` + `session_2`，合计小于 8 KB（多带会把必需项挤出去，401）；UA 需声明 `HeadlessChrome`，伪装成真实浏览器反而 401。

**昵称**：JWT 里的 `nickname` 在签发时固化，改名后看板会一直显示旧名。改用 `GET /console/accounts` 的实时昵称，挂在 `user_refresh()` 里，登录、巡检、切账户三条路都会纠偏。

**流水**：只记本实例主动执行的签到与领奖。签到被另一端抢先完成时，`user_refresh()` 发现 `today_checked_in` 为真且当天没有签到流水，补一条 `kind=passive`，每天至多一条。接口没有历史积分明细，历史日期无法回补。

**主题**：颜色全部走 CSS 变量，标记由 `logo_svg()` 输出几何，颜色交给 `--logo-bg / --logo-w / --logo-coin / --logo-hole / --logo-edge`。加主题时补齐这几个变量。

## Android

同一份 `app/`：Chaquopy 内嵌 CPython 3.12 在进程内跑本地服务，外面套 WebView 指到 `127.0.0.1`。

| 项 | 值 |
|---|---|
| 包名 | `com.workbuddy.board` |
| 架构 | 只打 `arm64-v8a`（要 32 位 / x86 模拟器改 `abiFilters`） |
| minSdk / targetSdk | 24 / 34 |
| 签名 | `android/keystore/wb-release.jks`，口令在 `keystore.properties`（升级覆盖安装要靠它，别丢） |

```bash
cd android
./build.sh      # 同步 ../app 与 ../pets，再 gradle assembleRelease
```

依赖 JDK 17、Android SDK（platform 34 + build-tools 34）、Gradle 8.7。`build.sh` 认 `ANDROID_HOME`、`JAVA_HOME`、`GRADLE`。首次构建从 Maven 拉 AGP + Chaquopy（约 300 MB），之后 1~2 分钟。

与桌面版的差异只在打包层：

| 位置 | 桌面版 | Android |
|---|---|---|
| 扫码 worker | 子进程 | `android_entry._patch_login_worker()` 换成线程，`_InlineWorker` 补齐 `poll/wait/terminate/kill` |
| 窗口 | pywebview | `MainActivity` 里的 WebView |
| 宠物图 | `PET_DIR` 指目录 | Java 先从 assets 复制到 `files/pets` |
| 单实例锁 | 有 | 不需要 |
| 后台定时 | 常驻 | 系统会冻结后台进程，只在 App 活着时生效，打开时补跑一轮 |

`networkSecurityConfig` 只对 `127.0.0.1` / `localhost` 放行明文，其余强制 HTTPS。

## 目录

```
.
├── desktop.py          # 客户端入口：环境变量 → 起服务 → 开窗口
├── app/                # 核心：checkin / login / login_page / session / store
├── pets/               # 宠物雪碧图
├── assets/             # 图标（明暗两套 svg、icns、ico）
├── android/            # Android 工程（Chaquopy + WebView）
├── server/             # 服务端：Dockerfile、compose、反代示例、Docker 版 app/
├── tools/lipo          # 交叉打包 x86_64 用的 lipo 包装
├── build_mac.sh        # macOS 打包（双架构）
└── build_win.bat       # Windows 打包
```

## 许可

未附许可证文件，默认保留所有权利。
