#!/bin/sh
# 跑一轮：先走 --harvest-only（只复用 profile 里还没过期的会话，不提交登录表单），
# 拿不到会话才真登录。
# 退出码沿用 sgcc_sidecar.py：0 成功 / 3 登录没完成 / 5 profile 里没有可用会话（含"会话看着还在、
# 但逐表一页都取不到数"）/ 7 登录成功却没取到数据 / 2 参数错。
set -u

PAGES="${SGCC_PAGES:-/electricityCharge?partNo=P02021703,/paymentRecord}"
PROFILE="${SGCC_PROFILE_DIR:-/data/profile}"
OUT="${SGCC_OUT:-/data/last.json}"
SESSION="${SGCC_SESSION_FILE:-/data/sgcc_session.json}"
LOG_DIR="${SGCC_LOG_DIR:-/data/logs}"
mkdir -p "$LOG_DIR" "$PROFILE" "$(dirname "$OUT")"
LOG="$LOG_DIR/$(date +%Y-%m-%d-%H%M).log"

say() { echo "[run_once] $*" | tee -a "$LOG"; }

# 标识从哪来：sgcc_sidecar 按「HA store 优先、环境变量兜底」解析，这里只取它的 stdout
# （日志都在 stderr，所以 $? 那两行就是主标识和备用标识）。密码不走这条路——
# store 里那份是 32 位 md5 摘要，填进登录框会被前端再哈希一次，只能继续用 SGCC_PASSWORD。
IDS="$(python /app/sgcc_sidecar.py --identifiers 2>/dev/null)"
ACCOUNT="$(printf '%s\n' "$IDS" | sed -n 1p)"
EMAIL="$(printf '%s\n' "$IDS" | sed -n 2p)"
[ -z "$ACCOUNT" ] && ACCOUNT="${SGCC_ACCOUNT:-}"
[ -z "$EMAIL" ] && EMAIL="${SGCC_EMAIL_ACCOUNT:-}"

# 1) 免登录：profile 里会话还活着就只花一次取数
say "先试免登录 harvest-only（pages=$PAGES）"
python /app/sgcc_sidecar.py --profile "$PROFILE" --harvest-only \
  --by-meter "$PAGES" --push --json-out "$OUT" --save-session "$SESSION" >>"$LOG" 2>&1
rc=$?
if [ "$rc" = 0 ]; then say "免登录这一轮成功"; exit 0; fi
if [ "$rc" != 5 ]; then say "harvest-only 返回 $rc，不是缺会话也照样往下走登录"; fi

# 2) 真登录：点选验证码交给集成的 LLM 解算器（--captcha llm），这是无人值守的前提
if [ -z "$ACCOUNT" ] || [ -z "${SGCC_PASSWORD:-}" ]; then
  say "没有可用主标识（store 与环境变量都没有）或缺 SGCC_PASSWORD，没法登录"; exit 2
fi
say "用主标识登录"
python /app/sgcc_sidecar.py --profile "$PROFILE" --account "$ACCOUNT" \
  --captcha llm --by-meter "$PAGES" --push --json-out "$OUT" --save-session "$SESSION" >>"$LOG" 2>&1
rc=$?
if [ "$rc" = 0 ]; then say "主标识这一轮成功"; exit 0; fi

# 3) 主标识没过去就换备用标识，只换一次：同一标识短时间内反复打会招来 RK001（实测过两次）
if [ "$rc" = 3 ] && [ -n "$EMAIL" ]; then
  say "主标识登录没完成（rc=3），改用备用邮箱一次"
  python /app/sgcc_sidecar.py --profile "$PROFILE" --account "$EMAIL" \
    --captcha llm --by-meter "$PAGES" --push --json-out "$OUT" --save-session "$SESSION" >>"$LOG" 2>&1
  rc=$?
  if [ "$rc" = 0 ]; then say "备用邮箱这一轮成功"; exit 0; fi
fi

say "这一轮失败 rc=$rc，日志见 $LOG"
exit "$rc"
