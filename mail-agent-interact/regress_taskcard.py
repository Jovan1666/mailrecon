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
