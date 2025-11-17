# -*- coding: utf-8 -*-
# 飞机打卡机器人（群用）

import os
import re
import shutil
from time import perf_counter
from datetime import datetime, timezone, timedelta, time as dtime
from typing import Optional, Any, Dict, Set, List

from telegram import (
    Update, constants, BotCommand,
    BotCommandScopeDefault, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
)
from telegram.error import RetryAfter
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, Defaults, filters as F, PicklePersistence
)

# ========= 基础配置 =========
BOT_TOKEN = os.getenv("BOT_TOKEN") or "8474574984:AAEQaBlw1MED0EPlx0sFD_gyFXJn7hh8rQw"
LOCAL_TZ = timezone(timedelta(hours=7))   # 柬埔寨 UTC+7

# 管理员（超时后会 @）
MANAGER_ID = 7736035882
MANAGER_NAME = "Kun"
MANAGER_USERNAME = "Knor1130"   # Telegram 用户名，用于真正 @

# ========= 业务参数 =========
LIMITS       = {"toilet": 10, "smoke": 10, "meal": 30}          # 每次最大时长（分钟）
LIMITS_COUNT = {"toilet": 5,  "smoke": 5,  "meal": 3}           # 每类每班最多次数
MIN_SECONDS  = {"toilet": 30, "smoke": 30, "meal": 60}          # 最小时长（秒）
COOLDOWN_MIN = {"toilet": 5,  "smoke": 5,  "meal": 15}          # 冷却（分钟）
GRACE_MINUTES = 3                                               # 超时后再等 X 分钟 @ 管理员

HELP_DELETE_MINUTES = 1   # 提示类消息保留时间（分钟）

TITLES = {"toilet": "厕所", "smoke": "抽烟", "meal": "吃饭"}

TRIGGERS: Dict[str, Set[str]] = {
    "toilet": {"厕所", "上厕所", "wc", "toilet", "restroom", "washroom", "bathroom", "pee", "loo"},
    "smoke":  {"抽", "抽烟", "抽煙", "烟", "煙", "smoke", "smoking", "cigarette"},
    "meal":   {"吃", "吃饭", "吃飯", "用餐", "eat", "eating", "meal", "lunch", "dinner", "food"},
}

# ========= 小工具 =========
def current_shift_label() -> str:
    now_local = datetime.now(LOCAL_TZ).time()
    return "白班" if dtime(7, 0) <= now_local < dtime(19, 0) else "夜班"

def mention_user_html(user) -> str:
    name = (getattr(user, "full_name", None) or getattr(user, "first_name", None) or "用户")
    name = name.replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={user.id}">{name}</a>'

def mention_id_html(user_id: int, visible_text: str) -> str:
    safe = visible_text.replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={user_id}">{safe}</a>'

def fmt_dur_mmss(seconds: int) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    return f"{m}分{s:02d}秒"

def ensure_stats_for_chat(ud: dict, chat_id: int) -> dict:
    """
    每个用户按群单独统计：
    ud["stats_by_chat"][chat_id]["smoke"|"toilet"|"meal"]["count"|"dur"]
    """
    all_stats = ud.setdefault("stats_by_chat", {})
    key = str(chat_id)
    if key not in all_stats:
        all_stats[key] = {
            "smoke":  {"count": 0, "dur": 0},
            "toilet": {"count": 0, "dur": 0},
            "meal":   {"count": 0, "dur": 0},
        }
    return all_stats[key]

