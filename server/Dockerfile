FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

# 镜像自带 chromium-1148（/ms-playwright），扫码登录直接用它，不需要额外装浏览器/node
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    STATE_DIR=/state \
    LOGIN_DIR=/tmp/wb-login \
    PET_DIR=/pets \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# 这个 tag 只带了浏览器二进制，Python 包要自己装；版本必须与 /ms-playwright 里的
# chromium-1148 对齐，否则 playwright 会去找不存在的浏览器目录。
# libzbar0 + pyzbar：二维码截图落盘前真实解码一次，解不出来就不给用户（防白块/裁残）
# 默认用国内镜像源；官方源：--build-arg PIP_INDEX=https://pypi.org/simple
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends libzbar0 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir playwright==1.49.0 pyzbar pillow -i "$PIP_INDEX"

COPY app/ /app/
# 保留原有 CDP 版扫码脚本，Playwright 版出问题时可用 node 版顶上（需自行装 node）
COPY tools/ /app/tools/

EXPOSE 8080
CMD ["python", "-u", "/app/checkin.py"]
