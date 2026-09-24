#!/bin/bash
# 邮件检查守护进程。
#
# 为什么不用 cron：实测这个 WSL 环境里的 cron 根本不会触发（进程活着、
# crontab 也配对，但整点任务和每分钟探针都不执行）。改用常驻循环。
#
# 行为：启动后立即跑一轮（补上关机期间漏掉的），然后每小时整点跑一次，
#       只在 8:00–20:00 之间实际执行。
D=/root/mail-agent
LOG=$D/daemon.log
PIDF=$D/daemon.pid

# 自我守护：已有实例在跑就直接退出（开机自启可能在多次登录时被重复调用）
if [ -f "$PIDF" ]; then
  old=$(cat "$PIDF" 2>/dev/null)
  if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
    echo "$(date '+%F %T') 已有守护进程在跑 (pid=$old)，本次启动跳过" >> "$LOG"
    exit 0
  fi
fi

echo $$ > "$PIDF"
echo "$(date '+%F %T') daemon start pid=$$" >> "$LOG"

while true; do
  h=$(date +%H)
  if [ "$h" -ge 8 ] && [ "$h" -lt 20 ]; then
    # 自检：run.sh 丢了可执行位的话，bash 只会往本进程的 stderr 吐一行
    # "Permission denied"，不会进任何日志——daemon 活着、日志安静、看起来
    # 一切正常，实际上一轮都没跑。已经这样静默停摆过两个半小时。
    if [ ! -x "$D/run.sh" ]; then
      echo "$(date '+%F %T') ⚠️ run.sh 不可执行（权限 $(stat -c %a "$D/run.sh" 2>/dev/null)），已自动修复" >> "$LOG"
      chmod +x "$D/run.sh" 2>/dev/null
    fi
    out=$("$D/run.sh" 2>&1)
    rc=$?
    if [ "$rc" -ne 0 ]; then
      echo "$(date '+%F %T') ⚠️ run.sh 退出码 $rc：$(printf '%s' "$out" | head -5 | tr '\n' ' ')" >> "$LOG"
    fi
  else
    echo "$(date '+%F %T') 非工作时段（$h 点），跳过" >> "$LOG"
  fi

  # 睡到下一个整点之后（+10 秒，确保跨过小时边界）
  now=$(date +%s)
  sleep $(( (now / 3600 + 1) * 3600 - now + 10 ))

  # 日志只留最近 500 行
  if [ -f "$LOG" ]; then
    tail -500 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
  fi
done
