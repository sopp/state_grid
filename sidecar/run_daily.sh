#!/bin/sh
# 每天定点跑一轮；失败后隔一小时再补一次，然后等下一个定时点。
# 站点的风控是按短时尝试密度判的，所以这里刻意不写"失败了就立刻重试"。
set -u

RUN_AT="${SGCC_RUN_AT:-07:10}"
RETRY_AFTER="${SGCC_RETRY_MIN:-60}"

while : ; do
  /app/run_once.sh
  rc=$?
  if [ "$rc" != 0 ]; then
    echo "[run_daily] 这一轮 rc=$rc，${RETRY_AFTER} 分钟后补一次"
    sleep "$((RETRY_AFTER * 60))"
    /app/run_once.sh
    echo "[run_daily] 补跑 rc=$?"
  fi
  now=$(date +%s)
  today=$(date -d "today $RUN_AT" +%s)
  tomorrow=$(date -d "tomorrow $RUN_AT" +%s)
  target=$today
  [ "$now" -ge "$today" ] && target=$tomorrow
  echo "[run_daily] 下一轮 $RUN_AT（$(( (target - now) / 60 )) 分钟后）"
  sleep "$((target - now))"
done
