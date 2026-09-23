#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无副作用的「状态自动推断」。

铁律（和任务约束一致）：
  绝不主动去 GET 任务链接。只从「已经躺在邮箱里的后续邮件」和「deadline」推断。

置信度分级：
  AUTO    —— 机械事实，直接改库。目前只有一条：截止时间已过 → expired。
  PROPOSE —— 有证据但不能替用户拍板，进 suggestions.json，在看板上显示为
             「❓ 疑似已完成」，用户回「确认 3」才落库。
  NOTE    —— 只是提示（例如"对方又在催了"），不改状态、不影响计数。

推断规则清单见 RULES 常量，每条都写清了依据字段和置信度。
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/root/mail-agent")
from common import DB, match_key, sanitize  # noqa: E402

_DEFAULT_STATE_DIR = "/var/lib/mail-agent-interact"


def _safe_state_dir(raw):
    """状态目录允许用环境变量覆盖，但必须规范化、不含 ..，且落在允许的根下。"""
    if ".." in raw.replace("\\", "/").split("/"):
        raise SystemExit("TASK_CMD_STATE 不允许包含 ..：%r" % raw)
    d = os.path.realpath(os.path.abspath(raw))
    if not (d == _DEFAULT_STATE_DIR or d.startswith(_DEFAULT_STATE_DIR + "/")
            or d.startswith("/tmp/")):
        raise SystemExit("TASK_CMD_STATE 必须位于 %s 或 /tmp 下：%r"
                         % (_DEFAULT_STATE_DIR, raw))
    return d


STATE_DIR = _safe_state_dir(os.environ.get("TASK_CMD_STATE", _DEFAULT_STATE_DIR))
SUG_FILE = os.path.join(STATE_DIR, "suggestions.json")

# ---- 动作关键词：把任务标题归到某一类动作 ---------------------------------
ACTION_KINDS = [
    ("测评", ("测评", "测试")),
    ("笔试", ("笔试",)),
    ("面试", ("面试",)),
    ("简历", ("简历",)),
    ("邮箱验证", ("验证", "邮箱")),
    ("投递", ("投递", "申请", "重发", "退信")),
]

# ---- 后续邮件的语义模式 ---------------------------------------------------
RE_DONE = re.compile(
    r"感谢.{0,8}(完成|参加|参与|提交|作答|确认)|已(?:完成|提交|确认|收到您的)|"
    r"(?:测评|笔试|面试|简历).{0,6}已(?:完成|提交|结束)|提交成功|确认成功|"
    r"作答(?:已)?完成|感谢您的(?:参与|作答|配合)", re.I)
RE_NEXT = re.compile(
    r"邀请.{0,8}(?:参加|参与).{0,6}(?:面试|笔试|测评)|(?:面试|笔试|测评).{0,4}邀请|"
    r"面试通知|笔试通知|测评通知", re.I)
# 「进入下一环节」= 上一环节已经过了（这条对 测评/笔试/面试 都成立）
RE_ADVANCE = re.compile(
    r"进入.{0,6}(?:面试|下一|复面|终面)|下一(?:阶段|轮|环节)|复面|终面|"
    r"录用|offer|意向书|拟录用", re.I)
RE_REMIND = re.compile(
    r"提醒|再次|尚未|未完成|待完成|请尽快|尽快完成|倒计时|还剩|逾期|"
    r"邀请您(?:完成|更新)", re.I)
RE_REJECT = re.compile(
    r"不合适|未通过|未能通过|感谢您的关注|已招满|岗位已关闭|不再推进|"
    r"遗憾|婉拒|职位已下线", re.I)

# ---- 规则清单（给报告和 /帮助 用）-----------------------------------------
RULES = [
    ("expired", "AUTO", "due_utc 已过且状态仍是 todo → state=expired",
     "机械事实，无需用户确认（push_actions.py 里已有同样逻辑）"),
    ("followup_done", "PROPOSE", "同公司/同发件域名在任务邮件之后来的新邮件，"
     "主题命中「感谢完成/已提交/测评已完成」且动作类型匹配 → 建议标记完成",
     "证据强（对方系统回的完成确认），但可能对应的是同一家的另一件事，故只建议"),
    ("next_stage", "PROPOSE", "同公司后续来了「邀请参加面试/笔试/进入下一轮」"
     "→ 说明前一步（测评/笔试）已经通过",
     "中：跳级通知确实意味着前一步过了，但不排除对方流程不规范"),
    ("rejected", "PROPOSE", "同公司后续邮件命中「不合适/未通过/已招满」"
     "→ 这件事已经没有意义，建议忽略",
     "中：语义明确，但可能对应同公司另一个岗位"),
    ("still_reminding", "NOTE", "同公司后续邮件命中「提醒/请尽快/尚未完成」"
     "且动作类型相同 → 对方还在催，说明还没做",
     "仅提示，用来纠正「没消息=做完了」的错觉；不做任何状态变更"),
    ("no_signal", "—", "没有后续邮件", "不是证据。发件方不一定会回执，不能据此认为没做"),
]


