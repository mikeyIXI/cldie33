"""Lightweight, resilient Telegram Bot API wrapper (pure Python 3 stdlib)."""
import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, Optional


class TelegramAPIError(Exception):
    def __init__(self, code: int, description: str, parameters: Optional[Dict[str, Any]] = None):
        super().__init__(f"Telegram API {code}: {description}")
        self.code = code
        self.description = description
        self.parameters = parameters or {}


class BotAPI:
    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self._me: Optional[Dict[str, Any]] = None

    def call(self, method: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 25, retries: int = 2) -> Any:
        url = f"{self.base_url}/{method}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}

        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    if not body.get("ok"):
                        raise TelegramAPIError(
                            body.get("error_code", 400),
                            body.get("description", "Unknown error"),
                            body.get("parameters"),
                        )
                    return body.get("result")
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", errors="replace")
                try:
                    body = json.loads(raw)
                    code = body.get("error_code", e.code)
                    desc = body.get("description", raw)
                    params = body.get("parameters") or {}
                except Exception:
                    code, desc, params = e.code, raw, {}

                if code == 429 and attempt < retries:
                    wait_s = int(params.get("retry_after", 3)) + 1
                    time.sleep(min(wait_s, 30))
                    continue
                raise TelegramAPIError(code, desc, params)
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < retries:
                    time.sleep(1.5)
                    continue
                raise TelegramAPIError(500, str(e))

    @property
    def me(self) -> Dict[str, Any]:
        if self._me is None:
            self._me = self.call("getMe")
        return self._me

    @property
    def username(self) -> str:
        return self.me.get("username", "")

    @property
    def id(self) -> int:
        return int(self.me["id"])

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: str = "HTML",
        disable_preview: bool = True,
    ) -> Optional[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload)

    def edit_message(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: str = "HTML",
        disable_preview: bool = True,
    ) -> Optional[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            return self.call("editMessageText", payload)
        except TelegramAPIError as e:
            if "message is not modified" in e.description.lower():
                return None
            if "message can't be edited" in e.description.lower() or "there is no text in the message" in e.description.lower():
                return self.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode, disable_preview=disable_preview)
            raise

    def answer_callback(self, callback_id: str, text: Optional[str] = None, show_alert: bool = False) -> None:
        payload: Dict[str, Any] = {"callback_query_id": callback_id, "show_alert": show_alert}
        if text:
            payload["text"] = text[:195]
        try:
            self.call("answerCallbackQuery", payload, timeout=10, retries=0)
        except Exception:
            pass

    def copy_message(
        self,
        chat_id: int | str,
        from_chat_id: int | str,
        message_id: int,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Any:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "from_chat_id": from_chat_id,
            "message_id": message_id,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("copyMessage", payload)

    def forward_message(self, chat_id: int | str, from_chat_id: int | str, message_id: int) -> Any:
        return self.call("forwardMessage", {
            "chat_id": chat_id,
            "from_chat_id": from_chat_id,
            "message_id": message_id,
        })

    def send_document_bytes(
        self,
        chat_id: int | str,
        filename: str,
        content: bytes,
        caption: str = "",
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Any:
        boundary = f"----CloudForgeBoundary{uuid.uuid4().hex}"
        parts = []

        def add_field(name: str, value: str) -> None:
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8"))

        add_field("chat_id", str(chat_id))
        if caption:
            add_field("caption", caption)
            add_field("parse_mode", "HTML")
        if reply_markup:
            add_field("reply_markup", json.dumps(reply_markup))

        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
        )
        parts.append(content)
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)

        req = urllib.request.Request(
            f"{self.base_url}/sendDocument",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8")).get("result")
