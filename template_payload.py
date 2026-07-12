import base64
import html as html_lib
import io
import json
import re
from collections import deque
from html.parser import HTMLParser
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity
from aiogram.utils.text_decorations import html_decoration
from telethon import Button
from telethon.helpers import add_surrogate, del_surrogate, strip_text
from telethon.tl import types


PAYLOAD_VERSION = 1
MAX_POST_DELAY_SECONDS = 24 * 60 * 60

SUPPORTED_HTML_RE = re.compile(
    r"</?(?:b|strong|i|em|u|s|strike|del|tg-spoiler|span|a|code|pre|blockquote|tg-emoji)\b",
    re.IGNORECASE,
)


class TelegramHTMLParser(HTMLParser):
    """Telethon-compatible Telegram HTML parser with premium emoji support."""

    def __init__(self):
        super().__init__()
        self.text = ""
        self.entities = []
        self._building_entities = {}
        self._open_tags = deque()
        self._open_tags_meta = deque()

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        self._open_tags.appendleft(tag)
        self._open_tags_meta.appendleft(None)

        attrs = dict(attrs)
        entity_type = None
        args = {}

        if tag in {"strong", "b"}:
            entity_type = types.MessageEntityBold
        elif tag in {"em", "i"}:
            entity_type = types.MessageEntityItalic
        elif tag == "u":
            entity_type = types.MessageEntityUnderline
        elif tag in {"del", "s", "strike"}:
            entity_type = types.MessageEntityStrike
        elif tag == "tg-spoiler" or (
            tag == "span" and attrs.get("class") == "tg-spoiler"
        ):
            entity_type = types.MessageEntitySpoiler
        elif tag == "blockquote":
            entity_type = types.MessageEntityBlockquote
            if "expandable" in attrs:
                args["collapsed"] = True
        elif tag == "tg-emoji":
            emoji_id = attrs.get("emoji-id") or attrs.get("emoji_id")
            if emoji_id and str(emoji_id).isdigit():
                entity_type = types.MessageEntityCustomEmoji
                args["document_id"] = int(emoji_id)
        elif tag == "code":
            pre = self._building_entities.get("pre")
            if pre:
                class_name = attrs.get("class", "")
                if class_name.startswith("language-"):
                    pre.language = class_name[len("language-") :]
            else:
                entity_type = types.MessageEntityCode
        elif tag == "pre":
            entity_type = types.MessageEntityPre
            args["language"] = ""
        elif tag == "a":
            url = attrs.get("href")
            if not url:
                return
            if url.startswith("mailto:"):
                entity_type = types.MessageEntityEmail
            elif self.get_starttag_text() == url:
                entity_type = types.MessageEntityUrl
            else:
                entity_type = types.MessageEntityTextUrl
                args["url"] = del_surrogate(url)

        key = tag
        if tag == "span" and attrs.get("class") == "tg-spoiler":
            key = "tg-spoiler"
        if entity_type and key not in self._building_entities:
            self._building_entities[key] = entity_type(
                offset=len(self.text),
                length=0,
                **args,
            )

    def handle_data(self, text):
        text = add_surrogate(text)
        for entity in self._building_entities.values():
            entity.length += len(text)
        self.text += text

    def handle_entityref(self, name):
        self.handle_data(html_lib.unescape(f"&{name};"))

    def handle_charref(self, name):
        self.handle_data(html_lib.unescape(f"&#{name};"))

    def handle_endtag(self, tag):
        tag = tag.lower()
        try:
            self._open_tags.popleft()
            self._open_tags_meta.popleft()
        except IndexError:
            pass

        key = "tg-spoiler" if tag == "span" else tag
        entity = self._building_entities.pop(key, None)
        if entity:
            self.entities.append(entity)


class TelegramHTML:
    @staticmethod
    def parse(html: str):
        if not html:
            return html, []
        parser = TelegramHTMLParser()
        parser.feed(html)
        text = strip_text(parser.text, parser.entities)
        parser.entities.reverse()
        parser.entities.sort(key=lambda entity: entity.offset)
        return del_surrogate(text), parser.entities

    @staticmethod
    def unparse(text: str, entities: list[Any]):
        return html_decoration.unparse(text, entities)


