#!/bin/bash
# 把 .app 打成 dmg：内含 Applications 快捷方式，压缩格式 UDZO
# 用法：tools/make_dmg.sh <app 路径> <输出 dmg 路径> [版本] [架构标签]
set -euo pipefail

APP="${1:?app 路径}"
OUT="${2:?输出 dmg 路径}"
VER="${3:-}"
LABEL="${4:-}"

[ -d "$APP" ] || { echo "找不到 $APP" >&2; exit 1; }
NAME=$(basename "$APP" .app)
VOL="$NAME${VER:+ $VER}${LABEL:+ $LABEL}"

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

ditto "$APP" "$STAGE/$NAME.app"
ln -s /Applications "$STAGE/Applications"

rm -f "$OUT"
mkdir -p "$(dirname "$OUT")"
hdiutil create -quiet -volname "$VOL" -srcfolder "$STAGE" -fs HFS+ \
  -format UDZO -imagekey zlib-level=9 -ov "$OUT"

echo "==> $OUT  ($(du -h "$OUT" | cut -f1))  卷名：$VOL"
