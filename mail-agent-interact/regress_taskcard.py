#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务卡渲染 + 按钮回执回归（任务 A：说清"要做什么/去哪做/有没有链接"；任务 B：点下去不能含糊）。

安全（三条硬约束都落在这里）：
  * **一块飞书消息都不发** —— board/poller/push_actions/taskboard 的 fsapi/feishu
    发送函数全部换成记录器，卡片只打到终端；
  * 生产库只读 —— 所有 INSERT/UPDATE 都在 /tmp 的副本库上，末尾用 md5 + tasks
    逐行快照对账，证明 `/root/mail-agent/mail.db` 一个字节没动；
  * 出站链接规则照旧 —— 断言卡片里出现的每个 url 都是 http/https，伪协议不出现。

用法：
  python3 regress_taskcard.py              # 预览 + 断言
  python3 regress_taskcard.py --preview    # 只打预览（少说废话）
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import types
from datetime import datetime, timedelta, timezone

WORK = "/tmp/taskcardwork"
os.makedirs(WORK, exist_ok=True)
os.environ["TASK_CMD_STATE"] = WORK          # 状态文件（board.json/poller.log）放临时目录
os.environ["TASK_CMD_HOOKFILE"] = os.path.join(WORK, "hook.jsonl")

sys.path.insert(0, "/root/mail-agent")
sys.path.insert(0, "/opt/mail-agent-interact")

import common          # noqa: E402
import board           # noqa: E402
import cmds            # noqa: E402
import poller          # noqa: E402
import push_actions    # noqa: E402
import taskboard       # noqa: E402

PROD_DB = "/root/mail-agent/mail.db"
COPY = os.path.join(WORK, "mail_copy.db")
ONLY_PREVIEW = "--preview" in sys.argv
PASS, FAIL = [], []

SENT = []            # 桩记录：("text"|"card"|"patch", ...) —— 只记录，不联网


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("　→ " + detail) if detail else ""))


def md5(path):
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return h.hexdigest()


def tasks_snapshot(path):
    db = sqlite3.connect(path)
    rows = db.execute("SELECT id, email_id, title, state, done_at, done_source, "
                      "due_utc, link, link_host, link_short FROM tasks ORDER BY id").fetchall()
    db.close()
    return rows


# ------------------------------------------------------------ 桩
def stub_all():
    """把三张卡 + 指令泵的出站口全部换成记录器（一块消息都不发）。"""
    def _rec(kind, payload):
        SENT.append((kind, payload))
        return {"code": 0, "msg": "stub", "data": {"message_id": "om_stub_%d" % len(SENT)}}

    board.fsapi = types.SimpleNamespace(
        token=lambda: "TOKEN", chat_id=lambda: "CHAT",
        patch_card=lambda mid, card, tok=None: _rec("patch", (mid, card)),
        send_card_to=lambda chat, card, tok=None: _rec("card", card),
        send_text_to=lambda chat, text, tok=None: _rec("text", text),
        allowed_user=lambda: ["ou_TEST"])
    poller.fsapi = board.fsapi
    push_actions.feishu = types.SimpleNamespace(
        send_card=lambda card: (SENT.append(("card", card)), (True, "stub"))[1])
    taskboard.feishu = push_actions.feishu


def clear():
    del SENT[:]


def replies_text():
    return [p for k, p in SENT if k == "text"]


def replies_card():
    return [p for k, p in SENT if k == "card"]


_ACK_TITLES = set(poller.ACK_HEAD.values())


def ack_cards():
    """只挑出"确认消息"那种卡（看板卡是另一张，头部标题不一样）。"""
    return [c for c in replies_card()
            if c.get("header", {}).get("title", {}).get("content") in _ACK_TITLES]


def card_text(card):
    """把卡片里所有可见文本拼起来（用于断言"某句话在不在卡上"）。"""
    out = [card.get("header", {}).get("title", {}).get("content", "")]
    for el in card.get("elements", []):
        if el.get("tag") == "div":
            out.append(el["text"]["content"])
        elif el.get("tag") == "note":
            out.extend(x.get("content", "") for x in el.get("elements", []))
        elif el.get("tag") == "action":
            out.extend(b["text"]["content"] for b in el["actions"])
    return "\n".join(out)


def card_urls(card):
    out = []
    for el in card.get("elements", []):
        for b in el.get("actions", []) or []:
            if b.get("url"):
                out.append(b["url"])
    return out


def card_buttons(card):
    return [b for el in card.get("elements", []) if el.get("tag") == "action"
            for b in el.get("actions", [])]