def payload_to_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def payload_from_json(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return None
    return payload


def legacy_payload(text: str | None) -> dict:
    html = html_lib.escape(text or "", quote=False)
    return {
        "version": PAYLOAD_VERSION,
        "post_delay_seconds": 0,
        "messages": [
            {
                "text": text or "",
                "html": html,
                "entities": [],
                "media": [],
                "buttons": [],
            }
        ],
    }


def template_payload(template: dict) -> dict:
    return payload_from_json(template.get("payload")) or legacy_payload(template.get("text"))


def normalize_post_delay_seconds(value: Any) -> float:
    try:
        seconds = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if seconds < 0:
        return 0.0
    return min(seconds, float(MAX_POST_DELAY_SECONDS))


def post_delay_seconds(payload: dict) -> float:
    return normalize_post_delay_seconds(payload.get("post_delay_seconds"))


def format_seconds(seconds: float) -> str:
    seconds = normalize_post_delay_seconds(seconds)
    if seconds.is_integer():
        return f"{int(seconds)} сек."
    return f"{seconds:.3f}".rstrip("0").rstrip(".") + " сек."


def _serialize_entities(entities: list[MessageEntity] | None) -> list[dict]:
    return [entity.model_dump(mode="json", exclude_none=True) for entity in (entities or [])]


def _entities_to_html(text: str, entities: list[MessageEntity] | None) -> str:
    if entities:
        return html_decoration.unparse(text, entities)
    if SUPPORTED_HTML_RE.search(text or ""):
        return text or ""
    return html_lib.escape(text or "", quote=False)


def _text_from_message(message: Message) -> tuple[str, list[MessageEntity]]:
    if message.text is not None:
        return message.text, list(message.entities or [])
    if message.caption is not None:
        return message.caption, list(message.caption_entities or [])
    return "", []


def _serialize_buttons(reply_markup: InlineKeyboardMarkup | None) -> list[list[dict]]:
    if not reply_markup:
        return []

    rows = []
    for row in reply_markup.inline_keyboard:
        out_row = []
        for button in row:
            item = {"text": button.text}
            if button.url:
                item["url"] = button.url
            elif button.callback_data:
                item["callback_data"] = button.callback_data
            else:
                dumped = button.model_dump(mode="json", exclude_none=True)
                for key in (
                    "login_url",
                    "web_app",
                    "switch_inline_query",
                    "switch_inline_query_current_chat",
                    "switch_inline_query_chosen_chat",
                    "pay",
                ):
                    if key in dumped:
                        item[key] = dumped[key]
            out_row.append(item)
        if out_row:
            rows.append(out_row)
    return rows


def bot_reply_markup(post: dict) -> InlineKeyboardMarkup | None:
    rows = []
    for row in post.get("buttons") or []:
        out_row = []
        for button in row:
            kwargs = {"text": button.get("text") or "Кнопка"}
            if button.get("url"):
                kwargs["url"] = button["url"]
            elif button.get("callback_data"):
                kwargs["callback_data"] = "template_preview_noop"
            elif button.get("login_url"):
                kwargs["login_url"] = button["login_url"]
            elif button.get("web_app"):
                kwargs["web_app"] = button["web_app"]
            elif button.get("switch_inline_query") is not None:
                kwargs["switch_inline_query"] = button.get("switch_inline_query")
            elif button.get("switch_inline_query_current_chat") is not None:
                kwargs["switch_inline_query_current_chat"] = button.get(
                    "switch_inline_query_current_chat"
                )
            elif button.get("pay"):
                kwargs["pay"] = True
            else:
                kwargs["callback_data"] = "template_preview_noop"
            out_row.append(InlineKeyboardButton(**kwargs))
        if out_row:
            rows.append(out_row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _photo_media(message: Message, bot) -> list[dict]:
    if not message.photo:
        return []

    photo = message.photo[-1]
    stream = io.BytesIO()
    await bot.download(photo, destination=stream)
    return [
        {
            "type": "photo",
            "filename": f"photo_{message.message_id}.jpg",
            "file_id": photo.file_id,
            "message_id": message.message_id,
            "data": base64.b64encode(stream.getvalue()).decode("ascii"),
        }
    ]


def _has_unsupported_media(message: Message) -> bool:
    return any(
        getattr(message, name, None)
        for name in (
            "animation",
            "audio",
            "document",
            "sticker",
            "video",
            "video_note",
            "voice",
        )
    )


async def post_from_message(message: Message, bot) -> dict:
    text, entities = _text_from_message(message)
    media = await _photo_media(message, bot)

    if not media and _has_unsupported_media(message):
        raise ValueError("Пока поддерживаются текст, фото и альбомы из фото.")
    if not text and not media and not message.reply_markup:
        raise ValueError("В этом сообщении нет текста, фото или inline-кнопок.")

    html = _entities_to_html(text, entities)
    return {
        "message_id": message.message_id,
        "media_group_id": message.media_group_id,
        "text": text,
        "html": html,
        "entities": _serialize_entities(entities),
        "media": media,
        "buttons": _serialize_buttons(message.reply_markup),
    }


def upsert_post(posts: list[dict], post: dict) -> tuple[list[dict], bool]:
    media_group_id = post.get("media_group_id")
    if media_group_id:
        for existing in posts:
            if existing.get("media_group_id") != media_group_id:
                continue
            existing.setdefault("media", []).extend(post.get("media") or [])
            existing["media"].sort(key=lambda item: item.get("message_id") or 0)
            if post.get("text") and not existing.get("text"):
                existing["text"] = post["text"]
                existing["html"] = post.get("html") or ""
                existing["entities"] = post.get("entities") or []
            if post.get("buttons") and not existing.get("buttons"):
                existing["buttons"] = post["buttons"]
            return posts, False

    posts.append(post)
    return posts, True


def add_url_button(post: dict, text: str, url: str):
    post.setdefault("buttons", []).append([{"text": text, "url": url}])


def make_payload(posts: list[dict], post_delay_seconds: float = 0) -> dict:
    return {
        "version": PAYLOAD_VERSION,
        "post_delay_seconds": normalize_post_delay_seconds(post_delay_seconds),
        "messages": posts,
    }


def messages(payload: dict) -> list[dict]:
    return list(payload.get("messages") or [])


def media_file(media: dict) -> io.BytesIO:
    stream = io.BytesIO(base64.b64decode(media["data"]))
    stream.name = media.get("filename") or "photo.jpg"
    return stream


def bot_entities(post: dict) -> list[MessageEntity] | None:
    entities = post.get("entities") or []
    return [MessageEntity(**entity) for entity in entities] if entities else None


def uses_raw_html(post: dict) -> bool:
    return not post.get("entities") and bool(SUPPORTED_HTML_RE.search(post.get("html") or ""))


def bot_text_kwargs(post: dict, *, caption: bool = False) -> dict:
    html = post.get("html") or ""
    entities = bot_entities(post)
    key = "caption" if caption else "text"
    kwargs = {key: post.get("text") or ""}
    if entities:
        kwargs["caption_entities" if caption else "entities"] = entities
    elif uses_raw_html(post):
        kwargs[key] = html
        kwargs["parse_mode"] = "HTML"
    return kwargs


def _button_url_rows(post: dict) -> list[list[dict]]:
    rows = []
    for row in post.get("buttons") or []:
        out_row = [button for button in row if button.get("url")]
        if out_row:
            rows.append(out_row)
    return rows


def html_for_send(post: dict, *, append_button_links: bool = False) -> str:
    html = post.get("html") or html_lib.escape(post.get("text") or "", quote=False)
    if not append_button_links:
        return html

    link_rows = []
    for row in _button_url_rows(post):
        links = []
        for button in row:
            text = html_lib.escape(button.get("text") or button["url"], quote=False)
            url = html_lib.escape(button["url"], quote=True)
            links.append(f'<a href="{url}">{text}</a>')
        if links:
            link_rows.append(" | ".join(links))

    if not link_rows:
        return html
    separator = "\n\n" if html.strip() else ""
    return f"{html}{separator}" + "\n".join(link_rows)


def telethon_buttons(post: dict) -> list[list[Any]] | None:
    rows = []
    for row in post.get("buttons") or []:
        out_row = []
        for button in row:
            text = button.get("text") or "Button"
            if button.get("url"):
                out_row.append(Button.url(text, button["url"]))
            elif button.get("callback_data"):
                out_row.append(Button.inline(text, button["callback_data"]))
        if out_row:
            rows.append(out_row)
    return rows or None


def plain_text_from_html(html: str) -> str:
    try:
        text, _ = TelegramHTML.parse(html)
        return text
    except Exception:
        return re.sub(r"<[^>]+>", "", html or "")


def post_summary(post: dict, limit: int = 160) -> str:
    parts = []
    media_count = len(post.get("media") or [])
    if media_count == 1:
        parts.append("[фото]")
    elif media_count > 1:
        parts.append(f"[альбом: {media_count} фото]")

    if uses_raw_html(post):
        text = plain_text_from_html(post.get("html") or "")
    else:
        text = post.get("text") or plain_text_from_html(post.get("html") or "")
    if text:
        parts.append(text.replace("\n", " ").strip())

    button_count = sum(len(row) for row in (post.get("buttons") or []))
    if button_count:
        parts.append(f"[кнопки: {button_count}]")

    summary = " ".join(part for part in parts if part).strip() or "[пусто]"
    return summary[:limit] + ("..." if len(summary) > limit else "")


def payload_summary(payload: dict, limit: int = 500) -> str:
    lines = []
    delay_seconds = post_delay_seconds(payload)
    if delay_seconds:
        lines.append(f"Задержка между постами: {format_seconds(delay_seconds)}")
    for index, post in enumerate(messages(payload), start=1):
        lines.append(f"{index}. {post_summary(post, limit=140)}")
    summary = "\n".join(lines) or "[пустой шаблон]"
    return summary[:limit] + ("..." if len(summary) > limit else "")
