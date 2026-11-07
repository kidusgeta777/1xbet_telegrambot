# Telegram deposit/withdraw bot with admin panel - Cloudflare Python Worker
# Secrets/vars: BOT_TOKEN, ADMIN_ID. Optional KV binding named SETTINGS (saves admin settings + stats).
import json
import re
import traceback
from html import escape as esc

import js
from js import fetch as js_fetch
from pyodide.ffi import to_js
from workers import WorkerEntrypoint, Response

LINE = "━━━━━━━━━━━━━━━━"

DEP1 = "DEPOSIT · Step 1 of 2"
DEP2 = "DEPOSIT · Step 2 of 2"
WIT1 = "WITHDRAW · Step 1 of 2"
WIT2 = "WITHDRAW · Step 2 of 2"
SET_MARK = "ADMIN SETTING"

DEFAULTS = {
    "dep_on": True,
    "wit_on": True,
    "min_dep": 50,
    "max_dep": 100000,
    "min_wit": 100,
    "max_wit": 100000,
    "pay_info": "Ask the admin for payment details.",
    "welcome": "Request a deposit or withdrawal in just a few taps.",
    "support": "",
}
STATS0 = {"dep_req": 0, "dep_ok": 0, "dep_no": 0, "wit_req": 0, "wit_ok": 0, "wit_no": 0}

# key -> (label, type)
SETTING_INFO = {
    "min_dep": ("Minimum deposit (ETB)", "number"),
    "max_dep": ("Maximum deposit (ETB)", "number"),
    "min_wit": ("Minimum withdrawal (ETB)", "number"),
    "max_wit": ("Maximum withdrawal (ETB)", "number"),
    "pay_info": ("Payment details shown to depositors", "text"),
    "welcome": ("Welcome message", "text"),
    "support": ("Support username, without @ (send - to clear)", "text"),
}


def fmt(n):
    n = float(n)
    return f"{int(n):,}" if n == int(n) else f"{n:,.2f}"


def parse_amount(text):
    try:
        n = float(str(text or "").replace(",", "").strip())
    except ValueError:
        return None
    if n > 0 and n < 1e12:
        n = round(n, 2)
        return int(n) if n == int(n) else n
    return None


def user_label(u):
    name = " ".join(x for x in [u.get("first_name"), u.get("last_name")] if x)
    return f"{name} (@{u['username']})" if u.get("username") else name


def force_reply(placeholder):
    return {"force_reply": True, "input_field_placeholder": placeholder}