def dump_card(card, title):
    """把卡片打成终端文本，供逐条核对。"""
    print("\n" + "─" * 74)
    print("【%s】  头部：%s" % (title, card.get("header", {}).get("title", {}).get("content")))
    print("─" * 74)
    for el in card.get("elements", []):
        if el.get("tag") == "div":
            for i, line in enumerate(el["text"]["content"].split("\n")):
                print(("    " if i else "  ") + line)
        elif el.get("tag") == "note":
            print("  ▸ " + " ｜ ".join(x.get("content", "") for x in el.get("elements", [])))
        elif el.get("tag") == "action":
            for b in el["actions"]:
                print("  ▸ [按钮 %s]  value=%s%s"
                      % (b["text"]["content"], json.dumps(b.get("value"), ensure_ascii=False),
                         ("  url=%s" % b["url"]) if b.get("url") else ""))
        elif el.get("tag") == "hr":
            print("  " + "·" * 60)


# ------------------------------------------------------------ 副本库 + 8 种情况
NOW = datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# email_id / 用例名 / action（标题）/ org / situation / where / due / link / host / short / credential
CASES = [
    dict(eid=901, key="① 有链接 + 有截止", action="完善星辰科技校招简历信息",
         org="星辰科技", situation="星辰科技请你更新校招简历信息，更新完才进下一轮",
         where="点标题登录招聘系统后在「简历」里改", due=iso(NOW + timedelta(hours=3)),
         link="https://recruit.xingchen.example.com/#/talent/resume-update?id=abc", host="recruit.xingchen.example.com",
         short=0, cred=""),
    dict(eid=902, key="② 有链接 + 没截止", action="在示例智能招聘系统确认面试时间",
         org="示例智能", situation="对方邀你参加面试，等你选时间",
         where="在招聘系统里挑一个时间段", due=None,
# SEP
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务卡渲染 + 按钮回执回归（任务 A：说清"要做什么/去哪做/有没有链接"；任务 B：点下去不能含糊）。

安全（三条硬约束都落在这里）：
  * **一块飞书消息都不发** —— board/poller/push_actions/taskboard 的 fsapi/feishu
    发送函数全部换成记录器，卡片只打到终端；
  * 生产库只读 —— 所有 INSERT/UPDATE 都在 /tmp 的副本库上，末尾用 md5 + tasks
    逐行快照对账，证明 `/root/mail-agent/mail.db` 一个字节没动；
  * 出站链接规则照旧 —— 断言卡片里出现的每个 url 都是 http/https，伪协议不出现。

用法：
  python3 regress_taskcard.py              # 预览 + 断言
  python3 regress_taskcard.py --preview    # 只打预览（少说废话）
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import types
from datetime import datetime, timedelta, timezone

WORK = "/tmp/taskcardwork"
os.makedirs(WORK, exist_ok=True)
os.environ["TASK_CMD_STATE"] = WORK          # 状态文件（board.json/poller.log）放临时目录
os.environ["TASK_CMD_HOOKFILE"] = os.path.join(WORK, "hook.jsonl")

sys.path.insert(0, "/root/mail-agent")
sys.path.insert(0, "/opt/mail-agent-interact")

import common          # noqa: E402
import board           # noqa: E402
import cmds            # noqa: E402
import poller          # noqa: E402
import push_actions    # noqa: E402
import taskboard       # noqa: E402

PROD_DB = "/root/mail-agent/mail.db"
COPY = os.path.join(WORK, "mail_copy.db")
ONLY_PREVIEW = "--preview" in sys.argv
PASS, FAIL = [], []

SENT = []            # 桩记录：("text"|"card"|"patch", ...) —— 只记录，不联网


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("　→ " + detail) if detail else ""))


def md5(path):
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return h.hexdigest()


def tasks_snapshot(path):
    db = sqlite3.connect(path)
    rows = db.execute("SELECT id, email_id, title, state, done_at, done_source, "
                      "due_utc, link, link_host, link_short FROM tasks ORDER BY id").fetchall()
    db.close()
    return rows


# ------------------------------------------------------------ 桩
def stub_all():
    """把三张卡 + 指令泵的出站口全部换成记录器（一块消息都不发）。"""
    def _rec(kind, payload):
        SENT.append((kind, payload))
        return {"code": 0, "msg": "stub", "data": {"message_id": "om_stub_%d" % len(SENT)}}

    board.fsapi = types.SimpleNamespace(
        token=lambda: "TOKEN", chat_id=lambda: "CHAT",
        patch_card=lambda mid, card, tok=None: _rec("patch", (mid, card)),
        send_card_to=lambda chat, card, tok=None: _rec("card", card),
        send_text_to=lambda chat, text, tok=None: _rec("text", text),
        allowed_user=lambda: ["ou_TEST"])
    poller.fsapi = board.fsapi
    push_actions.feishu = types.SimpleNamespace(
        send_card=lambda card: (SENT.append(("card", card)), (True, "stub"))[1])
    taskboard.feishu = push_actions.feishu


