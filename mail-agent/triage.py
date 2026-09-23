#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""求职邮件助手 —— 本地分诊（triage）v2

只吃邮件头两个字段（from_addr, subject），不碰正文，因此可以放在
"取正文"之前，决定这封信要不要取正文、取完怎么处理。

    层           取正文  送模型  必须打码   语义
    drop         否      否      否        与我无关，直接跳过
    template     是      否      否        机器群发，本地规则就能处理
    llm          是      是      是        真人/行动项，需要语义理解
    local_only   是      否*     是        含凭证（验证码/密码/通行证）

    * local_only 默认本地抽取后直接告诉用户；确实需要模型时，只送 mask() 之后的内容。

设计原则（按重要性排序）
----------
1. **内容优先，发件人次之。** 本库最重要的两封邮件（AI 面试邀请、在线测评邀请）
   都来自"机器人前缀 + 招聘系统域名"这种组合。任何"看到 noreply 就丢"
   的规则都会把它们杀掉 —— 所以行动类关键词（面试/笔试/测评/offer/截止）
   永远先于发件人规则判定。
2. **求职相关邮件不可能被 drop。** 所有 drop 规则都带 `not f_job` 闸门；
   求职域/求职关键词命中的信最多落到 template（取正文，本地看），绝不会静默消失。
3. **不确定时倒向便宜的一侧。** 未知发件人默认 template/llm，绝不默认 drop；
   drop 只给"确证批量"的域名和句式。
4. 判据全部来自主题 —— 正文未入库。若将来接入正文，需复核正文句式
   （"请耐心等待"/"尽快联系您"）对 template 判定的影响。

规则命中会以 (bucket, rule_name) 形式返回，rule_name 可直接写进日志/飞书消息。

规则链（自上而下，首个命中即返回）
----------
     1  bounce.delivery_failed     LLM   退信/未送达（投递失败的告警，量小必看）
     2  local_only.job_secret      LOC   求职/学校/竞赛语境 + 验证码/密码/通行证
   2.5  drop.security_advisory     DROP  反诈/安全宣导（"招聘"只是宣讲内容）
     3  llm.action_item            LLM   面试/笔试/测评/offer/截止/邀请您参加/未通过
     4  template.ats_receipt       TPL   招聘系统收件确认（感谢投递，无行动项）
     5  template.autoreply         TPL   自动回复 / Auto-Reply
     6  llm.human_reply            LLM   Re:/回复: 且非机器人前缀、非招聘系统域
     7  drop.platform_domain       DROP  确证平台域名白名单（非求职语境）
   7.5  drop.qq_official           DROP  10000@qq.com 官方号
     8  drop.promo                 DROP  营销/促销/广告词
     9  drop.receipt               DROP  发票/收据/订单/支付/退款
    10  drop.notification          DROP  登录/安全/CI/OAuth/条款更新
  10.5  drop.bare_template         DROP  "[品牌] Message/通知" 空壳主题
    11  local_only.otp_unknown     LOC   非白名单来源的验证码（安全兜底）
    12  drop.spam_heuristic        DROP  随机主题 / 非公共邮箱长数字串
    13  llm.human_personal         LLM   公共邮箱的个人地址（QQ 号也是真人）
    14  llm.competition_edu        LLM   竞赛/学校事务
    15  fallback.bot_unknown       TPL   机器人前缀 + 未知域名 -> 取正文本地看
    16  fallback.human_unknown     LLM   疑似真人地址 + 未知域名
    17  fallback.unparsable        TPL   地址解析失败

    15/16/17 是兜底：**没有一条兜底是 drop**。代价是未知来源要取正文
    （IMAP 免费）或走一次模型（本库实测只占 4 封 / 0.4%），换的是
    "再也不会有一封重要邮件被静默跳过"。
