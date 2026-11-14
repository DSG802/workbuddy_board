#!/usr/bin/env bash
# WorkBuddy 看板 · Android 打包
#
# 复用 ../app 的纯标准库 Python，Chaquopy 内嵌 CPython，WebView 打开 127.0.0.1 上的本地服务。
#
# 依赖：JDK 17、Android SDK（platform 34 + build-tools 34）、Gradle 8.7
set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
ROOT="$(cd .. && pwd)"

export ANDROID_HOME="${ANDROID_HOME:-$HOME/android-sdk}"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export JAVA_HOME="${JAVA_HOME:-/Library/Java/JavaVirtualMachines/temurin-17.jdk/Contents/Home}"
GRADLE="${GRADLE:-$HOME/android-tools/gradle-8.7/bin/gradle}"

if [ ! -x "$GRADLE" ]; then
  echo "找不到 gradle：$GRADLE（可用 GRADLE=/path/to/gradle 覆盖）" >&2
  exit 1
fi
if [ ! -d "$ANDROID_HOME/platforms/android-34" ]; then
  echo "Android SDK 缺 platform 34：$ANDROID_HOME" >&2
  exit 1
fi

echo "==> 1/4 同步 Python 源码"
mkdir -p app/src/main/python
rsync -a --delete --exclude '__pycache__' --exclude 'creds_orig.py' "$ROOT/app/" app/src/main/python/
cp pysrc/android_entry.py app/src/main/python/android_entry.py

# 页脚的 BUILD_TAG 是运行时常量，Chaquopy 直接编译这份源码副本，只能改这里。
TAG="${BUILD_TAG:-$(date +%Y-%m-%d.%H%M)}"
/usr/bin/sed -i '' -E \
  "s/^BUILD_TAG = os\.environ\.get\(\"BUILD_TAG\", \"[^\"]*\"\)/BUILD_TAG = os.environ.get(\"BUILD_TAG\", \"$TAG\")/" \
  app/src/main/python/checkin.py
grep -q "BUILD_TAG\", \"$TAG\"" app/src/main/python/checkin.py || {
  echo "BUILD_TAG 注入失败：app/src/main/python/checkin.py" >&2
  exit 1
}
echo "    BUILD_TAG：$TAG"

echo "==> 2/4 同步宠物图集"
mkdir -p app/src/main/assets/pets
rsync -a --delete "$ROOT/pets/" app/src/main/assets/pets/

echo "==> 3/4 签名密钥"
mkdir -p keystore
if [ ! -f keystore/wb-release.jks ]; then
  PASS="$(python3 -c "import secrets;print(secrets.token_urlsafe(18).replace(chr(45),chr(120)).replace(chr(95),chr(121)))")"
  "$JAVA_HOME/bin/keytool" -genkeypair \
    -keystore keystore/wb-release.jks -alias wb \
    -keyalg RSA -keysize 2048 -validity 10950 \
    -storepass "$PASS" -keypass "$PASS" \
    -dname "CN=WorkBuddy Board, OU=Personal, O=Personal, L=CN, C=CN" >/dev/null 2>&1
  cat > keystore/keystore.properties <<EOF
# 自签名密钥：升级覆盖安装必须同一签名，丢失只能卸载重装
storeFile=keystore/wb-release.jks
storePassword=$PASS
keyAlias=wb
keyPassword=$PASS
EOF
  chmod 600 keystore/keystore.properties keystore/wb-release.jks
  echo "    已生成新密钥，口令记在 keystore/keystore.properties"
else
  echo "    复用已有密钥"
fi

echo "==> 4/4 编译"
"$GRADLE" --no-daemon -p "$HERE" assembleRelease

OUT="$HERE/app/build/outputs/apk/release"
APK="$(ls -t "$OUT"/*.apk 2>/dev/null | head -1)"
if [ -z "$APK" ]; then
  echo "没找到 APK，编译可能失败" >&2
  exit 1
fi
cp "$APK" "$HERE/WorkBuddyBoard-android.apk"
echo
echo "APK: $HERE/WorkBuddyBoard-android.apk ($(du -h "$HERE/WorkBuddyBoard-android.apk" | cut -f1))"