def clear():
    del SENT[:]


def replies_text():
    return [p for k, p in SENT if k == "text"]


def replies_card():
    return [p for k, p in SENT if k == "card"]


_ACK_TITLES = set(poller.ACK_HEAD.values())


def ack_cards():
    """只挑出"确认消息"那种卡（看板卡是另一张，头部标题不一样）。"""
    return [c for c in replies_card()
            if c.get("header", {}).get("title", {}).get("content") in _ACK_TITLES]


def card_text(card):
    """把卡片里所有可见文本拼起来（用于断言"某句话在不在卡上"）。"""
    out = [card.get("header", {}).get("title", {}).get("content", "")]
    for el in card.get("elements", []):
        if el.get("tag") == "div":
            out.append(el["text"]["content"])
        elif el.get("tag") == "note":
            out.extend(x.get("content", "") for x in el.get("elements", []))
        elif el.get("tag") == "action":
            out.extend(b["text"]["content"] for b in el["actions"])
    return "\n".join(out)


def card_urls(card):
    out = []
    for el in card.get("elements", []):
        for b in el.get("actions", []) or []:
            if b.get("url"):
                out.append(b["url"])
    return out


def card_buttons(card):
    return [b for el in card.get("elements", []) if el.get("tag") == "action"
            for b in el.get("actions", [])]


def dump_card(card, title):
    """把卡片打成终端文本，供逐条核对。"""
    print("\n" + "─" * 74)
    print("【%s】  头部：%s" % (title, card.get("header", {}).get("title", {}).get("content")))
    print("─" * 74)
    for el in card.get("elements", []):
        if el.get("tag") == "div":
            for i, line in enumerate(el["text"]["content"].split("\n")):
                print(("    " if i else "  ") + line)
        elif el.get("tag") == "note":
            print("  ▸ " + " ｜ ".join(x.get("content", "") for x in el.get("elements", [])))
        elif el.get("tag") == "action":
            for b in el["actions"]:
                print("  ▸ [按钮 %s]  value=%s%s"
                      % (b["text"]["content"], json.dumps(b.get("value"), ensure_ascii=False),
                         ("  url=%s" % b["url"]) if b.get("url") else ""))
        elif el.get("tag") == "hr":
            print("  " + "·" * 60)


# ------------------------------------------------------------ 副本库 + 8 种情况
NOW = datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# email_id / 用例名 / action（标题）/ org / situation / where / due / link / host / short / credential
CASES = [
    dict(eid=901, key="① 有链接 + 有截止", action="完善星辰科技校招简历信息",
         org="星辰科技", situation="星辰科技请你更新校招简历信息，更新完才进下一轮",
         where="点标题登录招聘系统后在「简历」里改", due=iso(NOW + timedelta(hours=3)),
         link="https://recruit.xingchen.example.com/#/talent/resume-update?id=abc", host="recruit.xingchen.example.com",
         short=0, cred=""),
    dict(eid=902, key="② 有链接 + 没截止", action="在示例智能招聘系统确认面试时间",
         org="示例智能", situation="对方邀你参加面试，等你选时间",
         where="在招聘系统里挑一个时间段", due=None,
         link="https://hr.jobboard.example.com/v1/s/AbCdEfGh#", host="hr.jobboard.example.com",
         short=0, cred=""),
    dict(eid=903, key="③ 没链接 + 没截止", action="回复王 HR 确认面试时间",
         org="示例医疗", situation="对方问你哪天方便面试，还没回",
         where="直接回这封邮件", due=None, link=None, host=None, short=0, cred=""),
    dict(eid=904, key="③b 没链接 + 没截止 + 没有情境（老任务）", action="跟导师确认实习推荐信",
         org="", situation="", where="", due=None, link=None, host=None, short=0, cred=""),
    dict(eid=905, key="④ 退信类", action="重投示例工程师：原信未送达 campus@acme.example.com",
         org="acme.example.com",
         situation="你投给 campus@acme.example.com 的邮件没送到：该地址不存在",
         where="邮箱搜 postmaster@qq.com 看原始退信", due=None,
         link=None, host=None, short=0, cred=""),
    dict(eid=906, key="⑤ 短链（不给跳转）", action="完成云图信息在线测评，点击链接作答",
         org="云图信息", situation="云图信息邀你参加 2027 校招在线测评",
         where="点邮件里的测评链接作答", due=iso(NOW + timedelta(hours=40)),
         link="https://bs.example.com/v2/AbCdEfGh", host="bs.example.com", short=1,
         cred="通行证 12345678901234"),
    dict(eid=907, key="⑥ javascript: 伪协议", action="打开公司系统查看面试结果",
         org="example.com", situation="对方让你去系统里看结果", where="从官网重新进系统",
         due=None, link="javascript:alert(document.cookie)", host="", short=0, cred=""),
    dict(eid=908, key="⑦ 标题里塞假链接（markdown 注入）", action="确认可疑](https://evil.example/x) 的邀约",
         org="evil.example", situation="标题/正文里带着可疑的跳转语法",
         where="先打官网电话核实", due=iso(NOW + timedelta(hours=50)),
         link="https://good.example/ok", host="good.example", short=0, cred=""),
]


