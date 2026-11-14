#!/usr/bin/env python3
"""把目录打成 zip，条目路径统一用正斜杠。

Compress-Archive 走 .NET Framework 的 ZipFile，条目写反斜杠，unzip / macOS /
Linux 解压会告警并产生带反斜杠的文件名。zip 规范（APPNOTE 4.4.17.1）要求正斜杠。

用法：
    python tools/zipdir.py <源目录> <目标zip>
    python tools/zipdir.py dist/WorkBuddyBoard out/WorkBuddyBoard-win-x64-v8.zip
"""

import os
import sys
import zipfile


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    src = os.path.abspath(sys.argv[1])
    dst = os.path.abspath(sys.argv[2])

    if not os.path.isdir(src):
        print(f"源目录不存在: {src}", file=sys.stderr)
        return 1

    # zip 里保留一层顶层目录名，解压后是一个文件夹而不是撒一地文件
    base = os.path.basename(src.rstrip("\\/"))

    if os.path.exists(dst):
        os.remove(dst)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)

    count = 0
    skipped = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, dirs, files in os.walk(src):
            # 编译缓存不进包：既占体积，又会让「比 md5 核版本」这类校验失真
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                if name.endswith((".pyc", ".pyo")) or name == ".DS_Store":
                    skipped += 1
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, src)
                arc = base + "/" + rel.replace(os.sep, "/")
                z.write(full, arc)
                count += 1

    size = os.path.getsize(dst)
    print(f"  {count} 个文件 -> {os.path.basename(dst)}  {size:,} B"
          + (f"（跳过 {skipped} 个缓存文件）" if skipped else ""))

    # 自检：任何一个条目含反斜杠就说明平台又把分隔符换了
    with zipfile.ZipFile(dst) as z:
        bad = [n for n in z.namelist() if "\\" in n]
    if bad:
        print(f"  X 有条目用了反斜杠，前 3 个: {bad[:3]}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