async def is_admin(update: Update) -> bool:
    try:
        member = await update.effective_chat.get_member(update.effective_user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

def chat_is_muted(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    return bool(ctx.application.chat_data.get(chat_id, {}).get("muted", False))

async def safe_send(bot, chat_id: int, html_text: str, preview: bool = False):
    MAX = 3500
    for i in range(0, len(html_text), MAX):
        chunk = html_text[i:i+MAX]
        for attempt in range(2):
            try:
                await bot.send_message(
                    chat_id=chat_id, text=chunk,
                    parse_mode=constants.ParseMode.HTML,
                    disable_web_page_preview=not preview
                )
                break
            except RetryAfter as e:
                from time import sleep
                sleep(int(getattr(e, "retry_after", 3)))
            except Exception:
                if attempt == 1:
                    pass

def all_trigger_words() -> Set[str]:
    s: Set[str] = set()
    for words in TRIGGERS.values():
        s |= {w.lower() for w in words}
    return s

START_RE = re.compile(r"^(" + "|".join(map(re.escape, sorted(all_trigger_words()))) + r")$", re.IGNORECASE)
BACK_RE  = re.compile(r"^(回来|回|back|1)$", re.IGNORECASE)

# ========= 删除提示类消息（打卡相关误操作 & 员工乱输提示） =========
async def delete_help_messages(context: ContextTypes.DEFAULT_TYPE):
    """
    延迟删除类消息：
    - user_msg_id：用户发的那条
    - bot_msg_id：机器人回的那条
    （这里不区分管理员，因为管理员不会走 text_help；走的是打卡相关误操作）
    """
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    user_msg_id = data.get("user_msg_id")
    bot_msg_id = data.get("bot_msg_id")

    if not chat_id:
        return

    # 先删机器人自己的那条
    if bot_msg_id:
        try:
            await context.bot.delete_message(chat_id, bot_msg_id)
        except Exception:
            pass

    # 再删用户那条
    if user_msg_id:
        try:
            await context.bot.delete_message(chat_id, user_msg_id)
        except Exception:
            pass

# ========= 开始 / 结束 / 提醒 =========
async def begin(update: Update, ctx: ContextTypes.DEFAULT_TYPE, kind: str):
    """开始打卡：记录 active + 安排超时提醒 + 记录消息ID，方便结束时删除"""
    user = update.effective_user
    chat = update.effective_chat
    msg  = update.effective_message
    if kind not in LIMITS:
        return

    ud = ctx.user_data

    # 已有进行中的打卡：提示 + 定时删除（打卡相关误操作）
    if ud.get("active"):
        notice = await msg.reply_html(
            f"{mention_user_html(user)} 已有进行中的打卡，请先发送“回来/回/back/1”或 /back 结束。"
        )
        ctx.job_queue.run_once(
            delete_help_messages,
            when=HELP_DELETE_MINUTES * 60,
            data={
                "chat_id": chat.id,
                "user_msg_id": msg.id,
                "bot_msg_id": notice.message_id,
            },
            name=f"del-already-{chat.id}-{msg.id}",
        )
        return

    stats = ensure_stats_for_chat(ud, chat.id)
    today_count = stats[kind]["count"]
    limit_count = LIMITS_COUNT.get(kind, 0)
    if limit_count and today_count >= limit_count:
        await msg.reply_html(
            f"{mention_user_html(user)} 本{current_shift_label()}次数已达上限 <b>{limit_count}</b> 次。"
        )
        return

    last_end_ts = ud.get(f"last_end_{kind}")
    if last_end_ts:
        delta_min = (datetime.now(timezone.utc).timestamp() - last_end_ts) / 60.0
        if delta_min < COOLDOWN_MIN.get(kind, 0):
            need = int(COOLDOWN_MIN.get(kind, 0))
            await msg.reply_html(
                f"{mention_user_html(user)} 刚结束不久，{TITLES[kind]} 冷却 <b>{need}</b> 分钟内请勿重复开始。"
            )
            return

    limit = LIMITS[kind]
    ud["active"] = {
        "type":  kind,
        "title": TITLES[kind],
        "start": datetime.now(timezone.utc),
        "limit": limit,
    }
    ud["last_chat_id"] = chat.id
    ud["_last_seen"] = datetime.now(timezone.utc).timestamp()

    # 记录用户名 & 超时时用 @username
    ud["user_username"] = getattr(user, "username", None)
    ud["user_link"] = mention_user_html(user)

    # 取消旧提醒
    for key in ("reminder_job", "grace_job"):
        job: Optional[Any] = ud.get(key)
        if job:
            try:
                job.schedule_removal()
            except Exception:
                pass
        ud[key] = None

    # 超时提醒本人
    run_at = datetime.now(timezone.utc) + timedelta(minutes=limit)
    ud["reminder_job"] = ctx.job_queue.run_once(
        remind_timeout, when=run_at,
        data={"uid": user.id, "chat_id": chat.id},
        name=f"remind-{user.id}",
    )
    # 宽限后提醒管理员
    ud["grace_job"] = ctx.job_queue.run_once(
        remind_grace, when=run_at + timedelta(minutes=GRACE_MINUTES),
        data={"uid": user.id, "chat_id": chat.id},
        name=f"grace-{user.id}",
    )

    if chat_is_muted(ctx, chat.id):
        return

    # 发送开始提示，并记录双方消息 ID，方便结束时删除
    sent = await ctx.bot.send_message(
        chat_id=chat.id,
        text=(f"{mention_user_html(user)} 开始计时（上限 {limit} 分）。\n"
              f"📊 本{current_shift_label()} {TITLES[kind]} 已 <b>{today_count}</b> 次 / 限制 <b>{limit_count}</b> 次。\n"
              f"回来后发送“回来/回/back/1”或使用 /back 结束。"),
        disable_web_page_preview=True,
        reply_to_message_id=msg.id,
    )

    ud["start_user_msg_id"] = msg.id          # 你发的 wc/抽烟/吃饭
    ud["start_bot_msg_id"]  = sent.message_id # 机器人“开始计时”

async def end_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """结束打卡：删除 3 条消息 + 统计本次时长 + 累积次数/分钟"""
    user = update.effective_user
    chat = update.effective_chat
    msg  = update.effective_message

    ud = ctx.user_data
    active = ud.get("active")

    # 当前没有进行中的打卡：提示 + 自动删除两条（打卡相关误操作）
    if not active:
        notice = await msg.reply_html(f"{mention_user_html(user)} 当前没有进行中的打卡。")
        ctx.job_queue.run_once(
            delete_help_messages,
            when=HELP_DELETE_MINUTES * 60,
            data={
                "chat_id": chat.id,
                "user_msg_id": msg.id,
                "bot_msg_id": notice.message_id,
            },
            name=f"del-noactive-{chat.id}-{msg.id}",
        )
        return

    # 先删 3 条消息：开始指令 + 开始提示 + 回来（管理员也一样删）
    start_user_msg_id = ud.pop("start_user_msg_id", None)
    start_bot_msg_id  = ud.pop("start_bot_msg_id", None)
    back_msg_id       = msg.id

    for mid in (start_user_msg_id, start_bot_msg_id, back_msg_id):
        if not mid:
            continue
        try:
            await ctx.bot.delete_message(chat.id, mid)
        except Exception:
            pass

    # 取消超时/宽限提醒
    for key in ("reminder_job", "grace_job"):
        job: Optional[Any] = ud.get(key)
        if job:
            try:
                job.schedule_removal()
            except Exception:
                pass
        ud[key] = None

    now = datetime.now(timezone.utc)
    start: datetime = active["start"]
    used_sec = int((now - start).total_seconds())
    limit_min = int(active["limit"])
    used_min, used_sec_rem = divmod(used_sec, 60)
    title = active.get("title", "打卡")
    key   = active["type"]

    stats = ensure_stats_for_chat(ud, chat.id)

    # 未达最小时长：不计入统计、不开冷却
    if used_sec < MIN_SECONDS.get(key, 0):
        ud.pop("active", None)
        ud["_last_seen"] = now.timestamp()
        if not chat_is_muted(ctx, chat.id):
            await ctx.bot.send_message(
                chat_id=chat.id,
                text=(f"{mention_user_html(user)} 本次用时 {used_min}分{used_sec_rem:02d}秒，"
                      f"低于最小时长（{MIN_SECONDS.get(key,0)} 秒），不计入统计。"),
                parse_mode=constants.ParseMode.HTML,
                disable_web_page_preview=True,
            )
        return

    # 正常计入统计 + 记录冷却起点
    stats[key]["count"] += 1
    stats[key]["dur"]   += used_sec
    ud.pop("active", None)
    ud[f"last_end_{key}"] = now.timestamp()
    ud["_last_seen"] = now.timestamp()

    today_count = stats[key]["count"]
    today_total_sec = stats[key]["dur"]
    human_this  = f"{used_min}分{used_sec_rem:02d}秒"
    human_limit = f"{limit_min}分"
    human_total = fmt_dur_mmss(today_total_sec)
    overtime = used_min > limit_min or (used_min == limit_min and used_sec_rem > 0)
    limit_count = LIMITS_COUNT.get(key, 0)

    base = (f"✅ {mention_user_html(user)} 本次结束，用时 {human_this}（上限 {human_limit}）。\n"
            f"📊 本{current_shift_label()} {title}：第 <b>{today_count}</b> 次（限制 <b>{limit_count}</b> 次），累计 <b>{human_total}</b>。")
    text = base + ("\n⚠️ 本次已超时。" if overtime else "\n✅ 本次未超时。")

    if not chat_is_muted(ctx, chat.id):
        await ctx.bot.send_message(
            chat_id=chat.id, text=text,
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True,
        )

# ⏰ 刚超时提醒当事人（优先 @username）
async def remind_timeout(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    uid = data.get("uid")
    chat_id = data.get("chat_id")
    if uid is None or chat_id is None:
        return

    app = context.application
    ud = app.user_data.get(uid) or {}
    active = ud.get("active")
    if not active:
        return  # 已经结束了

    title = active.get("title", "打卡")
    limit_min = int(active.get("limit", 0))

    username = ud.get("user_username")
    if username:
        who = f"@{username}"
    else:
        who = mention_id_html(uid, "这位同事")

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏰ {who} 的 {title} 已到上限 <b>{limit_min}</b> 分，请尽快发送“回来 / 回 / back / 1”或 /back 结束。",
        parse_mode=constants.ParseMode.HTML
    )

# ⏰ 超时 +3 分钟提醒管理员（真正 @Kun）
async def remind_grace(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    uid = data.get("uid")
    chat_id = data.get("chat_id")
    if uid is None or chat_id is None:
        return

    app = context.application
    ud = app.user_data.get(uid) or {}
    active = ud.get("active")
    if not active:
        return  # 已结束则不提醒管理员

    title = active.get("title", "打卡")
    start: datetime = active.get("start") or datetime.now(timezone.utc)
    used = fmt_dur_mmss(int((datetime.now(timezone.utc) - start).total_seconds()))

    # 当事人显示
    user_link = ud.get("user_link") or mention_id_html(uid, "这位同事")

    # 管理员真正 @
    if MANAGER_USERNAME:
        manager_call = f"@{MANAGER_USERNAME}"
    else:
        manager_call = mention_id_html(MANAGER_ID, "管理员")

    await context.bot.send_message(
        chat_id=chat_id,
        text=(f"⚠️ {manager_call} 提醒：{user_link} 的 {title} 已超过上限并宽限 <b>{GRACE_MINUTES}</b> 分钟仍未结束，"
              f"当前已用时 <b>{used}</b>。"),
        parse_mode=constants.ParseMode.HTML,
        disable_web_page_preview=True
    )

# ========= 换班：发群里统计并清状态 =========
async def reset_shift(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    bot = context.bot
    if not hasattr(app, "user_data"):
        return

    now_utc = datetime.now(timezone.utc)
    grouped: Dict[int, List[str]] = {}

    # 统计当前仍然 active 的人
    for uid, ud in list(app.user_data.items()):
        active = ud.get("active")
        if not active:
            continue
        title = active.get("title", "打卡")
        start: datetime = active.get("start") or now_utc
        used_sec = int((now_utc - start).total_seconds())
        start_local = start.astimezone(LOCAL_TZ).strftime("%H:%M")
        line = (
            f"• <a href=\"tg://user?id={uid}\">这位同事</a> — {title} | 已用时 <b>{fmt_dur_mmss(used_sec)}</b> | "
            f"开始 <b>{start_local}</b> | ID <code>{uid}</code>"
        )
        chat_id = ud.get("last_chat_id")
        if chat_id:
            grouped.setdefault(chat_id, []).append(line)

    # 发群里统计
    for chat_id, lines in grouped.items():
        text = "🕖 换班统计：共有 <b>{}</b> 人尚未回来，系统已自动结束：\n{}".format(
            len(lines), "\n".join(lines)
        )
        try:
            await bot.send_message(
                chat_id=chat_id, text=text,
                parse_mode=constants.ParseMode.HTML,
                disable_web_page_preview=True
            )
        except Exception:
            pass

    # 清状态并取消提醒
    for uid, ud in list(app.user_data.items()):
        if not ud.get("active"):
            continue
        for key in ("reminder_job", "grace_job"):
            job: Optional[Any] = ud.get(key)
            if job:
                try:
                    job.schedule_removal()
                except Exception:
                    pass
            ud[key] = None
        ud.pop("active", None)
        ud.pop("start_user_msg_id", None)
        ud.pop("start_bot_msg_id", None)
        ud["_last_seen"] = now_utc.timestamp()

    # 清空当班统计（所有群），长期不用的用户清理
    for _uid, ud in list(app.user_data.items()):
        all_stats = ud.get("stats_by_chat") or {}
        for chat_stats in all_stats.values():
            for k in chat_stats:
                chat_stats[k]["count"] = 0
                chat_stats[k]["dur"] = 0
        last = ud.get("_last_seen")
        if (not ud.get("active")) and last and (now_utc.timestamp() - last > 30 * 86400):
            try:
                del app.user_data[_uid]
            except Exception:
                pass

# ========= 命令 =========
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await is_admin(update):
        txt = ("打卡说明：\n"
               "• 开始：发送“厕所/抽烟/吃饭”（或 wc/smoke/eat 等别名）\n"
               "• 结束：发送“回来/回/back/1”或 /back\n"
               "• 时长：厕所10分，抽烟10分，吃饭30分；到时提醒；超时提示。\n"
               "• 最小时长：厕所30秒、抽烟30秒、吃饭60秒，未达不计且不冷却。\n"
               f"• 超时：到时提醒本人，{GRACE_MINUTES} 分钟后仍未结束会@管理员。\n"
               "• 管理：/who /summary /setlimit /setcount /mute /unmute")
    else:
        txt = ("打卡说明：\n"
               "• 开始：发送“厕所 / 抽烟 / 吃饭”（或 wc / smoke / eat）\n"
               "• 结束：发送“回来 / 回 / back / 1”")
    await update.effective_message.reply_html(txt)

async def cmd_toilet(update: Update, ctx: ContextTypes.DEFAULT_TYPE): await begin(update, ctx, "toilet")
async def cmd_smoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):  await begin(update, ctx, "smoke")
async def cmd_meal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):   await begin(update, ctx, "meal")
async def cmd_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):   await end_session(update, ctx)

async def cmd_who(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        return await update.effective_message.reply_html("❌ 仅管理员可用。")
    chat = update.effective_chat
    app = ctx.application
    now_utc = datetime.now(timezone.utc)
    lines = []
    for uid, ud in list(app.user_data.items()):
        active = ud.get("active")
        if not active or ud.get("last_chat_id") != chat.id:
            continue
        start = active.get("start") or now_utc
        lines.append(
            f"• <a href=\"tg://user?id={uid}\">这位同事</a> — {active.get('title','打卡')} | "
            f"已用 <b>{fmt_dur_mmss(int((now_utc - start).total_seconds()))}</b> | "
            f"开始 <b>{start.astimezone(LOCAL_TZ).strftime('%H:%M')}</b> | ID <code>{uid}</code>"
        )
    await update.effective_message.reply_html(
        "📋 当前未结束清单：\n" + "\n".join(lines) if lines else "👍 本群当前无人处于进行中状态。"
    )

async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        return await update.effective_message.reply_html("❌ 仅管理员可用。")
    chat = update.effective_chat
    app = ctx.application
    lines = [f"📊 本{current_shift_label()}汇总（按用户）："]
    for uid, ud in list(app.user_data.items()):
        all_stats = ud.get("stats_by_chat") or {}
        stats = all_stats.get(str(chat.id)) or {}
        per = []
        for k in ("smoke", "toilet", "meal"):
            c = stats.get(k, {}).get("count", 0)
            d = stats.get(k, {}).get("dur", 0)
            if c or d:
                per.append(f"{TITLES[k]} <b>{c}</b> 次 / {fmt_dur_mmss(d)}")
        if per:
            lines.append(f"• {mention_id_html(uid, '这位同事')} — " + "；".join(per))
    await update.effective_message.reply_html(
        "\n".join(lines) if len(lines) > 1 else "暂无数据。"
    )

async def cmd_setlimit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        return await update.effective_message.reply_html("❌ 仅管理员可用。")
    try:
        name, minutes = ctx.args[0], int(ctx.args[1])
    except Exception:
        return await update.effective_message.reply_html("用法：/setlimit 抽烟 12")
    key = next((k for k, v in TITLES.items() if v == name), None)
    if not key:
        return await update.effective_message.reply_html("类型不对：厕所/抽烟/吃饭")
    LIMITS[key] = minutes
    await update.effective_message.reply_html(f"✅ 已将上限设置为 <b>{minutes}</b> 分。")

async def cmd_setcount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        return await update.effective_message.reply_html("❌ 仅管理员可用。")
    try:
        name, cnt = ctx.args[0], int(ctx.args[1])
    except Exception:
        return await update.effective_message.reply_html("用法：/setcount 抽烟 2")
    key = next((k for k, v in TITLES.items() if v == name), None)
    if not key:
        return await update.effective_message.reply_html("类型不对：厕所/抽烟/吃饭")
    LIMITS_COUNT[key] = cnt
    await update.effective_message.reply_html(f"✅ 已将每班次数上限设置为 <b>{cnt}</b> 次。")

async def cmd_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        return await update.effective_message.reply_html("❌ 仅管理员可用。")
    ctx.chat_data["muted"] = True
    await update.effective_message.reply_html("🔕 已开启静音（仅保留换班统计与到时提醒）。")

async def cmd_unmute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        return await update.effective_message.reply_html("❌ 仅管理员可用。")
    ctx.chat_data["muted"] = False
    await update.effective_message.reply_html("🔔 已取消静音（管理员提醒仍会保留）。")

async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.effective_message.reply_html(f"{mention_user_html(u)} 的 user_id 是 <code>{u.id}</code>")

async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t0 = perf_counter()
    m = await update.effective_message.reply_text("pong…")
    dt = (perf_counter() - t0) * 1000
    await m.edit_text(f"pong {dt:.0f} ms")

# ========= 文本触发 =========
def normalize_txt(s: str) -> str:
    return (s or "").strip().lower()

async def text_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = normalize_txt(update.effective_message.text)
    for kind, words in TRIGGERS.items():
        if txt in {w.lower() for w in words}:
            await begin(update, ctx, kind)
            return

async def text_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = normalize_txt(update.effective_message.text)
    if BACK_RE.match(txt):
        await end_session(update, ctx)

# 乱输入：普通员工提示打卡说明，管理员完全忽略
async def text_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # 管理员：不提示、不删
    if await is_admin(update):
        return

    if chat_is_muted(ctx, update.effective_chat.id):
        return

    msg  = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    txt = (
        "打卡说明：\n"
        "• 开始：发送“厕所/抽烟/吃饭”（或 wc/smoke/eat 等别名）\n"
        "• 结束：发送“回来/回/back/1”或 /back\n"
        "• 时长：厕所10分，抽烟10分，吃饭30分；到时提醒；超时提示。\n"
        "• 最小时长：厕所30秒、抽烟30秒、吃饭60秒，未达不计且不冷却。\n"
        f"• 超时：到时提醒本人，{GRACE_MINUTES} 分钟后仍未结束会@管理员。"
    )

    sent = await msg.reply_html(txt)

    ctx.job_queue.run_once(
        delete_help_messages,
        when=HELP_DELETE_MINUTES * 60,
        data={
            "chat_id": chat.id,
            "user_msg_id": msg.id,
            "bot_msg_id": sent.message_id,
        },
        name=f"del-help-{chat.id}-{msg.id}",
    )

# ========= 启动前：设置 / 菜单命令 =========
async def setup_bot_commands(app: Application):
    commands = [
        BotCommand("start", "查看打卡说明"),
        BotCommand("toilet", "开始厕所打卡"),
        BotCommand("smoke", "开始抽烟打卡"),
        BotCommand("meal", "开始吃饭打卡"),
        BotCommand("back", "结束打卡（回来）"),
        BotCommand("who", "查看当前未回来名单（管理员）"),
        BotCommand("summary", "查看本班汇总（管理员）"),
        BotCommand("setlimit", "设置上限时长（管理员）"),
        BotCommand("setcount", "设置每班次数上限（管理员）"),
        BotCommand("mute", "静音模式（管理员）"),
        BotCommand("unmute", "取消静音（管理员）"),
        BotCommand("id", "查看自己的 user_id"),
        BotCommand("ping", "延迟测试"),
    ]
    await app.bot.delete_my_commands(scope=BotCommandScopeDefault())
    await app.bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
    await app.bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())
    await app.bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    await app.bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
    await app.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())

