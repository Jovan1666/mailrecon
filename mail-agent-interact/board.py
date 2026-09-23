#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""求职任务看板：一张卡片，原地更新，永远只有一份。

每条待办说清三件事（用户原话："我要知道要做什么 要去哪里做 有没有跳转链接"）：
  - 要做什么 → 标题（有可用链接就内联链接，点标题直接跳）
  - 为什么有这件事 → situation（"你投给 campus@xxx 的邮件没送到：该地址不存在"），
    只在有内容时出现，空着就不占行
  - 去哪做 + 截止 + 链接情况 → 元信息行（📍 去哪 · ⏰ 截止 · 🔗 域名/没给链接）
  主体（公司名/域名）在标题/情境里没出现过时以 @org 挂在标题后 —— 用户抱怨过
  "我还以为是哪个公司让我重发简历"。

状态可见性设计（对着"哪些做了哪些没做也不直观"来的）：
  - 顶部一行进度条 + 计数：一眼看到做了几件 / 还剩几件。
  - 分组：🔥 24h内截止 → 📌 进行中 → ⌛ 已过期 → ✅ 今天完成 → ⚪ 更早完成。
  - 已完成用删除线 ~~~~ + ✅ + 完成时间 + 来源（手动/推断），留在卡里给成就感，
    但只展开最近 5 条，更早的只报数量，卡片不会无限长。
  - 已忽略只报数量，不展开。
  - 卡片头部颜色随紧急度变：有 24h 内截止 = 红，有 72h 内 = 橙，否则青。
  - 每条待办/已过期后面挂一个「✅ 完成」按钮（回调型，只带 value 不带 url）。
  - 底部固定一行操作说明（用户不用记文档）。
  - 同一张卡用 PATCH 原地刷新，不新发消息、不刷屏；用户回「状态」才新发一张。
    刷新时会连带最近几张历史看板一起刷新 —— 用户可能翻上去点旧卡上的按钮，
    只刷最新那张的话，旧卡会"停在旧内容上"。

用法：
  python board.py                 # 发一张新看板（并把旧看板作废）
  python board.py --update        # 原地刷新当前看板
  python board.py --text          # 只打终端
  python board.py --digest        # 早报模式：只列今天/48h 内截止
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/root/mail-agent")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (CST, DB, ensure_task_cols,  # noqa: E402
                    link_ok, sanitize, task_body, task_lines, title_md)
import fsapi  # noqa: E402
import cmds  # noqa: E402
import infer  # noqa: E402

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
BOARD_FILE = os.path.join(STATE_DIR, "board.json")
MAX_DONE_SHOWN = 5
BOARD_KEEP = 5           # 同时刷新最近这么多张看板（含最新那张）

URGENT = [("danger", "⚠️"), ("warning", "⏳")]


# ---------------------------------------------------------------- 数据

def load(db):
    db.row_factory = sqlite3.Row
    # situation / where_hint 是后加的列（幂等 ALTER，见 common.ensure_task_cols）——
    # 谁先跑谁建，不让看板死在 "no such column" 上。
    ensure_task_cols(db)
    return db.execute(
        """SELECT id, title, due_utc, link, link_host, link_short, org,
                  state, done_at, done_source, note, situation, where_hint
             FROM tasks ORDER BY id""").fetchall()


def _due_dt(iso):
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def fmt_due(dt):
    if not dt:
        return ""
    return dt.astimezone(CST).strftime("%m-%d %H:%M")


def fmt_done(iso):
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone(CST).strftime("%m-%d %H:%M")
    except Exception:
        return ""


def bucket(rows, now):
    """把行分成 5 组 + 计数。"""
    g = {"today": [], "open": [], "expired": [], "done_today": [], "done_old": []}
    done_old_n = 0
    # 「今天」必须按北京时间算 —— 之前用 now.date()（UTC）判断，
    # 00:00–08:00 CST 这段里刚点完完成的任务会被错分到「更早完成」。
    today = now.astimezone(CST).date() if now.tzinfo else now.date()
    for r in rows:
        st = r["state"]
        if st in ("todo", "expired"):
            dt = _due_dt(r["due_utc"])
            if st == "expired" or (dt and dt < now):
                g["expired"].append((r, dt))
            elif dt and dt <= now + timedelta(hours=24):
                g["today"].append((r, dt))
            else:
                g["open"].append((r, dt))
        elif st == "done":
            dd = _due_dt(r["done_at"])
            if dd and dd.astimezone(CST).date() == today:
                g["done_today"].append((r, dd))
            else:
                g["done_old"].append((r, dd))
                done_old_n += 1
    # 排序：有截止的在前按时间升序，没有截止的在后
    for k in ("today", "open", "expired", "done_today", "done_old"):
        g[k].sort(key=lambda p: (p[1] is None, p[1] or now, p[0]["id"]))
    return g, done_old_n


