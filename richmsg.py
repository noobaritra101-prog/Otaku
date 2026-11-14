"""Native Telegram rich messages (Bot API 10.1+: sendRichMessage).

aiogram may not ship this method yet, so it is declared here as a custom
TelegramMethod. Content is sent in Rich Markdown mode (headings 1-6, tables,
task lists, quotes, pull quotes, details, dividers, media blocks).
"""
import re
from typing import Any, Union

from aiogram.methods import TelegramMethod
from aiogram.types import InlineKeyboardMarkup, Message, ReplyParameters


class SendRichMessage(TelegramMethod[Message]):
    __returning__ = Message
    __api_method__ = "sendRichMessage"

    chat_id: int | str
    rich_message: dict[str, Any]
    reply_markup: InlineKeyboardMarkup | None = None
    message_thread_id: int | None = None   # forum topics in groups
    disable_notification: bool | None = None
    protect_content: bool | None = None
    reply_parameters: ReplyParameters | None = None   # makes it a reply to a message


class EditRichMessageText(TelegramMethod[Union[Message, bool]]):
    """editMessageText with rich_message (Bot API 10.1+): edits a rich message in place."""
    __returning__ = Union[Message, bool]
    __api_method__ = "editMessageText"

    chat_id: int | str
    message_id: int
    rich_message: dict[str, Any]
    reply_markup: InlineKeyboardMarkup | None = None


_MD_SPECIAL = re.compile(r"([\\`*_\[\]()#+\-.!|~<>=$])")


def esc(text: str) -> str:
    """Escape user-provided text for Rich Markdown."""
    return _MD_SPECIAL.sub(r"\\\1", text)


def media_ref(kind: str) -> tuple[str, str]:
    """Return (markdown media line, InputMedia type) for a stored start media kind."""
    if kind == "photo":
        return "![](tg://photo?id=lead)", "photo"
    if kind == "animation":
        return "![](tg://video?id=lead)", "animation"
    return "![](tg://video?id=lead)", "video"


def _payload(markdown: str, media: tuple[str, str] | None, media_below: bool,
             spoiler: bool = False) -> dict[str, Any]:
    """rich_message dict; media = (file_id, kind) becomes a media block (above the text unless media_below)."""
    rich: dict[str, Any] = {"markdown": markdown}
    if media:
        file_id, kind = media
        line, input_type = media_ref(kind)
        rich["markdown"] = f"{markdown}\n\n{line}" if media_below else f"{line}\n\n{markdown}"
        item = {"type": input_type, "media": file_id}
        if spoiler:
            item["has_spoiler"] = True
        rich["media"] = [{"id": "lead", "media": item}]
    return rich


async def send_rich(bot, chat_id: int, markdown: str, *,
                    media: tuple[str, str] | None = None,
                    reply_markup: InlineKeyboardMarkup | None = None,
                    message_thread_id: int | None = None,
                    reply_to_message_id: int | None = None,
                    media_below: bool = False,
                    spoiler: bool = False) -> Message:
    """media = (file_id, kind) where kind is photo / video / animation.

    media_below=True puts the media block after the text (caption-above-media style).
    """
    rich = _payload(markdown, media, media_below, spoiler)
    reply = (ReplyParameters(message_id=reply_to_message_id, allow_sending_without_reply=True)
             if reply_to_message_id else None)
    return await bot(SendRichMessage(chat_id=chat_id, rich_message=rich, reply_markup=reply_markup,
                                     message_thread_id=message_thread_id, reply_parameters=reply))


async def edit_rich(bot, chat_id: int, message_id: int, markdown: str, *,
                    reply_markup: InlineKeyboardMarkup | None = None,
                    media: tuple[str, str] | None = None,
                    media_below: bool = False):
    """Edit an existing rich message in place. Pass the same media again to keep the picture
    (media blocks are not carried over automatically)."""
    return await bot(EditRichMessageText(chat_id=chat_id, message_id=message_id,
                                         rich_message=_payload(markdown, media, media_below),
                                         reply_markup=reply_markup))


def media_html(kind: str, spoiler: bool = False) -> str:
    """HTML media block for the media registered as id "lead" (Rich HTML: <img>, <video>, tg-spoiler hides it)."""
    sp = " tg-spoiler" if spoiler else ""
    if kind == "photo":
        return f'<figure><img src="tg://photo?id=lead"{sp}/></figure>'
    return f'<figure><video src="tg://video?id=lead"{sp}></video></figure>'


async def send_rich_html(bot, chat_id: int, html: str, *,
                         media: tuple[str, str] | None = None,
                         spoiler: bool = False,
                         reply_markup: InlineKeyboardMarkup | None = None,
                         message_thread_id: int | None = None,
                         reply_to_message_id: int | None = None) -> Message:
    """Rich message written in Rich HTML. media = (file_id, kind) is placed on top as a media block, hidden by a
    spoiler when spoiler=True (the documented way: the tg-spoiler attribute on the media tag)."""
    rich: dict[str, Any] = {"html": html}
    if media:
        file_id, kind = media
        _, input_type = media_ref(kind)
        rich["html"] = media_html(kind, spoiler) + html
        rich["media"] = [{"id": "lead", "media": {"type": input_type, "media": file_id}}]
    reply = (ReplyParameters(message_id=reply_to_message_id, allow_sending_without_reply=True)
             if reply_to_message_id else None)
    return await bot(SendRichMessage(chat_id=chat_id, rich_message=rich, reply_markup=reply_markup,
                                     message_thread_id=message_thread_id, reply_parameters=reply))
