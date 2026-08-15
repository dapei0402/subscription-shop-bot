# -*- coding: utf-8 -*-
"""
════════════════════════════════════════════════════════════════════════════
   网络服务订阅商店机器人（中文版）
   ----------------------------------------------------------------------------
   版权所有 (c) 2026 培哥
   频道: https://t.me/pgkj666      联系机器人: https://t.me/pgkj666_bot
════════════════════════════════════════════════════════════════════════════

  功能：Telegram 订阅销售机器人（pyTelegramBotAPI + SQLite）。
  - 用户端：购买套餐、免费测试账号、我的服务、钱包充值、邀请返利、申请代理、客服。
  - 付款方式：卡转账上传回执（人工审核） 或 钱包余额直接扣款。
  - 管理端：库存管理、统计、用户高级管理（余额/封禁/代理/管理员）、通用设置、群发。

  依赖：pip install pyTelegramBotAPI
"""

import json
import logging
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime

import telebot
from telebot import types

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# ============================== 配置 ==============================

API_TOKEN = "在此填入你的机器人 Token"
OWNER_ID = 0                    # 管理员（拥有者）的数字 ID
CHANNEL_ID = "@pgkj666"         # 强制加入的频道

DB_NAME = "shop_bot.db"
REFERRAL_BONUS = 5000           # 每成功邀请一人的奖励金额
AGENT_DISCOUNT = 0.9            # 代理折扣（0.9 = 九折）

# 收款卡默认值（可在管理面板中修改，修改后会持久化到数据库）
DEFAULT_CARD_NUMBER = "0000000000000000"
DEFAULT_CARD_NAME = "收款人姓名"

bot = telebot.TeleBot(API_TOKEN)

_db_lock = threading.Lock()     # SQLite 写操作串行化，避免并发写冲突

MAIN_COMMANDS = [
    "🛒 购买套餐", "🔑 测试账号", "🛍️ 我的服务", "🏦 钱包 + 充值",
    "👥 邀请返利", "🙋‍♀️ 申请代理", "☎️ 客服", "👨‍💼 管理面板",
    "📦 库存", "📊 机器人统计", "🛠️ 用户高级管理", "⚙️ 通用设置",
    "📢 发送群发消息", "🏡 返回主菜单", "🔙 返回管理面板",
    "🔑 测试库存管理", "✏️ 修改收款卡", "➕ 新增套餐", "❌ 删除套餐",
]

RULES_TEXT = (
    "<b>网络服务使用条款</b>\n\n"
    "1. 每份订阅仅限一位用户使用\n\n"
    "2. 不支持退款或更换服务\n\n"
    "3. 提交虚假付款回执将被永久封禁\n\n"
    "4. 使用本机器人即视为接受本条款"
)

AGENCY_SUCCESS_TEXT = (
    "<b>👑 账户已升级为代理级别</b>\n\n"
    "尊敬的合作伙伴，你的账户已成功升级为 <b>销售代理</b>。\n\n"
    "<b>🛠️ 已为你开通的权益：</b>\n\n"
    "1️⃣ <b>永久 10% 折扣：</b> 今后机器人内所有服务将以低 10% 的价格为你计算与扣款。\n\n"
    "2️⃣ <b>每日测试额度：</b> 你每天可领取 1 个免费测试账号，用于提供给你的客户。"
)

#__DB__
# ============================== 数据库 ==============================

def db_connect():
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def db_execute(sql, params=(), fetch=None):
    """
    统一的数据库执行入口：自动关闭连接、写操作加锁。
    fetch: None=不取结果, "one"=取一行, "all"=取全部, "rowcount"=返回影响行数
    """
    with _db_lock:
        with closing(db_connect()) as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            result = None
            if fetch == "one":
                result = cur.fetchone()
            elif fetch == "all":
                result = cur.fetchall()
            elif fetch == "rowcount":
                result = cur.rowcount
            conn.commit()
            return result


def init_db():
    stmts = [
        """CREATE TABLE IF NOT EXISTS users
           (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0, is_agent INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0, has_accepted_rules INTEGER DEFAULT 0,
            referred_by INTEGER DEFAULT 0, last_test_date TEXT, blocked_by_bot INTEGER DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)""",
        """CREATE TABLE IF NOT EXISTS products
           (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price INTEGER)""",
        """CREATE TABLE IF NOT EXISTS configs
           (id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER, type TEXT,
            content TEXT, is_used INTEGER DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS user_services
           (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, type TEXT,
            content TEXT, price INTEGER,
            status TEXT DEFAULT 'active',   -- active（生效中）/ refunded（已退款）
            product_id INTEGER DEFAULT 0,   -- 关联商品ID，退款时用于把库存放回
            config_id INTEGER DEFAULT 0,    -- 关联的库存配置ID
            created_at TEXT)""",
        # 设置表：持久化收款卡等运行时配置
        """CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)""",
        # 交易流水表：记录充值/购买/退款/赠送/扣款等所有资金与订单动作，形成审计链
        """CREATE TABLE IF NOT EXISTS transactions
           (id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            kind TEXT,              -- charge/purchase/refund/gift/deduct/referral/agent_topup
            amount INTEGER,         -- 金额，正为入账、负为出账
            status TEXT,            -- pending/completed/rejected/refunded
            detail TEXT,            -- 备注（商品名、审核人、原因等）
            ref_id INTEGER,         -- 关联ID（如 user_services.id / 商品id）
            operator_id INTEGER,    -- 操作者（管理员ID；系统/用户自身为0）
            created_at TEXT)""",
        """CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id)""",
        """CREATE INDEX IF NOT EXISTS idx_tx_kind ON transactions(kind)""",
        # 用户会话状态表：持久化 user_states，机器人重启后不丢失待输入状态
        """CREATE TABLE IF NOT EXISTS user_states
           (user_id INTEGER PRIMARY KEY, state TEXT, updated_at TEXT)""",
    ]
    with _db_lock:
        with closing(db_connect()) as conn:
            cur = conn.cursor()
            for s in stmts:
                cur.execute(s)
            # ---- 轻量迁移：为老数据库的 user_services 补齐新列 ----
            cols = {r[1] for r in cur.execute("PRAGMA table_info(user_services)").fetchall()}
            if "status" not in cols:
                cur.execute("ALTER TABLE user_services ADD COLUMN status TEXT DEFAULT 'active'")
            if "product_id" not in cols:
                cur.execute("ALTER TABLE user_services ADD COLUMN product_id INTEGER DEFAULT 0")
            if "config_id" not in cols:
                cur.execute("ALTER TABLE user_services ADD COLUMN config_id INTEGER DEFAULT 0")
            if "created_at" not in cols:
                cur.execute("ALTER TABLE user_services ADD COLUMN created_at TEXT")
            if OWNER_ID:
                cur.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (OWNER_ID,))
            cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('card_number', ?)",
                        (DEFAULT_CARD_NUMBER,))
            cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('card_name', ?)",
                        (DEFAULT_CARD_NAME,))
            cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('locked', '0')")
            conn.commit()


def get_setting(key, default=""):
    row = db_execute("SELECT value FROM settings WHERE key = ?", (key,), fetch="one")
    return row[0] if row else default


def set_setting(key, value):
    db_execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def get_card_info():
    return get_setting("card_number", DEFAULT_CARD_NUMBER), get_setting("card_name", DEFAULT_CARD_NAME)


def is_bot_locked():
    return get_setting("locked", "0") == "1"


def set_bot_locked(locked: bool):
    set_setting("locked", "1" if locked else "0")


# 交易类型的中文标签，用于展示
TX_KIND_LABELS = {
    "charge": "💳 充值",
    "purchase": "🛒 购买",
    "refund": "↩️ 退款",
    "gift": "🎁 赠送",
    "deduct": "📉 扣款",
    "referral": "👥 邀请奖励",
    "agent_topup": "👑 代理调整",
    "manual": "⚙️ 手动调整",
}