def _g(r, key):
    """取一行里的一列，没有这一列就返回 ""（老行/手搓的行不该让整张卡崩）。"""
    try:
        return r[key] or ""
    except (IndexError, KeyError):
        return ""


def title_of(r):
    return sanitize(r["title"], 56)


def link_of(r):
    """标题（有可用链接就内联）。

    规则收到 common.link_ok / title_md 里了 —— 只有 http/https 才做成可点链接
    （邮件正文是外部输入，可能夹带 javascript: / data: 伪协议），短链不做链接。
    """
    return title_md(title_of(r), r["link"], r["link_short"])


def line_kwargs(r, dt, states=None, link_info=True):
    """一行 tasks → common.task_lines 的参数（看板/台账用同一套键）。

    「有没有跳转链接」交给 common.link_meta 统一回答：
    没链接的任务也明说一句"邮件里没给链接"，用户不用猜是不是卡片没显示出来。
    """
    extras = []
    if r["done_source"]:
        extras.append(sanitize(r["done_source"], 10))
    if r["state"] == "expired":
        extras.append("❗待确认是否还有效")
    if states is not None:
        due_txt = ("⏰ %s" % fmt_due(dt)) if dt else "📭 邮件里没写截止时间"
    else:
        due_txt = ("✅ %s 完成" % fmt_done(r["done_at"])) if dt else ""
    return dict(link=r["link"], link_short=r["link_short"], org=_g(r, "org"),
                situation=_g(r, "situation"), where=_g(r, "where_hint"),
                due_txt=due_txt, host=r["link_host"], extras=extras,
                link_info=link_info)


def meta_of(r, dt, states=None):
    """兼容旧调用：只取元信息行（换成 common.task_lines 之前的形态）。"""
    return task_lines(title_of(r), **line_kwargs(r, dt, states))[2]


def progress_bar(done, total, width=8):
    if not total:
        return "▰" * width
    filled = int(round(width * done / float(total)))
    return "▰" * filled + "▱" * (width - filled)


def done_action(tid):
    """「✅ 完成」按钮。

    飞书卡片按钮：带 url 是跳转，带 value 才是回调 —— 这里只做回调。
    点击后由 ~/.hermes/plugins/task-cmd-bridge 翻成「完成 <编号>」，走和文字指令
    完全一样的链路：落库 → 本看板被 PATCH 原地刷新（该条变删除线、进度数字变化）
    → 回一张带「↩️ 撤销」的确认卡。

    编号用的是 tasks.id（不可变），所以刷新后重新渲染出来的 value 不会"过期"；
    已经完成的任务不再渲染按钮，不会留下点了没用的僵尸按钮。
    （同样的按钮在 /root/mail-agent/push_actions.py 和 taskboard.py 里各有一份，
      三处保持一致。）
    """
    return {"tag": "action", "actions": [{
        "tag": "button", "type": "default",
        "text": {"tag": "plain_text", "content": "✅ 完成"},
        "value": {"k": "done", "id": tid}}]}


# ---------------------------------------------------------------- 渲染

