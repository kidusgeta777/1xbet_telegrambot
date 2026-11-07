# Telegram deposit/withdraw bot with admin panel - Cloudflare Python Worker
# Secrets/vars:
#   BOT_TOKEN       Telegram bot token
#   ADMIN_ID        one Telegram user id, or several separated by commas (e.g. 111,222)
#   WEBHOOK_SECRET  random string (A-Z a-z 0-9 _ -). Telegram must send it back with every update.
#                   Open  https://<your-worker>/setup?key=<WEBHOOK_SECRET>  once to register the webhook.
# KV binding named SETTINGS (needed for settings, stats, conversation state, broadcast, saved accounts,
# request history, duplicate-receipt check, reject reasons, spam limit).
import asyncio
import hashlib
import hmac
import json
import random
import re
import time
import traceback
from html import escape as esc, unescape
from urllib.parse import parse_qs, urlparse

import js
from js import fetch as js_fetch
from pyodide.ffi import to_js
from workers import WorkerEntrypoint, Response

# Step markers: "<Title> · <step>". Also used to recognise replies when storage is off.
DEP1 = "Deposit · Step 1 of 3"
DEP2 = "Deposit · Step 2 of 3"
DEP3 = "Deposit · Step 3 of 3"
WIT1 = "Withdraw · Step 1 of 3"
WIT2 = "Withdraw · Step 2 of 3"
WIT3 = "Withdraw · Step 3 of 3"
SET_MARK = "Admin setting"

MAX_SAVED = 3  # saved 1xBet IDs / withdraw accounts per user
MAX_TEXT = 1500  # max length of welcome / payment text
MAX_REQ_HOUR = 5  # requests (deposit + withdraw) one user can send per hour
HIST_MAX = 10  # requests kept in "My requests"
REQ_TTL = 90 * 86400  # how long Approve / Reject buttons stay valid
TXN_TTL = 180 * 86400  # how long a used receipt / withdraw code stays blocked
DEBUG_PROOF = True  # step-by-step "LOG ·" messages about screenshots, sent to the first admin only. False = off

# short key (used in callback_data) -> label
METHODS = {"c": "CBE", "t": "Telebirr", "b": "CBE Birr"}
LABEL_TO_KEY = {v: k for k, v in METHODS.items()}

DEFAULTS = {
    "dep_on": True,
    "wit_on": True,
    "min_dep": 50,
    "max_dep": 100000,
    "pay_info": "Ask the admin for payment details.",
    "welcome": "Request a deposit or withdrawal in just a few taps.",
    "support": "",
}
STATS0 = {"dep_req": 0, "dep_ok": 0, "dep_no": 0, "wit_req": 0, "wit_ok": 0, "wit_no": 0}

# key -> (label, type). Type "rich" = text that keeps formatting (stored as Telegram HTML).
SETTING_INFO = {
    "min_dep": ("Minimum deposit (ETB)", "number"),
    "max_dep": ("Maximum deposit (ETB)", "number"),
    "pay_info": ("Payment details shown to depositors", "rich"),
    "welcome": ("Welcome message", "rich"),
    "support": ("Support username, without @ (send - to clear)", "text"),
}


# ---------- Formatting helpers ----------
def fmt(n):
    n = float(n)
    return f"{int(n):,}" if n == int(n) else f"{n:,.2f}"


def head(marker):
    """'Deposit · Step 1 of 3' -> bold title + plain step."""
    title, step = marker.split(" · ")
    return f"<b>{title}</b> · {step}"


def card(rows):
    """A tidy quoted block of 'Label  value' lines (values must already be HTML-safe)."""
    return "<blockquote>" + "\n".join(f"{k}  {v}" for k, v in rows) + "</blockquote>"


def err(text):
    return f"<i>{text}</i>\n\n"


def as_json(body):
    try:
        return json.loads(body)
    except Exception:
        return None


# ---------- Rich text: Telegram formatting / Markdown -> Telegram HTML ----------
FORMAT_TYPES = {
    "bold", "italic", "underline", "strikethrough", "spoiler", "code", "pre",
    "text_link", "blockquote", "expandable_blockquote",
}


def _entity_tags(e):
    t = e.get("type")
    if t == "bold":
        return "<b>", "</b>"
    if t == "italic":
        return "<i>", "</i>"
    if t == "underline":
        return "<u>", "</u>"
    if t == "strikethrough":
        return "<s>", "</s>"
    if t == "spoiler":
        return "<tg-spoiler>", "</tg-spoiler>"
    if t == "code":
        return "<code>", "</code>"
    if t == "pre":
        return "<pre>", "</pre>"
    if t == "blockquote":
        return "<blockquote>", "</blockquote>"
    if t == "expandable_blockquote":
        return "<blockquote expandable>", "</blockquote>"
    if t == "text_link":
        url = e.get("url") or ""
        if url.startswith(("http://", "https://", "tg://")):
            return f'<a href="{esc(url)}">', "</a>"
    return None


def entities_to_html(text, entities):
    """Turn a message's Telegram entities (UTF-16 offsets) into HTML."""
    data = text.encode("utf-16-le")
    n = len(data) // 2
    items = []
    for e in entities or []:
        tags = _entity_tags(e)
        if not tags:
            continue
        start = e.get("offset", 0)
        end = min(start + e.get("length", 0), n)
        if start < n and end > start:
            items.append((start, end, tags[0], tags[1]))
    items.sort(key=lambda x: (x[0], -x[1]))

    out, stack, pos = [], [], 0

    def emit(upto):
        nonlocal pos
        if upto > pos:
            out.append(esc(data[pos * 2:upto * 2].decode("utf-16-le", "replace")))
            pos = upto

    for start, end, o, c in items:
        while stack and stack[-1][0] <= start:
            emit(stack[-1][0])
            out.append(stack.pop()[1])
        emit(start)
        out.append(o)
        stack.append((end, c))
    while stack:
        emit(stack[-1][0])
        out.append(stack.pop()[1])
    emit(n)
    return "".join(out)