# ========= 入口 =========
def backup_pickle():
    if os.path.exists("botdata.pkl"):
        os.makedirs("backup", exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2("botdata.pkl", f"backup/botdata-{ts}.pkl")

def main():
    if not BOT_TOKEN:
        raise RuntimeError("缺少 BOT_TOKEN：请设置环境变量 BOT_TOKEN 或在代码中填写。")

    defaults = Defaults(parse_mode=constants.ParseMode.HTML)
    persistence = PicklePersistence(filepath="botdata.pkl", update_interval=30)

    backup_pickle()
    app: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .defaults(defaults)
        .persistence(persistence)
        .post_init(setup_bot_commands)
        .build()
    )

    # 命令
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("toilet",  cmd_toilet))
    app.add_handler(CommandHandler("smoke",   cmd_smoke))
    app.add_handler(CommandHandler("meal",    cmd_meal))
    app.add_handler(CommandHandler("back",    cmd_back))
    app.add_handler(CommandHandler("who",     cmd_who))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("setlimit", cmd_setlimit))
    app.add_handler(CommandHandler("setcount", cmd_setcount))
    app.add_handler(CommandHandler("mute",    cmd_mute))
    app.add_handler(CommandHandler("unmute",  cmd_unmute))
    app.add_handler(CommandHandler("id",      cmd_id))
    app.add_handler(CommandHandler("ping",    cmd_ping))

    # 文本触发（群内）
    app.add_handler(MessageHandler(
        F.TEXT & F.ChatType.GROUPS & (~F.COMMAND) & F.Regex(START_RE),
        text_start
    ), group=0)

    app.add_handler(MessageHandler(
        F.TEXT & F.ChatType.GROUPS & (~F.COMMAND) & F.Regex(BACK_RE),
        text_back
    ), group=1)

    # 其它乱输（只有普通员工会走这里，管理员在 text_help 里直接 return）
    app.add_handler(MessageHandler(
        F.TEXT & F.ChatType.GROUPS & (~F.COMMAND) & (~F.Regex(START_RE)) & (~F.Regex(BACK_RE)),
        text_help
    ), group=99)

    # 定时：07:00 & 19:00（UTC+7）换班统计并清状态
    app.job_queue.run_daily(reset_shift, time=dtime(7, 0, tzinfo=LOCAL_TZ),  name="reset-shift-0700")
    app.job_queue.run_daily(reset_shift, time=dtime(19, 0, tzinfo=LOCAL_TZ), name="reset-shift-1900")

    # 启动后 5 秒执行一次换班（防止上次关机跨班数据残留）
    app.job_queue.run_once(reset_shift, when=5, name="reset-on-start")

    print("Bot running ...")
    app.run_polling(close_loop=False, allowed_updates=["message"], drop_pending_updates=True)

if __name__ == "__main__":
    main()
