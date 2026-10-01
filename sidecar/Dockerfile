FROM python:3.12-slim

# 站点把风控判定放在浏览器遥测上：同一套签名，HTTP 客户端走到 f06 就是 11401，真浏览器能过，
# 所以登录必须在浏览器里做，镜像里得带 chromium 本体。
# chromium 与 chromium-driver 由 apt 保证配对，selenium 4.1+ 直接用它，不需要 Selenium Manager 联网。
RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium chromium-driver fonts-liberation fonts-wqy-zenhei ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

# 依赖只留 requirements.txt 一处真相：sgcc_sidecar 顶层就 import captcha_solver.tencent，
# 那条链硬拉 numpy/cv2，少装一个容器会在导入期就炸（本机挡掉 numpy 实测过）。
COPY sidecar/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt "openai>=1.0.0" && rm /tmp/requirements.txt

WORKDIR /app
# 构建上下文是整个仓库：点选验证码的解算器是集成里那一份，这边直接复用，避免两处漂移
COPY sidecar/sgcc_sidecar.py sidecar/run_once.sh sidecar/run_daily.sh ./
COPY custom_components/state_grid/click_captcha_solver.py ./
COPY sidecar/captcha_solver/ ./captcha_solver/
RUN chmod +x /app/run_once.sh /app/run_daily.sh && mkdir -p /app/trace

# sgcc_sidecar 是按脚本所在目录去找集成里那份解算器的；容器里没有那棵树，
# 解算器被 COPY 到 /app，而 python 运行 /app/sgcc_sidecar.py 时本来就会把 /app 放进 sys.path
ENV PYTHONPATH=/app \
    SGCC_CHROME=/usr/bin/chromium \
    SGCC_DRIVER=/usr/bin/chromedriver \
    SGCC_BASE=https://95598.cn \
    TZ=Asia/Shanghai \
    SGCC_PROFILE_DIR=/data/profile \
    SGCC_OUT=/data/last.json

# 登录态、抓取结果、日志都落在 /data：容器可以随便重建，身份不能
VOLUME ["/data"]
CMD ["/app/run_daily.sh"]
