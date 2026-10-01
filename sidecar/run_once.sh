#!/bin/sh
# 跑一轮：先复用浏览器里还没过期的会话（不消耗登录额度），不行才真登录。
# 退出码沿用 sgcc_sidecar.py：0 成功 / 3 登录没完成 / 5 profile 里没有可用会话 / 2 参数错。
set -u

PAGES="${SGCC_PAGES:-/electricityCharge?partNo=P02021703,/paymentRecord}"
PROFILE="${SGCC_PROFILE_DIR:-/data/profile}"
OUT="${SGCC_OUT:-/data/last.json}"
SESSION="${SGCC_SESSION_FILE:-/data/sgcc_session.json}"
LOG_DIR="${SGCC_LOG_DIR:-/data/logs}"
mkdir -p "$LOG_DIR" "$PROFILE" "$(dirname "$OUT")"
LOG="$LOG_DIR/$(date +%Y-%m-%d-%H%M).log"

say() { echo "[run_once] $*" | tee -a "$LOG"; }

# 1) 免登录：profile 里会话还活着就只花一次取数
say "先试免登录 harvest-only（pages=$PAGES）"
python /app/sgcc_sidecar.py --profile "$PROFILE" --harvest-only \
  --by-meter "$PAGES" --push --json-out "$OUT" --save-session "$SESSION" >>"$LOG" 2>&1
rc=$?
if [ "$rc" = 0 ]; then say "免登录这一轮成功"; exit 0; fi
if [ "$rc" != 5 ]; then say "harvest-only 返回 $rc，不是缺会话也照样往下走登录"; fi

# 2) 真登录：点选验证码交给集成的 LLM 解算器（--captcha llm），这是无人值守的前提
if [ -z "${SGCC_ACCOUNT:-}" ] || [ -z "${SGCC_PASSWORD:-}" ]; then
  say "缺 SGCC_ACCOUNT 或 SGCC_PASSWORD，没法登录"; exit 2
fi
say "用主标识登录"
python /app/sgcc_sidecar.py --profile "$PROFILE" --account "$SGCC_ACCOUNT" \
  --captcha llm --by-meter "$PAGES" --push --json-out "$OUT" --save-session "$SESSION" >>"$LOG" 2>&1
rc=$?
if [ "$rc" = 0 ]; then say "主标识这一轮成功"; exit 0; fi

# 3) 主标识没过去就换备用标识，只换一次：同一标识短时间内连打第 4 次历史上必吃 RK001
if [ "$rc" = 3 ] && [ -n "${SGCC_EMAIL_ACCOUNT:-}" ]; then
  say "主标识登录没完成（rc=3），改用备用邮箱一次"
  python /app/sgcc_sidecar.py --profile "$PROFILE" --account "$SGCC_EMAIL_ACCOUNT" \
    --captcha llm --by-meter "$PAGES" --push --json-out "$OUT" --save-session "$SESSION" >>"$LOG" 2>&1
  rc=$?
  if [ "$rc" = 0 ]; then say "备用邮箱这一轮成功"; exit 0; fi
fi

say "这一轮失败 rc=$rc，日志见 $LOG"
exit "$rc"