"""

import os
import re
import sys

# triage.py 是这堆脚本里最底层、被最多人 import 的一个，历史上不依赖同目录模块。
# 现在它要从 platform_cfg 读平台域名清单，所以显式把自己所在目录放进 sys.path，
# 让 `python3 triage.py`、`import triage`、以及 runpy / importlib 动态加载
# 这几种姿势都能找到 platform_cfg。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import platform_cfg  # noqa: E402

BUCKETS = ("drop", "template", "llm", "local_only")

# 各层对应的动作计划：fetch=取正文, model=送模型, mask=必须先打码
PLAN = {
    "drop":       {"fetch": False, "model": False, "mask": False},
    "template":   {"fetch": True,  "model": False, "mask": False},
    "llm":        {"fetch": True,  "model": True,  "mask": True},
    "local_only": {"fetch": True,  "model": False, "mask": True},
}

# ---------------------------------------------------------------- 地址/域名 --

# 公共邮箱：必须按完整地址判断，不能按域名（common.py 的老教训）
PUBLIC = {
    "qq.com", "vip.qq.com", "foxmail.com", "163.com", "126.com", "yeah.net",
    "gmail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
    "sina.com", "sina.cn", "sohu.com", "139.com", "189.cn", "21cn.com",
    "aliyun.com", "tom.com", "263.net", "wo.cn",
}

# 招聘系统 / ATS / 求职平台。
#
# **内置默认只放明显虚构的示例域名** —— 这份源码是要公开的，任何来自真实邮箱的
# 域名都等于在公开"我投过哪些公司、用过哪些招聘平台"。真实清单从
# platform_domains.txt 的 [job] 节读（不入库），见 platform_cfg.py 与
# platform_domains.txt.example。
JOB_DOMAINS = {
    "jobboard.example.com", "usermail.jobboard.example.com", "hr.jobboard.example.com",
    "ats-one.example.com", "hr.ats-one.example.com",
    "ats-two.example.com", "mail.ats-two.example.com",
    "ats-three.example.com", "shmail.ats-three.example.com",
    "jobportal.example.com",
} | platform_cfg.job_domains()

# 学校 / 教育机构后缀（竞赛、教务、老师来信 —— 一律不 drop）
EDU_SUFFIX = (".edu.cn", ".edu", ".ac.cn", ".edu.hk", ".edu.tw", ".ac.uk")

# 消费级平台 / 开发者平台：批量通知、营销、收据、登录码。**按确证域名** drop。
#
# 同样：内置默认**只有虚构示例域名**，真实清单从 platform_domains.txt 的
# [noise] 节读。这份清单尤其敏感 —— 它原样列出来就是"我用过哪些电商、
# 显卡、网盘、代理、盗版站、开发工具"的画像。
# 唯一的例外是 `github.com`：通用的代码托管通知发件域，任何人的邮箱里都有，
# 不构成个人画像，按协调者裁定留在内置清单里。
PLATFORM_DOMAINS = {
    "github.com",
    "gameplatform.example.com", "devicevendor.example.com",
    "devicevendor-cn.example.com", "softwarevendor.example.com",
    "searchvendor.example.com", "peripheral.example.com",
    "gpuvendor.example.com", "gamepublisher.example.com",
} | platform_cfg.noise_domains()

# 机器人发件人前缀。**只用来兜底，不用来 drop** —— 招聘平台的面试邀请正是
# support@ 发出来的。前缀命中时默认 template（取正文，本地看，不花钱）。
BOT_PREFIX = re.compile(
    r"^(no[-_.]?reply|noreply|do[-_.]?not[-_.]?reply|donotreply|notification|"
    r"notifications|notify|mailer|mailer-daemon|postmaster|automated|auto|"
    r"system|service|support|info|news|newsletter|marketing|market|promo|"
    r"billing|invoice|receipt|account|accounts|security|verify|verification|"
    r"alert|alerts|ftnremind_admin|10000|updates?|hello|team)$", re.I)

# 垃圾/钓鱼特征域名。内置默认**空**：原实现里那三个是"本库实测抓到的
# 赌博/加速器/盗版站"—— 那本身就是画像。真实值从 platform_domains.txt 的
# [spam] 节读。
SPAM_DOMAINS = platform_cfg.spam_domains()

# 光秃秃的模板主题：整封主题就是 "[品牌] Message / 通知 / Newsletter"
# 实测抓到 `promo@shop.example.com | [示例品牌] Message`。
# **必须带方括号**：第一版写成 "任意前缀 + 通知$"，
# 把真人来信 `someone@example.com | 示例杯创新创业大赛复赛通知` 误杀了。
RE_BARE_TEMPLATE = re.compile(
    r"^\s*[\[【][^\]】]{1,16}[\]】]\s*"
    r"(?:message|notification|newsletter|update|公告|通知)\s*[!！.]?\s*$", re.I)

# --------------------------------------------------------------------------
# 策略开关：**已知消费级平台**发来的一次性验证码怎么处理。
#   "drop"       默认。各类消费平台的登录码占全库约 9%，
#                无行动价值（真要取码得开 App/网页），跳过最省。
#   "local_only" 改成这档 = "取正文 + 本地抽码 + 绝不送模型"，
#                多花 ~90 次 IMAP 取信，换来验证码能直接出现在日报里。
#   注意：**非**白名单来源的验证码永远是 local_only，不受这个开关影响
#   （见 local_only.otp_unknown）—— 否则一个我判断不出的招聘平台验证码会被静默丢掉。
OTP_FROM_PLATFORM = "drop"


def split_addr(addr):
    a = (addr or "").strip().lower()
    if "@" not in a:
        return "", ""
    lp, dp = a.rsplit("@", 1)
    return lp, dp


def match_key(addr):
    """公司域名 -> 域名；公共邮箱 -> 完整地址（与 common.py 一致）。"""
    lp, dp = split_addr(addr)
    if not dp:
        return ""
    return f"{lp}@{dp}" if dp in PUBLIC else dp


# ------------------------------------------------------------------- 句式 --

# 退信：信没送到 —— 求职场景里这是**必须处理**的行动项
RE_BOUNCE_FROM = re.compile(r"^(postmaster|mailer-daemon|mail-daemon|"
                            r"mailer|noreply-daemon)@", re.I)
RE_BOUNCE = re.compile(
    r"退信|未送达|发送失败|投递失败|无法投递|Undelivered|Delivery Status|"
    r"Mail Delivery|failure notice|returned mail|delivery has failed", re.I)

# 强行动项：出现即说明"要我做事/有结果了"，一律 llm。
# 注意：不含裸的 招聘/投递/简历 —— 那三个词在"感谢投递模板"里一定有。
# 也不含 `尽快`：本库 999 封主题里它一次都没出现，却会让
# "如有合适岗位会尽快与您联系"这种纯收件确认误送模型（回归用例实测）。
RE_ACTION = re.compile(
    r"面试|笔试|测评|录用|offer|入职|复试|终面|初面|签约|三方协议|背调|体检|"
    r"候选人|请于|截止|邀请您完成|邀请你完成|邀请您参加|邀请你参加|"
    r"邀请您更新|邀请你更新|更新.{0,4}简历|完善.{0,4}简历|补充.{0,4}材料|"
    r"参加在线|在线编程|机试|宣讲会|招聘会|宣讲|"
    r"未通过|不通过|很遗憾|遗憾|录取|调剂|转正|"
    r"interview|assessment|coding test|online test|next step|"
    r"application status|schedule", re.I)

# ATS 模板的"废话尾巴"：这些短语本身不含任何行动项，先剥掉再做行动项判定。
# 剥掉之后，"感谢投递……进入人才库，如有合适岗位会尽快与您联系" 就干干净净
# 落到 template；而"感谢投递，请于 X 前完成测评"里的 请于/测评 仍在，照样 llm。
RE_ATS_BOILER = re.compile(
    r"如有合适|有合适的?岗位|尽快(?:与|和)?您?联系|纳入人才库|进入人才库|"
    r"存入人才库|人才库|保持联系|保持沟通|耐心等待|等待(?:我们的)?(?:后续)?通知|"
    r"后续(?:再)?(?:与|和)?您?联系|请勿回复|请勿答复|无需回复|不必回复|"
    r"不予回复|系统自动发送|自动发送", re.I)

# 招聘系统收件确认模板：只有"收到了/谢谢"，没有任何行动项。
# `感谢.{0,6}(投递|应聘|申请)` 要能覆盖 "感谢您应聘" / "感谢你的投递" 两种写法；
# 因为排在 RE_ACTION 之后，"感谢您的投递，请于 X 前完成测评" 不会被它截走。
RE_ATS_RECEIPT = re.compile(
    r"感谢.{0,6}(?:投递|应聘|申请|来信|关注)|投递成功|简历已?收到|"
    r"已收到您的简历|应聘成功提交|申请已提交|收件确认|"
    r"thank you for your application|thanks for (?:your )?appl|"
    r"application received|we(?:'ve| have) received your application", re.I)

# 自动回复。注意 common.py 里的 AUTO_PAT 带的 `已收到您的` 太宽：
# "已收到您的退款申请"会被误判成自动回复，
# 这里收紧成"已收到您的来信/邮件/简历"这一类真正的收件确认。
RE_AUTOREPLY = re.compile(
    r"auto.?reply|autoreply|automatic reply|自动回复|自动答复|系统自动|"
    r"自动发送|out of office|away from|已收到您的(?:来信|邮件|来函|简历)|"
    r"感谢您的来信|自动确认|请勿回复|本邮件由系统", re.I)

# 反诈 / 安全宣导：主题里出现"招聘""求职"只是宣讲内容，不是给我的行动项。
# 不拦这一条的话，10000@qq.com 的反诈广告会命中求职闸门、被当重要邮件送模型。
RE_ADVISORY = re.compile(
    r"警惕|反诈|诈骗|防诈|防骗|骗局|安全提醒|安全指南|安全警示|反诈指南|"
    r"诈骗案例|上当", re.I)

# 凭证：验证码 / 密码 / 通行证 / 激活链接
# （`账号验证`"邮箱确认"这类不含"码"字的也要算 —— demo-service 的
#   "示例产品 账号验证" 一开始漏了，落到了 template）
RE_SECRET = re.compile(
    r"验证码|校验码|动态码|确认码|一次性(?:代码|密码|口令)|通行证|激活码|"
    r"密码|口令|PIN\b|passcode|verification code|security code|"
    r"one[- ]time (?:code|password)|confirmation code|验证您的邮箱|"
    r"请验证|邮箱验证|账号验证|邮箱确认|验证邮箱|密保|账号激活|"
    r"verify your (?:email|account)|confirm your (?:email|account)", re.I)

# 营销 / 促销 / 平台广告
RE_PROMO = re.compile(
    r"<广告>|（广告）|\(广告\)|\(AD\)|AD\)|广告|优惠|折扣|特价|限时|大促|"
    r"会员节|会员|积分|领券|券|红包|抽奖|好礼|礼品|免费|免费领|免费送|"
    r"赢取|赢|抢购|秒杀|砍价|特卖|生日|活动|邀请函|礼包|开班|冲刺营|"
    r"送你|尽在QQ邮箱|邮箱APP|使用小贴士|巧用邮箱|"
    r"newsletter|deals?\b|sale\b|discount|coupon|promotion|marketing|"
    r"sponsor|advertis|subscribe|welcome to|get started|tips|"
    r"try .{0,12}free|upgrade", re.I)

# 收据 / 账单 / 订单 / 支付
RE_RECEIPT = re.compile(
    r"发票|收据|账单|订单|退款|扣款|付款|支付|交易|充值|续费|到期|过期|"
    r"对账单|receipt|invoice|refund|order|payment|purchase|subscription|"
    r"billing|renew|expir|storage|储存空间|储存|中转站", re.I)

# 平台机器通知：登录 / 安全 / CI / OAuth
RE_NOTIFY = re.compile(
    r"登录提醒|新登录|登录操作|新的登录|新设备|新电脑|异常登录|安全提醒|安全警报|"
    r"身份认证|两步|2FA|two-factor|two factor|恢复代码|recovery code|"
    r"OAuth|third-party|first-party|workflow|Run (?:failed|started|completed)|"
    r"Run failed|PR #|Issue #|Pull request|Merge|CI\b|依赖|构建|"
    r"new login|sign[- ]?in|security alert|verify your email|"
    r"privacy settings|使用条款|terms of|policy|"
    r"账号绑定|绑定通知|举报处理结果|好友申请", re.I)

# 竞赛 / 学校事务（重要的实体通知，不是营销）
# 只留**通用词**：具体赛事名同样属于"我参加过什么"的画像，已移到
# platform_domains.txt 的 [contest] 节（见 platform_cfg.contest_keywords）。
_EXTRA_CONTEST = "|".join(re.escape(c) for c in platform_cfg.contest_keywords())
RE_COMPETITION = re.compile(
    r"大赛|竞赛|赛事|赛道|赛区|初赛|复赛|决赛|省赛|国赛|"
    r"创新创业|作品上传|奖项|获奖|证书|奖学金|保研|毕设|毕业论文|"
    r"导师|辅导员|学院|教务处|学生社团|社团|立项|结项|申报|成绩|答辩|"
    r"训练营|课程通知" + (("|" + _EXTRA_CONTEST) if _EXTRA_CONTEST else ""), re.I)

# 疑似垃圾：随机短主题、乱码、非公共邮箱的数字串
RE_SPAM_SUBJECT = re.compile(
    r"^\s*(?:\d{1,6}[a-z]{0,3}|[a-z]{1,4}\d{1,4}|\d{1,2}:\d{2}(?::\d{2})?|"
    r"[\W_]{1,8})\s*$", re.I)
RE_SPAM_LOCAL = re.compile(r"\d{8,}")

# 公共邮箱里的"官方账号"地址（QQ 邮箱自己的 10000、中转站提醒等）
PUBLIC_BOT_LOCAL = re.compile(
    r"^(?:noreply|no_reply|no-reply|10000|postmaster|ftnremind_admin|admin|"
    r"service|system|notice|mail|hr|news|marketing)\b", re.I)


# ------------------------------------------------------------------ 判分 --

def classify(from_addr, subject):
    """返回 (bucket, rule_name)。bucket ∈ drop / template / llm / local_only。

    规则自上而下、首个命中即返回；rule_name 说明"为什么"。
    """
    lp, dom = split_addr(from_addr)
    subj = (subject or "").replace("\r", " ").replace("\n", " ")

    # ---- 预计算闸门 ----
    f_edu = dom.endswith(EDU_SUFFIX)
    f_job_dom = dom in JOB_DOMAINS
    f_platform = dom in PLATFORM_DOMAINS
    f_public = dom in PUBLIC
    f_bot = bool(BOT_PREFIX.match(lp))
    # "求职相关"闸门：命中者永不被 drop（只能 template / llm / local_only）
    f_job = (f_job_dom or f_edu
             or bool(re.search(r"应聘|简历|求职|面试|笔试|测评|录用|实习|校招|"
                               r"应届|管培|投递|招聘|岗位|职位|内推|offer|"
                               r"意向|入职|候选人", subj, re.I)))
    f_secret = bool(RE_SECRET.search(subj))
    # 先剥掉 ATS 废话尾巴，再做行动项判定（见 RE_ATS_BOILER 注释）
    subj_eff = RE_ATS_BOILER.sub(" ", subj)
    f_action = bool(RE_ACTION.search(subj_eff))
    f_comp = bool(RE_COMPETITION.search(subj))

    # 1. 退信 —— 投递失败的告警，只有量极小（5/999）但每条都要看原因，送模型
    if RE_BOUNCE_FROM.match(lp) or RE_BOUNCE.search(subj):
        return "llm", "bounce.delivery_failed"

    # 2. 求职语境 + 凭证 —— 笔试通行证 / 测评账号密码 / 平台邮箱验证
    if f_secret and (f_job or f_edu or f_comp):
        return "local_only", "local_only.job_secret"

    # 2.5 反诈/安全宣导 —— 先于 action 判定，避免"招聘"二字把广告拉进 llm；
    #     带真行动项（面试/笔试/验证码）的不受影响，仍走各自的规则
    if RE_ADVISORY.search(subj) and not f_action and not f_secret:
        return "drop", "drop.security_advisory"

    # 3. 强行动项 —— 面试/笔试/测评/offer/截止/邀请您参加，先于一切 drop
    if f_action:
        return "llm", "llm.action_item"

    # 4. 招聘系统收件确认模板（无行动项）—— 本地归档，不送模型
    if RE_ATS_RECEIPT.search(subj):
        return "template", "template.ats_receipt"

    # 5. 自动回复
    if RE_AUTOREPLY.search(subj):
        return "template", "template.autoreply"

    # 6. 真人回复 —— 有 Re:/回复: 且不是机器人前缀、不是招聘系统群发
    if re.match(r"^\s*(?:re|回复|答复|回覆|转发|fw|fwd)\s*[:：]", subj, re.I) \
            and not f_bot and not f_platform and dom not in JOB_DOMAINS:
        return "llm", "llm.human_reply"

    # 7. 确证批量平台（域名白名单）—— 非求职语境才允许
    if f_platform and not f_job:
        if f_secret and OTP_FROM_PLATFORM == "local_only":
            return "local_only", "local_only.otp_platform"
        return "drop", "drop.platform_domain"

    # 7.5 QQ 邮箱自己的官方号（10000@qq.com）—— 全是安全宣导/邮箱推广，
    #     公共邮箱闸门管不到它，单拎出来；求职语境除外
    if dom == "qq.com" and lp == "10000" and not f_job:
        return "drop", "drop.qq_official"

    # 8. 营销 / 促销（非求职语境）
    if RE_PROMO.search(subj) and not f_job and not f_action:
        return "drop", "drop.promo"

    # 9. 收据 / 账单 / 订单 / 发票
    if RE_RECEIPT.search(subj) and not f_job:
        return "drop", "drop.receipt"

    # 10. 平台机器通知：登录 / 安全 / CI / OAuth
    if RE_NOTIFY.search(subj) and not f_job and not f_action:
        return "drop", "drop.notification"

    # 10.5 光秃秃的模板主题（"[示例品牌] Message"这种），非求职语境才丢
    if RE_BARE_TEMPLATE.match(subj) and not f_job:
        return "drop", "drop.bare_template"

    # 11. 其他来源的凭证（各类平台的验证码、邮箱验证、账号激活码…）
    #     —— 放这里是为了"非白名单来源的验证码"不静默消失
    if f_secret:
        return "local_only", "local_only.otp_unknown"

    # 12. 疑似垃圾（随机主题 / 非公共邮箱的长数字串）
    if RE_SPAM_SUBJECT.match(subj) or (not f_public and RE_SPAM_LOCAL.search(lp)) \
            or dom in SPAM_DOMAINS:
        return "drop", "drop.spam_heuristic"

    # 13. 公共邮箱的**个人**地址 —— QQ 号/163 就是中国真人的地址形态，
    #     313-321 那批"附件1""某某队"就是同学，绝不能按域名丢；
    #     同理 RE_SPAM_LOCAL(长数字串) 对公共邮箱一律不生效
    if f_public and not f_bot and not PUBLIC_BOT_LOCAL.match(lp):
        return "llm", "llm.human_personal"

    # 14. 竞赛 / 学校事务
    if f_comp:
        return "llm", "llm.competition_edu"

    # 15. 兜底：机器人前缀 -> 取正文本地看（不花钱，也不会误杀）
    if f_bot:
        return "template", "fallback.bot_unknown"

    # 16. 兜底：看起来是个人/公司的真人地址 -> 送模型
    if dom:
        return "llm", "fallback.human_unknown"

    # 17. 连域名都解析不出来 —— 只取正文，本地看
    return "template", "fallback.unparsable"


# --------------------------------------------------------------- 脱敏工具 --

RE_EMAIL = re.compile(r"[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}")
RE_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
RE_CODE = re.compile(r"(?<!\d)\d{4,8}(?!\d)")
RE_IDCARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
RE_LINK = re.compile(r"https?://[^\s<>\"')]{20,}")


def mask(text, keep_code=False):
    """送模型前的脱敏。验证码默认也打掉（local_only 的码不该进模型）。"""
    t = text or ""
    t = RE_IDCARD.sub("[身份证]", t)
    t = RE_PHONE.sub("[手机号]", t)
    t = RE_EMAIL.sub("[邮箱]", t)
    if not keep_code:
        t = RE_CODE.sub("[数字]", t)
    t = RE_LINK.sub("[长链接]", t)
    return t


def clip(text, limit=1500):
    """截断：只留开头，尾部通常是引用的历史邮件和页脚。"""
    t = (text or "").strip()
    return t if len(t) <= limit else t[:limit] + "\n...[已截断]"


def plan(bucket):
    """该层对应的动作计划。"""
    return dict(PLAN.get(bucket, PLAN["template"]))


# ----------------------------------------------------------------- 自测 --

if __name__ == "__main__":
    # 用例取自本地邮件样本，已脱敏为示例值（原始 id 见行内注释）
    CASES = [
        # 646 示例网络 感谢投递 -> 模板
        ("noreply-recruit@hr.ats-one.example.com",
         "张三，感谢你投递示例网络公司的示例工程师职位", "template"),
        # 706 招聘平台 面试邀请 -> 必须 llm（机器人前缀 + 招聘域）
        ("support@usermail.jobboard.example.com",
         "云图信息科技股份有限公司邀请你参加在线AI面试", "llm"),
        # 705 测评平台 测评邀请 -> 必须 llm
        ("noreply@shmail.ats-three.example.com",
         "【云图信息科技股份有限公司2027届校园招聘】测评邀请通知｜解锁求职下一阶段", "llm"),
        # 647 感谢投递模板（同一发件人，同一天的另一封）-> 模板
        ("noreply@shmail.ats-three.example.com", "感谢您投递本公司职位", "template"),
        # 693 星辰 测评 -> llm；692 更新简历 -> llm
        ("hr@xingchen.example.com",
         "[星辰科技]邀请您完成在线测评【若多次收到提醒，仅需完成一次测评即可!!!!】", "llm"),
        ("hr@xingchen.example.com", "[星辰科技]邀请您更新您的简历", "llm"),
        # 657 真人回复 -> llm
        ("hr@demo-corp.example.com",
         "RE: 应聘示例工程师-张三-某某大学-某某专业", "llm"),
        # 642 人事自动回复 -> template
        ("hr@demo-hr.example.com",
         "人事 Auto Reply  某某大学+某某专业+本科+示例工程师+张三", "template"),
        # 674 退信（demo-link 是示例公司）-> llm
        ("postmaster@demo-link.example.com", "来自demo-link.example.com的退信", "llm"),
        # 686 招聘平台验证邮箱 -> local_only
        ("service@jobportal.example.com", "[示例招聘] 请验证您的邮箱", "local_only"),
        # 313 同学报名材料（QQ 号个人地址）-> llm，绝不能被当垃圾
        ("100000001@qq.com", "某某大学某某学院报名材料", "llm"),
        # 725 空主题的个人来信 -> llm
        ("100000002@qq.com", "", "llm"),
        # 159 导师回复 -> llm
        ("teacher@example-univ.edu.cn", "Re:某某学院导师制学生申请表", "llm"),
        # 536 竞赛复赛通知 -> llm
        ("someone@example.com", "示例杯创新创业大赛复赛通知", "llm"),
        # 656 QQ 邮箱反诈营销（含"招聘"二字）-> drop 而非 llm
        ("10000@qq.com", "QQ邮箱安全提醒：警惕招聘欺诈等7类诈骗！", "drop"),
        # 108 平台退款回执（含"申请"）-> drop
        ("noreply@gameplatform.example.com", "已收到您的退款申请", "drop"),
        # 1 平台登录通知 -> drop
        ("noreply@gameplatform.example.com", "示例游戏平台上有新的登录操作", "drop"),
        # 45 设备厂商收据 -> drop
        ("no_reply@devicevendor.example.com", "示例设备厂商提供的收据", "drop"),
        # 301 代码托管平台 OAuth -> drop
        ("noreply@github.com",
         "[示例代码托管] A third-party OAuth application has been added to your account", "drop"),
        # 赛事营销 -> drop
        ("contest-mail.example.com", "【示例杯】往届选手们，示例杯喊你回来领免费T恤啦！", "drop"),
        # 行业赛事重要通知（含截止）-> llm
        ("contest-org.example.com",
         "【“示例杯”赛事重要通知】大赛作品上传将于 8 月 27 日截止，请及时完成提交", "llm"),
        # 垃圾邮件（随机主题）-> drop
        ("someone2@example.com", "74s", "drop"),
        # ---- template / llm 边界的回归用例（Q4 的核心判据）----
        # 同一发件人、同一天：有行动项 -> llm，只有"收到了" -> template
        ("noreply@shmail.ats-three.example.com",
         "感谢您的投递，请于 9 月 25 日 23:59 前完成在线测评（本邮件由系统自动发送，请勿回复）",
         "llm"),
        ("noreply@shmail.ats-three.example.com",
         "感谢你的投递，我们已收到你的简历，请耐心等待后续通知（请勿回复）", "template"),
        ("noreply@mail.ats-two.example.com",
         "【笔试通知】张三，请于 9月24日 前登录系统完成笔试（本邮件由系统自动发送）",
         "llm"),
        ("hr@somecorp.example.com",
         "感谢您应聘我司示例工程师岗位，您的简历已进入人才库，如有合适岗位会尽快与您联系",
         "template"),
        # 修正记录：demo-service 的"账号验证"原先漏进 template
        ("support@demo-service.example.com", "示例产品 账号验证", "local_only"),
        # 修正记录：RE_BARE_TEMPLATE 第一版把真人来信误杀，现要求带方括号
        ("someone@example.com", "示例杯创新创业大赛复赛通知", "llm"),
        ("promo@shop.example.com", "[示例品牌] Message", "drop"),
        # 修正记录：10000@qq.com 走 qq_official
        ("10000@qq.com", "开始使用你的邮箱", "drop"),
        # 修正记录：RE_AUTOREPLY 去掉 `已收到您的` 泛匹配
        ("noreply@gameplatform.example.com", "已收到您的退款申请", "drop"),
        # 遗留不确定案例：代码托管平台强制 2FA —— 判 drop（非求职），记录在案
        ("noreply@github.com",
         "[ACTION REQUIRED] Your example-code-host account, SanZhang2027, will soon require 2FA",
         "drop"),
    ]
    bad = 0
    for f, s, want in CASES:
        got, rule = classify(f, s)
        flag = "OK " if got == want else "FAIL"
        if got != want:
            bad += 1
        print(f"{flag} want={want:10s} got={got:10s} {rule:26s} {f} | {s[:42]}")
    print(f"\n{len(CASES) - bad}/{len(CASES)} passed")