TX_STATUS_LABELS = {
    "pending": "⏳ 待处理",
    "completed": "✅ 已完成",
    "rejected": "❌ 已拒绝",
    "refunded": "↩️ 已退款",
}


def log_tx(user_id, kind, amount, status="completed", detail="", ref_id=0, operator_id=0):
    """写入一条交易流水。所有资金/订单动作都应调用它，形成完整审计链。"""
    db_execute(
        "INSERT INTO transactions (user_id, kind, amount, status, detail, ref_id, operator_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, kind, amount, status, detail, ref_id, operator_id,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )


def get_user_transactions(user_id, limit=10):
    return db_execute(
        "SELECT kind, amount, status, detail, created_at FROM transactions "
        "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit), fetch="all",
    ) or []


init_db()


# ============================== 会话状态（持久化） ==============================
# 用一个「像字典一样用」的对象封装 user_states，底层落地到 SQLite。
# 这样机器人重启后，用户正在进行的多步操作（如填金额、传回执）不会丢失。
# 支持的用法保持不变：
#   user_states[uid] = "state" / {...} / None
#   user_states.get(uid)

class PersistentStateStore:
    def _serialize(self, value):
        # None 表示清除状态
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False)

    def _deserialize(self, raw):
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception:
            # 兼容极端情况下的裸字符串
            return raw

    def __setitem__(self, user_id, value):
        if value is None:
            db_execute("DELETE FROM user_states WHERE user_id = ?", (user_id,))
            return
        db_execute(
            "INSERT INTO user_states (user_id, state, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at",
            (user_id, self._serialize(value), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )

    def get(self, user_id, default=None):
        row = db_execute("SELECT state FROM user_states WHERE user_id = ?", (user_id,), fetch="one")
        if not row:
            return default
        return self._deserialize(row[0])

    def __getitem__(self, user_id):
        return self.get(user_id)

    def pop(self, user_id, default=None):
        val = self.get(user_id, default)
        db_execute("DELETE FROM user_states WHERE user_id = ?", (user_id,))
        return val


user_states = PersistentStateStore()

# ============================== 工具函数 ==============================

def is_admin(user_id) -> bool:
    return db_execute("SELECT user_id FROM admins WHERE user_id = ?", (user_id,), fetch="one") is not None


def get_user(user_id) -> dict:
    row = db_execute(
        "SELECT balance, is_agent, is_banned, has_accepted_rules, referred_by, last_test_date "
        "FROM users WHERE user_id = ?",
        (user_id,), fetch="one",
    )
    if not row:
        db_execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        row = (0, 0, 0, 0, 0, None)
    return {
        "balance": row[0], "is_agent": row[1], "is_banned": row[2],
        "has_accepted_rules": row[3], "referred_by": row[4], "last_test_date": row[5],
    }


def update_user_balance(user_id, amount):
    """余额变动。扣款时不允许扣成负数。"""
    db_execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    if amount < 0:
        db_execute(
            "UPDATE users SET balance = MAX(balance + ?, 0) WHERE user_id = ?",
            (amount, user_id),
        )
    else:
        db_execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))


def get_products():
    return db_execute("SELECT id, name, price FROM products ORDER BY id", fetch="all") or []


def calc_price(base_price: int, is_agent: int) -> int:
    return int(base_price * AGENT_DISCOUNT) if is_agent == 1 else int(base_price)


def check_join(user_id) -> bool:
    """检查频道加入状态。机器人无权限查询时放行，避免误伤所有用户。"""
    if not CHANNEL_ID:
        return True
    try:
        member = bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ("creator", "administrator", "member")
    except Exception as e:
        logging.warning("加入检查失败（机器人可能不是 %s 的管理员）: %s", CHANNEL_ID, e)
        return True


def safe_send(chat_id, text, **kwargs):
    """发送消息并吞掉「用户已拉黑机器人」等异常，同时记录日志。"""
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        logging.info("向 %s 发送消息失败: %s", chat_id, e)
        return None


def edit_caption_safe(text, chat_id, message_id):
    try:
        bot.edit_message_caption(text, chat_id, message_id)
    except Exception as e:
        logging.info("编辑图片说明失败: %s", e)


def edit_text_safe(text, chat_id, message_id, **kwargs):
    try:
        bot.edit_message_text(text, chat_id, message_id, **kwargs)
    except Exception as e:
        logging.info("编辑消息失败: %s", e)