class Bot:
    def __init__(self, env):
        self.token = env.BOT_TOKEN
        self.admin_id = getattr(env, "ADMIN_ID", None)
        self.kv = getattr(env, "SETTINGS", None)

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

    async def send(self, chat_id, text, markup=None):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if markup:
            payload["reply_markup"] = markup
        return await self.tg("sendMessage", payload)

    async def edit(self, q, text, markup=None):
        payload = {
            "chat_id": q["message"]["chat"]["id"],
            "message_id": q["message"]["message_id"],
            "text": text,
            "parse_mode": "HTML",
        }
        if markup:
            payload["reply_markup"] = markup
        return await self.tg("editMessageText", payload)

    async def answer(self, q, text=None, alert=False):
        payload = {"callback_query_id": q["id"]}
        if text:
            payload["text"] = text
            payload["show_alert"] = alert
        return await self.tg("answerCallbackQuery", payload)

    async def kv_get(self, key, default):
        if not self.kv:
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

    async def kv_set(self, key, value):
        if not self.kv:
            return False
        try:
            await self.kv.put(key, json.dumps(value))
            return True
        except Exception:
            print("KV write error", traceback.format_exc())
            return False

    async def settings(self):
        return await self.kv_get("settings", dict(DEFAULTS))

    def is_admin(self, uid):
        return bool(self.admin_id) and str(uid) == str(self.admin_id)

    # ---------- Conversation state (so users can just type) ----------
    async def get_state(self, cid):
        if not self.kv:
            return None
        try:
            raw = await self.kv.get(f"state:{cid}")
            if isinstance(raw, str):
                return json.loads(raw)
        except Exception:
            print("state read error", traceback.format_exc())
        return None

    async def set_state(self, cid, st):
        if not self.kv:
            return
        try:
            opts = to_js({"expirationTtl": 3600}, dict_converter=js.Object.fromEntries)
            await self.kv.put(f"state:{cid}", json.dumps(st), opts)
        except Exception:
            print("state write error", traceback.format_exc())

    async def clear_state(self, cid):
        if not self.kv:
            return
        try:
            await self.kv.delete(f"state:{cid}")
        except Exception:
            print("state delete error", traceback.format_exc())

    def prompt_markup(self, placeholder):
        # With storage the bot remembers the step, so no "reply" is needed.
        if self.kv:
            return {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "cancel"}]]}
        return force_reply(placeholder)

    def how_to(self):
        return "Just type it below 👇" if self.kv else "↩️ Reply to this message."

    # ---------- User menu ----------
    def menu_markup(self, s, admin=False):
        rows = [[
            {"text": "💰 Deposit", "callback_data": "deposit"},
            {"text": "💸 Withdraw", "callback_data": "withdraw"},
        ]]
        if s.get("support"):
            rows.append([{"text": "📞 Support", "url": f"https://t.me/{s['support']}"}])
        if admin:
            rows.append([{"text": "⚙️ Admin Panel", "callback_data": "adm:home"}])
        return {"inline_keyboard": rows}

    def menu_view(self, s, uid):
        text = (
            f"<b>👋 Welcome!</b>\n{LINE}\n{esc(s['welcome'])}\n\n"
            "<i>Choose an option below 👇</i>"
        )
        return text, self.menu_markup(s, self.is_admin(uid))

    async def menu(self, chat_id, uid=None):
        s = await self.settings()
        text, markup = self.menu_view(s, uid)
        await self.send(chat_id, text, markup)

    async def ask_amount(self, chat_id, is_dep, s, error=False):
        lo, hi = (s["min_dep"], s["max_dep"]) if is_dep else (s["min_wit"], s["max_wit"])
        title = f"💰 {DEP1}" if is_dep else f"💸 {WIT1}"
        note = f"⚠️ Enter a valid amount between {fmt(lo)} and {fmt(hi)} ETB.\n\n" if error else ""
        text = (
            f"<b>{title}</b>\n{LINE}\n{note}"
            f"Enter the amount in <b>ETB</b>.\n<i>Min {fmt(lo)} · Max {fmt(hi)}</i>\n\n"
            f"{self.how_to()}"
        )
        await self.set_state(chat_id, {"step": "dep1" if is_dep else "wit1"})
        await self.send(chat_id, text, self.prompt_markup("Amount in ETB"))

    # ---------- Admin panel ----------
    def panel_view(self, s):
        warn = "" if self.kv else "⚠️ <b>Storage not connected</b> — settings can't be saved.\n\n"
        support = f"@{esc(s['support'])}" if s["support"] else "not set"
        text = (
            f"<b>⚙️ ADMIN PANEL</b>\n{LINE}\n{warn}"
            f"💰 Deposits: <b>{'ON' if s['dep_on'] else 'OFF'}</b>\n"
            f"💸 Withdrawals: <b>{'ON' if s['wit_on'] else 'OFF'}</b>\n\n"
            f"📏 Deposit limits: {fmt(s['min_dep'])} – {fmt(s['max_dep'])} ETB\n"
            f"📏 Withdraw limits: {fmt(s['min_wit'])} – {fmt(s['max_wit'])} ETB\n"
            f"📞 Support: {support}\n\n"
            "<i>Tap an option to change it.</i>"
        )
        markup = {"inline_keyboard": [
            [
                {"text": ("🟢 Deposits: ON" if s["dep_on"] else "🔴 Deposits: OFF"), "callback_data": "adm:tdep"},
                {"text": ("🟢 Withdrawals: ON" if s["wit_on"] else "🔴 Withdrawals: OFF"), "callback_data": "adm:twit"},
            ],
            [
                {"text": "Min deposit", "callback_data": "adm:set:min_dep"},
                {"text": "Max deposit", "callback_data": "adm:set:max_dep"},
            ],
            [
                {"text": "Min withdraw", "callback_data": "adm:set:min_wit"},
                {"text": "Max withdraw", "callback_data": "adm:set:max_wit"},
            ],
            [
                {"text": "🏦 Payment info", "callback_data": "adm:set:pay_info"},
                {"text": "👋 Welcome text", "callback_data": "adm:set:welcome"},
            ],
            [
                {"text": "📞 Support", "callback_data": "adm:set:support"},
                {"text": "📊 Stats", "callback_data": "adm:stats"},
            ],
            [{"text": "🏠 Main menu", "callback_data": "menu"}],
        ]}
        return text, markup

    async def send_panel(self, chat_id):
        text, markup = self.panel_view(await self.settings())
        await self.send(chat_id, text, markup)

    async def stats_view(self):
        st = await self.kv_get("stats", dict(STATS0))
        def block(title, p):
            pending = max(st[f"{p}_req"] - st[f"{p}_ok"] - st[f"{p}_no"], 0)
            return (
                f"<b>{title}</b>\n"
                f"Requests: {st[f'{p}_req']}  ·  ✅ {st[f'{p}_ok']}  ·  ❌ {st[f'{p}_no']}  ·  ⏳ {pending}"
            )
        text = f"<b>📊 STATS</b>\n{LINE}\n{block('💰 Deposits', 'dep')}\n\n{block('💸 Withdrawals', 'wit')}"
        if not self.kv:
            text += "\n\n⚠️ <i>Storage not connected — stats aren't being recorded.</i>"
        markup = {"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": "adm:home"}]]}
        return text, markup

    async def bump(self, key):
        if not self.kv:
            return
        st = await self.kv_get("stats", dict(STATS0))
        st[key] = st.get(key, 0) + 1
        await self.kv_set("stats", st)

    async def save_setting(self, chat_id, key, text):
        if key not in SETTING_INFO:
            return await self.send_panel(chat_id)
        label, kind = SETTING_INFO[key]
        s = await self.settings()

        if kind == "number":
            value = parse_amount(text)
            if value is None:
                return await self.ask_setting(chat_id, key, error=True)
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
            await self.send(chat_id, f"✅ <b>Saved:</b> {esc(label)}")
        else:
            await self.send(chat_id, "⚠️ Couldn't save. Connect a KV namespace named <code>SETTINGS</code> first.")
        await self.send_panel(chat_id)

    async def ask_setting(self, chat_id, key, error=False):
        label, kind = SETTING_INFO[key]
        note = "⚠️ That value isn't valid. Try again.\n\n" if error else ""
        hint = "a number" if kind == "number" else "your text"
        text = (
            f"<b>⚙️ {SET_MARK}</b>\n{LINE}\n{note}"
            f"<b>{esc(label)}</b>\nSend {hint}.\n\n"
            f"{self.how_to()}\n<i>(ref: {key})</i>"
        )
        await self.set_state(chat_id, {"step": "set", "key": key})
        await self.send(chat_id, text, self.prompt_markup(label[:60]))

    # ---------- Requests ----------
    async def send_to_admin(self, msg, kind, amount, account=None):
        user = msg["from"]
        title = "📥 NEW DEPOSIT REQUEST" if kind == "d" else "📤 NEW WITHDRAW REQUEST"
        lines = [
            f"<b>{title}</b>",
            LINE,
            f"👤 <b>User:</b> {esc(user_label(user))}",
            f"🆔 <b>ID:</b> <code>{user['id']}</code>",
            f"💵 <b>Amount:</b> {fmt(amount)} ETB",
        ]
        if account:
            lines.append(f"🏦 <b>Account:</b> <code>{esc(account)}</code>")
        markup = {"inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"ok:{kind}:{user['id']}:{amount}"},
            {"text": "❌ Reject", "callback_data": f"no:{kind}:{user['id']}:{amount}"},
        ]]}
        if kind == "d":
            await self.tg("copyMessage", {
                "chat_id": self.admin_id,
                "from_chat_id": msg["chat"]["id"],
                "message_id": msg["message_id"],
            })
        await self.send(self.admin_id, "\n".join(lines), markup)

    async def on_amount(self, chat_id, is_dep, text):
        s = await self.settings()
        if not s["dep_on" if is_dep else "wit_on"]:
            await self.clear_state(chat_id)
            return await self.send(chat_id, "⛔ Temporarily unavailable. Please try again later.")
        lo, hi = (s["min_dep"], s["max_dep"]) if is_dep else (s["min_wit"], s["max_wit"])
        amount = parse_amount(text)
        if amount is None or amount < lo or amount > hi:
            return await self.ask_amount(chat_id, is_dep, s, error=True)
        if is_dep:
            body = (
                f"<b>💰 {DEP2}</b>\n{LINE}\n"
                f"💵 <b>Amount:</b> {fmt(amount)} ETB\n\n"
                f"🏦 <b>Pay to:</b>\n{esc(s['pay_info'])}\n\n"
                "After paying, send your <b>screenshot</b> or <b>transaction ID</b>.\n"
                f"{self.how_to()}"
            )
            hint = "Screenshot or transaction ID"
        else:
            body = (
                f"<b>💸 {WIT2}</b>\n{LINE}\n"
                f"💵 <b>Amount:</b> {fmt(amount)} ETB\n\n"
                "Send the <b>account / phone number</b> where you want to receive your money.\n"
                f"{self.how_to()}"
            )
            hint = "Account or phone number"
        await self.set_state(chat_id, {"step": "dep2" if is_dep else "wit2", "amount": str(amount)})
        await self.send(chat_id, body, self.prompt_markup(hint))

    async def on_proof(self, msg, is_dep, amount, text):
        chat_id = msg["chat"]["id"]
        uid = msg["from"]["id"]
        s = await self.settings()
        if not s["dep_on" if is_dep else "wit_on"]:
            await self.clear_state(chat_id)
            return await self.send(chat_id, "⛔ Temporarily unavailable. Please try again later.")
        if not self.admin_id:
            print("ADMIN_ID is not set")
            return await self.send(chat_id, "⚠️ Service unavailable. Please try again later.")
        if not is_dep and not text.strip():
            return await self.send(
                chat_id,
                "⚠️ Please send text: your account or phone number.",
                self.prompt_markup("Account or phone number"),
            )

        await self.send_to_admin(msg, "d" if is_dep else "w", amount, None if is_dep else text.strip())
        await self.bump("dep_req" if is_dep else "wit_req")
        await self.clear_state(chat_id)
        what = "deposit" if is_dep else "withdrawal"
        await self.send(
            chat_id,
            f"<b>📨 REQUEST SENT</b>\n{LINE}\n"
            f"Your {what} request for <b>{fmt(amount)} ETB</b> is with the admin.\n"
            "You'll be notified here once it's reviewed ⏳",
            self.menu_markup(s, self.is_admin(uid)),
        )

    async def on_message(self, msg):
        chat_id = msg["chat"]["id"]
        uid = msg["from"]["id"]
        text = msg.get("text") or ""
        reply = (msg.get("reply_to_message") or {}).get("text") or ""

        if text.startswith("/start"):
            await self.clear_state(chat_id)
            return await self.menu(chat_id, uid)
        if text.startswith("/admin"):
            await self.clear_state(chat_id)
            if self.is_admin(uid):
                return await self.send_panel(chat_id)
            return await self.menu(chat_id, uid)

        state = await self.get_state(chat_id) or {}
        step = state.get("step")

        # Fallback when storage is off: work out the step from the message being replied to
        if not step and reply:
            if SET_MARK in reply:
                m = re.search(r"\(ref: (\w+)\)", reply)
                step, state = "set", {"key": m.group(1) if m else None}
            elif DEP1 in reply:
                step = "dep1"
            elif WIT1 in reply:
                step = "wit1"
            elif DEP2 in reply or WIT2 in reply:
                m = re.search(r"Amount: ([\d,.]+) ETB", reply)
                if m:
                    step = "dep2" if DEP2 in reply else "wit2"
                    state = {"amount": m.group(1).replace(",", "")}

        if step == "set" and self.is_admin(uid):
            return await self.save_setting(chat_id, state.get("key"), text)
        if step in ("dep1", "wit1"):
            return await self.on_amount(chat_id, step == "dep1", text)
        if step in ("dep2", "wit2") and state.get("amount"):
            return await self.on_proof(msg, step == "dep2", state["amount"], text)

        await self.menu(chat_id, uid)

    async def on_callback(self, q):
        data = q.get("data") or ""
        uid = q["from"]["id"]
        chat_id = q["message"]["chat"]["id"]

        if data in ("deposit", "withdraw"):
            is_dep = data == "deposit"
            s = await self.settings()
            if not s["dep_on" if is_dep else "wit_on"]:
                return await self.answer(q, "Temporarily unavailable. Please try again later.", True)
            await self.answer(q)
            return await self.ask_amount(chat_id, is_dep, s)

        if data in ("menu", "cancel"):
            await self.answer(q)
            await self.clear_state(chat_id)
            text, markup = self.menu_view(await self.settings(), uid)
            return await self.edit(q, text, markup)

        if data.startswith(("ok:", "no:")):
            if not self.is_admin(uid):
                return await self.answer(q, "Not allowed", True)
            await self.answer(q)
            action, kind, user_id, amount = data.split(":")
            approved = action == "ok"
            what = "DEPOSIT" if kind == "d" else "WITHDRAWAL"
            await self.bump(f"{'dep' if kind == 'd' else 'wit'}_{'ok' if approved else 'no'}")
            await self.tg("editMessageReplyMarkup", {
                "chat_id": chat_id,
                "message_id": q["message"]["message_id"],
                "reply_markup": {"inline_keyboard": [[
                    {"text": "✅ Approved" if approved else "❌ Rejected", "callback_data": "noop"}
                ]]},
            })
            if approved:
                note = "Thank you! 🎉"
                head = f"✅ {what} APPROVED"
            else:
                note = "Please contact support if you have questions."
                head = f"❌ {what} REJECTED"
            return await self.send(
                user_id,
                f"<b>{head}</b>\n{LINE}\n💵 <b>Amount:</b> {fmt(amount)} ETB\n\n{note}",
            )

        if data.startswith("adm:"):
            if not self.is_admin(uid):
                return await self.answer(q, "Admins only", True)
            parts = data.split(":")
            if parts[1] != "set":
                await self.clear_state(chat_id)
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
                return await self.edit(q, text, markup)
            text, markup = self.panel_view(s)
            return await self.edit(q, text, markup)

        await self.answer(q)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if request.method != "POST":
            return Response("OK")
        try:
            update = await request.json()
            bot = Bot(self.env)
            if update.get("message"):
                await bot.on_message(update["message"])
            elif update.get("callback_query"):
                await bot.on_callback(update["callback_query"])
        except Exception:
            print("ERROR:", traceback.format_exc())
        return Response("OK")
