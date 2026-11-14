#!/usr/bin/env bash
# 打包 macOS 客户端。用法：
#   ./build_mac.sh            # 本机架构（Apple Silicon 上是 arm64）
#   ./build_mac.sh arm64      # M 系列
#   ./build_mac.sh x86_64     # Intel
#
# PyInstaller 只能打当前解释器能跑的架构：Intel 版要用 x86_64 的 Python 建独立
# 虚拟环境（默认 .venv-x64），在 Apple Silicon 上靠 Rosetta 2 转译。
#
# 产物：dist-<arch>/WorkBuddyBoard.app
set -euo pipefail
cd "$(dirname "$0")"

ARCH="${1:-$(uname -m)}"
case "$ARCH" in
  arm64) ;;
  x86_64|intel) ARCH=x86_64 ;;
  *) echo "架构只支持 arm64 / x86_64，收到：$ARCH" >&2; exit 1 ;;
esac

# 各架构用各自的解释器与虚拟环境，互不干扰
if [ "$ARCH" = "x86_64" ]; then
  VENV=".venv-x64"
  # x86_64 解释器优先取已有 venv，其次由 PY_X64 指定，不写死机器路径。
  if [ -x "$VENV/bin/python" ]; then
    PY="$VENV/bin/python"
  else
    PY="${PY_X64:?请用 PY_X64=/path/to/python3.13 指定 x86_64 解释器}"
  fi
else
  VENV=".venv"
  PY="${PY_ARM:-python3}"
fi

if [ ! -x "$PY" ] && ! command -v "$PY" >/dev/null 2>&1; then
  echo "找不到 $ARCH 用的 Python：$PY" >&2
  echo "Intel 版需要一个 x86_64 的 Python 3.13，可用 PY_X64=/path/to/python3.13 指定。" >&2
  exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
  echo "==> 创建 $VENV（$ARCH）"
  if [ "$ARCH" = "x86_64" ]; then
    arch -x86_64 "$PY" -m venv "$VENV"
  else
    "$PY" -m venv "$VENV"
  fi
  "$VENV/bin/python" -m pip install -q --upgrade pip
  # 不能加 --only-binary=:all:：proxy_tools 只有源码包，加了会退回 pywebview 3.4。
  "$VENV/bin/python" -m pip install -q "pywebview==6.2.1" pyinstaller
fi

echo "==> 解释器：$("$VENV/bin/python" -c 'import sys,platform;print(sys.version.split()[0], platform.machine())')"
echo "==> 打包 macOS $ARCH"

# 交叉瘦身时 PyInstaller 调 lipo，x86_64 进程里的 shim 会 dlopen 只有 arm64 的
# libxcrun 而失败；tools/lipo 强制用原生 arm64 跑系统 lipo。
if [ "$ARCH" = "x86_64" ] && [ "$(uname -m)" = "arm64" ]; then
  PATH="$PWD/tools:$PATH"
  export PATH
fi

# 清上次产物：挪到 /tmp，不用 rm -rf（沙箱的 safe-delete 会拦）。
for d in "dist-$ARCH" "build-$ARCH"; do
  if [ -d "$d" ]; then
    STAMP="$(date +%s)"
    mv "$d" "/tmp/wb-$d-$STAMP"
  fi
done

"$VENV/bin/pyinstaller" \
  --noconfirm --clean --windowed \
  --name WorkBuddyBoard \
  --osx-bundle-identifier cn.lhwj.workbuddy.board \
  --target-arch "$ARCH" \
  --icon assets/icon.icns \
  --hidden-import sqlite3 \
  --hidden-import _sqlite3 \
  --add-data "app:app" \
  --add-data "pets:pets" \
  --distpath "dist-$ARCH" \
  --workpath "build-$ARCH" \
  desktop.py

APP="dist-$ARCH/WorkBuddyBoard.app"

# 页脚的 BUILD_TAG 是运行时常量，构建期 export 不会传给双击启动的进程，
# 只能改写产物里的那份 checkin.py，改完重签。
TAG="${BUILD_TAG:-$(date +%Y-%m-%d.%H%M)}"
APPSRC="$APP/Contents/Resources/app"
/usr/bin/sed -i '' -E \
  "s/^BUILD_TAG = os\.environ\.get\(\"BUILD_TAG\", \"[^\"]*\"\)/BUILD_TAG = os.environ.get(\"BUILD_TAG\", \"$TAG\")/" \
  "$APPSRC/checkin.py"
grep -q "BUILD_TAG\", \"$TAG\"" "$APPSRC/checkin.py" || {
  echo "BUILD_TAG 注入失败：$APPSRC/checkin.py" >&2
  exit 1
}
find "$APPSRC/__pycache__" -name 'checkin.*.pyc' -delete 2>/dev/null || true

# 版本号取 android/app/build.gradle 的 versionName，避免另立一份
VER="${APP_VER:-$(sed -n 's/.*versionName "\([0-9.]*\)".*/\1/p' android/app/build.gradle)}"
[ -n "$VER" ] || VER="8.0"
PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VER" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VER" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VER" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VER" "$PLIST"

codesign --force --deep --sign - "$APP" >/dev/null 2>&1

echo
echo "==> 完成：$APP  ($(du -sh "$APP" | cut -f1))"
echo "    架构：$(lipo -archs "$APP/Contents/MacOS/WorkBuddyBoard")"
echo "    版本：$VER"
echo "    BUILD_TAG：$TAG"
echo "    双击即可运行；数据在 ~/Library/Application Support/WorkBuddyBoard/"

if [ "${MAKE_DMG:-1}" = "1" ]; then
  LABEL=""
  [ "$ARCH" = "arm64" ] && LABEL="AppleSilicon"
  [ "$ARCH" = "x86_64" ] && LABEL="Intel"
  tools/make_dmg.sh "$APP" "dist-$ARCH/WorkBuddyBoard-$ARCH.dmg" "$VER" "$LABEL"
fi
