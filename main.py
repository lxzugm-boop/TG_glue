import asyncio
import os
import tempfile
import uuid
import subprocess
from pathlib import Path
from typing import Dict

from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    FSInputFile,
    CallbackQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения")

# ID чата владельца бота (для пересылки видео без указания авторов)
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

# Base URL сервиса (Render сам прокинет RENDER_EXTERNAL_URL)
BASE_WEBHOOK_URL = os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")
if not BASE_WEBHOOK_URL:
    raise RuntimeError(
        "Не удалось определить BASE_WEBHOOK_URL. "
        "На Render переменная RENDER_EXTERNAL_URL должна быть доступна автоматически."
    )

BASE_WEBHOOK_URL = BASE_WEBHOOK_URL.rstrip("/")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------------- НАСТРОЙКИ ПОЛЬЗОВАТЕЛЯ ----------------

# Формат кадра: "png" | "jpg" | "webp"
user_format_prefs: Dict[int, str] = {}
# Размер: "orig" | "1024" | "1024sq"
user_size_prefs: Dict[int, str] = {}
# Последний файл видео пользователя (file_id)
user_last_file_id: Dict[int, str] = {}


def get_user_format(user_id: int) -> str:
    """Формат по умолчанию — PNG, если пользователь ничего не выбирал."""
    return user_format_prefs.get(user_id, "png")


def set_user_format(user_id: int, fmt: str) -> None:
    """Сохраняем предпочтительный формат кадра."""
    fmt = fmt.lower()
    if fmt == "jpeg":
        fmt = "jpg"
    if fmt not in ("png", "jpg", "webp"):
        fmt = "png"
    user_format_prefs[user_id] = fmt


def get_user_size(user_id: int) -> str:
    """Размер по умолчанию — оригинальный."""
    return user_size_prefs.get(user_id, "orig")


def set_user_size(user_id: int, size: str) -> None:
    size = size.lower()
    if size not in ("orig", "1024", "1024sq"):
        size = "orig"
    user_size_prefs[user_id] = size


def describe_size(size_mode: str) -> str:
    size_mode = size_mode.lower()
    if size_mode == "1024":
        return "большая сторона 1024 px"
    if size_mode == "1024sq":
        return "квадрат 1024×1024 (кроп по центру)"
    return "оригинальное разрешение"


def build_settings_keyboard(user_id: int):
    """
    Инлайн-клавиатура под ответом:
    [ PNG ] [ JPG ] [ WEBP ]
    [ Оригинал ] [ 1024 px ] [ Квадрат 1024×1024 ]
    [ 🔁 Перегенерировать ]
    Текущие настройки помечаем ✅
    """
    current_fmt = get_user_format(user_id)
    current_size = get_user_size(user_id)

    kb = InlineKeyboardBuilder()

    # Форматы
    for fmt in ("png", "jpg", "webp"):
        label = fmt.upper()
        if fmt == current_fmt:
            label += " ✅"
        kb.button(text=label, callback_data=f"fmt:{fmt}")

    kb.row()

    # Размеры
    size_labels = {
        "orig": "Оригинал",
        "1024": "1024 px",
        "1024sq": "Квадрат 1024×1024",
    }
    for sz in ("orig", "1024", "1024sq"):
        label = size_labels[sz]
        if sz == current_size:
            label += " ✅"
        kb.button(text=label, callback_data=f"size:{sz}")

    kb.row()

    # Перегенерация
    kb.button(text="🔁 Перегенерировать", callback_data="regen")

    return kb.as_markup()


# ---------------- ОБРАБОТКА ВИДЕО ----------------