def markdown_to_html(text):
    """Small Markdown subset: **bold**, __underline__, *italic* / _italic_, ~~strike~~, `code`, [text](url)."""
    codes, links = [], []

    def stash_code(m):
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    t = re.sub(r"`([^`\n]+)`", stash_code, text)
    t = esc(t)

    def stash_link(m):
        links.append(f'<a href="{m.group(2)}">{m.group(1)}</a>')
        return f"\x01{len(links) - 1}\x01"

    t = re.sub(r"\[([^\]\n]+)\]\(((?:https?|tg)://[^\s)]+)\)", stash_link, t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)
    t = re.sub(r"__(.+?)__", r"<u>\1</u>", t, flags=re.S)
    t = re.sub(r"~~(.+?)~~", r"<s>\1</s>", t, flags=re.S)
    t = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", t)
    t = re.sub(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", t)
    t = re.sub(r"\x01(\d+)\x01", lambda m: links[int(m.group(1))], t)
    t = re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{esc(codes[int(m.group(1))])}</code>", t)
    return t


def html_ok(h):
    """True when every tag is closed in the right order (Telegram rejects the message otherwise)."""
    stack = []
    for m in re.finditer(r"<(/?)([a-z-]+)[^>]*>", h):
        if m.group(1):
            if not stack or stack.pop() != m.group(2):
                return False
        else:
            stack.append(m.group(2))
    return not stack


def plain(h):
    """Telegram HTML -> plain text (last-resort fallback)."""
    return unescape(re.sub(r"<[^>]+>", "", h))


def to_html(text, entities):
    """Keep whatever formatting the admin used: Telegram's own, or typed Markdown.
    If the result would be broken HTML, fall back to the plain text."""
    if any(e.get("type") in FORMAT_TYPES for e in (entities or [])):
        out = entities_to_html(text, entities)
    else:
        out = markdown_to_html(text)
    return out if html_ok(out) else esc(text)


# ---------- Parsing / validation ----------
def parse_amount(text):
    try:
        n = float(str(text or "").replace(",", "").strip())
    except ValueError:
        return None
    if n > 0 and n < 1e12:
        n = round(n, 2)
        return int(n) if n == int(n) else n
    return None


def parse_xbet_id(text):
    t = str(text or "").strip().replace(" ", "")
    return t if re.fullmatch(r"\d{5,15}", t) else None


def parse_wit_code(text):
    """Withdraw code: exactly 4 letters / digits, upper and lower case are different."""
    t = str(text or "").strip().replace(" ", "")
    return t if re.fullmatch(r"[A-Za-z0-9]{4}", t) else None


def parse_account(method, text):
    """CBE: 13-digit account. Telebirr / CBE Birr: Ethiopian phone number (09.. or 07..)."""
    t = str(text or "").strip().replace(" ", "").replace("-", "")
    if method == "c":
        return t if re.fullmatch(r"\d{13}", t) else None
    if t.startswith("+251"):
        t = "0" + t[4:]
    elif t.startswith("251") and len(t) == 12:
        t = "0" + t[3:]
    return t if re.fullmatch(r"0[79]\d{8}", t) else None


def user_label(u):
    name = " ".join(x for x in [u.get("first_name"), u.get("last_name")] if x)
    return f"{name} (@{u['username']})" if u.get("username") else name


def force_reply(placeholder):
    return {"force_reply": True, "input_field_placeholder": placeholder}


def new_rid():
    return "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=8))


def hkey(prefix, value):
    return prefix + hashlib.sha256(value.encode()).hexdigest()[:32]


def _file_id(msg, key):
    """Unique id of the photo / file. Works whether Telegram's data arrives as plain Python
    or as JavaScript objects, and never raises (falls back to a random id)."""
    try:
        v = msg.get(key)
        if key == "photo":
            v = list(v)[-1]  # biggest size is last
        for getter in (lambda o: o["file_unique_id"], lambda o: getattr(o, "file_unique_id")):
            try:
                got = getter(v)
                if got:
                    return str(got)
            except Exception:
                pass
    except Exception:
        print("file id error", traceback.format_exc())
    return new_rid()


def proof_ident(msg):
    """What identifies a payment proof: a photo / file id, or a normalised transaction ID.
    None means the message is not a usable proof."""
    if msg.get("photo"):
        return "p:" + _file_id(msg, "photo")
    if msg.get("document"):
        return "d:" + _file_id(msg, "document")
    t = re.sub(r"\s+", "", str(msg.get("text") or "")).upper()
    if len(t) >= 6 and re.search(r"\d", t) and not t.startswith("/"):
        return "t:" + t
    return None


class Bot:
    def __init__(self, env):
        self.token = env.BOT_TOKEN
        raw_admin = getattr(env, "ADMIN_ID", None)
        self.admins = (
            [p.strip() for p in str(raw_admin).replace(";", ",").split(",") if p.strip()]
            if raw_admin else []
        )
        self.admin_id = self.admins[0] if self.admins else None
        self.secret = getattr(env, "WEBHOOK_SECRET", None)
        self.kv = getattr(env, "SETTINGS", None)
        self.pm = None  # id of the bot message currently on screen (edited in place)

    # ---------- Telegram + storage helpers ----------
    async def tg(self, method, payload):
        options = to_js(
            {
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(payload),
            },
            dict_converter=js.Object.fromEntries,
        )
        resp = await js_fetch(f"https://api.telegram.org/bot{self.token}/{method}", options)
        body = await resp.text()
        if resp.status != 200:
            print(method, resp.status, body)
        return body

    async def dbg(self, text):
        """Debug line for the first admin only (never the user). Plain text, never raises."""
        if not DEBUG_PROOF or not self.admins:
            return
        try:
            await self.tg("sendMessage", {"chat_id": self.admins[0], "text": ("LOG · " + text)[:900]})
        except Exception:
            print("dbg error", traceback.format_exc())

    async def tg_text(self, method, payload):
        """sendMessage / editMessageText. If Telegram can't parse the HTML, retry as plain text
        so one bad formatting never makes the bot go silent."""
        body = await self.tg(method, payload)
        d = as_json(body)
        if d and not d.get("ok") and "parse entities" in str(d.get("description", "")).lower():
            p = {**payload, "text": plain(payload.get("text", ""))}
            p.pop("parse_mode", None)
            body = await self.tg(method, p)
        return body

    async def send(self, chat_id, text, markup=None, reply_to=None):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if markup:
            payload["reply_markup"] = markup
        if reply_to:
            payload["reply_to_message_id"] = reply_to
            payload["allow_sending_without_reply"] = True
        return await self.tg_text("sendMessage", payload)

    async def delete(self, chat_id, mid):
        if mid:
            await self.tg("deleteMessage", {"chat_id": chat_id, "message_id": mid})

    async def ui(self, chat_id, text, markup=None):
        """Show a screen by editing the message already on screen; send a new one only if needed."""
        if self.pm and not (markup and "force_reply" in markup):
            payload = {
                "chat_id": chat_id,
                "message_id": self.pm,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": markup if markup else {"inline_keyboard": []},
            }
            d = as_json(await self.tg_text("editMessageText", payload))
            if d and (d.get("ok") or "not modified" in str(d.get("description", "")).lower()):
                return
        elif self.pm:
            # a reply prompt can't be made by editing, so drop the old message and send a fresh one
            await self.delete(chat_id, self.pm)
        d = as_json(await self.send(chat_id, text, markup))
        self.pm = ((d or {}).get("result") or {}).get("message_id")

    async def step(self, chat_id, text, markup, st):
        """Show a screen and remember which step (and which message) we're on."""
        await self.ui(chat_id, text, markup)
        if self.pm:
            st = {**st, "pm": self.pm}
        await self.set_state(chat_id, st)

    async def answer(self, q, text=None, alert=False):
        payload = {"callback_query_id": q["id"]}
        if text:
            payload["text"] = text
            payload["show_alert"] = alert
        return await self.tg("answerCallbackQuery", payload)

    async def kv_get(self, key, default):
        if self.kv is None:
            return default
        try:
            raw = await self.kv.get(key)
            if isinstance(raw, str):
                data = json.loads(raw)
                if isinstance(default, dict) and isinstance(data, dict):
                    return {**default, **data}
                return data
        except Exception:
            print("KV read error", traceback.format_exc())
        return default

    async def kv_exists(self, key):
        if self.kv is None:
            return False
        try:
            return isinstance(await self.kv.get(key), str)
        except Exception:
            print("KV read error", traceback.format_exc())
            return False

    async def kv_set(self, key, value, ttl=None):
        if self.kv is None:
            return False
        try:
            if ttl:
                opts = to_js({"expirationTtl": int(ttl)}, dict_converter=js.Object.fromEntries)
                await self.kv.put(key, json.dumps(value), opts)
            else:
                await self.kv.put(key, json.dumps(value))
            return True
        except Exception:
            print("KV write error", traceback.format_exc())
            return False

    async def settings(self):
        return await self.kv_get("settings", dict(DEFAULTS))

    def is_admin(self, uid):
        return str(uid) in self.admins

    # ---------- Conversation state (so users can just type) ----------
    async def get_state(self, cid):
        if self.kv is None:
            return None
        try:
            raw = await self.kv.get(f"state:{cid}")
            if isinstance(raw, str):
                return json.loads(raw)
        except Exception:
            print("state read error", traceback.format_exc())
        return None

    async def set_state(self, cid, st):
        if self.kv is None:
            return
        try:
            opts = to_js({"expirationTtl": 3600}, dict_converter=js.Object.fromEntries)
            await self.kv.put(f"state:{cid}", json.dumps(st), opts)
        except Exception:
            print("state write error", traceback.format_exc())

    async def clear_state(self, cid, notify=True):
        """Drop the saved step. If an admin was about to give a reject reason and walks away,
        the user still gets the rejection (without a reason)."""
        if self.kv is None:
            return
        try:
            raw = await self.kv.get(f"state:{cid}")
            await self.kv.delete(f"state:{cid}")
            if notify and isinstance(raw, str):
                st = json.loads(raw)
                if st.get("step") == "reason":
                    await self.notify_decision(st["uid"], st["kind"], st["ref"], False, None)
        except Exception:
            print("state delete error", traceback.format_exc())

    def prompt_markup(self, placeholder):
        # With storage the bot remembers the step, so no "reply" is needed.
        if self.kv is not None:
            return {"inline_keyboard": [[{"text": "Cancel", "callback_data": "cancel"}]]}
        return force_reply(placeholder)

    def how_to(self):
        return "Type it below." if self.kv is not None else "Reply to this message."

    # ---------- Users (for broadcast) ----------
    async def touch(self, uid):
        """Remember every user who talks to the bot."""
        if self.kv is None or not uid:
            return
        try:
            raw = await self.kv.get(f"user:{uid}")
            if not isinstance(raw, str):
                await self.kv.put(f"user:{uid}", "1")
        except Exception:
            print("touch error", traceback.format_exc())

    async def list_users(self):
        ids, cursor = [], None
        if self.kv is None:
            return ids
        try:
            while True:
                opts = {"prefix": "user:", "limit": 1000}
                if cursor:
                    opts["cursor"] = cursor
                res = await self.kv.list(to_js(opts, dict_converter=js.Object.fromEntries))
                data = json.loads(js.JSON.stringify(res))
                ids += [k["name"][5:] for k in data.get("keys", [])]
                if data.get("list_complete") or not data.get("cursor"):
                    break
                cursor = data["cursor"]
        except Exception:
            print("list users error", traceback.format_exc())
        return ids

    # ---------- Saved items (max 3 per user): withdraw accounts + 1xBet IDs ----------
    async def _saved(self, key):
        items = await self.kv_get(key, [])
        return items if isinstance(items, list) else []

    async def get_accounts(self, uid):
        items = await self._saved(f"accts:{uid}")
        return [a for a in items if isinstance(a, dict) and a.get("m") in METHODS and a.get("a")][:MAX_SAVED]

    async def get_xids(self, uid):
        items = await self._saved(f"xids:{uid}")
        return [a for a in items if isinstance(a, dict) and a.get("a")][:MAX_SAVED]

    async def _record(self, key, items, new, same):
        """Known item -> use count +1. New item -> added, or replaces the least-used one
        (oldest use breaks a tie) when MAX_SAVED are already stored."""
        now = int(time.time())
        for it in items:
            if same(it):
                it["n"] = int(it.get("n", 0)) + 1
                it["t"] = now
                break
        else:
            new.update({"n": 1, "t": now})
            if len(items) < MAX_SAVED:
                items.append(new)
            else:
                victim = min(range(len(items)), key=lambda i: (items[i].get("n", 0), items[i].get("t", 0)))
                items[victim] = new
        await self.kv_set(key, items)

    async def record_account(self, uid, mkey, account):
        if self.kv is None:
            return
        items = await self.get_accounts(uid)
        await self._record(
            f"accts:{uid}", items, {"m": mkey, "a": account},
            lambda a: a["m"] == mkey and a["a"] == account,
        )

    async def record_xid(self, uid, xid):
        if self.kv is None:
            return
        items = await self.get_xids(uid)
        await self._record(f"xids:{uid}", items, {"a": xid}, lambda a: a["a"] == xid)

    # ---------- User menu ----------
    def menu_markup(self, s, admin=False):
        rows = [[
            {"text": "Deposit", "callback_data": "deposit"},
            {"text": "Withdraw", "callback_data": "withdraw"},
        ]]
        row2 = [{"text": "My requests", "callback_data": "hist"}]
        if s.get("support"):
            row2.append({"text": "Support", "url": f"https://t.me/{s['support']}"})
        rows.append(row2)
        if admin:
            rows.append([{"text": "Admin panel", "callback_data": "adm:home"}])
        return {"inline_keyboard": rows}

    def menu_view(self, s, uid):
        text = f"<b>Welcome</b>\n\n{s['welcome']}"  # already Telegram HTML
        return text, self.menu_markup(s, self.is_admin(uid))

    async def menu(self, chat_id, uid=None):
        s = await self.settings()
        text, markup = self.menu_view(s, uid)
        await self.ui(chat_id, text, markup)

    # ---------- Admin panel ----------
    def panel_view(self, s, note=""):
        warn = "" if self.kv is not None else err("Storage not connected. Settings can't be saved.")
        if not self.secret:
            warn += err("WEBHOOK_SECRET is not set. Anyone can send fake updates to this bot.")
        support = f"@{esc(s['support'])}" if s["support"] else "not set"
        rows = [
            ("Deposits", "<b>On</b>" if s["dep_on"] else "<b>Off</b>"),
            ("Withdrawals", "<b>On</b>" if s["wit_on"] else "<b>Off</b>"),
            ("Deposit limits", f"{fmt(s['min_dep'])} – {fmt(s['max_dep'])} ETB"),
            ("Support", support),
        ]
        text = f"<b>Admin panel</b>\n\n{note}{warn}{card(rows)}\n\nTap an option to change it."
        markup = {"inline_keyboard": [
            [
                {"text": f"Deposits: {'On' if s['dep_on'] else 'Off'}", "callback_data": "adm:tdep"},
                {"text": f"Withdrawals: {'On' if s['wit_on'] else 'Off'}", "callback_data": "adm:twit"},
            ],
            [
                {"text": "Min deposit", "callback_data": "adm:set:min_dep"},
                {"text": "Max deposit", "callback_data": "adm:set:max_dep"},
            ],
            [
                {"text": "Payment info", "callback_data": "adm:set:pay_info"},
                {"text": "Welcome text", "callback_data": "adm:set:welcome"},
            ],
            [
                {"text": "Support", "callback_data": "adm:set:support"},
                {"text": "Stats", "callback_data": "adm:stats"},
            ],
            [{"text": "Broadcast", "callback_data": "adm:bc"}],
            [{"text": "Main menu", "callback_data": "menu"}],
        ]}
        return text, markup

    async def send_panel(self, chat_id):
        text, markup = self.panel_view(await self.settings())
        await self.ui(chat_id, text, markup)

    async def stats_view(self):
        st = await self.kv_get("stats", dict(STATS0))

        def block(title, p):
            pending = max(st[f"{p}_req"] - st[f"{p}_ok"] - st[f"{p}_no"], 0)
            return (
                f"<b>{title}</b>\n"
                + card([
                    ("Requests", st[f"{p}_req"]),
                    ("Approved", st[f"{p}_ok"]),
                    ("Rejected", st[f"{p}_no"]),
                    ("Pending", pending),
                ])
            )

        text = f"<b>Stats</b>\n\n{block('Deposits', 'dep')}\n\n{block('Withdrawals', 'wit')}"
        if self.kv is not None:
            text += f"\n\n<b>Users</b>\n{card([('Registered', len(await self.list_users()))])}"
        else:
            text += "\n\n" + err("Storage not connected. Stats aren't being recorded.").strip()
        markup = {"inline_keyboard": [[{"text": "Back", "callback_data": "adm:home"}]]}
        return text, markup

    async def bump(self, key):
        if self.kv is None:
            return
        st = await self.kv_get("stats", dict(STATS0))
        st[key] = st.get(key, 0) + 1
        await self.kv_set("stats", st)

    async def save_setting(self, chat_id, key, text, entities=None):
        if key not in SETTING_INFO:
            return await self.send_panel(chat_id)
        label, kind = SETTING_INFO[key]
        s = await self.settings()

        if kind == "number":
            value = parse_amount(text)
            if value is None:
                return await self.ask_setting(chat_id, key, error=True)
        elif kind == "rich":
            if not text.strip() or len(text) > MAX_TEXT:
                return await self.ask_setting(chat_id, key, error=True)
            value = to_html(text.strip(), entities)
        else:
            value = text.strip()
            if key == "support":
                value = "" if value == "-" else re.sub(r"[^A-Za-z0-9_]", "", value.lstrip("@"))
            elif not value:
                return await self.ask_setting(chat_id, key, error=True)
            value = value[:500]

        s[key] = value
        saved = await self.kv_set("settings", s)
        await self.clear_state(chat_id)
        if saved:
            note = f"<b>Saved</b> · {esc(label)}\n\n"
        else:
            note = err("Couldn't save. Connect a KV namespace named <code>SETTINGS</code> first.")
        text_out, markup = self.panel_view(s, note)
        await self.ui(chat_id, text_out, markup)

    async def ask_setting(self, chat_id, key, error=False):
        label, kind = SETTING_INFO[key]
        if error and kind == "rich":
            note = err(f"That text isn't valid. Send up to {MAX_TEXT} characters.")
        else:
            note = err("That value isn't valid. Try again.") if error else ""
        hint = "a number" if kind == "number" else "your text"
        extra = ""
        if kind == "rich":
            extra = (
                "\n<i>Formatting is kept. Use Telegram's formatting, or type "
                "**bold**, _italic_, __underline__, ~~strike~~, `code`, [text](https://link).</i>\n"
            )
        ref = "" if self.kv is not None else f"\n\n<i>ref: {key}</i>"
        text = (
            f"<b>{SET_MARK}</b>\n\n{note}"
            f"<b>{esc(label)}</b>\nSend {hint}.\n{extra}\n"
            f"{self.how_to()}{ref}"
        )
        await self.step(chat_id, text, self.prompt_markup(label[:60]), {"step": "set", "key": key})

    # ---------- Broadcast (admin) ----------
    async def ask_broadcast(self, chat_id):
        if self.kv is None:
            return await self.ui(
                chat_id,
                "Broadcast needs storage. Connect a KV namespace named <code>SETTINGS</code> first.",
                {"inline_keyboard": [[{"text": "Back", "callback_data": "adm:home"}]]},
            )
        await self.step(
            chat_id,
            "<b>Broadcast</b>\n\n"
            "Send the message everyone should receive. Text, photo, video or any file works, "
            "and your formatting is kept.\n\nType it below.",
            self.prompt_markup("Message to broadcast"),
            {"step": "bc"},
        )

    async def on_broadcast_msg(self, msg):
        chat_id = msg["chat"]["id"]
        users = await self.list_users()
        if not users:
            await self.clear_state(chat_id)
            return await self.ui(
                chat_id, "No users registered yet.",
                {"inline_keyboard": [[{"text": "Back", "callback_data": "adm:home"}]]},
            )
        # the admin's message stays (it's what gets copied); replace the old prompt with a confirm
        await self.delete(chat_id, self.pm)
        self.pm = None
        await self.set_state(chat_id, {"step": "bc_confirm", "mid": msg["message_id"]})
        markup = {"inline_keyboard": [
            [{"text": f"Send to {len(users)} users", "callback_data": "bc:go"}],
            [{"text": "Cancel", "callback_data": "cancel"}],
        ]}
        await self.send(
            chat_id,
            f"<b>Broadcast</b>\n\nSend the message above to <b>{len(users)}</b> users?",
            markup,
            reply_to=msg["message_id"],
        )

    async def _copy_one(self, uid, from_chat, mid):
        for _ in range(2):
            body = await self.tg("copyMessage", {
                "chat_id": int(uid), "from_chat_id": from_chat, "message_id": mid,
            })
            d = as_json(body)
            if d is None:
                return "fail"
            if d.get("ok"):
                return "ok"
            code = d.get("error_code")
            if code == 429:
                wait = (d.get("parameters") or {}).get("retry_after", 1)
                await asyncio.sleep(min(int(wait), 5))
                continue
            desc = str(d.get("description", "")).lower()
            if code == 403 or "chat not found" in desc or "user is deactivated" in desc:
                try:
                    await self.kv.delete(f"user:{uid}")
                except Exception:
                    pass
                return "blocked"
            return "fail"
        return "fail"

    async def run_broadcast(self, admin_chat, mid):
        ids = await self.list_users()
        counts = {"ok": 0, "blocked": 0, "fail": 0}
        for i in range(0, len(ids), 20):
            results = await asyncio.gather(*[self._copy_one(u, admin_chat, mid) for u in ids[i:i + 20]])
            for r in results:
                counts[r] += 1
            await asyncio.sleep(0.7)  # stay under Telegram's ~30 msg/sec limit
        await self.ui(
            admin_chat,
            "<b>Broadcast finished</b>\n\n"
            + card([
                ("Delivered", f"<b>{counts['ok']}</b>"),
                ("Blocked the bot", counts["blocked"]),
                ("Failed", counts["fail"]),
            ]),
            {"inline_keyboard": [[{"text": "Back to panel", "callback_data": "adm:home"}]]},
        )

    # ---------- Admin notification ----------
    async def send_to_admin(self, user, kind, amount, proof=None, account=None, xid=None,
                            code=None, method=None, tk=None):
        """Notify every admin. Returns the request id carried by the Approve / Reject buttons."""
        rid = new_rid()
        title = "New deposit request" if kind == "d" else "New withdraw request"
        rows = [
            ("User", esc(user_label(user))),
            ("User ID", f"<code>{user['id']}</code>"),
        ]
        if xid:
            rows.append(("1xBet ID", f"<code>{esc(xid)}</code>"))
        if code:
            rows.append(("Withdraw code", f"<code>{esc(code)}</code>"))
        if amount is not None:
            rows.append(("Amount", f"<b>{fmt(amount)} ETB</b>"))
        if method:
            rows.append(("Method", esc(method)))
        if account:
            rows.append(("Account", f"<code>{esc(account)}</code>"))
        ref = amount if kind == "d" else code  # what the buttons carry: amount (deposit) or code (withdraw)
        markup = {"inline_keyboard": [[
            {"text": "Approve", "callback_data": f"ok:{kind}:{user['id']}:{ref}:{rid}"},
            {"text": "Reject", "callback_data": f"no:{kind}:{user['id']}:{ref}:{rid}"},
        ]]}
        msgs = []
        for aid in self.admins:
            is_file = bool(proof and (proof.get("photo") or proof.get("document")))
            if kind == "d" and proof:
                fwd = {
                    "chat_id": aid,
                    "from_chat_id": proof["chat"]["id"],
                    "message_id": proof["message_id"],
                }
                cd = as_json(await self.tg("forwardMessage", fwd))  # real forward of the user's message
                if not (cd and cd.get("ok")):
                    cd = as_json(await self.tg("copyMessage", fwd))  # fallback if forwarding is refused
                if is_file:
                    good = bool(cd and cd.get("ok"))
                    await self.dbg(f"forward of file to admin {aid}: ok={good}" + ("" if good else f" {str(cd)[:300]}"))
            d = as_json(await self.send(aid, f"<b>{title}</b>\n\n{card(rows)}", markup))
            mid = ((d or {}).get("result") or {}).get("message_id")
            if is_file:
                await self.dbg(f"request card to admin {aid}: message_id={mid}" + ("" if mid else f" {str(d)[:300]}"))
            if mid:
                msgs.append([aid, mid])
        # the record lets us ignore a second tap and update every admin's copy
        await self.kv_set(
            f"req:{rid}",
            {"k": kind, "u": user["id"], "a": ref, "m": msgs, "tk": tk, "s": "p"},
            REQ_TTL,
        )
        return rid

    async def notify_decision(self, user_id, kind, ref, approved, reason):
        what = "Deposit" if kind == "d" else "Withdrawal"
        if approved:
            title, note = f"{what} approved", "Thank you."
        else:
            title = f"{what} rejected"
            note = f"<b>Reason</b>\n<blockquote>{esc(reason)}</blockquote>\n\n" if reason else ""
            note += "Please contact support if you have questions."
        if kind == "d":
            info = card([("Amount", f"<b>{fmt(ref)} ETB</b>")])
        else:
            info = card([("Withdraw code", f"<code>{esc(str(ref))}</code>")])
        s = await self.settings()
        await self.send(
            user_id,
            f"<b>{title}</b>\n\n{info}\n\n{note}",
            self.menu_markup(s, self.is_admin(user_id)),
        )

    async def finish_reject(self, chat_id, st, reason):
        """Admin gave a reason (or skipped): tell the user."""
        back = {"inline_keyboard": [[{"text": "Admin panel", "callback_data": "adm:home"}]]}
        if st.get("step") != "reason":
            return await self.ui(chat_id, "Nothing to send.", back)
        await self.clear_state(chat_id, notify=False)
        await self.notify_decision(st["uid"], st["kind"], st["ref"], False, reason)
        await self.ui(chat_id, "<b>Rejected</b>\n\nThe user was notified.", back)

    # ---------- Spam limit ----------
    async def rate_ok(self, uid):
        if self.kv is None or self.is_admin(uid):
            return True
        d = await self.kv_get(f"rl:{uid}", {"n": 0, "t": 0})
        if int(time.time()) - int(d.get("t", 0)) >= 3600:
            return True
        return int(d.get("n", 0)) < MAX_REQ_HOUR

    async def rate_hit(self, uid):
        if self.kv is None or self.is_admin(uid):
            return
        now = int(time.time())
        d = await self.kv_get(f"rl:{uid}", {"n": 0, "t": 0})
        if now - int(d.get("t", 0)) >= 3600:
            d = {"n": 1, "t": now}
        else:
            d["n"] = int(d.get("n", 0)) + 1
        await self.kv_set(f"rl:{uid}", d, max(60, 3600 - (now - int(d["t"]))))

    # ---------- Request history ("My requests") ----------
    async def hist_add(self, uid, rid, kind, amount):
        if self.kv is None:
            return
        items = await self._saved(f"hist:{uid}")
        items.append({"r": rid, "k": kind, "a": amount, "s": "p", "t": int(time.time())})
        await self.kv_set(f"hist:{uid}", items[-HIST_MAX:])

    async def hist_update(self, uid, rid, status):
        if self.kv is None or not rid:
            return
        items = await self._saved(f"hist:{uid}")
        for it in items:
            if isinstance(it, dict) and it.get("r") == rid:
                it["s"] = status
        await self.kv_set(f"hist:{uid}", items)

    def hist_view(self, items):
        names = {"p": "Pending", "a": "Approved", "r": "Rejected"}
        items = [i for i in items if isinstance(i, dict) and i.get("k") in ("d", "w")]
        if not items:
            body = "You have no requests yet."
        else:
            rows = []
            for it in reversed(items):
                what = "Deposit" if it["k"] == "d" else "Withdrawal"
                day = time.strftime("%d %b", time.gmtime(int(it.get("t", 0)) + 3 * 3600))  # Ethiopia, UTC+3
                detail = f"{fmt(it['a'])} ETB" if it["k"] == "d" else f"code {esc(str(it['a']))}"
                rows.append((day, f"{what} {detail} · {names.get(it.get('s'), '')}"))
            body = card(rows)
        markup = {"inline_keyboard": [[{"text": "Main menu", "callback_data": "menu"}]]}
        return f"<b>Your requests</b>\n\n{body}", markup

    def help_view(self, uid):
        lines = [
            "<b>Help</b>\n",
            "<b>Deposit</b> · send the amount, your 1xBet ID, then your payment proof.",
            "<b>Withdraw</b> · send your 4-character withdraw code, then where to receive the money.",
            "<b>My requests</b> · your latest requests and their status.\n",
            "/start · main menu",
            "/history · your requests",
            "/help · this message",
        ]
        if self.is_admin(uid):
            lines.append("/admin · admin panel")
        markup = {"inline_keyboard": [[{"text": "Main menu", "callback_data": "menu"}]]}
        return "\n".join(lines), markup

    # ---------- Deposit flow: amount -> 1xBet ID (type or tap saved) -> proof ----------
    async def ask_amount(self, chat_id, s, error=False):
        lo, hi = s["min_dep"], s["max_dep"]
        note = err(f"Enter a valid amount between {fmt(lo)} and {fmt(hi)} ETB.") if error else ""
        text = (
            f"{head(DEP1)}\n\n{note}"
            f"Enter the amount in <b>ETB</b>.\n<i>Min {fmt(lo)} · Max {fmt(hi)}</i>\n\n"
            f"{self.how_to()}"
        )
        await self.step(chat_id, text, self.prompt_markup("Amount in ETB"), {"step": "dep1"})

    async def on_amount(self, chat_id, text):
        s = await self.settings()
        if not s["dep_on"]:
            await self.clear_state(chat_id)
            return await self.ui(chat_id, "Deposits are temporarily unavailable. Please try again later.")
        amount = parse_amount(text)
        if amount is None or amount < s["min_dep"] or amount > s["max_dep"]:
            return await self.ask_amount(chat_id, s, error=True)
        await self.ask_xbet(chat_id, amount)

    async def ask_xbet(self, chat_id, amount, error=False):
        saved = await self.get_xids(chat_id)
        note = err("Invalid 1xBet ID. Send numbers only.") if error else ""
        if saved:
            ask = "Tap a saved ID below, or type a new <b>1xBet ID</b>."
            rows = [
                [{"text": f"1xBet · {x['a']}", "callback_data": f"sx:{x['a']}:{amount}"}]
                for x in saved
            ]
            rows.append([{"text": "Cancel", "callback_data": "cancel"}])
            markup = {"inline_keyboard": rows}
        else:
            ask = "Send your <b>1xBet ID</b>, the account to be credited."
            markup = self.prompt_markup("Your 1xBet ID")
        text = (
            f"{head(DEP2)}\n\n{note}"
            f"{card([('Amount', f'<b>{fmt(amount)} ETB</b>')])}\n\n"
            f"{ask}\n{self.how_to()}"
        )
        await self.step(chat_id, text, markup, {"step": "dep2", "amount": str(amount)})

    async def proof_error(self, chat_id, amount, xid, s, error):
        """Show the problem as a NEW message under the user's proof (an edit of the old prompt
        up the chat would go unnoticed)."""
        await self.delete(chat_id, self.pm)
        self.pm = None
        await self.show_pay_step(chat_id, amount, xid, s, error=error)

    async def show_pay_step(self, chat_id, amount, xid, s, error=None):
        pay = s["pay_info"]  # already Telegram HTML
        pay_block = pay if "<blockquote" in pay else f"<blockquote>{pay}</blockquote>"
        note = err(error) if error else ""
        body = (
            f"{head(DEP3)}\n\n{note}"
            f"{card([('Amount', f'<b>{fmt(amount)} ETB</b>'), ('1xBet ID', f'<code>{xid}</code>')])}\n\n"
            f"<b>Pay to</b>\n{pay_block}\n\n"
            "After paying, send a <b>screenshot</b> or the <b>transaction ID</b>.\n"
            f"{self.how_to()}"
        )
        await self.step(
            chat_id, body, self.prompt_markup("Screenshot or transaction ID"),
            {"step": "dep3", "amount": str(amount), "xid": xid},
        )

    async def on_xbet(self, chat_id, amount, text):
        s = await self.settings()
        if not s["dep_on"]:
            await self.clear_state(chat_id)
            return await self.ui(chat_id, "Deposits are temporarily unavailable. Please try again later.")
        xid = parse_xbet_id(text)
        if not xid:
            return await self.ask_xbet(chat_id, amount, error=True)
        await self.show_pay_step(chat_id, amount, xid, s)

    async def on_proof(self, msg, amount, xid):
        is_file = bool(msg.get("photo") or msg.get("document"))
        try:
            if is_file:
                await self.dbg(f"on_proof start: amount={amount}, xid={xid}")
            result = await self._on_proof(msg, amount, xid)
            if is_file:
                await self.dbg("on_proof finished")
            return result
        except Exception:
            if is_file:
                await self.dbg("on_proof EXCEPTION:\n" + traceback.format_exc()[-700:])
            raise

    async def _on_proof(self, msg, amount, xid):
        is_file = bool(msg.get("photo") or msg.get("document"))
        chat_id = msg["chat"]["id"]
        uid = msg["from"]["id"]
        s = await self.settings()
        if not s["dep_on"]:
            await self.clear_state(chat_id)
            return await self.ui(chat_id, "Deposits are temporarily unavailable. Please try again later.")
        if not self.admins:
            print("ADMIN_ID is not set")
            return await self.ui(chat_id, "Service unavailable. Please try again later.")
        if not xid:
            return await self.ask_xbet(chat_id, amount)  # safety net
        if not await self.rate_ok(uid):
            if is_file:
                await self.dbg("stopped: spam limit")
            await self.clear_state(chat_id)
            return await self.ui(
                chat_id, "Too many requests. Please try again later.",
                self.menu_markup(s, self.is_admin(uid)),
            )

        # the proof must be a screenshot / file, or a transaction ID that looks real
        ident = proof_ident(msg)
        if is_file:
            await self.dbg(f"proof id: {ident}")
        if not ident:
            return await self.proof_error(
                chat_id, amount, xid, s, "Send a screenshot or a valid transaction ID.",
            )
        tk = None
        if self.kv is not None and not self.is_admin(uid):  # admins can test with the same screenshot
            tk = hkey("txn:", ident)
            dup = await self.kv_exists(tk)
            if is_file:
                await self.dbg(f"duplicate receipt: {dup}")
            if dup:
                return await self.proof_error(
                    chat_id, amount, xid, s, "This receipt was already used. Send a different one.",
                )

        rid = await self.send_to_admin(msg["from"], "d", amount, proof=msg, xid=xid, tk=tk)
        if is_file:
            await self.dbg(f"request {rid} created")
        if tk:
            await self.kv_set(tk, 1, TXN_TTL)
        await self.record_xid(chat_id, xid)
        await self.hist_add(uid, rid, "d", amount)
        await self.rate_hit(uid)
        await self.bump("dep_req")
        await self.clear_state(chat_id)
        # the user's proof stays in the chat, so remove the old prompt and confirm below it
        await self.delete(chat_id, self.pm)
        self.pm = None
        await self.ui(
            chat_id,
            "<b>Request sent</b>\n\n"
            f"{card([('Deposit', f'<b>{fmt(amount)} ETB</b>'), ('1xBet ID', f'<code>{esc(xid)}</code>')])}\n\n"
            "The admin will review it. You'll be notified here.",
            self.menu_markup(s, self.is_admin(uid)),
        )

    # ---------- Withdraw flow: code -> method / saved account -> account ----------
    async def ask_wit_code(self, chat_id, error=None):
        note = err(error) if error else ""
        text = (
            f"{head(WIT1)}\n\n{note}"
            "Send your <b>withdraw code</b>.\n"
            "<i>4 characters, letters and numbers. Capital and small letters are different.</i>\n\n"
            f"{self.how_to()}"
        )
        await self.step(chat_id, text, self.prompt_markup("Withdraw code"), {"step": "wit1"})

    async def on_wit_code(self, chat_id, text):
        s = await self.settings()
        if not s["wit_on"]:
            await self.clear_state(chat_id)
            return await self.ui(chat_id, "Withdrawals are temporarily unavailable. Please try again later.")
        code = parse_wit_code(text)
        if not code:
            return await self.ask_wit_code(chat_id, error="Invalid code. It must be exactly 4 letters or numbers.")
        if await self.kv_exists(hkey("wc:", code)):
            return await self.ask_wit_code(chat_id, error="This code is already waiting for the admin's review.")
        await self.ask_wit_method(chat_id, code)

    async def ask_wit_method(self, chat_id, code):
        saved = await self.get_accounts(chat_id)
        rows = []
        # one-tap saved accounts, e.g. "CBE · 1000010201"
        for a in saved:
            rows.append([{
                "text": f"{METHODS[a['m']]} · {a['a']}",
                "callback_data": f"sa:{a['m']}:{a['a']}:{code}",
            }])
        rows.append([
            {"text": "CBE", "callback_data": f"wm:c:{code}"},
            {"text": "Telebirr", "callback_data": f"wm:t:{code}"},
            {"text": "CBE Birr", "callback_data": f"wm:b:{code}"},
        ])
        rows.append([{"text": "Cancel", "callback_data": "cancel"}])

        hint = (
            "Tap a saved account to send instantly, or pick a method below to use a new one."
            if saved else "Choose where you want to receive your money."
        )
        text = f"{head(WIT2)}\n\n{card([('Withdraw code', esc(code))])}\n\n{hint}"
        await self.step(chat_id, text, {"inline_keyboard": rows}, {"step": "wit2", "code": code})

    async def ask_wit_account(self, chat_id, code, mkey, error=False):
        if mkey == "c":
            hint, ph = "13-digit CBE account number", "CBE account number"
        else:
            hint, ph = f"{METHODS[mkey]} phone number (09… or 07…)", "Phone number"
        note = err(f"Invalid {hint}. Try again.") if error else ""
        text = (
            f"{head(WIT3)}\n\n{note}"
            f"{card([('Withdraw code', esc(code)), ('Method', METHODS[mkey])])}\n\n"
            f"Send your <b>{hint}</b>.\n{self.how_to()}"
        )
        await self.step(
            chat_id, text, self.prompt_markup(ph),
            {"step": "wit3", "code": code, "method": mkey},
        )

    async def submit_withdraw(self, user, chat_id, code, mkey, account):
        """Final step, shared by typed accounts and saved-account buttons."""
        s = await self.settings()
        if not s["wit_on"]:
            await self.clear_state(chat_id)
            return await self.ui(chat_id, "Withdrawals are temporarily unavailable. Please try again later.")
        if not self.admins:
            print("ADMIN_ID is not set")
            return await self.ui(chat_id, "Service unavailable. Please try again later.")
        if not await self.rate_ok(user["id"]):
            await self.clear_state(chat_id)
            return await self.ui(
                chat_id, "Too many requests. Please try again later.",
                self.menu_markup(s, self.is_admin(user["id"])),
            )
        tk = hkey("wc:", code) if self.kv is not None else None  # case sensitive
        if tk and await self.kv_exists(tk):
            await self.clear_state(chat_id)
            return await self.ui(
                chat_id, "This code is already waiting for the admin's review.",
                self.menu_markup(s, self.is_admin(user["id"])),
            )

        rid = await self.send_to_admin(
            user, "w", None, account=account, code=code, method=METHODS[mkey], tk=tk
        )
        if tk:
            await self.kv_set(tk, 1, TXN_TTL)
        await self.record_account(chat_id, mkey, account)
        await self.hist_add(user["id"], rid, "w", code)
        await self.rate_hit(user["id"])
        await self.bump("wit_req")
        await self.clear_state(chat_id)
        await self.ui(
            chat_id,
            "<b>Request sent</b>\n\n"
            f"{card([('Withdraw code', esc(code)), (METHODS[mkey], f'<code>{esc(account)}</code>')])}\n\n"
            "The admin will review it. You'll be notified here.",
            self.menu_markup(s, self.is_admin(user["id"])),
        )

    async def on_wit_account(self, msg, code, mkey, text):
        chat_id = msg["chat"]["id"]
        if not code:
            return await self.ask_wit_code(chat_id)
        if mkey not in METHODS:
            return await self.ask_wit_method(chat_id, code)
        account = parse_account(mkey, text)
        if not account:
            return await self.ask_wit_account(chat_id, code, mkey, error=True)
        await self.submit_withdraw(msg["from"], chat_id, code, mkey, account)

    # ---------- Routing ----------
    async def on_message(self, msg):
        chat_id = msg["chat"]["id"]
        uid = msg["from"]["id"]
        text = msg.get("text") or ""
        reply = (msg.get("reply_to_message") or {}).get("text") or ""

        state = await self.get_state(chat_id) or {}
        step = state.get("step")
        self.pm = state.get("pm")

        if text.startswith("/start") or text.startswith("/admin"):
            # start fresh: drop any old prompt and the command itself
            await self.delete(chat_id, self.pm)
            if self.kv is not None:
                await self.delete(chat_id, msg["message_id"])
            self.pm = None
            await self.clear_state(chat_id)
            if text.startswith("/admin") and self.is_admin(uid):
                return await self.send_panel(chat_id)
            return await self.menu(chat_id, uid)

        if text.startswith("/help") or text.startswith("/history"):
            await self.delete(chat_id, self.pm)
            if self.kv is not None:
                await self.delete(chat_id, msg["message_id"])
            self.pm = None
            await self.clear_state(chat_id)
            if text.startswith("/help"):
                out, markup = self.help_view(uid)
            else:
                out, markup = self.hist_view(await self._saved(f"hist:{uid}"))
            return await self.ui(chat_id, out, markup)

        # Admin is typing the reason for a rejection
        if step == "reason" and self.is_admin(uid):
            if self.kv is not None:
                await self.delete(chat_id, msg["message_id"])
            reason = text.strip()[:300]
            if not reason:
                return
            return await self.finish_reject(chat_id, state, reason)

        # Admin is composing a broadcast: any message type is accepted
        if step == "bc" and self.is_admin(uid):
            return await self.on_broadcast_msg(msg)

        # Fallback when storage is off: work out the step from the message being replied to
        if not step and reply:
            if SET_MARK in reply:
                m = re.search(r"ref: (\w+)", reply)
                step, state = "set", {"key": m.group(1) if m else None}
            elif DEP1 in reply:
                step = "dep1"
            elif WIT1 in reply:
                step = "wit1"
            else:
                am = re.search(r"Amount\s+([\d,.]+) ETB", reply)
                xm = re.search(r"1xBet ID\s+(\d+)", reply)
                cm = re.search(r"Withdraw code\s+(\w+)", reply)
                mm = re.search(r"Method\s+(CBE Birr|Telebirr|CBE)", reply)
                if am and DEP2 in reply:
                    step, state = "dep2", {"amount": am.group(1).replace(",", "")}
                elif am and xm and DEP3 in reply:
                    step, state = "dep3", {"amount": am.group(1).replace(",", ""), "xid": xm.group(1)}
                elif cm and mm and WIT3 in reply:
                    step, state = "wit3", {
                        "code": cm.group(1),
                        "method": LABEL_TO_KEY[mm.group(1)],
                    }

        if DEBUG_PROOF and (msg.get("photo") or msg.get("document")):
            await self.dbg(
                f"file received from {uid}: step={step!r}, photo={bool(msg.get('photo'))}, "
                f"document={bool(msg.get('document'))}, state keys={sorted(state.keys()) if state else []}"
            )

        # Keep the chat tidy: remove typed answers (the payment proof stays)
        if self.kv is not None and step in ("set", "dep1", "dep2", "wit1", "wit2", "wit3"):
            await self.delete(chat_id, msg["message_id"])

        if step == "set" and self.is_admin(uid):
            return await self.save_setting(chat_id, state.get("key"), text, msg.get("entities"))
        if step == "dep1":
            return await self.on_amount(chat_id, text)
        if step == "dep2" and state.get("amount"):
            return await self.on_xbet(chat_id, state["amount"], text)
        if step == "dep3" and state.get("amount"):
            return await self.on_proof(msg, state["amount"], state.get("xid"))
        if step == "wit1":
            return await self.on_wit_code(chat_id, text)
        if step == "wit2" and state.get("code"):
            return await self.ask_wit_method(chat_id, state["code"])
        if step == "wit3" and state.get("code"):
            return await self.on_wit_account(msg, state["code"], state.get("method"), text)

        if (msg.get("photo") or msg.get("document")) and not step:
            if msg.get("media_group_id"):  # several photos at once: the first one already got an answer
                return
            s = await self.settings()
            return await self.ui(
                chat_id,
                "<b>Session expired</b>\n\nI couldn't match this file to a request. "
                "Tap <b>Deposit</b>, then send the receipt when asked.",
                self.menu_markup(s, self.is_admin(uid)),
            )

        await self.menu(chat_id, uid)

    async def on_callback(self, q):
        data = q.get("data") or ""
        uid = q["from"]["id"]
        chat_id = q["message"]["chat"]["id"]
        self.pm = q["message"]["message_id"]  # the message the user tapped is the one we edit

        if data in ("deposit", "withdraw"):
            is_dep = data == "deposit"
            s = await self.settings()
            if not s["dep_on" if is_dep else "wit_on"]:
                return await self.answer(q, "Temporarily unavailable. Please try again later.", True)
            if not await self.rate_ok(uid):
                return await self.answer(q, "Too many requests. Please try again later.", True)
            await self.answer(q)
            if is_dep:
                return await self.ask_amount(chat_id, s)
            return await self.ask_wit_code(chat_id)

        # Saved 1xBet ID: one tap moves on to the payment step
        if data.startswith("sx:"):
            s = await self.settings()
            if not s["dep_on"]:
                return await self.answer(q, "Temporarily unavailable. Please try again later.", True)
            try:
                _, xid, amount = data.split(":", 2)
            except ValueError:
                return await self.answer(q)
            amt = parse_amount(amount)
            if not parse_xbet_id(xid) or amt is None:
                return await self.answer(q)
            if amt < s["min_dep"] or amt > s["max_dep"]:
                return await self.answer(q, "Amount is outside the allowed limits.", True)
            await self.answer(q)
            return await self.show_pay_step(chat_id, amt, xid, s)

        # New withdraw account: user picked CBE / Telebirr / CBE Birr
        if data.startswith("wm:"):
            s = await self.settings()
            if not s["wit_on"]:
                return await self.answer(q, "Temporarily unavailable. Please try again later.", True)
            try:
                _, mkey, code = data.split(":", 2)
            except ValueError:
                return await self.answer(q)
            if mkey not in METHODS or not parse_wit_code(code):
                return await self.answer(q)
            await self.answer(q)
            return await self.ask_wit_account(chat_id, code, mkey)

        # Saved withdraw account: one tap sends the request
        if data.startswith("sa:"):
            s = await self.settings()
            if not s["wit_on"]:
                return await self.answer(q, "Temporarily unavailable. Please try again later.", True)
            try:
                _, mkey, account, code = data.split(":", 3)
            except ValueError:
                return await self.answer(q)
            if mkey not in METHODS or not parse_account(mkey, account) or not parse_wit_code(code):
                return await self.answer(q)
            await self.answer(q)
            # remove the buttons first so a double tap can't send it twice
            await self.tg("editMessageReplyMarkup", {
                "chat_id": chat_id,
                "message_id": q["message"]["message_id"],
                "reply_markup": {"inline_keyboard": []},
            })
            return await self.submit_withdraw(q["from"], chat_id, code, mkey, account)

        # Admin confirmed the broadcast
        if data == "bc:go":
            if not self.is_admin(uid):
                return await self.answer(q, "Admins only", True)
            state = await self.get_state(chat_id) or {}
            if state.get("step") != "bc_confirm" or not state.get("mid"):
                return await self.answer(q, "This broadcast has expired.", True)
            await self.clear_state(chat_id)  # clear first so a retried update can't send it twice
            await self.answer(q)
            await self.ui(chat_id, "<b>Broadcast</b>\n\nSending…")
            return await self.run_broadcast(chat_id, state["mid"])

        if data in ("menu", "cancel"):
            await self.answer(q)
            await self.clear_state(chat_id)
            text, markup = self.menu_view(await self.settings(), uid)
            return await self.ui(chat_id, text, markup)

        if data == "hist":
            await self.answer(q)
            out, markup = self.hist_view(await self._saved(f"hist:{uid}"))
            return await self.ui(chat_id, out, markup)

        # Admin skipped the rejection reason
        if data == "rs:skip":
            if not self.is_admin(uid):
                return await self.answer(q, "Admins only", True)
            await self.answer(q)
            return await self.finish_reject(chat_id, await self.get_state(chat_id) or {}, None)

        if data.startswith(("ok:", "no:")):
            if not self.is_admin(uid):
                return await self.answer(q, "Not allowed", True)
            parts = data.split(":")
            if len(parts) < 4 or parts[1] not in ("d", "w"):
                return await self.answer(q)
            action, kind, user_id, ref = parts[:4]
            rid = parts[4] if len(parts) > 4 else None
            approved = action == "ok"
            rec = None
            if rid and self.kv is not None:
                rec = await self.kv_get(f"req:{rid}", None)
                if not isinstance(rec, dict):
                    return await self.answer(q, "This request has expired.", True)
                if rec.get("s") != "p":  # another tap or another admin got there first
                    done = "approved" if rec.get("s") == "a" else "rejected"
                    return await self.answer(q, f"Already {done}.", True)
                rec["s"] = "a" if approved else "r"
                await self.kv_set(f"req:{rid}", rec, REQ_TTL)
            await self.answer(q)

            who = q["from"].get("first_name") or "admin"
            label = f"{'Approved' if approved else 'Rejected'} by {who}"[:60]
            targets = rec["m"] if rec and rec.get("m") else [[chat_id, q["message"]["message_id"]]]
            for c, m in targets:  # every admin's copy shows the result
                await self.tg("editMessageReplyMarkup", {
                    "chat_id": c,
                    "message_id": m,
                    "reply_markup": {"inline_keyboard": [[{"text": label, "callback_data": "noop"}]]},
                })
            await self.bump(f"{'dep' if kind == 'd' else 'wit'}_{'ok' if approved else 'no'}")
            await self.hist_update(user_id, rid, "a" if approved else "r")

            # a rejected receipt may be used again; a withdraw code is free again once handled
            if rec and rec.get("tk") and (not approved or kind == "w"):
                try:
                    await self.kv.delete(rec["tk"])
                except Exception:
                    print("txn delete error", traceback.format_exc())
            if approved:
                return await self.notify_decision(user_id, kind, ref, True, None)
            if self.kv is None:  # no storage = can't wait for a reason
                return await self.notify_decision(user_id, kind, ref, False, None)
            await self.clear_state(chat_id)  # an unfinished earlier reason is sent without text
            self.pm = None  # new message, the request card above stays as it is
            return await self.step(
                chat_id,
                "<b>Reject reason</b>\n\nSend a short reason for the user, or tap Skip.\n\nType it below.",
                {"inline_keyboard": [[{"text": "Skip", "callback_data": "rs:skip"}]]},
                {"step": "reason", "uid": user_id, "kind": kind, "ref": ref},
            )

        if data.startswith("adm:"):
            if not self.is_admin(uid):
                return await self.answer(q, "Admins only", True)
            parts = data.split(":")
            if parts[1] != "set":
                await self.clear_state(chat_id)
            if parts[1] == "bc":
                await self.answer(q)
                return await self.ask_broadcast(chat_id)
            s = await self.settings()
            if parts[1] in ("tdep", "twit"):
                key = "dep_on" if parts[1] == "tdep" else "wit_on"
                s[key] = not s[key]
                if not await self.kv_set("settings", s):
                    return await self.answer(q, "Storage not connected: can't save.", True)
            await self.answer(q)
            if parts[1] == "set" and len(parts) == 3 and parts[2] in SETTING_INFO:
                return await self.ask_setting(chat_id, parts[2])
            if parts[1] == "stats":
                text, markup = await self.stats_view()
                return await self.ui(chat_id, text, markup)
            text, markup = self.panel_view(s)
            return await self.ui(chat_id, text, markup)

        await self.answer(q)


class Default(WorkerEntrypoint):
    async def setup(self, request):
        """GET /setup?key=<WEBHOOK_SECRET> registers the webhook (with the secret) and the command list."""
        url = urlparse(str(request.url))
        if url.path.rstrip("/") != "/setup":
            return Response("OK")
        secret = getattr(self.env, "WEBHOOK_SECRET", None)
        key = (parse_qs(url.query).get("key") or [""])[0]
        if not secret or not hmac.compare_digest(key.encode(), str(secret).encode()):
            return Response("Forbidden", status=403)
        bot = Bot(self.env)
        hook = await bot.tg("setWebhook", {
            "url": f"{url.scheme}://{url.netloc}/",
            "secret_token": str(secret),
            "allowed_updates": ["message", "callback_query"],
        })
        cmds = await bot.tg("setMyCommands", {"commands": [
            {"command": "start", "description": "Main menu"},
            {"command": "history", "description": "My requests"},
            {"command": "help", "description": "How it works"},
        ]})
        return Response(f"setWebhook: {hook}\nsetMyCommands: {cmds}")

    async def report_error(self, update, tb):
        """Never stay silent: tell the user to retry and show the first admin what broke."""
        try:
            bot = Bot(self.env)
            m = (update or {}).get("message") or {}
            q = (update or {}).get("callback_query") or {}
            chat_id = (m.get("chat") or {}).get("id") or ((q.get("message") or {}).get("chat") or {}).get("id")
            who = m.get("from") or q.get("from") or {}
            if q.get("id"):
                await bot.answer(q)
            short = "\n".join(tb.strip().splitlines()[-4:])[-700:]
            if chat_id:
                await bot.send(
                    chat_id,
                    "<b>Something went wrong</b>\n\nPlease send /start and try again. The admin has been told.",
                )
            for aid in bot.admins[:1]:
                await bot.send(aid, f"<b>Bot error</b>\n\nUser <code>{who.get('id', '?')}</code>\n<pre>{esc(short)}</pre>")
        except Exception:
            print("report error failed", traceback.format_exc())

    async def fetch(self, request):
        if request.method == "GET":
            return await self.setup(request)
        if request.method != "POST":
            return Response("OK")
        # only Telegram knows the secret, so fake updates are refused
        secret = getattr(self.env, "WEBHOOK_SECRET", None)
        if secret:
            got = request.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
            if not hmac.compare_digest(str(got).encode(), str(secret).encode()):
                return Response("Forbidden", status=403)
        update = None
        try:
            update = await request.json()
            bot = Bot(self.env)
            if update.get("message"):
                msg = update["message"]
                if msg["chat"].get("type") == "private":
                    await bot.touch(msg["from"]["id"])
                await bot.on_message(msg)
            elif update.get("callback_query"):
                q = update["callback_query"]
                await bot.touch(q["from"]["id"])
                await bot.on_callback(q)
        except Exception:
            tb = traceback.format_exc()
            print("ERROR:", tb)
            await self.report_error(update, tb)
        return Response("OK")
