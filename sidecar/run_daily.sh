#!/bin/sh
# 每天定点跑一轮；失败后隔一小时再补一次，然后等下一个定时点。
# 站点的风控是按短时尝试密度判的，所以这里刻意不写"失败了就立刻重试"。
set -u

RUN_AT="${SGCC_RUN_AT:-07:10}"
RETRY_AFTER="${SGCC_RETRY_MIN:-60}"
# 部署当天先不要立刻跑：新 profile 里没有可用会话，run_once 必然走到真登录那一支，
# 别在没人盯着的时候花掉一发额度
RUN_ON_START="${SGCC_RUN_ON_START:-1}"

RUN_ONCE="${SGCC_RUN_ONCE:-/app/run_once.sh}"
first="$RUN_ON_START"
while : ; do
  if [ "$first" = 1 ]; then
    first=0
  else
    now=$(date +%s)
    today=$(date -d "today $RUN_AT" +%s)
    tomorrow=$(date -d "tomorrow $RUN_AT" +%s)
    target=$today
    [ "$now" -ge "$today" ] && target=$tomorrow
    echo "[run_daily] 下一轮 $RUN_AT（$(( (target - now) / 60 )) 分钟后）"
    sleep "$((target - now))"
  fi
  "$RUN_ONCE"
  rc=$?
  if [ "$rc" != 0 ]; then
    echo "[run_daily] 这一轮 rc=$rc，${RETRY_AFTER} 分钟后补一次"
    sleep "$((RETRY_AFTER * 60))"
    "$RUN_ONCE"
    echo "[run_daily] 补跑 rc=$?"
  fi
done