def _kind_of(title):
    t = title or ""
    for name, kws in ACTION_KINDS:
        if any(k in t for k in kws):
            return name
    return "其他"


def _later_emails(db, key, after_iso):
    """同 match_key 的、比 after_iso 晚的收件箱邮件。只读。"""
    rows = db.execute(
        """SELECT e.id, e.subject, e.from_addr, e.internaldate
             FROM email e JOIN folder f ON f.id = e.folder_id
            WHERE f.role = 'inbox' AND e.fetched = 1
              AND e.internaldate > ?
            ORDER BY e.internaldate""", (after_iso or "",)).fetchall()
    out = []
    for r in rows:
        if match_key(r["from_addr"]) == key and key:
            out.append(r)
    return out


def suggest(db, now=None):
    """算出所有 PROPOSE / NOTE。返回 {"proposals": {...}, "notes": {...}}"""
    now = now or datetime.now(timezone.utc)
    db.row_factory = sqlite3.Row
    proposals, notes = {}, {}
    counters = {"expired_auto": 0}

    # ---------- AUTO：过期 ----------
    n = db.execute(
        "UPDATE tasks SET state='expired' WHERE state='todo' "
        "AND due_utc IS NOT NULL AND due_utc < ?",
        (now.isoformat(),)).rowcount
    if n:
        db.commit()
        counters["expired_auto"] = n

    # ---------- PROPOSE / NOTE ----------
    tasks = db.execute(
        """SELECT t.id, t.title, t.state, t.due_utc, t.email_id,
                  e.from_addr, e.internaldate, e.subject
             FROM tasks t LEFT JOIN email e ON e.id = t.email_id
            WHERE t.state IN ('todo','expired')""").fetchall()

    for t in tasks:
        key = match_key(t["from_addr"] or "")
        if not key or not t["internaldate"]:
            continue
        kind = _kind_of(t["title"])
        for m in _later_emails(db, key, t["internaldate"]):
            subj = m["subject"] or ""
            hit_kind = _kind_of(subj)
            same_kind = (hit_kind == kind) or hit_kind == "其他"

            if RE_REJECT.search(subj):
                proposals.setdefault(t["id"], []).append({
                    "kind": "rejected", "confidence": 0.6,
                    "reason": "收到「%s」（%s）" % (sanitize(subj, 40), sanitize(key, 20))})
                break
            if RE_DONE.search(subj) and same_kind:
                proposals.setdefault(t["id"], []).append({
                    "kind": "followup_done", "confidence": 0.8 if hit_kind == kind else 0.55,
                    "reason": "收到「%s」（%s）" % (sanitize(subj, 40), sanitize(key, 20))})
                break
            if RE_ADVANCE.search(subj):
                proposals.setdefault(t["id"], []).append({
                    "kind": "next_stage", "confidence": 0.5,
                    "reason": "对方已把你推进到下一环节：「%s」" % sanitize(subj, 40)})
                break
            if RE_NEXT.search(subj) and kind in ("测评", "笔试"):
                proposals.setdefault(t["id"], []).append({
                    "kind": "next_invite", "confidence": 0.5,
                    "reason": "同公司又发来新的%s：「%s」" % (kind, sanitize(subj, 40))})
                break
            if RE_REMIND.search(subj) and hit_kind == kind:
                notes.setdefault(t["id"], []).append(
                    "对方仍在催（%s）：%s" % (sanitize(m["internaldate"][:10], 10),
                                             sanitize(subj, 34)))

    _save(proposals, notes, counters, now)
    return {"proposals": proposals, "notes": notes, "auto": counters}


def _save(proposals, notes, counters, now):
    os.makedirs(STATE_DIR, exist_ok=True)
    data = {"generated_at": now.astimezone().isoformat(timespec="seconds"),
            "auto": counters,
            "proposals": {str(k): v for k, v in proposals.items()},
            "notes": {str(k): v for k, v in notes.items()}}
    tmp = SUG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SUG_FILE)


def load():
    try:
        with open(SUG_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {"proposals": {}, "notes": {}, "auto": {}}
    d.setdefault("proposals", {})
    d.setdefault("notes", {})
    d.setdefault("auto", {})
    return d


def main():
    db = sqlite3.connect(DB)
    r = suggest(db)
    print("AUTO 过期: %d 条" % r["auto"].get("expired_auto", 0))
    if not r["proposals"]:
        print("没有需要用户确认的建议")
    for tid, items in sorted(r["proposals"].items()):
        for it in items:
            print("  ❓ #%s [%s conf=%.2f] %s" % (tid, it["kind"], it["confidence"], it["reason"]))
    for tid, items in sorted(r["notes"].items()):
        for n in items:
            print("  ℹ️  #%s %s" % (tid, n))
    db.close()


if __name__ == "__main__":
    main()