def build_copy():
    """生产库 → 副本库，然后在副本上补列 + 造 8 条待办（生产库一行不动）。"""
    if os.path.exists(COPY):
        os.remove(COPY)
    shutil.copy(PROD_DB, COPY)
    db = sqlite3.connect(COPY)
    # 模拟 analyze.py 的幂等 ALTER（生产库由常驻守护进程在下一轮做同样的事）
    for col in ("org", "situation", "where_hint"):
        if col not in [r[1] for r in db.execute("PRAGMA table_info(email_verdict)")]:
            db.execute("ALTER TABLE email_verdict ADD COLUMN %s TEXT" % col)
    if "qa_flags" not in [r[1] for r in db.execute("PRAGMA table_info(email_verdict)")]:
        db.execute("ALTER TABLE email_verdict ADD COLUMN qa_flags TEXT")
    for c in CASES:
        db.execute("""INSERT OR REPLACE INTO email
                        (id, folder_id, uid, uidvalidity, subject, from_addr, to_addr,
                         internaldate, origin, fetched, first_seen_at)
                      VALUES(?,1,?,?,'[合成用例] ' + ?,?,?,?,'live',1,?)""",
                   (c["eid"], c["eid"], 999000 + c["eid"], c["key"],
                    "hr@%s" % (c["host"] or "example.com"), "me@qq.com", iso(NOW), iso(NOW)))
        db.execute("""INSERT OR REPLACE INTO email_verdict
                        (email_id, run_id, kind, action, due_utc, expired, need_reply,
                         reply_ask, red_flags, confidence, pushed_at, updated_at,
                         link, link_host, link_short, credential,
                         org, situation, where_hint, qa_flags)
                      VALUES(?,1,'action',?,?,0,0,'','',0.9,NULL,?,?,?,?,?,?,?,?,'""",
                   (c["eid"], c["action"], c["due"], iso(NOW), c["link"], c["host"],
                    c["short"], c["cred"], c["org"], c["situation"], c["where"]))
    db.commit()
    db.close()


def run_push():
    """跑真的 push_actions.main()（写副本库 + 生成推送卡），把卡片接住。"""
    clear()
    push_actions.DB = COPY
    push_actions.QUIET_START, push_actions.QUIET_END = 0, 24     # 不受当前钟点影响
    push_actions.MAX_SHOW = 20
    push_actions.main()
    return replies_card()[0] if replies_card() else None


def load_synth(conn):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM tasks WHERE email_id>=901 ORDER BY id").fetchall()