def parse_two_ints(text):
    """解析「数字 数字」格式，返回 (a, b) 或 None。"""
    parts = (text or "").split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def issue_service(target_user_id: int, product_id: int, price: int, tx_kind="purchase",
                  tx_detail="", operator_id=0):
    """
    从库存取一个未使用的配置并发放给用户。
    使用同一连接内的事务，避免「取到同一个配置发给两个人」的竞态。
    同时在事务内写入交易流水（审计链）。
    返回 (product_name, content, service_id) 或 None（库存为空/商品不存在）。
    """
    with _db_lock:
        with closing(db_connect()) as conn:
            cur = conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                p = cur.execute("SELECT name FROM products WHERE id = ?", (product_id,)).fetchone()
                name = p[0] if p else ("免费测试" if product_id == 0 else None)
                if name is None:
                    conn.rollback()
                    return None
                row = cur.execute(
                    "SELECT id, content FROM configs WHERE product_id = ? AND is_used = 0 LIMIT 1",
                    (product_id,),
                ).fetchone()
                if not row:
                    conn.rollback()
                    return None
                cur.execute("UPDATE configs SET is_used = 1 WHERE id = ?", (row[0],))
                cur.execute(
                    "INSERT INTO user_services (user_id, type, content, price, status, product_id, config_id, created_at) "
                    "VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
                    (target_user_id, name, row[1], price, product_id, row[0],
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                service_id = cur.lastrowid
                cur.execute(
                    "INSERT INTO transactions (user_id, kind, amount, status, detail, ref_id, operator_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (target_user_id, tx_kind, -price, "completed",
                     tx_detail or name, service_id, operator_id,
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                conn.commit()
                return name, row[1], service_id
            except Exception as e:
                conn.rollback()
                logging.exception("发放服务失败: %s", e)
                return None


def purchase_with_balance(user_id: int, product_id: int):
    """
    钱包余额购买：在一个事务内校验余额、扣款、取库存、写订单、记流水。
    返回 (product_name, content, price, service_id) 或错误字符串。
    """
    with _db_lock:
        with closing(db_connect()) as conn:
            cur = conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                p = cur.execute("SELECT name, price FROM products WHERE id = ?", (product_id,)).fetchone()
                if not p:
                    conn.rollback()
                    return "商品不存在"
                urow = cur.execute(
                    "SELECT balance, is_agent FROM users WHERE user_id = ?", (user_id,)
                ).fetchone()
                balance, is_agent = (urow[0], urow[1]) if urow else (0, 0)
                final_price = calc_price(p[1], is_agent)
                if balance < final_price:
                    conn.rollback()
                    return "余额不足"
                row = cur.execute(
                    "SELECT id, content FROM configs WHERE product_id = ? AND is_used = 0 LIMIT 1",
                    (product_id,),
                ).fetchone()
                if not row:
                    conn.rollback()
                    return "库存为空"
                cur.execute("UPDATE configs SET is_used = 1 WHERE id = ?", (row[0],))
                cur.execute(
                    "INSERT INTO user_services (user_id, type, content, price, status, product_id, config_id, created_at) "
                    "VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
                    (user_id, p[0], row[1], final_price, product_id, row[0],
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                service_id = cur.lastrowid
                cur.execute(
                    "UPDATE users SET balance = balance - ? WHERE user_id = ?",
                    (final_price, user_id),
                )
                cur.execute(
                    "INSERT INTO transactions (user_id, kind, amount, status, detail, ref_id, operator_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_id, "purchase", -final_price, "completed",
                     f"钱包购买 {p[0]}", service_id, 0,
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                conn.commit()
                return p[0], row[1], final_price, service_id
            except Exception as e:
                conn.rollback()
                logging.exception("余额购买失败: %s", e)
                return "处理出错"


def refund_service(service_id: int, operator_id: int, return_stock: bool = True):
    """
    管理员退款：在单事务内校验订单、退回余额、标记订单已退款、
    （可选）把库存配置放回可售、写入退款流水。
    返回 (user_id, service_name, refund_amount) 或错误字符串。
    """
    with _db_lock:
        with closing(db_connect()) as conn:
            cur = conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                s = cur.execute(
                    "SELECT user_id, type, price, status, config_id FROM user_services WHERE id = ?",
                    (service_id,),
                ).fetchone()
                if not s:
                    conn.rollback()
                    return "订单不存在"
                svc_user, svc_type, svc_price, svc_status, svc_cfg = s
                if svc_status == "refunded":
                    conn.rollback()
                    return "该订单已退款"

                # 退回余额（免费订单退 0）
                if svc_price and svc_price > 0:
                    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?",
                                (svc_price, svc_user))

                # 标记订单已退款
                cur.execute("UPDATE user_services SET status = 'refunded' WHERE id = ?", (service_id,))

                # 库存放回（可选）：把对应配置重新标记为可售
                if return_stock and svc_cfg:
                    cur.execute("UPDATE configs SET is_used = 0 WHERE id = ?", (svc_cfg,))

                # 写退款流水
                cur.execute(
                    "INSERT INTO transactions (user_id, kind, amount, status, detail, ref_id, operator_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (svc_user, "refund", svc_price or 0, "refunded",
                     f"退款 {svc_type}" + ("（含库存回收）" if (return_stock and svc_cfg) else ""),
                     service_id, operator_id,
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                conn.commit()
                return svc_user, svc_type, (svc_price or 0)
            except Exception as e:
                conn.rollback()
                logging.exception("退款失败: %s", e)
                return "处理出错"


def get_service_status_label(status: str) -> str:
    return {"active": "🟢 生效中", "refunded": "↩️ 已退款"}.get(status, status)
#__KEYBOARDS__
# ============================== 键盘 ==============================

def get_main_keyboard(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(types.KeyboardButton("🛒 购买套餐"), types.KeyboardButton("🔑 测试账号"))
    markup.row(types.KeyboardButton("🛍️ 我的服务"), types.KeyboardButton("🏦 钱包 + 充值"))
    markup.row(types.KeyboardButton("👥 邀请返利"), types.KeyboardButton("🙋‍♀️ 申请代理"))
    markup.row(types.KeyboardButton("☎️ 客服"))
    if is_admin(user_id):
        markup.row(types.KeyboardButton("👨‍💼 管理面板"))
    return markup


def get_admin_reply_keyboard(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(types.KeyboardButton("📦 库存"), types.KeyboardButton("📊 机器人统计"))
    markup.row(types.KeyboardButton("🛠️ 用户高级管理"))
    markup.row(types.KeyboardButton("⚙️ 通用设置"), types.KeyboardButton("📢 发送群发消息"))
    markup.row(types.KeyboardButton("🏡 返回主菜单"))
    return markup


def get_advanced_users_inline():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(types.InlineKeyboardButton("🔍 查询用户信息", callback_data="cap_1"),
               types.InlineKeyboardButton("➕ 增加余额", callback_data="cap_2"))
    markup.row(types.InlineKeyboardButton("➖ 扣减余额", callback_data="cap_3"),
               types.InlineKeyboardButton("🚫 封禁用户", callback_data="cap_4"))
    markup.row(types.InlineKeyboardButton("🟢 解除封禁", callback_data="cap_5"),
               types.InlineKeyboardButton("👑 授予代理", callback_data="cap_6"))
    markup.row(types.InlineKeyboardButton("👤 取消代理", callback_data="cap_7"),
               types.InlineKeyboardButton("🎁 全员充值", callback_data="cap_8"))
    markup.row(types.InlineKeyboardButton("❌ 清空其服务", callback_data="cap_9"),
               types.InlineKeyboardButton("🧹 清理僵尸用户", callback_data="cap_10"))
    markup.row(types.InlineKeyboardButton("➕ 添加管理员", callback_data="cap_11"),
               types.InlineKeyboardButton("❌ 移除管理员", callback_data="cap_12"))
    markup.row(types.InlineKeyboardButton("📊 管理员列表", callback_data="cap_13"),
               types.InlineKeyboardButton("📉 全员扣款", callback_data="cap_14"))
    markup.row(types.InlineKeyboardButton("🔓 全员解封", callback_data="cap_15"),
               types.InlineKeyboardButton("🔒 临时锁定机器人", callback_data="cap_16"))
    markup.row(types.InlineKeyboardButton("🔗 手动设置推荐人", callback_data="cap_17"))
    return markup


def get_receipt_management_keyboard(user_id, action_type, extra_id=0, amount=0):
    markup = types.InlineKeyboardMarkup(row_width=2)
    if action_type == "charge":
        cb_approve, cb_reject = f"ok_dp_{user_id}_{amount}", f"no_dp_{user_id}"
    elif action_type == "buy":
        cb_approve, cb_reject = f"ok_by_{user_id}_{extra_id}", f"no_by_{user_id}"
    elif action_type == "agency_req":
        markup.row(types.InlineKeyboardButton("✅ 通过申请", callback_data=f"ok_ag_{user_id}"),
                   types.InlineKeyboardButton("❌ 拒绝申请", callback_data=f"no_ag_{user_id}"))
        return markup
    else:
        return markup

    markup.row(types.InlineKeyboardButton("✅ 通过回执", callback_data=cb_approve),
               types.InlineKeyboardButton("❌ 拒绝回执", callback_data=cb_reject))
    markup.row(types.InlineKeyboardButton("🚫 封禁用户", callback_data=f"adm_ban_{user_id}"),
               types.InlineKeyboardButton("⚙️ 手动调整余额", callback_data=f"adm_man_{user_id}"))
    return markup
#__START__
# ============================== /start ==============================

@bot.message_handler(commands=["start"])
def send_welcome(message):
    user_id = message.from_user.id
    user_states[user_id] = None
    u = get_user(user_id)
    if u["is_banned"] == 1:
        return
    if is_bot_locked() and not is_admin(user_id):
        bot.send_message(message.chat.id, "<b>机器人正在维护中，请稍后再试</b>", parse_mode="HTML")
        return

    # 处理推荐链接 /start <推荐人ID>
    if u["referred_by"] == 0:
        parts = (message.text or "").split()
        if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) != user_id:
            db_execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (int(parts[1]), user_id))

    if not check_join(user_id):
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            "📢 加入频道", url=f"https://t.me/{CHANNEL_ID.replace('@', '')}"))
        markup.add(types.InlineKeyboardButton("✅ 我已加入（重新检查）", callback_data="check_membership"))
        bot.send_message(
            message.chat.id,
            f"<b>尊敬的用户，你好</b>\n\n使用本服务前请先加入我们的频道\n\n{CHANNEL_ID}",
            reply_markup=markup, parse_mode="HTML",
        )
        return

    if u["has_accepted_rules"] == 0:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ 我接受条款", callback_data="accept_rules"))
        bot.send_message(message.chat.id, RULES_TEXT, reply_markup=markup, parse_mode="HTML")
        return

    bot.send_message(
        message.chat.id,
        "<b>欢迎来到综合网络服务门户</b>\n\n请选择你需要的选项",
        reply_markup=get_main_keyboard(user_id), parse_mode="HTML",
    )
#__CALLBACKS__
# ============================== 回调处理 ==============================

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = call.from_user.id
    u = get_user(user_id)
    if u["is_banned"] == 1:
        return

    data = call.data or ""
    admin = is_admin(user_id)

    # 管理员专属回调前缀，非管理员直接拒绝
    admin_prefixes = ("ok_dp_", "no_dp_", "ok_by_", "no_by_", "ok_ag_", "no_ag_",
                      "adm_ban_", "adm_man_", "adm_order_", "adm_refund_",
                      "delprod_", "m_add_", "m_rst_", "cap_")
    if data.startswith(admin_prefixes) and not admin:
        bot.answer_callback_query(call.id, "⛔ 你没有管理员权限", show_alert=True)
        return

    # ---------- 充值回执审核 ----------
    if data.startswith("ok_dp_"):
        p = data.split("_")
        target, amt = int(p[2]), int(p[3])
        update_user_balance(target, amt)
        log_tx(target, "charge", amt, "completed", "卡转账充值（管理员通过）", operator_id=user_id)
        edit_caption_safe(f"充值已通过\n\n用户 {target} 余额增加 {amt:,}。",
                          call.message.chat.id, call.message.message_id)
        safe_send(target,
                  f"<b>充值到账通知</b>\n\n你的钱包已成功充值\n\n"
                  f"金额 {amt:,} 元已加入你的账户\n\n现在可以购买你需要的服务了",
                  parse_mode="HTML")
        return

    if data.startswith("no_dp_"):
        target = int(data.split("_")[2])
        log_tx(target, "charge", 0, "rejected", "卡转账充值（管理员拒绝）", operator_id=user_id)
        edit_caption_safe(f"用户 {target} 的充值回执已被拒绝。",
                          call.message.chat.id, call.message.message_id)
        safe_send(target,
                  "<b>回执未通过通知</b>\n\n你提交的付款回执未通过审核\n\n请重新核对你的转账信息",
                  parse_mode="HTML")
        return

    if data.startswith("adm_ban_"):
        target = int(data.split("_")[2])
        db_execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (target,))
        edit_caption_safe(f"用户 {target} 已被封禁。", call.message.chat.id, call.message.message_id)
        safe_send(target, "<b>你的账户已被封禁</b>", parse_mode="HTML")
        return

    if data.startswith("adm_man_"):
        target = int(data.split("_")[2])
        user_states[user_id] = "p_cap_2"
        bot.send_message(call.message.chat.id,
                         f"如需手动增加余额，请按以下格式发送\n\n<code>{target} 金额</code>",
                         parse_mode="HTML")
        return

    # ---------- 代理申请审核 ----------
    if data.startswith("ok_ag_"):
        target = int(data.split("_")[2])
        db_execute("UPDATE users SET is_agent = 1 WHERE user_id = ?", (target,))
        edit_text_safe(f"用户 {target} 已升级为代理。", call.message.chat.id, call.message.message_id)
        safe_send(target, AGENCY_SUCCESS_TEXT, parse_mode="HTML")
        return

    if data.startswith("no_ag_"):
        target = int(data.split("_")[2])
        edit_text_safe(f"用户 {target} 的代理申请已被拒绝。", call.message.chat.id, call.message.message_id)
        safe_send(target, "<b>你的代理申请未通过审核</b>", parse_mode="HTML")
        return

    # ---------- 购买回执审核 ----------
    if data.startswith("ok_by_"):
        p = data.split("_")
        target, prod_id = int(p[2]), int(p[3])
        prod = db_execute("SELECT name, price FROM products WHERE id = ?", (prod_id,), fetch="one")
        if not prod:
            bot.answer_callback_query(call.id, "错误：商品不存在", show_alert=True)
            return
        target_info = get_user(target)
        final_price = calc_price(prod[1], target_info["is_agent"])
        issued = issue_service(target, prod_id, final_price, tx_kind="purchase",
                               tx_detail=f"卡转账购买 {prod[0]}（管理员通过）", operator_id=user_id)
        if not issued:
            bot.answer_callback_query(call.id, "错误：该商品库存为空", show_alert=True)
            return
        name, content, _sid = issued
        edit_caption_safe("购买回执已通过，订阅已发放。", call.message.chat.id, call.message.message_id)
        safe_send(target,
                  f"<b>你的服务已成功开通</b>\n\n"
                  f"订阅类型: {name}\n\n"
                  f"连接密钥:\n<code>{content}</code>\n\n"
                  f"请复制以上密钥用于连接",
                  parse_mode="HTML")
        return

    if data.startswith("no_by_"):
        target = int(data.split("_")[2])
        log_tx(target, "purchase", 0, "rejected", "卡转账购买（管理员拒绝）", operator_id=user_id)
        edit_caption_safe(f"用户 {target} 的购买回执已被拒绝。",
                          call.message.chat.id, call.message.message_id)
        safe_send(target, "<b>你的购买申请因回执问题被拒绝</b>", parse_mode="HTML")
        return

    # ---------- 商品与库存管理 ----------
    if data.startswith("delprod_"):
        p_id = int(data.split("_")[1])
        db_execute("DELETE FROM products WHERE id = ?", (p_id,))
        db_execute("DELETE FROM configs WHERE product_id = ?", (p_id,))
        edit_text_safe("商品及其相关库存已删除。", call.message.chat.id, call.message.message_id)
        return

    if data.startswith("m_add_"):
        p_id = int(data.split("_")[2])
        user_states[user_id] = {"action": "waiting_for_configs_reply", "product_id": p_id}
        bot.send_message(call.message.chat.id, "请发送连接密钥（每行一个配置）：")
        return

    if data.startswith("m_rst_"):
        p_id = int(data.split("_")[2])
        user_states[user_id] = {"action": "waiting_for_reset_count", "product_id": p_id}
        bot.send_message(call.message.chat.id, "请输入要从库存中删除的配置数量：")
        return

    # ---------- 用户高级管理 ----------
    if data.startswith("cap_"):
        cap_num = data.split("_")[1]
        user_states[user_id] = f"p_cap_{cap_num}"
        prompts = {
            "1": "🔍 请输入目标用户的数字 ID：",
            "2": "➕ 增加余额格式：用户ID 金额",
            "3": "➖ 扣减余额格式：用户ID 金额",
            "4": "🚫 请输入要封禁的用户 ID：",
            "5": "🟢 请输入要解除封禁的用户 ID：",
            "6": "👑 请输入要授予代理权限的用户 ID：",
            "7": "👤 请输入要取消代理权限的用户 ID：",
            "8": "🎁 请输入要赠送给所有用户的金额（元）：",
            "9": "❌ 请输入要清空其全部服务的用户 ID：",
            "11": "➕ 请输入要添加为管理员的数字 ID：",
            "12": "❌ 请输入要移除的管理员 ID：",
            "14": "📉 请输入要从所有用户扣减的金额（元）：",
            "17": "设置推荐关系格式：用户ID 推荐人ID",
        }
        if cap_num in prompts:
            bot.send_message(call.message.chat.id, prompts[cap_num])
            return

        # 无需输入的即时操作
        user_states[user_id] = None
        if cap_num == "10":
            c = db_execute(
                "DELETE FROM users WHERE balance = 0 AND user_id NOT IN "
                "(SELECT DISTINCT user_id FROM user_services)",
                fetch="rowcount",
            )
            bot.send_message(call.message.chat.id, f"清理完成，共删除 {c} 个不活跃账户。")
        elif cap_num == "13":
            rows = db_execute("SELECT user_id FROM admins", fetch="all") or []
            body = "\n".join(f"<code>{r[0]}</code>" for r in rows) or "（无）"
            bot.send_message(call.message.chat.id, "<b>系统管理员列表</b>\n\n" + body, parse_mode="HTML")
        elif cap_num == "15":
            db_execute("UPDATE users SET is_banned = 0")
            bot.send_message(call.message.chat.id, "所有被封禁的用户已解封。")
        elif cap_num == "16":
            new_state = not is_bot_locked()
            set_bot_locked(new_state)
            bot.send_message(call.message.chat.id,
                             f"机器人临时锁定状态已改为：{'锁定' if new_state else '开放'}")
        return

    # ---------- 通用回调 ----------
    if data == "check_membership":
        if check_join(user_id):
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            u = get_user(user_id)
            if u["has_accepted_rules"] == 0:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("✅ 我接受条款", callback_data="accept_rules"))
                bot.send_message(call.message.chat.id, RULES_TEXT, reply_markup=markup, parse_mode="HTML")
            else:
                bot.send_message(call.message.chat.id, "系统已就绪，可以开始使用",
                                 reply_markup=get_main_keyboard(user_id))
        else:
            bot.answer_callback_query(call.id, "未检测到你已加入频道", show_alert=True)
        return

    if data == "accept_rules":
        # 只有首次接受条款才发放邀请奖励，避免重复点击刷奖励
        changed = db_execute(
            "UPDATE users SET has_accepted_rules = 1 WHERE user_id = ? AND has_accepted_rules = 0",
            (user_id,), fetch="rowcount",
        )
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        if changed and u["referred_by"]:
            update_user_balance(u["referred_by"], REFERRAL_BONUS)
            log_tx(u["referred_by"], "referral", REFERRAL_BONUS, "completed",
                   f"邀请用户 {user_id} 奖励")
            safe_send(u["referred_by"],
                      f"<b>邀请新用户奖励</b>\n\n有一位用户通过你的链接加入\n\n"
                      f"{REFERRAL_BONUS:,} 元已加入你的钱包",
                      parse_mode="HTML")
        bot.send_message(call.message.chat.id, "条款已确认",
                         reply_markup=get_main_keyboard(user_id))
        return

    if data == "wallet_charge":
        user_states[user_id] = "waiting_for_charge_amount"
        bot.send_message(call.message.chat.id,
                         "<b>账户充值</b>\n\n请输入你要充值的金额（阿拉伯数字，单位：元）：",
                         parse_mode="HTML")
        return

    if data == "tx_history":
        txs = get_user_transactions(user_id, limit=15)
        if not txs:
            bot.answer_callback_query(call.id, "你还没有任何交易记录", show_alert=True)
            return
        lines = ["<b>📜 你最近的交易记录</b>\n"]
        for kind, amount, status, detail, created_at in txs:
            klabel = TX_KIND_LABELS.get(kind, kind)
            slabel = TX_STATUS_LABELS.get(status, status)
            sign = "+" if amount > 0 else ""
            lines.append(
                f"{klabel} | {sign}{amount:,} 元 | {slabel}\n"
                f"  {detail or '-'}  ·  {created_at}"
            )
        edit_text_safe("\n".join(lines), call.message.chat.id, call.message.message_id,
                       parse_mode="HTML")
        return

    if data.startswith("buy_prod_"):
        p_id = int(data.split("_")[2])
        p_info = db_execute("SELECT name, price FROM products WHERE id = ?", (p_id,), fetch="one")
        if not p_info:
            bot.answer_callback_query(call.id, "商品不存在", show_alert=True)
            return
        final_price = calc_price(p_info[1], u["is_agent"])
        user_role = "👑 代理（专享 9 折）" if u["is_agent"] == 1 else "👤 普通用户"

        markup = types.InlineKeyboardMarkup()
        markup.row(types.InlineKeyboardButton("💳 卡转账（上传回执）", callback_data=f"direct_pay_{p_id}"))
        markup.row(types.InlineKeyboardButton("🏦 使用钱包余额支付", callback_data=f"wallet_pay_{p_id}"))

        buy_text = (
            f"<b>已选套餐：</b>{p_info[0]}\n\n"
            f"<b>原价：</b>{p_info[1]:,} 元\n\n"
            f"<b>你的价格：</b>{final_price:,} 元（{user_role}）\n\n"
            f"<b>你的余额：</b>{u['balance']:,} 元"
        )
        edit_text_safe(buy_text, call.message.chat.id, call.message.message_id,
                       reply_markup=markup, parse_mode="HTML")
        return

    if data.startswith("direct_pay_"):
        p_id = int(data.split("_")[2])
        user_states[user_id] = {"action": "direct_buy_receipt", "product_id": p_id}
        card_number, card_name = get_card_info()
        bot.send_message(call.message.chat.id,
                         f"<b>转账付款单</b>\n\n请将款项转入以下账户，并仅发送回执图片\n\n"
                         f"卡号：\n<code>{card_number}</code>\n\n户名：{card_name}",
                         parse_mode="HTML")
        return

    if data.startswith("wallet_pay_"):
        p_id = int(data.split("_")[2])
        result = purchase_with_balance(user_id, p_id)
        if isinstance(result, str):
            msgs = {"余额不足": "你的余额不足", "库存为空": "该服务库存暂时为空",
                    "商品不存在": "商品不存在", "处理出错": "处理过程出错，请重试"}
            bot.answer_callback_query(call.id, msgs.get(result, result), show_alert=True)
            return
        name, content, price, _sid = result
        edit_text_safe(
            f"<b>你的服务已成功开通</b>\n\n"
            f"订阅类型: {name}\n\n"
            f"已扣金额: {price:,} 元\n\n"
            f"连接密钥:\n<code>{content}</code>",
            call.message.chat.id, call.message.message_id, parse_mode="HTML",
        )
        return

    if data.startswith("view_service_"):
        srv_id = int(data.split("_")[2])
        row = db_execute(
            "SELECT type, content, price, status, created_at FROM user_services WHERE id = ? AND user_id = ?",
            (srv_id, user_id), fetch="one",
        )
        if row:
            svc_type, content, price, status, created_at = row
            price_str = "免费 🎁" if not price else f"{price:,} 元"
            markup = None
            body = f"访问密钥:\n<code>{content}</code>"
            if status == "refunded":
                body = "（该订单已退款，密钥已失效）"
            bot.send_message(
                call.message.chat.id,
                f"<b>服务详情</b>\n\n"
                f"类型: {svc_type}\n"
                f"金额: {price_str}\n"
                f"状态: {get_service_status_label(status)}\n"
                f"时间: {created_at or '-'}\n\n"
                f"{body}",
                parse_mode="HTML", reply_markup=markup,
            )
        return

    # ---------- 管理员：查看某用户订单并可退款 ----------
    if data.startswith("adm_order_") and is_admin(user_id):
        srv_id = int(data.split("_")[2])
        row = db_execute(
            "SELECT user_id, type, price, status, created_at FROM user_services WHERE id = ?",
            (srv_id,), fetch="one",
        )
        if not row:
            bot.answer_callback_query(call.id, "订单不存在", show_alert=True)
            return
        svc_user, svc_type, price, status, created_at = row
        price_str = "免费 🎁" if not price else f"{price:,} 元"
        markup = types.InlineKeyboardMarkup()
        if status == "active":
            markup.row(types.InlineKeyboardButton("↩️ 退款并回收库存",
                                                  callback_data=f"adm_refund_{srv_id}_1"))
            markup.row(types.InlineKeyboardButton("↩️ 仅退款（不回收库存）",
                                                  callback_data=f"adm_refund_{srv_id}_0"))
        edit_text_safe(
            f"<b>订单 #{srv_id}</b>\n\n"
            f"用户: <code>{svc_user}</code>\n"
            f"类型: {svc_type}\n"
            f"金额: {price_str}\n"
            f"状态: {get_service_status_label(status)}\n"
            f"时间: {created_at or '-'}",
            call.message.chat.id, call.message.message_id,
            parse_mode="HTML", reply_markup=markup,
        )
        return

    if data.startswith("adm_refund_") and is_admin(user_id):
        parts = data.split("_")
        srv_id = int(parts[2])
        return_stock = parts[3] == "1"
        result = refund_service(srv_id, operator_id=user_id, return_stock=return_stock)
        if isinstance(result, str):
            bot.answer_callback_query(call.id, result, show_alert=True)
            return
        svc_user, svc_type, amount = result
        edit_text_safe(
            f"✅ 订单 #{srv_id} 已退款\n\n"
            f"用户: <code>{svc_user}</code>\n"
            f"类型: {svc_type}\n"
            f"退回金额: {amount:,} 元\n"
            f"库存回收: {'是' if return_stock else '否'}",
            call.message.chat.id, call.message.message_id, parse_mode="HTML",
        )
        safe_send(svc_user,
                  f"<b>退款通知</b>\n\n你的订单「{svc_type}」已被管理员退款\n\n"
                  f"退回金额: {amount:,} 元已返还到你的余额",
                  parse_mode="HTML")
        return
#__TEXT__
# ============================== 文本消息处理 ==============================

@bot.message_handler(func=lambda message: True, content_types=["text"])
def handle_text_messages(message):
    user_id = message.from_user.id
    u = get_user(user_id)
    if u["is_banned"] == 1:
        return

    admin = is_admin(user_id)

    # 机器人锁定时，非管理员一律拦截
    if is_bot_locked() and not admin:
        bot.send_message(message.chat.id, "<b>机器人正在维护中，请稍后再试</b>", parse_mode="HTML")
        return

    # 未加入频道 / 未接受条款的用户不能使用功能
    if not check_join(user_id):
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            "📢 加入频道", url=f"https://t.me/{CHANNEL_ID.replace('@', '')}"))
        markup.add(types.InlineKeyboardButton("✅ 我已加入（重新检查）", callback_data="check_membership"))
        bot.send_message(message.chat.id, f"<b>请先加入频道</b>\n\n{CHANNEL_ID}",
                         reply_markup=markup, parse_mode="HTML")
        return
    if u["has_accepted_rules"] == 0:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ 我接受条款", callback_data="accept_rules"))
        bot.send_message(message.chat.id, RULES_TEXT, reply_markup=markup, parse_mode="HTML")
        return

    text = message.text or ""
    current_state = user_states.get(user_id)

    # 点击主菜单按钮时清除待输入状态
    if text in MAIN_COMMANDS:
        user_states[user_id] = None
        current_state = None

    # ---------- 管理员待输入状态 ----------
    if isinstance(current_state, str) and current_state.startswith("p_cap_") and admin:
        cap = current_state[len("p_cap_"):]
        user_states[user_id] = None

        if cap == "1":
            if not text.strip().isdigit():
                bot.reply_to(message, "请输入有效的数字 ID")
                return
            target = int(text.strip())
            inf = get_user(target)
            markup = types.InlineKeyboardMarkup()
            svc_rows = db_execute(
                "SELECT id, type, status FROM user_services WHERE user_id = ? ORDER BY id DESC LIMIT 20",
                (target,), fetch="all") or []
            for sid, stype, status in svc_rows:
                tag = "" if status == "active" else "（已退款）"
                markup.add(types.InlineKeyboardButton(
                    f"🧾 #{sid} {stype}{tag}", callback_data=f"adm_order_{sid}"))
            bot.reply_to(message,
                         f"<b>账户状态</b>\n\n数字 ID: {target}\n\n"
                         f"余额: {inf['balance']:,} 元\n\n"
                         f"代理: {'是' if inf['is_agent'] else '否'}\n\n"
                         f"封禁: {'是' if inf['is_banned'] else '否'}\n\n"
                         f"（点击下方订单可退款）",
                         parse_mode="HTML",
                         reply_markup=markup if svc_rows else None)
            return

        if cap in ("2", "3"):
            pair = parse_two_ints(text)
            if not pair:
                bot.reply_to(message, "输入格式不正确，应为：用户ID 金额")
                return
            t, m = pair
            if m <= 0:
                bot.reply_to(message, "金额必须为正数")
                return
            amount = m if cap == "2" else -m
            update_user_balance(t, amount)
            log_tx(t, "manual", amount, "completed",
                   "管理员手动增加余额" if cap == "2" else "管理员手动扣减余额",
                   operator_id=user_id)
            bot.reply_to(message, "余额已成功增加" if cap == "2" else "余额已成功扣减")
            if cap == "2":
                safe_send(t, f"<b>到账通知</b>\n\n{m:,} 元已加入你的账户余额", parse_mode="HTML")
            else:
                safe_send(t, f"<b>扣款通知</b>\n\n已从你的账户扣除 {m:,} 元", parse_mode="HTML")
            return

        if cap in ("4", "5", "6", "7", "9", "11", "12"):
            if not text.strip().isdigit():
                bot.reply_to(message, "请输入有效的数字 ID")
                return
            target = int(text.strip())
            if cap == "4":
                db_execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (target,))
                bot.reply_to(message, "用户已封禁。")
            elif cap == "5":
                db_execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (target,))
                bot.reply_to(message, "已解除封禁。")
            elif cap == "6":
                db_execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (target,))
                db_execute("UPDATE users SET is_agent = 1 WHERE user_id = ?", (target,))
                bot.reply_to(message, "已授予代理权限。")
                safe_send(target, AGENCY_SUCCESS_TEXT, parse_mode="HTML")
            elif cap == "7":
                db_execute("UPDATE users SET is_agent = 0 WHERE user_id = ?", (target,))
                bot.reply_to(message, "已取消代理权限。")
            elif cap == "9":
                db_execute("DELETE FROM user_services WHERE user_id = ?", (target,))
                bot.reply_to(message, "该用户的全部服务已清空。")
            elif cap == "11":
                db_execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (target,))
                bot.reply_to(message, f"用户 {target} 已加入管理员。")
            elif cap == "12":
                if target == OWNER_ID:
                    bot.reply_to(message, "无法移除拥有者权限")
                    return
                db_execute("DELETE FROM admins WHERE user_id = ?", (target,))
                bot.reply_to(message, "已移除该管理员。")
            return

        if cap in ("8", "14"):
            if not text.strip().isdigit():
                bot.reply_to(message, "请输入有效的正整数金额")
                return
            amt = int(text.strip())
            users = db_execute("SELECT user_id FROM users", fetch="all") or []
            if cap == "8":
                for (uid,) in users:
                    update_user_balance(uid, amt)
                    log_tx(uid, "gift", amt, "completed", "全员赠送", operator_id=user_id)
                    safe_send(uid, f"<b>全员赠送</b>\n\n{amt:,} 元赠送金额已到账", parse_mode="HTML")
                bot.reply_to(message, f"全员赠送完成，共 {len(users)} 人。")
            else:
                for (uid,) in users:
                    update_user_balance(uid, -amt)
                    log_tx(uid, "deduct", -amt, "completed", "全员扣款", operator_id=user_id)
                bot.reply_to(message, f"全员扣款完成，共 {len(users)} 人。")
            return

        if cap == "17":
            pair = parse_two_ints(text)
            if not pair:
                bot.reply_to(message, "格式不正确，应为：用户ID 推荐人ID")
                return
            uid, rid = pair
            db_execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
            db_execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (rid, uid))
            bot.reply_to(message, "推荐关系已设置。")
            return

        bot.reply_to(message, "未知操作")
        return

    # ---------- 修改收款卡 ----------
    if current_state == "waiting_for_new_card" and admin:
        user_states[user_id] = {"action": "waiting_for_new_card_name", "card_num": text.strip()}
        bot.reply_to(message, "👤 现在请输入新的收款人姓名：")
        return

    if isinstance(current_state, dict) and current_state.get("action") == "waiting_for_new_card_name" and admin:
        set_setting("card_number", current_state["card_num"])
        set_setting("card_name", text.strip())
        user_states[user_id] = None
        bot.reply_to(message, "✅ 收款卡信息已更新（已持久化保存）。")
        return

    # ---------- 新增商品 ----------
    if current_state == "waiting_new_btn_name" and admin:
        user_states[user_id] = {"action": "waiting_new_btn_price", "name": text.strip()}
        bot.reply_to(message, "💰 请输入新商品的价格（元）：")
        return

    if isinstance(current_state, dict) and current_state.get("action") == "waiting_new_btn_price" and admin:
        if not text.strip().isdigit():
            bot.reply_to(message, "❌ 请输入有效的数字价格。")
            return
        db_execute("INSERT INTO products (name, price) VALUES (?, ?)",
                   (current_state["name"], int(text.strip())))
        user_states[user_id] = None
        bot.reply_to(message, "✅ 新商品已添加。")
        return

    # ---------- 库存录入 / 清理 ----------
    if isinstance(current_state, dict) and current_state.get("action") == "waiting_for_configs_reply" and admin:
        p_id = current_state.get("product_id")
        user_states[user_id] = None
        cfg_type = "test" if p_id == 0 else "unlimited"
        added = 0
        for line in text.split("\n"):
            line = line.strip()
            if line:
                db_execute("INSERT INTO configs (product_id, type, content) VALUES (?, ?, ?)",
                           (p_id, cfg_type, line))
                added += 1
        bot.reply_to(message, f"✅ 已成功向库存添加 {added} 个账号。")
        return

    if isinstance(current_state, dict) and current_state.get("action") == "waiting_for_reset_count" and admin:
        p_id = current_state.get("product_id")
        user_states[user_id] = None
        if not text.strip().isdigit():
            bot.reply_to(message, "请输入有效的数字")
            return
        count = int(text.strip())
        deleted = db_execute(
            "DELETE FROM configs WHERE id IN "
            "(SELECT id FROM configs WHERE product_id = ? AND is_used = 0 LIMIT ?)",
            (p_id, count), fetch="rowcount",
        )
        bot.reply_to(message, f"🗑️ 已从库存删除 {deleted} 个配置。")
        return

    # ---------- 充值金额 ----------
    if current_state == "waiting_for_charge_amount":
        raw = text.replace(",", "").strip()
        if not raw.isdigit() or int(raw) <= 0:
            bot.reply_to(message, "请只输入正整数金额（阿拉伯数字）")
            return
        amount = int(raw)
        user_states[user_id] = {"action": "send_charge_receipt", "amount": amount}
        card_number, card_name = get_card_info()
        bot.send_message(
            message.chat.id,
            f"<b>账户充值</b>\n\n"
            f"请准确转账 {amount:,} 元\n\n"
            f"然后回复本消息并发送你的付款回执图片\n\n"
            f"卡号: <code>{card_number}</code>\n\n"
            f"户名: {card_name}",
            parse_mode="HTML",
        )
        return

    # ---------- 群发 ----------
    if current_state == "waiting_for_broadcast" and admin:
        user_states[user_id] = None
        users = db_execute("SELECT user_id FROM users", fetch="all") or []
        s = 0
        for (uid,) in users:
            if safe_send(uid, f"<b>管理员通知</b>\n\n{text}", parse_mode="HTML"):
                s += 1
        bot.send_message(message.chat.id, f"✅ 群发消息已成功发送给 {s} 位用户。")
        return

    # ---------- 客服留言 ----------
    if current_state == "waiting_for_support_msg":
        user_states[user_id] = None
        try:
            bot.forward_message(OWNER_ID, message.chat.id, message.message_id)
        except Exception as e:
            logging.info("转发客服消息失败: %s", e)
        bot.reply_to(message, "✅ 已收到你的消息，我们会尽快回复你。")
        return

    user_states[user_id] = None

    # ---------- 主菜单功能 ----------
    if text == "🛒 购买套餐":
        prods = get_products()
        if not prods:
            bot.reply_to(message, "目前还没有可购买的套餐。")
            return
        markup = types.InlineKeyboardMarkup()
        for p in prods:
            markup.row(types.InlineKeyboardButton(f"📦 {p[1]} - {p[2]:,} 元",
                                                  callback_data=f"buy_prod_{p[0]}"))
        bot.send_message(message.chat.id, "<b>可用套餐列表</b>", reply_markup=markup, parse_mode="HTML")
        return

    if text == "🔑 测试账号":
        today_str = datetime.now().strftime("%Y-%m-%d")
        if u["is_agent"] == 0:
            if u["last_test_date"] is not None:
                bot.reply_to(message, "<b>你已经领取过免费测试额度</b>", parse_mode="HTML")
                return
        else:
            if u["last_test_date"] == today_str:
                bot.reply_to(message,
                             "<b>👑 尊敬的代理，你今天的额度（每日 1 个测试账号）已领取，请明天再试。</b>",
                             parse_mode="HTML")
                return

        issued = issue_service(user_id, 0, 0, tx_kind="purchase", tx_detail="领取免费测试账号")
        if not issued:
            bot.reply_to(message, "目前库存中没有可用的测试账号")
            return
        _, content, _sid = issued
        db_execute("UPDATE users SET last_test_date = ? WHERE user_id = ?", (today_str, user_id))
        bot.reply_to(message, f"<b>测试连接配置已发放</b>\n\n<code>{content}</code>", parse_mode="HTML")
        return

    if text == "🙋‍♀️ 申请代理":
        if u["is_agent"] == 1:
            bot.reply_to(message, "你的账户已是代理状态")
            return
        username = f"@{message.from_user.username}" if message.from_user.username else "无用户名"
        markup = get_receipt_management_keyboard(user_id, "agency_req")
        safe_send(OWNER_ID, f"代理申请\n\nID: {user_id}\n\n用户名: {username}", reply_markup=markup)
        bot.reply_to(message,
                     "<b>你的账户升级申请已提交</b>\n\n"
                     "该申请已发送审核\n\n"
                     "审核结果将通过本机器人通知你\n\n"
                     "感谢你的合作意愿",
                     parse_mode="HTML")
        return

    if text == "🏦 钱包 + 充值":
        markup = types.InlineKeyboardMarkup()
        markup.row(types.InlineKeyboardButton("➕ 充值余额（卡转账）", callback_data="wallet_charge"))
        markup.row(types.InlineKeyboardButton("📜 交易记录", callback_data="tx_history"))
        bot.send_message(message.chat.id,
                         f"<b>钱包与余额管理</b>\n\n"
                         f"你的当前余额: {u['balance']:,} 元\n\n"
                         f"账户状态: 正常\n\n"
                         f"如需充值请点击下方按钮",
                         reply_markup=markup, parse_mode="HTML")
        return

    if text == "🛍️ 我的服务":
        rows = db_execute(
            "SELECT id, type, status FROM user_services WHERE user_id = ? ORDER BY id DESC",
            (user_id,), fetch="all") or []
        if not rows:
            bot.reply_to(message, "未找到你的任何有效服务。")
            return
        markup = types.InlineKeyboardMarkup()
        for sid, stype, status in rows:
            tag = "" if status == "active" else "（已退款）"
            markup.add(types.InlineKeyboardButton(f"📦 {stype}{tag}",
                                                  callback_data=f"view_service_{sid}"))
        bot.send_message(message.chat.id, "🛍️ 你已购买的服务列表：", reply_markup=markup)
        return

    if text == "👥 邀请返利":
        try:
            bot_username = bot.get_me().username
        except Exception:
            bot_username = ""
        ref_link = f"https://t.me/{bot_username}?start={user_id}"
        bot.send_message(message.chat.id,
                         f"<b>邀请好友赚取收益</b>\n\n"
                         f"分享你的链接邀请好友加入\n\n"
                         f"每成功邀请一人奖励: {REFERRAL_BONUS:,} 元即时到账\n\n"
                         f"你的专属邀请链接:\n{ref_link}",
                         parse_mode="HTML")
        return

    if text == "☎️ 客服":
        user_states[user_id] = "waiting_for_support_msg"
        bot.send_message(message.chat.id,
                         "<b>联系客服</b>\n\n请写下你的问题，我们会尽快回复你",
                         parse_mode="HTML")
        return

    if text == "🏡 返回主菜单":
        bot.send_message(message.chat.id, "🏡 已返回主菜单：", reply_markup=get_main_keyboard(user_id))
        return

    # ---------- 管理端菜单 ----------
    if not admin:
        return

    if text == "👨‍💼 管理面板":
        bot.send_message(message.chat.id, "🛠️ 欢迎来到管理工作台：",
                         reply_markup=get_admin_reply_keyboard(user_id))
        return

    if text == "📦 库存":
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.row(types.KeyboardButton("🔑 测试库存管理"))
        for p in get_products():
            markup.row(types.KeyboardButton(f"📥 库存管理: {p[1]}"))
        markup.row(types.KeyboardButton("🔙 返回管理面板"))
        bot.send_message(message.chat.id, "📦 中央库存，请选择要管理的服务：", reply_markup=markup)
        return

    if text == "🔑 测试库存管理":
        cnt = db_execute("SELECT COUNT(*) FROM configs WHERE product_id = 0 AND is_used = 0",
                         fetch="one")[0]
        markup = types.InlineKeyboardMarkup()
        markup.row(types.InlineKeyboardButton("➕ 添加测试账号", callback_data="m_add_0"),
                   types.InlineKeyboardButton("🗑️ 清理测试库存", callback_data="m_rst_0"))
        bot.send_message(message.chat.id,
                         f"🔑 <b>测试库存设置</b>\n\n当前可用数量: {cnt}\n\n请选择操作：",
                         reply_markup=markup, parse_mode="HTML")
        return

    if text.startswith("📥 库存管理: "):
        p_name = text.replace("📥 库存管理: ", "")
        row = db_execute("SELECT id FROM products WHERE name = ?", (p_name,), fetch="one")
        if not row:
            bot.reply_to(message, "未找到该商品")
            return
        cnt = db_execute("SELECT COUNT(*) FROM configs WHERE product_id = ? AND is_used = 0",
                         (row[0],), fetch="one")[0]
        markup = types.InlineKeyboardMarkup()
        markup.row(types.InlineKeyboardButton("➕ 添加配置", callback_data=f"m_add_{row[0]}"),
                   types.InlineKeyboardButton("🗑️ 清理库存", callback_data=f"m_rst_{row[0]}"))
        bot.send_message(message.chat.id,
                         f"📦 <b>套餐库存设置：</b>{p_name}\n\n当前可用账号数: {cnt}\n\n请选择操作：",
                         reply_markup=markup, parse_mode="HTML")
        return

    if text == "🛠️ 用户高级管理":
        bot.send_message(message.chat.id, "⚙️ <b>用户高级管理控制台：</b>",
                         reply_markup=get_advanced_users_inline(), parse_mode="HTML")
        return

    if text == "📊 机器人统计":
        total_users = db_execute("SELECT COUNT(*) FROM users", fetch="one")[0]
        total_configs = db_execute("SELECT COUNT(*) FROM configs WHERE is_used = 0", fetch="one")[0]
        total_sales = db_execute("SELECT COUNT(*) FROM user_services", fetch="one")[0]
        total_agents = db_execute("SELECT COUNT(*) FROM users WHERE is_agent = 1", fetch="one")[0]
        total_balance = db_execute("SELECT COALESCE(SUM(balance), 0) FROM users", fetch="one")[0]
        bot.send_message(message.chat.id,
                         f"📊 运营统计:\n\n"
                         f"👥 用户数: {total_users}\n\n"
                         f"👑 代理数: {total_agents}\n\n"
                         f"📦 剩余库存: {total_configs}\n\n"
                         f"🛒 已售服务: {total_sales}\n\n"
                         f"💰 用户余额总额: {total_balance:,} 元")
        return

    if text == "⚙️ 通用设置":
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.row(types.KeyboardButton("✏️ 修改收款卡"))
        markup.row(types.KeyboardButton("➕ 新增套餐"), types.KeyboardButton("❌ 删除套餐"))
        markup.row(types.KeyboardButton("🔙 返回管理面板"))
        bot.send_message(message.chat.id, "⚙️ 通用设置：", reply_markup=markup)
        return

    if text == "✏️ 修改收款卡":
        user_states[user_id] = "waiting_for_new_card"
        bot.send_message(message.chat.id, "💳 请发送新的收款卡号：")
        return

    if text == "➕ 新增套餐":
        user_states[user_id] = "waiting_new_btn_name"
        bot.send_message(message.chat.id, "✍️ 请输入新套餐的名称：")
        return

    if text == "❌ 删除套餐":
        prods = get_products()
        if not prods:
            bot.reply_to(message, "目前没有可删除的套餐。")
            return
        markup = types.InlineKeyboardMarkup()
        for p in prods:
            markup.row(types.InlineKeyboardButton(f"🗑️ 删除: {p[1]}", callback_data=f"delprod_{p[0]}"))
        bot.send_message(message.chat.id, "⚠️ 请选择要删除的商品：", reply_markup=markup)
        return

    if text == "📢 发送群发消息":
        user_states[user_id] = "waiting_for_broadcast"
        bot.send_message(message.chat.id, "✍️ 请输入你要群发的消息：")
        return

    if text == "🔙 返回管理面板":
        bot.send_message(message.chat.id, "🛠️ 已返回管理面板：",
                         reply_markup=get_admin_reply_keyboard(user_id))
        return
