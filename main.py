# Telegram deposit/withdraw request bot - Cloudflare Python Worker
# Env needed: BOT_TOKEN (secret), ADMIN_ID (variable: your Telegram numeric user ID)
import json
import re
import traceback

import js
from js import fetch as js_fetch
from pyodide.ffi import to_js
from workers import WorkerEntrypoint, Response

DEP_ASK = "Deposit: enter the amount in ETB (reply to this message)."
DEP_PROOF = "Deposit amount: "
WIT_ASK = "Withdraw: enter the amount in ETB (reply to this message)."
WIT_ACCT = "Withdraw amount: "

MENU = {
    "inline_keyboard": [[
        {"text": "Deposit", "callback_data": "deposit"},
        {"text": "Withdraw", "callback_data": "withdraw"},
    ]]
}


def force_reply(placeholder):
    return {"force_reply": True, "input_field_placeholder": placeholder}


def parse_amount(text):
    try:
        n = float(str(text or "").replace(",", "").strip())
    except ValueError:
        return None
    if n > 0 and n != float("inf"):
        return int(n) if n == int(n) else n
    return None


def user_label(u):
    name = " ".join(x for x in [u.get("first_name"), u.get("last_name")] if x)
    return f"{name} (@{u['username']})" if u.get("username") else name


class Bot:
    def __init__(self, env):
        self.env = env
        self.token = env.BOT_TOKEN
        self.admin_id = getattr(env, "ADMIN_ID", None)

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
        text = await resp.text()
        if resp.status != 200:
            print(method, resp.status, text)
        return text

    async def send_menu(self, chat_id):
        await self.tg("sendMessage", {
            "chat_id": chat_id,
            "text": "Welcome! Choose an option:",
            "reply_markup": MENU,
        })

    async def send_to_admin(self, msg, kind, amount, account=None):
        label = "DEPOSIT" if kind == "d" else "WITHDRAW"
        user = msg["from"]
        lines = [
            f"New {label} request",
            f"User: {user_label(user)}",
            f"User ID: {user['id']}",
            f"Amount: {amount} ETB",
        ]
        if account:
            lines.append(f"Account: {account}")

        keyboard = {
            "inline_keyboard": [[
                {"text": "Approve", "callback_data": f"ok:{kind}:{user['id']}:{amount}"},
                {"text": "Reject", "callback_data": f"no:{kind}:{user['id']}:{amount}"},
            ]]
        }

        # For deposits, also send the user's proof (screenshot / text) to the admin
        if kind == "d":
            await self.tg("copyMessage", {
                "chat_id": self.admin_id,
                "from_chat_id": msg["chat"]["id"],
                "message_id": msg["message_id"],
            })
        await self.tg("sendMessage", {
            "chat_id": self.admin_id,
            "text": "\n".join(lines),
            "reply_markup": keyboard,
        })

    async def on_message(self, msg):
        chat_id = msg["chat"]["id"]
        text = msg.get("text") or ""
        reply_to = (msg.get("reply_to_message") or {}).get("text")

        if text.startswith("/start"):
            return await self.send_menu(chat_id)

        if reply_to:
            # Step 1: user typed an amount
            if reply_to.startswith(DEP_ASK) or reply_to.startswith(WIT_ASK):
                is_dep = reply_to.startswith(DEP_ASK)
                amount = parse_amount(text)
                if not amount:
                    return await self.tg("sendMessage", {
                        "chat_id": chat_id,
                        "text": DEP_ASK if is_dep else WIT_ASK,
                        "reply_markup": force_reply("Amount in ETB"),
                    })
                if is_dep:
                    prompt = f"{DEP_PROOF}{amount} ETB\nNow send your payment screenshot or transaction ID (reply to this message)."
                    hint = "Screenshot or transaction ID"
                else:
                    prompt = f"{WIT_ACCT}{amount} ETB\nSend the account / phone number to receive your money (reply to this message)."
                    hint = "Account or phone number"
                return await self.tg("sendMessage", {
                    "chat_id": chat_id,
                    "text": prompt,
                    "reply_markup": force_reply(hint),
                })

            # Step 2: user sent proof (deposit) or account (withdraw)
            if reply_to.startswith(DEP_PROOF) or reply_to.startswith(WIT_ACCT):
                is_dep = reply_to.startswith(DEP_PROOF)
                m = re.search(r"amount: ([\d.]+) ETB", reply_to)
                if not m:
                    return await self.send_menu(chat_id)
                amount = m.group(1)

                if not self.admin_id:
                    print("ADMIN_ID is not set")
                    return await self.tg("sendMessage", {
                        "chat_id": chat_id,
                        "text": "Service unavailable, try again later.",
                    })

                if not is_dep and not text:
                    return await self.tg("sendMessage", {
                        "chat_id": chat_id,
                        "text": reply_to,
                        "reply_markup": force_reply("Account or phone number"),
                    })

                await self.send_to_admin(msg, "d" if is_dep else "w", amount, None if is_dep else text)
                return await self.tg("sendMessage", {
                    "chat_id": chat_id,
                    "text": "Your request was sent. Please wait for confirmation.",
                    "reply_markup": MENU,
                })

        await self.send_menu(chat_id)

    async def on_callback(self, q):
        data = q.get("data") or ""
        await self.tg("answerCallbackQuery", {"callback_query_id": q["id"]})

        if data == "deposit":
            return await self.tg("sendMessage", {
                "chat_id": q["message"]["chat"]["id"],
                "text": DEP_ASK,
                "reply_markup": force_reply("Amount in ETB"),
            })
        if data == "withdraw":
            return await self.tg("sendMessage", {
                "chat_id": q["message"]["chat"]["id"],
                "text": WIT_ASK,
                "reply_markup": force_reply("Amount in ETB"),
            })

        # Admin approve / reject
        if data.startswith("ok:") or data.startswith("no:"):
            if str(q["from"]["id"]) != str(self.admin_id):
                return
            action, kind, user_id, amount = data.split(":")
            approved = action == "ok"
            what = "deposit" if kind == "d" else "withdrawal"

            await self.tg("editMessageText", {
                "chat_id": q["message"]["chat"]["id"],
                "message_id": q["message"]["message_id"],
                "text": f"{q['message'].get('text', '')}\n\n{'APPROVED' if approved else 'REJECTED'}",
            })
            await self.tg("sendMessage", {
                "chat_id": user_id,
                "text": (
                    f"Your {what} of {amount} ETB has been approved."
                    if approved
                    else f"Your {what} of {amount} ETB was rejected. Please contact support."
                ),
            })


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