# ------------------------------------------------------------ A. 渲染预览
def part_a(conn):
    print("\n" + "=" * 74)
    print("A. 一条任务现在长什么样（看板卡 / 台账卡 / 推送卡 三张卡对照）")
    print("=" * 74)

    rows, by_title = load_synth(conn), {}

    # —— 推送卡（push_actions.main 真的跑过一遍，8 条是它自己判断/落库的）
    push_card = run_push()
    rows = load_synth(conn)                    # 落库之后再读（编号是 push 分配的）
    by_title = {r["title"]: r for r in rows}
    if push_card:
        dump_card(push_card, "推送卡 push_actions.py（新任务进来时发的）")
    else:
        check("推送卡生成", False, "没抓到卡片")

    # —— 看板卡（只渲染合成的 8 条，方便逐条核对）
    board_card = board.render(rows, now=NOW, sugg={})
    dump_card(board_card, "看板卡 board.py（回「状态」/原地刷新的那张）")

    # —— 台账卡
    t_card, t_text = taskboard.render(rows)
    dump_card(t_card, "台账卡 taskboard.py")
    print("\n台账卡终端版：\n" + t_text)

    # —— 每条任务占几个「块」：证明没有把卡撑密（1 个正文块，信息在块内换行）
    divs = [el["text"]["content"] for el in board_card["elements"] if el.get("tag") == "div"]
    task_divs = [d for d in divs if re.match(r"^\*\*\d+[. ]", d)]
    # 允许出现的非任务块：进度条那一行、空态提示（分组标题都以 ** 开头）
    extra = [d for d in divs if not d.startswith("**")
             and not d.startswith(("▰", "▱")) and d != "暂时没有任务 🎉"]
    check("每条待办只占 1 个正文块（信息在块内换行，不额外占块）",
          not extra and len(task_divs) >= 5,
          "任务正文块 %d 个，多出来的非标题块：%s" % (len(task_divs), extra[:2]))
    check("一条任务最多 3 行（标题／情境／元信息）",
          all(len(d.split("\n")) <= 3 for d in task_divs),
          "最长 %d 行" % max(len(d.split("\n")) for d in task_divs))

    return rows, by_title, push_card, board_card, t_card


def part_a_assert(rows, by_title, push_card, board_card, t_card):
    print("\n--- A 断言 ---")
    all_text = {"看板": card_text(board_card), "推送": card_text(push_card or {}),
                "台账": card_text(t_card)}
    joined = "\n".join(all_text.values())

    # 1) situation / where 三张卡都要有
    for c in CASES:
        if not (c["situation"] or c["where"]):
            continue
        miss = [k for k, v in all_text.items() if c["key"][:2] not in v
                and (c["situation"][:12] not in v and c["where"][:8] not in v)]
        # 看板卡/推送卡/台账卡都该出现这条任务的一句话情境
        check("三张卡都显示了「%s」的情境/去哪做" % c["key"],
              not miss, "缺: %s" % miss if miss else "")

    # 2) 主体（org）必须看得见：标题或情境里没出现过时要挂 @org
    check("退信类任务挂上了主体 acme.example.com（或情境里已点名）",
          "acme.example.com" in all_text["看板"], "")
    check("没链接的任务明说「邮件里没给链接」",
          "邮件里没给链接" in all_text["看板"] and "邮件里没给链接" in all_text["台账"])
    check("没有截止时间不出现空壳「⏰」（改为 📭 明说）",
          "📭 邮件里没写截止时间" in all_text["看板"] and "⏰ \n" not in all_text["看板"])

    # 3) 出站链接：只允许 http/https
    urls = []
    for card in (board_card, t_card, push_card or {}):
        urls += card_urls(card)
        for el in card.get("elements", []):
            if el.get("tag") == "div":
                urls += [p.split("](")[1].split(")")[0] for p in
                         el["text"]["content"].split("[") if "](" in p]
    bad = [u for u in urls if not u.lower().startswith(("http://", "https://"))]
    check("卡片里出现的每个 url 都是 http/https（共 %d 个）" % len(urls), not bad, str(bad[:3]))
    check("javascript: 伪协议没有变成链接（只留纯文本标题）",
          "javascript:" not in json.dumps([board_card, t_card, push_card], ensure_ascii=False)
          and "打开公司系统查看面试结果" in all_text["看板"])
    check("短链没做成可点链接，且明说要自己核对来源",
          "bs.example.com" in all_text["看板"] and "短链" in all_text["看板"])

    # 4) markdown 注入：标题里的 `](https://evil` 必须被中和
    check("标题里塞的 `](https://evil...)` 没有拼出第二个链接",
          "](https://evil.example" not in json.dumps(board_card, ensure_ascii=False)
          and "evil.example" in all_text["看板"])

    # 5) 「去做」按钮：前两条没链接时，后面有链接的仍要拿到按钮（先过滤后切片）
    btns = [b for b in card_buttons(board_card) if b["text"]["content"].startswith("去做")]
    check("「去做」按钮只发给有 http/https 链接的任务，且前两条无链接也拿得到",
          len(btns) == 2 and all(b["url"].startswith("https://") for b in btns),
          "按钮=%s" % [b["text"]["content"] for b in btns])

    # 6) 每条待办都有「✅ 完成」按钮（三张卡）
    for name, card in (("看板", board_card), ("台账", t_card)):
        n = len([b for b in card_buttons(card) if b["text"]["content"] == "✅ 完成"])
        check("%s卡：每条待办都带「✅ 完成」按钮（%d 个）" % (name, n),
              n == len([r for r in rows]))


# SEP
