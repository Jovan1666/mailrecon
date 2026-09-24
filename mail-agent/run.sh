#!/bin/bash
# 每小时跑一轮：取信 → 对账 → AI 分析 → 推送。由 daemon.sh 调用。
# 手动执行也安全（有 flock 防并发）。
D=/root/mail-agent
LOG=$D/run.log
PY=$D/.venv/bin/python

# 单实例锁：定时跑和手动跑撞车时，后来的直接退出（不排队）
exec 9>"$D/.run.lock"
if ! flock -n 9; then
  echo "$(date '+%F %T') 已有实例在运行，跳过" >> "$LOG"
  exit 0
fi

{
  echo "===== $(date '+%F %T') ====="
  echo "--- 1/5 取新邮件 ---"
  "$PY" "$D/sync.py"
  echo "--- 2/5 对账 ---"
  "$PY" "$D/reconcile.py" > "$D/对账报告.txt" 2>&1
  echo "    报告已更新：$D/对账报告.txt"
  echo "--- 3/5 AI 分析 ---"
  "$PY" "$D/analyze.py"
  echo "--- 4/5 推行动项 ---"
  "$PY" "$D/push_actions.py"
  echo "--- 5/5 推回复/退信 ---"
  "$PY" "$D/notify.py"
  echo "--- 附：导出脱敏台账给 Hermes 读 ---"
  "$PY" "$D/export_for_hermes.py"
} >> "$LOG" 2>&1

# 日志只保留最近 1500 行
if [ -f "$LOG" ]; then
  tail -1500 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
fi