def render(rows, now=None, digest=False, sugg=None):
    now = now or datetime.now(timezone.utc)
    g, done_old_n = bucket(rows, now)
    sugg = sugg if sugg is not None else infer.load()
    props = sugg.get("proposals", {})
    notes = sugg.get("notes", {})
    n_done = sum(1 for r in rows if r["state"] == "done")
    n_open = sum(1 for r in rows if r["state"] in ("todo", "expired"))
    n_dismissed = sum(1 for r in rows if r["state"] == "dismissed")
    urgent = len(g["today"])
    near = urgent + sum(1 for r, dt in g["open"] if dt and dt <= now + timedelta(hours=72))

    if digest:
        head = "☀️ 今天要做的事"
        tmpl = "blue"
    else:
        head = "📋 求职任务 · 已完成 %d/%d" % (n_done, n_done + n_open)
        tmpl = "red" if urgent else ("orange" if near else "turquoise")

    el = []
    bar = progress_bar(n_done, n_done + n_open)
    chips = ["%s %d 待办" % (bar, n_open)]
    if urgent:
        chips.append("🔥 24h 内 %d" % urgent)
    if g["expired"]:
        chips.append("⌛ 过期 %d" % len(g["expired"]))
    if n_done:
        chips.append("✅ 完成 %d" % n_done)
    el.append({"tag": "div", "text": {"tag": "lark_md",
                                      "content": "　".join(chips)}})
    el.append({"tag": "hr"})

    def block(name, emoji, items, done_style=False, note=None):
        if not items:
            return
        el.append({"tag": "div", "text": {"tag": "lark_md",
                                          "content": "**%s %s（%d）**" % (emoji, name, len(items))}})
        for r, dt in items:
            if done_style:
                # 已完成的行保持一行：事情办完了，"去哪做/什么情况"不再有行动价值，
                # 展开只会把卡撑长（用户抱怨过"每条占 4 行太挤"）。
                head, _sit, meta = task_lines(title_of(r), **line_kwargs(r, dt, link_info=False))
                body = "~~%d %s~~\n　%s" % (r["id"], head, meta)
            else:
                # 待办/已过期：标题 + （有情境时）为什么 + 去哪做/截止/链接，同一个 div 内换行
                body = task_body(r["id"], title_of(r), **line_kwargs(r, dt, states=1))
            el.append({"tag": "div", "text": {"tag": "lark_md", "content": body}})
            if not done_style:
                # 待办 / 已过期 都能一键完成；已完成的不给按钮
                el.append(done_action(r["id"]))
        if note:
            el.append({"tag": "note", "elements": [{"tag": "plain_text", "content": note}]})

    block("24 小时内截止", "🔥", g["today"])
    block("进行中", "📌", [] if digest else g["open"])
    if digest:
        soon = [(r, dt) for r, dt in g["open"]
                if dt and dt <= now + timedelta(hours=48)]
        block("48 小时内截止", "⏳", soon)
    block("已过期（确认一下还有没有效）", "⌛", g["expired"])

    # ❓ 推断建议：只提示，用户回「确认 N / 驳回 N」才落库
    if props and not digest:
        el.append({"tag": "div", "text": {"tag": "lark_md",
                                          "content": "**❓ 疑似已完成，等你确认（%d）**" % len(props)}})
        by_id = {str(r["id"]): r for r in rows}
        for tid, items in sorted(props.items(), key=lambda kv: int(kv[0])):
            r = by_id.get(str(tid))
            if not r:
                continue
            best = max(items, key=lambda x: x.get("confidence", 0))
            el.append({"tag": "div", "text": {"tag": "lark_md", "content":
                       "**%s %s**\n　依据：%s\n　回复「确认 %s」或「驳回 %s」"
                       % (tid, title_of(r), sanitize(best["reason"], 70), tid, tid)}})

    if notes and not digest:
        flat = []
        by_id = {str(r["id"]): r for r in rows}
        for tid, items in sorted(notes.items(), key=lambda kv: int(kv[0])):
            if str(tid) in props:
                continue
            if by_id.get(str(tid)):
                flat.append("#%s %s" % (tid, sanitize(items[0], 60)))
        if flat:
            el.append({"tag": "note", "elements": [
                {"tag": "plain_text", "content": "ℹ️ " + "　|　".join(flat[:3])}]})

    if not digest:
        block("今天完成", "✅", g["done_today"], done_style=True)
        block("更早完成", "⚪", g["done_old"][-MAX_DONE_SHOWN:],
              done_style=True,
              note=("另有 %d 条更早完成的已折叠" % (done_old_n - MAX_DONE_SHOWN)
                    if done_old_n > MAX_DONE_SHOWN else None))

    if not any([g["today"], g["open"], g["expired"], g["done_today"], g["done_old"]]):
        el.append({"tag": "div", "text": {"tag": "lark_md", "content": "暂时没有任务 🎉"}})

    # 最紧急的几条给"去做"按钮（短链不给按钮：信任转移风险）
    # 必须先过滤再切前几条 —— 反过来写的话，前两条里只要有一条没链接，
    # 后面真正有链接的任务就永远轮不到按钮（这个顺序错误用户已经踩过一次）
    # 判定走 common.link_ok：和标题内联链接、推送卡、台账卡同一套 http/https 规则
    btns = [r for r, dt in g["today"] + g["open"]
            if link_ok(r["link"], r["link_short"])
            and dt and dt <= now + timedelta(days=7)][:2]
    if btns and not digest:
        el.append({"tag": "action", "actions": [
            {"tag": "button", "type": "primary",
             "text": {"tag": "plain_text", "content": "去做 #%d" % r["id"]},
             "url": link_ok(r["link"], r["link_short"])} for r in btns]})

    foot = ("点每条任务下面的「✅ 完成」即可标记 · 也能回「完成 3」· "
            "「放弃 3」忽略 · 「确认 3」采纳建议 · 「状态」重发本卡"
            if not digest else "点「✅ 完成」即可标记，也可以回「完成 编号」")
    if n_dismissed:
        foot += "　（已忽略 %d 条）" % n_dismissed
    el.append({"tag": "note", "elements": [{"tag": "plain_text", "content": foot}]})

    card = {"config": {"wide_screen_mode": True},
            "header": {"template": tmpl,
                       "title": {"tag": "plain_text", "content": head}},
            "elements": el}
    return card


