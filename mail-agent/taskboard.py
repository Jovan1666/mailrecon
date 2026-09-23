#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务台账：查看全貌 / 标记完成。

用法：
  python taskboard.py            把台账以卡片推到飞书
  python taskboard.py --text     打到终端（不推飞书）
  python taskboard.py done 3     把编号 3 标记为已完成
  python taskboard.py drop 3     把编号 3 标记为已忽略

每条待办和看板卡(board.py)、推送卡(push_actions.py)长得一样：
标题（有链接则内联）／发生了什么（situation，有才显示）／去哪做 + 截止 + 链接情况。
"""
import re
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/root/mail-agent")
from common import (DB, CST, ensure_task_cols, sanitize,  # noqa: E402
                    task_lines)
import feishu

STATE_LABEL = {"todo": "进行中", "done": "已完成", "expired": "已过期", "dismissed": "已忽略"}
UNSAFE = re.compile(r"[\n\r`$|;&<>{}]|MEDIA:|\.\./|(^|\s)/|~")


def clean(s, n=60):
    return re.sub(r"\s{2,}", " ", UNSAFE.sub("", sanitize(s or "", n))).strip()


def fmt_due(iso):
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone(CST).strftime("%m-%d %H:%M")
    except Exception:
        return ""


def _due_dt(iso):
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def done_action(tid):
    """「✅ 完成」按钮。

    飞书卡片按钮：带 url 是跳转，带 value 才是回调 —— 这里只做回调，
    点击后由 ~/.hermes/plugins/task-cmd-bridge 翻译成「完成 <编号>」，
    走和文字指令完全相同的一条链路（解析 → 落库 → 刷新看板 → 撤销按钮）。
    """
    return {"tag": "action", "actions": [{
        "tag": "button", "type": "default",
        "text": {"tag": "plain_text", "content": "✅ 完成"},
        "value": {"k": "done", "id": tid}}]}


def load(db):
    db.row_factory = sqlite3.Row
    ensure_task_cols(db)          # 幂等补列：谁先跑谁建，不让台账卡崩在 no such column
    return db.execute(
        """SELECT id, title, due_utc, link, link_host, link_short, credential,
                  state, done_at, done_source, org, situation, where_hint
             FROM tasks ORDER BY
             CASE state WHEN 'todo' THEN 0 WHEN 'expired' THEN 1
                        WHEN 'done' THEN 2 ELSE 3 END,
             due_utc IS NULL, due_utc""").fetchall()


def _g(r, key):
    """取一列，没有就返回 ""（老行/手搓的行不该让整张卡崩）。"""
    try:
        return r[key] or ""
    except (IndexError, KeyError):
        return ""


def render(rows):
    """左：飞书卡片；右：终端文本。一行一条，链接内联。"""
    today = datetime.now(CST).date()
    buckets = {"today": [], "later": [], "none": [], "expired": [], "done": [], "dismissed": []}
    for r in rows:
        if r["state"] == "todo":
            d = _due_dt(r["due_utc"])
            if d and d.astimezone(CST).date() == today:
                buckets["today"].append(r)
            elif d:
                buckets["later"].append(r)
            else:
                buckets["none"].append(r)
        else:
            buckets[r["state"]].append(r)

    n_open = len(buckets["today"]) + len(buckets["later"]) + len(buckets["none"])
    elements = []
    txt = ["📋 求职任务台账 · %d 条待办 · %d 已完成" % (n_open, len(buckets["done"]))]

    def block(title, items, key):
        if not items:
            return
        if elements:
            elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                "content": "**%s**（%d）" % (title, len(items))}})
        for r in items:
            act = clean(r["title"], 50)
            due = fmt_due(r["due_utc"])
            sit = ""
            if r["state"] == "done":
                # 已完成的行保持一行：事情办完了，"什么情况/去哪做"不再有行动价值，
                # 展开只会把卡撑长（用户抱怨过"每条占 4 行太挤"）。
                line = "**%s** ~~%s~~" % (r["id"], act)
                if due:
                    line += "　`%s`" % due
                if r["done_source"]:
                    line += "　✅%s" % sanitize(r["done_source"], 10)
            else:
                # 待办和已过期都做内联链接（已过期也常有"晚点补做/去核对"的需求；
                # 之前只给 todo 做链接，已过期那组的链接就点不到）。
                # 排版规则来自 common.task_lines —— 看板/推送/台账三张卡共用同一份。
                head, sit, meta = task_lines(
                    act, r["link"], r["link_short"], org=_g(r, "org"),
                    situation=_g(r, "situation"), where=_g(r, "where_hint"),
                    due_txt=("⏰ %s" % due) if due else "", host=r["link_host"],
                    limit=50)
                line = "**%s** %s" % (r["id"], head)
                if sit:
                    line += "\n　%s" % sit
                line += "\n　%s" % meta
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": line}})
            # 一键完成：待办和已过期两类都能点（已过期也常有"其实我做了"的情况）
            if r["state"] in ("todo", "expired"):
                elements.append(done_action(r["id"]))
            txt.append("  [%s] #%s %s  截止 %s"
                       % (STATE_LABEL.get(r["state"], r["state"]), r["id"], act, due or "—"))
            if sit:
                txt.append("        %s" % sit)

    block("🔥 今天截止", buckets["today"], "today")
    block("📌 之后", buckets["later"], "later")
    block("⚪ 没有截止时间", buckets["none"], "none")
    block("⌛ 已过期（确认一下是否还有效）", buckets["expired"], "expired")
    block("✅ 已完成", buckets["done"][-8:], "done")
    block("🚫 已忽略", buckets["dismissed"][:5], "dismissed")

    if not elements:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "暂无任务"}})

    creds = [(r["id"], sanitize(r["credential"], 40)) for r in rows
             if r["credential"] and r["state"] == "todo"]
    if creds:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": "**🔑 登录凭据**\n" + "\n".join("#%s %s" % c for c in creds[:5])}})

    elements.append({"tag": "note", "elements": [{"tag": "plain_text",
        "content": "回复「完成 编号」标记 · 「放弃 编号」忽略 · 「状态」重发本卡"}]})

    header = "📋 求职任务 · %d 待办" % n_open
    if buckets["today"]:
        header += "　🔥 今天 %d" % len(buckets["today"])
    card = {"config": {"wide_screen_mode": True},
            "header": {"template": "red" if buckets["today"] else "turquoise",
                       "title": {"tag": "plain_text", "content": header}},
            "elements": elements}
    return card, "\n".join(txt)


def main():
    db = sqlite3.connect(DB)
    args = list(sys.argv[1:])

    if args and args[0] in ("done", "drop"):
        if len(args) < 2 or not args[1].isdigit():
            print("用法: taskboard.py done <编号>")
            db.close()
            return
        tid = int(args[1])
        state = "done" if args[0] == "done" else "dismissed"
        n = db.execute("UPDATE tasks SET state=?, done_at=?, done_source=? WHERE id=?",
                       (state, datetime.now(CST).isoformat(timespec="seconds"),
                        "手动标记", tid)).rowcount
        db.commit()
        print("已把 #%d 标记为 %s" % (tid, STATE_LABEL.get(state)) if n else "没找到 #%d" % tid)
        db.close()
        return

    rows = load(db)
    if not rows:
        print("台账是空的")
        db.close()
        return
    card, text = render(rows)

    if "--text" in args:
        print(text)
    else:
        ok, msg = feishu.send_card(card)
        print("台账已推送: %s" % ("成功" if ok else "失败 " + msg))
        print()
        print(text)
    db.close()


if __name__ == "__main__":
    main()
