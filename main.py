import json
from workers import WorkerEntrypoint, Response
from pyodide.http import pyfetch

KEYBOARD = {
    "inline_keyboard": [[
        {"text": "Deposit", "callback_data": "deposit"},
        {"text": "Withdraw", "callback_data": "withdraw"},
    ]]
}


async def tg(token, method, payload):
    return await pyfetch(
        f"https://api.telegram.org/bot{token}/{method}",
        method="POST",
        headers={"Content-Type": "application/json"},
        body=json.dumps(payload),
    )


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        token = self.env.BOT_TOKEN

        # Only accept POSTs sent to /<BOT_TOKEN> (the webhook URL you register)
        if request.method != "POST" or not request.url.endswith("/" + token):
            return Response("OK")

        update = await request.json()

        # /start (or any text message) -> show the two buttons
        message = update.get("message")
        if message and "text" in message:
            await tg(token, "sendMessage", {
                "chat_id": message["chat"]["id"],
                "text": "Choose an option:",
                "reply_markup": KEYBOARD,
            })

        # Button taps
        query = update.get("callback_query")
        if query:
            await tg(token, "answerCallbackQuery", {"callback_query_id": query["id"]})
            label = "Deposit" if query["data"] == "deposit" else "Withdraw"
            await tg(token, "editMessageText", {
                "chat_id": query["message"]["chat"]["id"],
                "message_id": query["message"]["message_id"],
                "text": f"You selected {label}.",
            })

        return Response("OK")
