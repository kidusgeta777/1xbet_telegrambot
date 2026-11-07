import json
import traceback
import js
from js import fetch as js_fetch
from pyodide.ffi import to_js
from workers import WorkerEntrypoint, Response

KEYBOARD = {
    "inline_keyboard": [[
        {"text": "Deposit", "callback_data": "deposit"},
        {"text": "Withdraw", "callback_data": "withdraw"},
    ]]
}


async def tg(token, method, payload):
    options = to_js(
        {
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload),
        },
        dict_converter=js.Object.fromEntries,
    )
    resp = await js_fetch(f"https://api.telegram.org/bot{token}/{method}", options)
    text = await resp.text()
    print(f"Telegram {method} -> {resp.status} {text}")
    return resp


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        try:
            token = self.env.BOT_TOKEN
            if request.method != "POST":
                return Response("OK")

            update = await request.json()
            print("Update received:", json.dumps(update))

            message = update.get("message")
            if message and "text" in message:
                await tg(token, "sendMessage", {
                    "chat_id": message["chat"]["id"],
                    "text": "Choose an option:",
                    "reply_markup": KEYBOARD,
                })

            query = update.get("callback_query")
            if query:
                await tg(token, "answerCallbackQuery", {"callback_query_id": query["id"]})
                label = "Deposit" if query["data"] == "deposit" else "Withdraw"
                await tg(token, "editMessageText", {
                    "chat_id": query["message"]["chat"]["id"],
                    "message_id": query["message"]["message_id"],
                    "text": f"You selected {label}.",
                })
        except Exception:
            print("ERROR:", traceback.format_exc())

        return Response("OK")