def digest_text(rows):
    """早报的纯文本版（终端/日志用）。"""
    now = datetime.now(timezone.utc)
    g, _ = bucket(rows, now)
    out = ["今天/24h 内截止："]
    for r, dt in g["today"]:
        out.append("  #%d %s  截止 %s" % (r["id"], title_of(r), fmt_due(dt)))
    if len(out) == 1:
        out.append("  （无）")
    return "\n".join(out)


# ---------------------------------------------------------------- 状态文件

def _read_board():
    try:
        with open(BOARD_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_board(d):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = BOARD_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, BOARD_FILE)


def _board_targets(st):
    """要刷新的看板卡片 id：最新那张 + 最近几张历史看板。

    为什么要带上历史看板：看板是原地刷新的持久卡片，但「状态」会再发一张新的，
    老的还留在聊天记录里、按钮也还在。用户很可能翻上去就点那张。
    只刷最新那张的话，用户点完会看到"我点的那张没变"，体验就断了。
    """
    ids = []
    for m in ([st.get("message_id")] + list(st.get("recent") or [])):
        if m and m not in ids:
            ids.append(m)
    return ids[:BOARD_KEEP]


def publish(db, rows=None, new=False, tok=None):
    """发新看板 或 原地刷新。返回 (ok, 说明)。"""
    rows = rows if rows is not None else load(db)
    card = render(rows)
    st = _read_board()
    tok = tok or fsapi.token()
    chat = st.get("chat") or fsapi.chat_id()
    why = ""

    if not new:
        targets = _board_targets(st)
        if targets:
            done_ok, last_err = 0, ""
            for mid in targets:
                d = fsapi.patch_card(mid, card, tok=tok)
                if d.get("code") == 0:
                    done_ok += 1
                else:
                    last_err = d.get("msg")      # 卡片被撤回 / 太旧 → 忽略，继续刷别的
            if done_ok:
                return True, "已原地刷新 %d 张看板（共 %d 张）" % (done_ok, len(targets))
            why = last_err

    d = fsapi.send_card_to(chat, card, tok=tok)
    if d.get("code") != 0:
        return False, "发看板失败: %s" % d.get("msg")
    mid = (d.get("data") or {}).get("message_id")
    recent = []
    if st.get("message_id"):
        recent.append(st["message_id"])
    for m in (st.get("recent") or []):
        if m and m not in recent:
            recent.append(m)
    _write_board({"message_id": mid, "chat": chat,
                  "recent": recent[:BOARD_KEEP - 1],
                  "updated_at": datetime.now(CST).isoformat(timespec="seconds")})
    tail = ("（旧卡刷新失败：%s）" % why) if why else ""
    return True, "已发新看板 %s%s" % (mid, tail)


def main():
    db = sqlite3.connect(DB)
    args = sys.argv[1:]
    rows = load(db)

    if "--text" in args:
        print(("☀️ 今天要做的事" if "--digest" in args
               else "📋 求职任务 · 已完成 %d/%d" % (cmds.progress(db)[0],
                                                 cmds.progress(db)[0] + cmds.progress(db)[1])))
        print(digest_text(rows) if "--digest" in args else _dump_text(rows))
        db.close()
        return

    if "--digest" in args:
        ok, msg = fsapi.send_card_to(fsapi.chat_id(), _digest_card(rows, db))
        print("早报: %s" % ("成功" if ok else msg))
        db.close()
        return

    ok, msg = publish(db, rows, new=("--new" in args))
    print(msg)
    db.close()


def _digest_card(rows, db):
    now = datetime.now(timezone.utc)
    card = render(rows, now=now, digest=True)
    n_done, n_open = cmds.progress(db)
    return card


def _dump_text(rows):
    now = datetime.now(timezone.utc)
    g, done_old_n = bucket(rows, now)
    L = []
    for name, key in (("🔥 24h 内截止", "today"), ("📌 进行中", "open"),
                      ("⌛ 已过期", "expired")):
        if g[key]:
            L.append("%s（%d）" % (name, len(g[key])))
            for r, dt in g[key]:
                # 和卡片同一条渲染链路，终端预览就是用户看到的东西
                head, sit, meta = task_lines(title_of(r), **line_kwargs(r, dt, states=1))
                L.append("  #%-3d %s" % (r["id"], head))
                if sit:
                    L.append("        %s" % sit)
                L.append("        %s" % meta)
    L.append("✅ 今天完成（%d）" % len(g["done_today"]))
    for r, dt in g["done_today"]:
        L.append("  #%-3d %s" % (r["id"], title_of(r)))
    L.append("⚪ 更早完成 %d 条（折叠）" % done_old_n)
    return "\n".join(L)


if __name__ == "__main__":
    main()
