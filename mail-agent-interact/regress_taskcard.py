
# ------------------------------------------------------------ B. 按钮链路
def call_handle(conn, text, via="card", uid="ou_TEST", btn_t=None):
    clear()
    poller.handle(conn, text, "om_test_%d" % time.time(), uid, via, {"ou_TEST"},
                  "TOKEN", "CHAT", btn_t=btn_t)
    return replies_text(), ack_cards()


def part_b(conn):
    print("\n" + "=" * 74)
    print("B. 点「✅ 完成」之后：每条失败路径现在说什么（飞书全被桩拦住）")
    print("=" * 74)
    conn.row_factory = sqlite3.Row
    tid = conn.execute("SELECT id FROM tasks WHERE email_id=902").fetchone()[0]
    done_tid = conn.execute("SELECT id FROM tasks WHERE state='done' ORDER BY id LIMIT 1").fetchone()[0]
    t901 = conn.execute("SELECT id FROM tasks WHERE email_id=901").fetchone()[0]

    # B1 正常路径（先证明这条链路没坏）
    texts, cards = call_handle(conn, "完成 %d" % tid)
    st = conn.execute("SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    card = cards[0] if cards else {}
    btns = card_buttons(card)
    check("B1 点「完成」→ 落库为 done", st == "done")
    check("B1 → 回一张确认卡，写明改了什么",
          bool(cards) and ("已完成" in card_text(card)), card_text(card).replace("\n", " / ")[:90])
    check("B1 → 确认卡带「↩️ 撤销」按钮，且 value 里有编号和时间戳",
          any(b["value"].get("id") == tid and b["value"].get("t") for b in btns),
          json.dumps([b.get("value") for b in btns], ensure_ascii=False))

    # B2 重复点击（任务已经是 done）
    texts, cards = call_handle(conn, "完成 %d" % tid)
    card = cards[0] if cards else {}
    body = card_text(card) if card else (texts[0] if texts else "")
    btns = card_buttons(card)
    check("B2 重复点击 → 明说「这次没有任何改动」+ 早就是该状态",
          "没有任何改动" in body and "早就是" in body, body.replace("\n", " / ")[:100])
    check("B2 重复点击 → 不再给「撤销」按钮（避免手滑把自己标好的撤掉）",
          not any(b["value"].get("k") == "undo" for b in btns))

    # B3 编号不存在
    texts, cards = call_handle(conn, "完成 9999")
    body = card_text(cards[0]) if cards else (texts[0] if texts else "")
    check("B3 编号不存在 → 明说「台账里没有 #9999 这条」并给下一步",
          "#9999" in body and "台账里没有" in body and "状态" in body,
          body.replace("\n", " / ")[:110])

    # B4 撤销窗口过期
    old = int(time.time()) - (poller.UNDO_WINDOW + 300)
    texts, cards = call_handle(conn, "撤销 %d" % done_tid, btn_t=old)
    body = texts[0] if texts else ""
    check("B4 撤销按钮过期 → 明说过期多久 + 文字指令仍可用",
          "过期" in body and "撤销 %d" % done_tid in body, body.replace("\n", " / ")[:110])

    # B5 落库失败（数据库被锁）
    real_apply = poller.cmds.apply
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    poller.cmds.apply = boom
# SEP

# ------------------------------------------------------------ B. 按钮链路
def call_handle(conn, text, via="card", uid="ou_TEST", btn_t=None):
    clear()
    poller.handle(conn, text, "om_test_%d" % time.time(), uid, via, {"ou_TEST"},
                  "TOKEN", "CHAT", btn_t=btn_t)
    return replies_text(), ack_cards()


def part_b(conn):
    print("\n" + "=" * 74)
    print("B. 点「✅ 完成」之后：每条失败路径现在说什么（飞书全被桩拦住）")
    print("=" * 74)
    conn.row_factory = sqlite3.Row
    tid = conn.execute("SELECT id FROM tasks WHERE email_id=902").fetchone()[0]
    done_tid = conn.execute("SELECT id FROM tasks WHERE state='done' ORDER BY id LIMIT 1").fetchone()[0]
    t901 = conn.execute("SELECT id FROM tasks WHERE email_id=901").fetchone()[0]

    # B1 正常路径（先证明这条链路没坏）
    texts, cards = call_handle(conn, "完成 %d" % tid)
    st = conn.execute("SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    card = cards[0] if cards else {}
    btns = card_buttons(card)
    check("B1 点「完成」→ 落库为 done", st == "done")
    check("B1 → 回一张确认卡，写明改了什么",
          bool(cards) and ("已完成" in card_text(card)), card_text(card).replace("\n", " / ")[:90])
    check("B1 → 确认卡带「↩️ 撤销」按钮，且 value 里有编号和时间戳",
          any(b["value"].get("id") == tid and b["value"].get("t") for b in btns),
          json.dumps([b.get("value") for b in btns], ensure_ascii=False))

    # B2 重复点击（任务已经是 done）
    texts, cards = call_handle(conn, "完成 %d" % tid)
    card = cards[0] if cards else {}
    body = card_text(card) if card else (texts[0] if texts else "")
    btns = card_buttons(card)
    check("B2 重复点击 → 明说「这次没有任何改动」+ 早就是该状态",
          "没有任何改动" in body and "早就是" in body, body.replace("\n", " / ")[:100])
    check("B2 重复点击 → 不再给「撤销」按钮（避免手滑把自己标好的撤掉）",
          not any(b["value"].get("k") == "undo" for b in btns))

    # B3 编号不存在
    texts, cards = call_handle(conn, "完成 9999")
    body = card_text(cards[0]) if cards else (texts[0] if texts else "")
    check("B3 编号不存在 → 明说「台账里没有 #9999 这条」并给下一步",
          "#9999" in body and "台账里没有" in body and "状态" in body,
          body.replace("\n", " / ")[:110])

    # B4 撤销窗口过期
    old = int(time.time()) - (poller.UNDO_WINDOW + 300)
    texts, cards = call_handle(conn, "撤销 %d" % done_tid, btn_t=old)
    body = texts[0] if texts else ""
    check("B4 撤销按钮过期 → 明说过期多久 + 文字指令仍可用",
          "过期" in body and "撤销 %d" % done_tid in body, body.replace("\n", " / ")[:110])

    # B5 落库失败（数据库被锁）
    real_apply = poller.cmds.apply
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    poller.cmds.apply = boom
    texts, cards = call_handle(conn, "完成 %d" % t901)
    poller.cmds.apply = real_apply
    body = texts[0] if texts else ""
    check("B5 落库失败 → 明说「没写进台账」+ 原因（不再静默）",
          "没写进台账" in body and "locked" in body, body.replace("\n", " / ")[:110])
    check("B5 落库失败 → 状态没有被改坏",
          conn.execute("SELECT state FROM tasks WHERE id=?", (t901,)).fetchone()[0] == "todo")

    # B6 看板刷新失败：操作落库了，但看板没刷上
    real_pub = poller._publish
    poller._publish = lambda db, tok, new=False: (False, "卡片太旧，patch 失败")
    texts, cards = call_handle(conn, "完成 %d" % t901)
    poller._publish = real_pub
    body = card_text(cards[0]) if cards else (texts[0] if texts else "")
    check("B6 看板刷新失败 → 确认里明说「看板没刷新成功」+ 怎么补救",
          "看板没刷新成功" in body and "状态" in body, body.replace("\n", " / ")[:110])

    # B7 确认卡发送失败 → 退纯文本
    real_send = poller.fsapi.send_card_to
    poller.fsapi.send_card_to = lambda chat, card, tok=None: {"code": 99999, "msg": "卡片被限流"}
    texts, cards = call_handle(conn, "撤销 %d" % t901)
    poller.fsapi.send_card_to = real_send
    check("B7 确认卡发不出去 → 自动退回纯文本（用户仍能看到 #%d 的结果）" % t901,
          bool(texts) and str(t901) in texts[0], (texts[0] if texts else "(什么都没有)")[:100])

    # B8 卡片点击被闸门拒掉（动词不在白名单）—— 走完整链路：JSONL → admit → 回话
    hook = os.path.join(WORK, "hook.jsonl")
    with open(hook, "w", encoding="utf-8") as f:
        f.write(json.dumps({"text": "确认 901", "message_id": "om_rej1",
                            "user_id": "ou_TEST", "ts": time.time(), "src": "card"},
                           ensure_ascii=False) + "\n")
    poller.HOOK_FILE = hook
    clear()
    poller.tick(conn, "TOKEN", "CHAT", {"ou_TEST"},
                {"seen": [], "hook_offset": 0, "history_cursor": time.time(), "inited": True},
                use_history=False)
    body = replies_text()[0] if replies_text() else ""
    check("B8 卡片点击被闸门拒掉 → 回一句「这个按钮没法处理」+ 替代做法（不再静默）",
          "没法处理" in body and "完成 3" in body, body.replace("\n", " / ")[:110])

    # B9 非白名单用户的点击：必须保持安静（不回声给陌生人）
    clear()
    poller._card_reject_note({"src": "card", "text": "确认 901", "uid": "ou_OTHER"},
                             {"ou_TEST"}, "CHAT", "TOKEN")
    check("B9 非白名单用户点卡片 → 不回声（安全边界不变）", not replies_text())


def part_b_dedup(conn):
    """B10 同一条事件被投递两次（hook + 历史兜底）：不重复回话，但日志留痕。"""
    print("\n--- B10 重复投递（去重）---")
    hook = os.path.join(WORK, "hook.jsonl")
    mid = "om_dup_%d" % time.time()
    real_tid = conn.execute("SELECT id FROM tasks WHERE email_id=902").fetchone()[0]
    with open(hook, "w", encoding="utf-8") as f:
        for _ in range(2):
            f.write(json.dumps({"text": "完成 %d" % real_tid, "message_id": mid, "user_id": "ou_TEST",
                                "ts": time.time(), "src": "card"}, ensure_ascii=False) + "\n")
    poller.HOOK_FILE = hook
    state = {"seen": [], "hook_offset": 0, "history_cursor": time.time(), "inited": True}
# SEP

# ------------------------------------------------------------ B. 按钮链路
def call_handle(conn, text, via="card", uid="ou_TEST", btn_t=None):
    clear()
    poller.handle(conn, text, "om_test_%d" % time.time(), uid, via, {"ou_TEST"},
                  "TOKEN", "CHAT", btn_t=btn_t)
    return replies_text(), ack_cards()


def part_b(conn):
    print("\n" + "=" * 74)
    print("B. 点「✅ 完成」之后：每条失败路径现在说什么（飞书全被桩拦住）")
    print("=" * 74)
    conn.row_factory = sqlite3.Row
    tid = conn.execute("SELECT id FROM tasks WHERE email_id=902").fetchone()[0]
    done_tid = conn.execute("SELECT id FROM tasks WHERE state='done' ORDER BY id LIMIT 1").fetchone()[0]
    t901 = conn.execute("SELECT id FROM tasks WHERE email_id=901").fetchone()[0]

    # B1 正常路径（先证明这条链路没坏）
    texts, cards = call_handle(conn, "完成 %d" % tid)
    st = conn.execute("SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    card = cards[0] if cards else {}
    btns = card_buttons(card)
    check("B1 点「完成」→ 落库为 done", st == "done")
    check("B1 → 回一张确认卡，写明改了什么",
          bool(cards) and ("已完成" in card_text(card)), card_text(card).replace("\n", " / ")[:90])
    check("B1 → 确认卡带「↩️ 撤销」按钮，且 value 里有编号和时间戳",
          any(b["value"].get("id") == tid and b["value"].get("t") for b in btns),
          json.dumps([b.get("value") for b in btns], ensure_ascii=False))

    # B2 重复点击（任务已经是 done）
    texts, cards = call_handle(conn, "完成 %d" % tid)
    card = cards[0] if cards else {}
    body = card_text(card) if card else (texts[0] if texts else "")
    btns = card_buttons(card)
    check("B2 重复点击 → 明说「这次没有任何改动」+ 早就是该状态",
          "没有任何改动" in body and "早就是" in body, body.replace("\n", " / ")[:100])
    check("B2 重复点击 → 不再给「撤销」按钮（避免手滑把自己标好的撤掉）",
          not any(b["value"].get("k") == "undo" for b in btns))

    # B3 编号不存在
    texts, cards = call_handle(conn, "完成 9999")
    body = card_text(cards[0]) if cards else (texts[0] if texts else "")
    check("B3 编号不存在 → 明说「台账里没有 #9999 这条」并给下一步",
          "#9999" in body and "台账里没有" in body and "状态" in body,
          body.replace("\n", " / ")[:110])

    # B4 撤销窗口过期
    old = int(time.time()) - (poller.UNDO_WINDOW + 300)
    texts, cards = call_handle(conn, "撤销 %d" % done_tid, btn_t=old)
    body = texts[0] if texts else ""
    check("B4 撤销按钮过期 → 明说过期多久 + 文字指令仍可用",
          "过期" in body and "撤销 %d" % done_tid in body, body.replace("\n", " / ")[:110])

    # B5 落库失败（数据库被锁）
    real_apply = poller.cmds.apply
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    poller.cmds.apply = boom
    texts, cards = call_handle(conn, "完成 %d" % t901)
    poller.cmds.apply = real_apply
    body = texts[0] if texts else ""
    check("B5 落库失败 → 明说「没写进台账」+ 原因（不再静默）",
          "没写进台账" in body and "locked" in body, body.replace("\n", " / ")[:110])
    check("B5 落库失败 → 状态没有被改坏",
          conn.execute("SELECT state FROM tasks WHERE id=?", (t901,)).fetchone()[0] == "todo")

    # B6 看板刷新失败：操作落库了，但看板没刷上
    real_pub = poller._publish
    poller._publish = lambda db, tok, new=False: (False, "卡片太旧，patch 失败")
    texts, cards = call_handle(conn, "完成 %d" % t901)
    poller._publish = real_pub
    body = card_text(cards[0]) if cards else (texts[0] if texts else "")
    check("B6 看板刷新失败 → 确认里明说「看板没刷新成功」+ 怎么补救",
          "看板没刷新成功" in body and "状态" in body, body.replace("\n", " / ")[:110])

    # B7 确认卡发送失败 → 退纯文本
    real_send = poller.fsapi.send_card_to
    poller.fsapi.send_card_to = lambda chat, card, tok=None: {"code": 99999, "msg": "卡片被限流"}
    texts, cards = call_handle(conn, "撤销 %d" % t901)
    poller.fsapi.send_card_to = real_send
    check("B7 确认卡发不出去 → 自动退回纯文本（用户仍能看到 #%d 的结果）" % t901,
          bool(texts) and str(t901) in texts[0], (texts[0] if texts else "(什么都没有)")[:100])

    # B8 卡片点击被闸门拒掉（动词不在白名单）—— 走完整链路：JSONL → admit → 回话
    hook = os.path.join(WORK, "hook.jsonl")
    with open(hook, "w", encoding="utf-8") as f:
        f.write(json.dumps({"text": "确认 901", "message_id": "om_rej1",
                            "user_id": "ou_TEST", "ts": time.time(), "src": "card"},
                           ensure_ascii=False) + "\n")
    poller.HOOK_FILE = hook
    clear()
    poller.tick(conn, "TOKEN", "CHAT", {"ou_TEST"},
                {"seen": [], "hook_offset": 0, "history_cursor": time.time(), "inited": True},
                use_history=False)
    body = replies_text()[0] if replies_text() else ""
    check("B8 卡片点击被闸门拒掉 → 回一句「这个按钮没法处理」+ 替代做法（不再静默）",
          "没法处理" in body and "完成 3" in body, body.replace("\n", " / ")[:110])

    # B9 非白名单用户的点击：必须保持安静（不回声给陌生人）
    clear()
    poller._card_reject_note({"src": "card", "text": "确认 901", "uid": "ou_OTHER"},
                             {"ou_TEST"}, "CHAT", "TOKEN")
    check("B9 非白名单用户点卡片 → 不回声（安全边界不变）", not replies_text())


def part_b_dedup(conn):
    """B10 同一条事件被投递两次（hook + 历史兜底）：不重复回话，但日志留痕。"""
    print("\n--- B10 重复投递（去重）---")
    hook = os.path.join(WORK, "hook.jsonl")
    mid = "om_dup_%d" % time.time()
    real_tid = conn.execute("SELECT id FROM tasks WHERE email_id=902").fetchone()[0]
    with open(hook, "w", encoding="utf-8") as f:
        for _ in range(2):
            f.write(json.dumps({"text": "完成 %d" % real_tid, "message_id": mid, "user_id": "ou_TEST",
                                "ts": time.time(), "src": "card"}, ensure_ascii=False) + "\n")
    poller.HOOK_FILE = hook
    state = {"seen": [], "hook_offset": 0, "history_cursor": time.time(), "inited": True}
    clear()
    n = poller.tick(conn, "TOKEN", "CHAT", {"ou_TEST"}, state, use_history=False)
    log = ""
    try:
        with open(os.path.join(WORK, "poller.log"), encoding="utf-8") as f:
            log = f.read()
    except OSError:
        pass
    check("B10 同一条点击投递两次 → 只处理 1 次（n=%s）" % n, n == 1,
          "回话条数=%d" % (len(replies_text()) + len(replies_card())))
    check("B10 第二次被跳过时有日志留痕（不是静默吞掉）",
          "重复投递，跳过" in log and mid in log)


def main():
    md5_before = md5(PROD_DB)
    print("生产库 md5（跑之前）: %s" % md5_before)
    before = tasks_snapshot(PROD_DB)
    stub_all()
    build_copy()

    conn = sqlite3.connect(COPY)
    # load() 是看板/台账真正的入口（里面会 ensure_task_cols）—— 先证明它不炸
    if not ONLY_PREVIEW:
        check("board.load() 在生产库同构的 schema 上跑得通（幂等补列）",
              len(board.load(conn)) >= 1)
        check("taskboard.load() 同上", len(taskboard.load(conn)) >= 1)

    rows, by_title, push_card, board_card, t_card = part_a(conn)
    if not ONLY_PREVIEW:
        part_a_assert(rows, by_title, push_card, board_card, t_card)
        part_b(conn)
        part_b_dedup(conn)

    # 落库证据：新列真的写进 tasks 了
    print("\n--- tasks 表新增列（副本库上跑出来的真实数据）---")
    for r in conn.execute("""SELECT id, email_id, title, org, situation, where_hint,
                                    state, link_short FROM tasks WHERE email_id>=901
                             ORDER BY id"""):
        print("  #%s (email %s) %s" % (r[0], r[1], r[2][:34]))
        print("        org=%s ｜ situation=%s" % (r[3], r[4]))
        print("        where=%s" % r[5])
    conn.close()

    after = tasks_snapshot(PROD_DB)
    md5_after = md5(PROD_DB)
    print("\n生产库 md5（跑之后）: %s" % md5_after)
    if ONLY_PREVIEW:
        return 0
    check("生产库 tasks 逐行未变（那 7 条 done 一条没动）", before == after,
          "%d 行" % len(after))
    check("生产库文件字节未被改动（md5 一致）", md5_before == md5_after)
    print("\n=== 汇总 ===  PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAIL: %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
# SEP