async def extract_last_frame(
    input_path: Path,
    output_format: str = "png",
    size_mode: str = "orig",
    timeout_sec: int = 60,
) -> Path:
    """
    Вырезает последний кадр из видео с помощью ffmpeg.

    - -sseof -0.1 — прыжок на 0.1 секунды до конца
    - -vframes 1 — берём один кадр
    - size_mode:
        "orig"   — исходное разрешение
        "1024"   — большая сторона 1024 px, вторая пропорциональна
        "1024sq" — квадрат 1024×1024 с кропом по центру
    """
    output_format = output_format.lower()
    if output_format not in ("png", "jpg", "jpeg", "webp"):
        output_format = "png"
    if output_format == "jpeg":
        output_format = "jpg"

    tmp_dir = Path(tempfile.gettempdir())
    output_path = tmp_dir / f"last_frame_{uuid.uuid4().hex}.{output_format}"

    cmd = [
        "ffmpeg",
        "-y",
        "-sseof", "-0.1",
        "-i", str(input_path),
    ]

    size_mode = size_mode.lower()
    if size_mode == "1024":
        scale_filter = "scale='if(gt(iw,ih),1024,-2)':'if(gt(ih,iw),1024,-2)'"
        cmd += ["-vf", scale_filter]
    elif size_mode == "1024sq":
        scale_crop_filter = (
            "scale=1024:1024:force_original_aspect_ratio=increase,"
            "crop=1024:1024"
        )
        cmd += ["-vf", scale_crop_filter]

    cmd += [
        "-vframes", "1",
        str(output_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg timeout: {e}") from e

    if result.returncode != 0 or not output_path.exists():
        raise RuntimeError(
            f"Ошибка ffmpeg (код {result.returncode}): {result.stderr.decode(errors='ignore')}"
        )

    return output_path


async def download_video_to_temp(message: Message) -> Path:
    """
    Скачивает видео/кружок/анимацию во временный файл и возвращает Path.
    Работает с:
    - message.video
    - message.video_note
    - message.animation
    """
    tmp_dir = Path(tempfile.gettempdir())
    tmp_dir.mkdir(parents=True, exist_ok=True)

    file_suffix = ".mp4"
    tmp_path = tmp_dir / f"input_{uuid.uuid4().hex}{file_suffix}"

    if message.video:
        file_obj = message.video
    elif message.video_note:
        file_obj = message.video_note
    elif message.animation:
        file_obj = message.animation
    else:
        raise ValueError("В сообщении нет поддерживаемого видео")

    await bot.download(file_obj, destination=tmp_path)

    return tmp_path


async def download_file_id_to_temp(file_id: str) -> Path:
    """
    Скачивает файл по file_id во временный .mp4.
    Используется для перегенерации без повторной отправки видео.
    """
    tmp_dir = Path(tempfile.gettempdir())
    tmp_dir.mkdir(parents=True, exist_ok=True)

    tmp_path = tmp_dir / f"input_{uuid.uuid4().hex}.mp4"
    await bot.download(file_id, destination=tmp_path)
    return tmp_path


async def send_video_to_admin(message: Message) -> None:
    """
    Дополнительно отправляет видео/кружок/анимацию владельцу бота
    без указания автора (как новый пост по file_id, не forward).
    Если ADMIN_CHAT_ID не задан, ничего не делает.
    """
    if not ADMIN_CHAT_ID:
        return

    try:
        if message.video:
            await bot.send_video(chat_id=ADMIN_CHAT_ID, video=message.video.file_id)
        elif message.video_note:
            await bot.send_video_note(
                chat_id=ADMIN_CHAT_ID,
                video_note=message.video_note.file_id,
            )
        elif message.animation:
            await bot.send_animation(
                chat_id=ADMIN_CHAT_ID,
                animation=message.animation.file_id,
            )
    except Exception as e:
        # Логируем в stdout, но не ломаем основную логику
        print(f"Не удалось отправить видео владельцу: {e}", flush=True)


# ---------------- ХЕНДЛЕРЫ СООБЩЕНИЙ ----------------


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    text = (
        "Привет! Я вытаскиваю последний кадр из видео для бесшовных переходов в нейросетях.\n\n"
        "Как пользоваться:\n"
        "1. Пришли мне видео, кружок или gif.\n"
        "2. Я пришлю последний кадр.\n"
        "3. Под ответом будут кнопки — можно выбрать формат (PNG/JPG/WEBP) и размер:\n"
        "   • оригинал,\n"
        "   • большая сторона 1024 px,\n"
        "   • квадрат 1024×1024 с кропом по центру.\n"
        "4. Кнопка «🔁 Перегенерировать» пересчитает кадр с новыми настройками без повторной отправки видео.\n\n"
        "Отправляя видео, ты соглашаешься на его техническую обработку для работы сервиса."
    )
    await message.answer(text)


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "Я сохраняю последний кадр из присланного видео.\n\n"
        "Просто отправь мне видео или кружок.\n"
        "Настройки формата и размера — через кнопки под ответом.\n"
        "«🔁 Перегенерировать» пересчитает кадр с текущими настройками.\n\n"
        "Отправляя видео, ты соглашаешься на его техническую обработку для работы сервиса."
    )
    await message.answer(text)


@dp.message(F.video | F.video_note | F.animation)
async def handle_video(message: Message) -> None:
    await message.chat.do("upload_photo")

    user_id = message.from_user.id
    preferred_format = get_user_format(user_id)
    size_mode = get_user_size(user_id)

    # Сохраняем file_id последнего видео для этого пользователя
    if message.video:
        user_last_file_id[user_id] = message.video.file_id
    elif message.video_note:
        user_last_file_id[user_id] = message.video_note.file_id
    elif message.animation:
        user_last_file_id[user_id] = message.animation.file_id

    # Параллельно отправляем видео владельцу (если указан ADMIN_CHAT_ID)
    await send_video_to_admin(message)

    tmp_video_path: Path | None = None
    frame_path: Path | None = None

    try:
        tmp_video_path = await download_video_to_temp(message)

        frame_path = await extract_last_frame(
            tmp_video_path,
            output_format=preferred_format,
            size_mode=size_mode,
        )

        photo = FSInputFile(frame_path)
        caption = (
            "Последний кадр из твоего видео.\n\n"
            f"Формат: {preferred_format.upper()}\n"
            f"Размер: {describe_size(size_mode)}"
        )
        kb = build_settings_keyboard(user_id)
        await message.answer_photo(photo=photo, caption=caption, reply_markup=kb)

    except Exception as e:
        await message.answer(f"Не получилось обработать видео 😔\nОшибка: {e}")
    finally:
        for p in (tmp_video_path, frame_path):
            if p and isinstance(p, Path) and p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass


@dp.message()
async def fallback_handler(message: Message) -> None:
    await message.answer(
        "Пришли мне видео или кружок — я сохраню из него последний кадр.\n"
        "Настройки — кнопки под ответом.\n"
        "Можно перегенерировать кадр с новыми настройками кнопкой «🔁 Перегенерировать».\n\n"
        "Отправляя видео, ты соглашаешься на его техническую обработку для работы сервиса."
    )


# ---------------- ХЕНДЛЕРЫ CALLBACK (ИНЛАЙН-КНОПКИ) ----------------


@dp.callback_query(F.data.startswith("fmt:"))
async def cb_set_format(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    fmt = callback.data.split(":", 1)[1]
    set_user_format(user_id, fmt)

    kb = build_settings_keyboard(user_id)
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer(f"Формат установлен: {get_user_format(user_id).upper()}")


@dp.callback_query(F.data.startswith("size:"))
async def cb_set_size(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    size = callback.data.split(":", 1)[1]
    set_user_size(user_id, size)

    kb = build_settings_keyboard(user_id)
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer(f"Размер установлен: {describe_size(get_user_size(user_id))}")


@dp.callback_query(F.data == "regen")
async def cb_regenerate(callback: CallbackQuery) -> None:
    """
    Перегенерировать последний кадр из последнего видео пользователя
    с текущими настройками формата и размера.
    """
    user_id = callback.from_user.id
    file_id = user_last_file_id.get(user_id)

    if not file_id:
        await callback.answer(
            "Нет сохранённого видео — пришли сначала ролик 🎥",
            show_alert=True,
        )
        return

    preferred_format = get_user_format(user_id)
    size_mode = get_user_size(user_id)

    tmp_video_path: Path | None = None
    frame_path: Path | None = None

    try:
        await callback.message.chat.do("upload_photo")

        tmp_video_path = await download_file_id_to_temp(file_id)

        frame_path = await extract_last_frame(
            tmp_video_path,
            output_format=preferred_format,
            size_mode=size_mode,
        )

        photo = FSInputFile(frame_path)
        caption = (
            "Перегенерированный последний кадр.\n\n"
            f"Формат: {preferred_format.upper()}\n"
            f"Размер: {describe_size(size_mode)}"
        )
        kb = build_settings_keyboard(user_id)
        await callback.message.answer_photo(photo=photo, caption=caption, reply_markup=kb)

        await callback.answer("Готово! Перегенерировал с текущими настройками ✅")

    except Exception as e:
        await callback.answer("Не получилось перегенерировать 😔", show_alert=True)
        await callback.message.answer(f"Ошибка при перегенерации: {e}")
    finally:
        for p in (tmp_video_path, frame_path):
            if p and isinstance(p, Path) and p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass


# ---------------- WEBHOOK + AIOHTTP ----------------


async def on_startup(bot: Bot) -> None:
    """Регистрируем webhook в Telegram при старте."""
    await bot.set_webhook(WEBHOOK_URL)
    print(f"Webhook установлен: {WEBHOOK_URL}")


async def healthcheck(request: web.Request) -> web.Response:
    """Простой healthcheck для Render."""
    return web.Response(text="OK", status=200)


async def main() -> None:
    dp.startup.register(on_startup)

    app = web.Application()

    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        handle_in_background=True,
    )
    webhook_handler.register(app, path=WEBHOOK_PATH)

    # healthcheck на /
    app.router.add_get("/", healthcheck)

    setup_application(app, dp, bot=bot)

    port = int(os.getenv("PORT", "10000"))
    print(f"Стартуем aiohttp на порту {port}, webhook: {WEBHOOK_URL}")
    await web._run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    asyncio.run(main())