#__PHOTO__
# ============================== 图片（回执）处理 ==============================

@bot.message_handler(content_types=["photo"])
def handle_all_photos(message):
    user_id = message.from_user.id
    u = get_user(user_id)
    if u["is_banned"] == 1:
        return

    state = user_states.get(user_id)
    if not isinstance(state, dict):
        return

    action = state.get("action")

    if action == "send_charge_receipt":
        amount = state.get("amount", 0)
        user_states[user_id] = None
        log_tx(user_id, "charge", amount, "pending", "卡转账充值（等待审核）")
        bot.reply_to(message, "已收到回执图片，正在排队审核。")
        markup = get_receipt_management_keyboard(user_id, "charge", amount=amount)
        try:
            bot.send_photo(
                OWNER_ID, message.photo[-1].file_id,
                caption=f"充值审核申请\n\n用户: {user_id}\n\n金额: {amount:,} 元",
                reply_markup=markup,
            )
        except Exception as e:
            logging.exception("向管理员发送充值回执失败: %s", e)
        return

    if action == "direct_buy_receipt":
        prod_id = state.get("product_id")
        user_states[user_id] = None
        bot.reply_to(message, "已收到购买回执，审核通过后将为你开通服务。")
        markup = get_receipt_management_keyboard(user_id, "buy", extra_id=prod_id)
        try:
            bot.send_photo(
                OWNER_ID, message.photo[-1].file_id,
                caption=f"转账购买回执\n\n用户: {user_id}\n\n商品 ID: {prod_id}",
                reply_markup=markup,
            )
        except Exception as e:
            logging.exception("向管理员发送购买回执失败: %s", e)
        return
#__MAIN__
# ============================== 启动 ==============================

def main():
    if not OWNER_ID:
        logging.warning("尚未设置 OWNER_ID，管理功能将不可用。")
    print("✅ 机器人已启动，正在运行...")
    bot.infinity_polling(timeout=30, long_polling_timeout=30)


if __name__ == "__main__":
    main()
