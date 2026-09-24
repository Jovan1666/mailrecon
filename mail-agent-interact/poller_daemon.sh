#!/bin/bash
# 指令泵常驻启动器（root 身份跑）。
#
# 为什么不用 cron：这个 WSL 环境里 cron 不会触发（/root/mail-agent/daemon.sh 里
# 也写了同样的结论）。所以用常驻循环，和现有 daemon.sh 一个路子。
#
# 代码正式位置：/opt/mail-agent-interact/（持久）
# 同份代码也在 /tmp/interactwork/（需求要求，重启会丢）
D=/opt/mail-agent-interact
PIDF=/var/lib/mail-agent-interact/poller.pid
LOG=/var/lib/mail-agent-interact/poller_stdout.log
PY=/usr/bin/python3

mkdir -p /var/lib/mail-agent-interact
chmod 700 /var/lib/mail-agent-interact

if [ -f "$PIDF" ]; then
  old=$(cat "$PIDF" 2>/dev/null)
  if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
    echo "指令泵已在跑 (pid=$old)，本次跳过"
    exit 0
  fi
fi

cd "$D" || exit 1
setsid "$PY" "$D/poller.py" --daemon >> "$LOG" 2>&1 < /dev/null &
new=$!
echo "$new" > "$PIDF"
sleep 3
if kill -0 "$new" 2>/dev/null; then
  echo "指令泵已启动 pid=$new"
  tail -5 "$LOG" 2>/dev/null
else
  echo "启动失败，看 $LOG"
  tail -20 "$LOG" 2>/dev/null
  exit 1
fi
