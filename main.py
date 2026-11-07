# Telegram deposit/withdraw bot with admin panel - Cloudflare Python Worker
# Secrets/vars: BOT_TOKEN, ADMIN_ID. Optional KV binding named SETTINGS (saves admin settings, stats,
# conversation state, and each user's saved 1xBet IDs / withdraw accounts).
import json
import re
import time
import traceback
from html import escape as esc

import js
from js import fetch as js_fetch
from pyodide.ffi import to_js
from workers import WorkerEntrypoint, Response

# Step markers: "<Title> · <step>". Also used to recognise replies when storage is off.
DEP1 = "Deposit · Step 1 of 3"
DEP2 = "Deposit · Step 2 of 3"
DEP3 = "Deposit · Step 3 of 3"
WIT1 = "Withdraw · Step 1 of 4"
WIT2 = "Withdraw · Step 2 of 4"
WIT3 = "Withdraw · Step 3 of 4"
WIT4 = "Withdraw · Step 4 of 4"
SET_MARK = "Admin setting"

MAX_SAVED = 3  # saved 1xBet IDs / withdraw accounts per user

# short key (used in callback_data) -> label
METHODS = {"c": "CBE", "t": "Telebirr", "b": "CBE Birr"}
LABEL_TO_KEY = {v: k for k, v in METHODS.items()}

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
    t = str(text or "").strip().replace(" ", "")
    return t if re.fullmatch(r"[A-Za-z0-9]{3,20}", t) else None


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

    async def kv_set(self, key, value):
        if self.kv is None:
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

    async def clear_state(self, cid):
        if self.kv is None:
            return
        try:
            await self.kv.delete(f"state:{cid}")
        except Exception:
            print("state delete error", traceback.format_exc())

    def prompt_markup(self, placeholder):
        # With storage the bot remembers the step, so no "reply" is needed.
        if self.kv is not None:
            return {"inline_keyboard": [[{"text": "Cancel", "callback_data": "cancel"}]]}
        return force_reply(placeholder)

    def how_to(self):
        return "Type it below." if self.kv is not None else "Reply to this message."

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
        if s.get("support"):
            rows.append([{"text": "Support", "url": f"https://t.me/{s['support']}"}])
        if admin:
            rows.append([{"text": "Admin panel", "callback_data": "adm:home"}])
        return {"inline_keyboard": rows}

    def menu_view(self, s, uid):
        text = f"<b>Welcome</b>\n\n{esc(s['welcome'])}"
        return text, self.menu_markup(s, self.is_admin(uid))

    async def menu(self, chat_id, uid=None):
        s = await self.settings()
        text, markup = self.menu_view(s, uid)
        await self.send(chat_id, text, markup)

    # ---------- Admin panel ----------
    def panel_view(self, s):
        warn = "" if self.kv is not None else err("Storage not connected. Settings can't be saved.")
        support = f"@{esc(s['support'])}" if s["support"] else "not set"
        rows = [
            ("Deposits", "<b>On</b>" if s["dep_on"] else "<b>Off</b>"),
            ("Withdrawals", "<b>On</b>" if s["wit_on"] else "<b>Off</b>"),
            ("Deposit limits", f"{fmt(s['min_dep'])} – {fmt(s['max_dep'])} ETB"),
            ("Withdraw limits", f"{fmt(s['min_wit'])} – {fmt(s['max_wit'])} ETB"),
            ("Support", support),
        ]
        text = f"<b>Admin panel</b>\n\n{warn}{card(rows)}\n\nTap an option to change it."
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
                {"text": "Min withdraw", "callback_data": "adm:set:min_wit"},
                {"text": "Max withdraw", "callback_data": "adm:set:max_wit"},
            ],
            [
                {"text": "Payment info", "callback_data": "adm:set:pay_info"},
                {"text": "Welcome text", "callback_data": "adm:set:welcome"},
            ],
            [
                {"text": "Support", "callback_data": "adm:set:support"},
                {"text": "Stats", "callback_data": "adm:stats"},
            ],
            [{"text": "Main menu", "callback_data": "menu"}],
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
                + card([
                    ("Requests", st[f"{p}_req"]),
                    ("Approved", st[f"{p}_ok"]),
                    ("Rejected", st[f"{p}_no"]),
                    ("Pending", pending),
                ])
            )

        text = f"<b>Stats</b>\n\n{block('Deposits', 'dep')}\n\n{block('Withdrawals', 'wit')}"
        if self.kv is None:
            text += "\n\n" + err("Storage not connected. Stats aren't being recorded.").strip()
        markup = {"inline_keyboard": [[{"text": "Back", "callback_data": "adm:home"}]]}
        return text, markup

    async def bump(self, key):
        if self.kv is None:
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
            await self.send(chat_id, f"<b>Saved</b> · {esc(label)}")
        else:
            await self.send(chat_id, "Couldn't save. Connect a KV namespace named <code>SETTINGS</code> first.")
        await self.send_panel(chat_id)

    async def ask_setting(self, chat_id, key, error=False):
        label, kind = SETTING_INFO[key]
        note = err("That value isn't valid. Try again.") if error else ""
        hint = "a number" if kind == "number" else "your text"
        ref = "" if self.kv is not None else f"\n\n<i>ref: {key}</i>"
        text = (
            f"<b>{SET_MARK}</b>\n\n{note}"
            f"<b>{esc(label)}</b>\nSend {hint}.\n\n"
            f"{self.how_to()}{ref}"
        )
        await self.set_state(chat_id, {"step": "set", "key": key})
        await self.send(chat_id, text, self.prompt_markup(label[:60]))

    # ---------- Admin notification ----------
    async def send_to_admin(self, msg, kind, amount, account=None, xid=None, code=None, method=None):
        user = msg["from"]
        title = "New deposit request" if kind == "d" else "New withdraw request"
        rows = [
            ("User", esc(user_label(user))),
            ("User ID", f"<code>{user['id']}</code>"),
        ]
        if xid:
            rows.append(("1xBet ID", f"<code>{esc(xid)}</code>"))
        if code:
            rows.append(("Withdraw code", f"<code>{esc(code)}</code>"))
        rows.append(("Amount", f"<b>{fmt(amount)} ETB</b>"))
        if method:
            rows.append(("Method", esc(method)))
        if account:
            rows.append(("Account", f"<code>{esc(account)}</code>"))
        markup = {"inline_keyboard": [[
            {"text": "Approve", "callback_data": f"ok:{kind}:{user['id']}:{amount}"},
            {"text": "Reject", "callback_data": f"no:{kind}:{user['id']}:{amount}"},
        ]]}
        if kind == "d":
            await self.tg("copyMessage", {
                "chat_id": self.admin_id,
                "from_chat_id": msg["chat"]["id"],
                "message_id": msg["message_id"],
            })
        await self.send(self.admin_id, f"<b>{title}</b>\n\n{card(rows)}", markup)

    # ---------- Deposit flow: amount -> 1xBet ID (type or tap saved) -> proof ----------
    async def ask_amount(self, chat_id, s, error=False):
        lo, hi = s["min_dep"], s["max_dep"]
        note = err(f"Enter a valid amount between {fmt(lo)} and {fmt(hi)} ETB.") if error else ""
        text = (
            f"{head(DEP1)}\n\n{note}"
            f"Enter the amount in <b>ETB</b>.\n<i>Min {fmt(lo)} · Max {fmt(hi)}</i>\n\n"
            f"{self.how_to()}"
        )
        await self.set_state(chat_id, {"step": "dep1"})
        await self.send(chat_id, text, self.prompt_markup("Amount in ETB"))

    async def on_amount(self, chat_id, text):
        s = await self.settings()
        if not s["dep_on"]:
            await self.clear_state(chat_id)
            return await self.send(chat_id, "Deposits are temporarily unavailable. Please try again later.")
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
        await self.set_state(chat_id, {"step": "dep2", "amount": str(amount)})
        await self.send(chat_id, text, markup)

    async def show_pay_step(self, chat_id, amount, xid, s):
        body = (
            f"{head(DEP3)}\n\n"
            f"{card([('Amount', f'<b>{fmt(amount)} ETB</b>'), ('1xBet ID', f'<code>{xid}</code>')])}\n\n"
            f"<b>Pay to</b>\n<blockquote>{esc(s['pay_info'])}</blockquote>\n\n"
            "After paying, send a <b>screenshot</b> or the <b>transaction ID</b>.\n"
            f"{self.how_to()}"
        )
        await self.set_state(chat_id, {"step": "dep3", "amount": str(amount), "xid": xid})
        await self.send(chat_id, body, self.prompt_markup("Screenshot or transaction ID"))

    async def on_xbet(self, chat_id, amount, text):
        s = await self.settings()
        if not s["dep_on"]:
            await self.clear_state(chat_id)
            return await self.send(chat_id, "Deposits are temporarily unavailable. Please try again later.")
        xid = parse_xbet_id(text)
        if not xid:
            return await self.ask_xbet(chat_id, amount, error=True)
        await self.show_pay_step(chat_id, amount, xid, s)

    async def on_proof(self, msg, amount, xid):
        chat_id = msg["chat"]["id"]
        uid = msg["from"]["id"]
        s = await self.settings()
        if not s["dep_on"]:
            await self.clear_state(chat_id)
            return await self.send(chat_id, "Deposits are temporarily unavailable. Please try again later.")
        if not self.admin_id:
            print("ADMIN_ID is not set")
            return await self.send(chat_id, "Service unavailable. Please try again later.")
        if not xid:
            return await self.ask_xbet(chat_id, amount)  # safety net

        await self.send_to_admin(msg, "d", amount, xid=xid)
        await self.record_xid(chat_id, xid)
        await self.bump("dep_req")
        await self.clear_state(chat_id)
        await self.send(
            chat_id,
            "<b>Request sent</b>\n\n"
            f"{card([('Deposit', f'<b>{fmt(amount)} ETB</b>'), ('1xBet ID', f'<code>{esc(xid)}</code>')])}\n\n"
            "The admin will review it. You'll be notified here.",
            self.menu_markup(s, self.is_admin(uid)),
        )

    # ---------- Withdraw flow: code -> amount -> method / saved account -> account ----------
    async def ask_wit_code(self, chat_id, error=False):
        note = err("Invalid code. Use letters and numbers only (3–20 characters).") if error else ""
        text = (
            f"{head(WIT1)}\n\n{note}"
            f"Send your <b>withdraw code</b>.\n{self.how_to()}"
        )
        await self.set_state(chat_id, {"step": "wit1"})
        await self.send(chat_id, text, self.prompt_markup("Withdraw code"))

    async def on_wit_code(self, chat_id, text):
        s = await self.settings()
        if not s["wit_on"]:
            await self.clear_state(chat_id)
            return await self.send(chat_id, "Withdrawals are temporarily unavailable. Please try again later.")
        code = parse_wit_code(text)
        if not code:
            return await self.ask_wit_code(chat_id, error=True)
        await self.ask_wit_amount(chat_id, code, s)

    async def ask_wit_amount(self, chat_id, code, s, error=False):
        lo, hi = s["min_wit"], s["max_wit"]
        note = err(f"Enter a valid amount between {fmt(lo)} and {fmt(hi)} ETB.") if error else ""
        text = (
            f"{head(WIT2)}\n\n{note}"
            f"{card([('Withdraw code', esc(code))])}\n\n"
            f"Enter the amount in <b>ETB</b>.\n<i>Min {fmt(lo)} · Max {fmt(hi)}</i>\n\n"
            f"{self.how_to()}"
        )
        await self.set_state(chat_id, {"step": "wit2", "code": code})
        await self.send(chat_id, text, self.prompt_markup("Amount in ETB"))

    async def on_wit_amount(self, chat_id, code, text):
        s = await self.settings()
        if not s["wit_on"]:
            await self.clear_state(chat_id)
            return await self.send(chat_id, "Withdrawals are temporarily unavailable. Please try again later.")
        amount = parse_amount(text)
        if amount is None or amount < s["min_wit"] or amount > s["max_wit"]:
            return await self.ask_wit_amount(chat_id, code, s, error=True)
        await self.ask_wit_method(chat_id, amount, code)

    async def ask_wit_method(self, chat_id, amount, code):
        saved = await self.get_accounts(chat_id)
        pre = f"{amount}:{code}"
        rows = []
        # one-tap saved accounts, e.g. "CBE · 1000010201"
        for a in saved:
            rows.append([{
                "text": f"{METHODS[a['m']]} · {a['a']}",
                "callback_data": f"sa:{a['m']}:{a['a']}:{pre}",
            }])
        rows.append([
            {"text": "CBE", "callback_data": f"wm:c:{pre}"},
            {"text": "Telebirr", "callback_data": f"wm:t:{pre}"},
            {"text": "CBE Birr", "callback_data": f"wm:b:{pre}"},
        ])
        rows.append([{"text": "Cancel", "callback_data": "cancel"}])

        hint = (
            "Tap a saved account to send instantly, or pick a method below to use a new one."
            if saved else "Choose where you want to receive your money."
        )
        text = (
            f"{head(WIT3)}\n\n"
            f"{card([('Withdraw code', esc(code)), ('Amount', f'<b>{fmt(amount)} ETB</b>')])}\n\n"
            f"{hint}"
        )
        await self.set_state(chat_id, {"step": "wit3", "amount": str(amount), "code": code})
        await self.send(chat_id, text, {"inline_keyboard": rows})

    async def ask_wit_account(self, chat_id, amount, code, mkey, error=False):
        if mkey == "c":
            hint, ph = "13-digit CBE account number", "CBE account number"
        else:
            hint, ph = f"{METHODS[mkey]} phone number (09… or 07…)", "Phone number"
        note = err(f"Invalid {hint}. Try again.") if error else ""
        text = (
            f"{head(WIT4)}\n\n{note}"
            f"{card([('Withdraw code', esc(code)), ('Amount', f'<b>{fmt(amount)} ETB</b>'), ('Method', METHODS[mkey])])}\n\n"
            f"Send your <b>{hint}</b>.\n{self.how_to()}"
        )
        await self.set_state(
            chat_id, {"step": "wit4", "amount": str(amount), "code": code, "method": mkey}
        )
        await self.send(chat_id, text, self.prompt_markup(ph))

    async def submit_withdraw(self, user, chat_id, amount, code, mkey, account):
        """Final step, shared by typed accounts and saved-account buttons."""
        s = await self.settings()
        if not s["wit_on"]:
            await self.clear_state(chat_id)
            return await self.send(chat_id, "Withdrawals are temporarily unavailable. Please try again later.")
        if not self.admin_id:
            print("ADMIN_ID is not set")
            return await self.send(chat_id, "Service unavailable. Please try again later.")

        fake_msg = {"from": user, "chat": {"id": chat_id}}
        await self.send_to_admin(
            fake_msg, "w", amount, account=account, code=code, method=METHODS[mkey]
        )
        await self.record_account(chat_id, mkey, account)
        await self.bump("wit_req")
        await self.clear_state(chat_id)
        await self.send(
            chat_id,
            "<b>Request sent</b>\n\n"
            f"{card([('Withdrawal', f'<b>{fmt(amount)} ETB</b>'), (METHODS[mkey], f'<code>{esc(account)}</code>')])}\n\n"
            "The admin will review it. You'll be notified here.",
            self.menu_markup(s, self.is_admin(user["id"])),
        )

    async def on_wit_account(self, msg, amount, code, mkey, text):
        chat_id = msg["chat"]["id"]
        if mkey not in METHODS or not code:
            return await self.ask_wit_method(chat_id, amount, code or "")
        account = parse_account(mkey, text)
        if not account:
            return await self.ask_wit_account(chat_id, amount, code, mkey, error=True)
        await self.submit_withdraw(msg["from"], chat_id, amount, code, mkey, account)

    # ---------- Routing ----------
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
                elif cm and WIT2 in reply:
                    step, state = "wit2", {"code": cm.group(1)}
                elif am and cm and mm and WIT4 in reply:
                    step, state = "wit4", {
                        "amount": am.group(1).replace(",", ""),
                        "code": cm.group(1),
                        "method": LABEL_TO_KEY[mm.group(1)],
                    }

        if step == "set" and self.is_admin(uid):
            return await self.save_setting(chat_id, state.get("key"), text)
        if step == "dep1":
            return await self.on_amount(chat_id, text)
        if step == "dep2" and state.get("amount"):
            return await self.on_xbet(chat_id, state["amount"], text)
        if step == "dep3" and state.get("amount"):
            return await self.on_proof(msg, state["amount"], state.get("xid"))
        if step == "wit1":
            return await self.on_wit_code(chat_id, text)
        if step == "wit2" and state.get("code"):
            return await self.on_wit_amount(chat_id, state["code"], text)
        if step == "wit3" and state.get("amount") and state.get("code"):
            return await self.ask_wit_method(chat_id, state["amount"], state["code"])
        if step == "wit4" and state.get("amount") and state.get("code"):
            return await self.on_wit_account(
                msg, state["amount"], state["code"], state.get("method"), text
            )

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
            await self.tg("editMessageReplyMarkup", {
                "chat_id": chat_id,
                "message_id": q["message"]["message_id"],
                "reply_markup": {"inline_keyboard": []},
            })
            return await self.show_pay_step(chat_id, amt, xid, s)

        # New withdraw account: user picked CBE / Telebirr / CBE Birr
        if data.startswith("wm:"):
            s = await self.settings()
            if not s["wit_on"]:
                return await self.answer(q, "Temporarily unavailable. Please try again later.", True)
            try:
                _, mkey, amount, code = data.split(":", 3)
            except ValueError:
                return await self.answer(q)
            if mkey not in METHODS:
                return await self.answer(q)
            await self.answer(q)
            return await self.ask_wit_account(chat_id, amount, code, mkey)

        # Saved withdraw account: one tap sends the request
        if data.startswith("sa:"):
            s = await self.settings()
            if not s["wit_on"]:
                return await self.answer(q, "Temporarily unavailable. Please try again later.", True)
            try:
                _, mkey, account, amount, code = data.split(":", 4)
            except ValueError:
                return await self.answer(q)
            amt = parse_amount(amount)
            if mkey not in METHODS or amt is None or not parse_account(mkey, account) or not parse_wit_code(code):
                return await self.answer(q)
            if amt < s["min_wit"] or amt > s["max_wit"]:
                return await self.answer(q, "Amount is outside the allowed limits.", True)
            await self.answer(q)
            # remove the buttons so it can't be sent twice
            await self.tg("editMessageReplyMarkup", {
                "chat_id": chat_id,
                "message_id": q["message"]["message_id"],
                "reply_markup": {"inline_keyboard": []},
            })
            return await self.submit_withdraw(q["from"], chat_id, amt, code, mkey, account)

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
            what = "Deposit" if kind == "d" else "Withdrawal"
            await self.bump(f"{'dep' if kind == 'd' else 'wit'}_{'ok' if approved else 'no'}")
            await self.tg("editMessageReplyMarkup", {
                "chat_id": chat_id,
                "message_id": q["message"]["message_id"],
                "reply_markup": {"inline_keyboard": [[
                    {"text": "Approved" if approved else "Rejected", "callback_data": "noop"}
                ]]},
            })
            if approved:
                note = "Thank you."
                title = f"{what} approved"
            else:
                note = "Please contact support if you have questions."
                title = f"{what} rejected"
            return await self.send(
                user_id,
                f"<b>{title}</b>\n\n{card([('Amount', f'<b>{fmt(amount)} ETB</b>')])}\n\n{note}",
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
