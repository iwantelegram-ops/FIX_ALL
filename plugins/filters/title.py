"""
plugins/filters/title.py
─────────────────────────
Fitur Auto Title RPG — memberi gelar (custom title) admin berdasarkan
skor mengetik mingguan, terintegrasi dengan panel Security OS di bot ini.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ALUR KERJA:
  1. Bot mendeteksi sinyal "typing..." dari anggota grup.
  2. Skor user di grup tersebut bertambah +1 di collection typing_stats.
  3. Setiap ada update skor → bot mencoba set custom title admin
     (hanya berhasil jika user adalah admin grup).
  4. Custom title diambil dari:
       a. Konfigurasi custom per grup  (jika admin sudah set)
       b. Default RPG 10 tingkat       (jika custom belum diset)
  5. Setiap Minggu 23:59 WIB → umumkan juara, reset skor & hapus semua title.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
KOLEKSI MongoDB (via database.db):
  typing_stats       — { user_id, chat_id, user_name, score }
  title_config       — { chat_id, enabled, custom_titles: [10 str] }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CALLBACK DATA (untuk UI panel di pages.py / handlers):
  autotitle_panel_{chat_id}          — buka sub-panel Auto Title
  autotitle_on_{chat_id}             — aktifkan fitur
  autotitle_off_{chat_id}            — nonaktifkan fitur
  autotitle_custom_{chat_id}         — mulai FSM input 10 custom title
  autotitle_reset_{chat_id}          — reset ke title RPG default
  autotitle_cleartitles_{chat_id}    — hapus semua custom title member di grup

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INTEGRASI UI (tambahkan secara manual di pages.py & handlers_dm.py):

  Di pages.py → page_manage():
    Tambah tombol baru di keyboard setelah tombol Security OS:
      InlineKeyboardButton("🏅 Auto Title", callback_data=f"autotitle_panel_{chat_id}")

  Di handlers_dm.py:
    Import dan daftarkan callback autotitle_panel dengan memanggil
    page_autotitle() dari modul ini.

    Contoh (tambahkan di handlers_dm.py):
      from plugins.filters.title import (
          page_autotitle,
          cb_autotitle_panel, cb_autotitle_on, cb_autotitle_off,
          cb_autotitle_reset, cb_autotitle_cleartitles,
          cb_autotitle_custom, handle_autotitle_custom_input,
      )
    (Semua handler sudah di-register via @Client.on_* di file ini,
     cukup import modul agar handler aktif saat bot start.)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import re

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery, Message,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from pyrogram.enums import ParseMode, ChatMemberStatus
from pyrogram.errors import (
    ChatAdminRequired, UserNotParticipant,
    BadRequest, MessageNotModified, MessageIdInvalid,
)

from database import db
import admin_session as _adm_sess

# ── Koleksi MongoDB ────────────────────────────────────────────────────────────
typing_stats_col = db["typing_stats"]   # skor mengetik per user per grup
title_config_col = db["title_config"]   # konfigurasi Auto Title per grup

# ── Default RPG Titles (10 tingkat, indeks 0 = tertinggi) ─────────────────────
DEFAULT_TITLES = [
    "🌌 God Level",
    "👑 Mythic Grandmaster",
    "🐉 Dragon Slayer",
    "🔮 Platinum Sage",
    "🦅 Gold Knight",
    "🏹 Silver Vanguard",
    "🛡️ Iron Warrior",
    "⚔️ Bronze Adventurer",
    "🌾 Novice",
    "🪵 Vagabond",
]

# Ambang skor untuk tiap tingkat (urut dari tertinggi)
_SCORE_THRESHOLDS = [300, 200, 150, 100, 75, 50, 30, 15, 5, 0]

# Timeout FSM input custom title (detik)
_CUSTOM_TITLE_TIMEOUT = 180

# FSM state: { user_id: { "chat_id": int, "msg_id": int, "_task": Task|None } }
_pending_custom_title: dict[int, dict] = {}


# ─────────────────────────────────────────────────────────────────────────────
#  Helper — Ambil konfigurasi title untuk satu grup
# ─────────────────────────────────────────────────────────────────────────────
async def _get_title_config(chat_id: int) -> dict:
    """
    Return dokumen konfigurasi title untuk grup.
    Struktur: { chat_id, enabled, custom_titles }
    Jika belum ada → return default (disabled, no custom).
    """
    doc = await title_config_col.find_one({"chat_id": chat_id})
    if not doc:
        return {"chat_id": chat_id, "enabled": False, "custom_titles": []}
    return doc


async def _set_title_config(chat_id: int, **fields) -> None:
    """Update atau insert konfigurasi title grup."""
    await title_config_col.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, **fields}},
        upsert=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Helper — Tentukan title berdasarkan skor
# ─────────────────────────────────────────────────────────────────────────────
def _get_title_for_score(score: int, titles: list[str]) -> str:
    """
    Cocokkan skor ke salah satu dari 10 tingkat title.
    `titles` harus list 10 elemen (indeks 0 = gelar tertinggi).
    Gunakan DEFAULT_TITLES jika custom belum diset.
    """
    for i, threshold in enumerate(_SCORE_THRESHOLDS):
        if score >= threshold:
            return titles[i] if i < len(titles) else DEFAULT_TITLES[i]
    return titles[-1] if titles else DEFAULT_TITLES[-1]


def _resolve_titles(cfg: dict) -> list[str]:
    """Ambil daftar 10 title — custom jika ada, otherwise default."""
    custom = cfg.get("custom_titles", [])
    if custom and len(custom) == 10:
        return custom
    return DEFAULT_TITLES


# ─────────────────────────────────────────────────────────────────────────────
#  Helper — Set custom title admin di grup (via Pyrogram promote)
# ─────────────────────────────────────────────────────────────────────────────
async def _apply_title(client: Client, chat_id: int, user_id: int, title: str) -> bool:
    """
    Coba set custom_title untuk user di chat.
    Hanya berhasil jika:
      - user adalah admin grup
      - bot punya izin promote_members
      - panjang title ≤ 16 karakter (batas Telegram)

    Return True jika berhasil, False jika gagal (bukan admin / tidak ada izin).
    """
    # Potong title agar tidak melebihi 16 karakter (batas Telegram)
    title_trimmed = title[:16]
    try:
        member = await client.get_chat_member(chat_id, user_id)
        # Hanya admin yang bisa diberi custom title
        if member.status not in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ):
            return False

        # promote_chat_member dengan parameter minimal — hanya update custom_title
        # Tetap pertahankan hak admin yang sudah ada (is_anonymous tidak diubah)
        await client.promote_chat_member(
            chat_id,
            user_id,
            custom_title=title_trimmed,
        )
        return True

    except (ChatAdminRequired, UserNotParticipant, BadRequest):
        return False
    except Exception as e:
        print(f"[auto_title] _apply_title error uid={user_id} chat={chat_id}: {e}")
        return False


async def _clear_title(client: Client, chat_id: int, user_id: int) -> bool:
    """Hapus custom title (set ke string kosong)."""
    try:
        member = await client.get_chat_member(chat_id, user_id)
        if member.status not in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ):
            return False
        await client.promote_chat_member(chat_id, user_id, custom_title="")
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  1. Handler Deteksi Sinyal Typing — update skor & set title
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_chat_action()
async def track_typing_and_title(client: Client, chat_action):
    """
    Setiap kali anggota grup mengetik:
      1. Tambah skor +1 di DB.
      2. Jika Auto Title aktif → coba pasang title ke admin.
    """
    # Hanya proses sinyal "typing"
    action_name = getattr(chat_action.action, "value", str(chat_action.action))
    # Pyrogram action bisa berupa enum ChatAction.TYPING
    if "typing" not in action_name.lower():
        return

    user = chat_action.from_user
    if not user or user.is_bot:
        return

    chat = chat_action.chat
    if not chat:
        return

    user_id  = user.id
    chat_id  = chat.id
    user_name = user.first_name or str(user_id)

    # Update skor di MongoDB
    result = await typing_stats_col.find_one_and_update(
        {"user_id": user_id, "chat_id": chat_id},
        {
            "$set": {"user_name": user_name, "chat_id": chat_id},
            "$inc": {"score": 1},
        },
        upsert=True,
        return_document=True,  # ambil dokumen setelah update
    )

    # Ambil skor terbaru
    new_score = (result or {}).get("score", 1)

    # Cek apakah fitur Auto Title aktif untuk grup ini
    cfg = await _get_title_config(chat_id)
    if not cfg.get("enabled", False):
        return

    # Tentukan title yang sesuai dan pasang
    titles    = _resolve_titles(cfg)
    new_title = _get_title_for_score(new_score, titles)

    # Fire-and-forget — jangan blokir pipeline chat_action
    asyncio.create_task(_apply_title(client, chat_id, user_id, new_title))


# ─────────────────────────────────────────────────────────────────────────────
#  2. Reset Mingguan — umumkan juara & hapus semua skor + title
# ─────────────────────────────────────────────────────────────────────────────
async def weekly_title_reset(client: Client, chat_id: int) -> None:
    """
    Dipanggil oleh scheduler mingguan (dari antigcast.py / main bot).
    - Umumkan top-3 ke grup.
    - Hapus semua custom title admin di grup.
    - Reset semua skor.

    Cara menghubungkan ke scheduler (di antigcast.py atau main):
        from plugins.filters.title import weekly_title_reset
        scheduler.add_job(
            lambda: asyncio.create_task(weekly_title_reset(app, GROUP_CHAT_ID)),
            'cron', day_of_week='sun', hour=23, minute=59
        )
    Atau panggil per-grup jika banyak grup.
    """
    cfg = await _get_title_config(chat_id)
    titles = _resolve_titles(cfg)

    # Ambil top 3
    cursor   = typing_stats_col.find({"chat_id": chat_id}).sort("score", -1).limit(3)
    top_three = await cursor.to_list(length=3)

    announcement = (
        "🏆 <b>PENGUMUMAN JUARA AUTO TITLE MINGGUAN!</b> 🏆\n"
        f"<code>Grup: {chat_id}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Selamat kepada para petualang paling aktif minggu ini:\n\n"
    )

    medals = ["🥇", "🥈", "🥉"]
    if top_three:
        for idx, member in enumerate(top_three):
            score  = member.get("score", 0)
            title  = _get_title_for_score(score, titles)
            announcement += (
                f"{medals[idx]} <b>{member['user_name']}</b>\n"
                f"   {title} — <code>{score} poin</code>\n\n"
            )
    else:
        announcement += "Sayang sekali, tidak ada aktivitas mengetik minggu ini. 🏝️\n\n"

    announcement += (
        "🔄 <i>Poin telah direset ke 0 untuk minggu yang baru!\n"
        "Semua gelar kembali ke Vagabond.</i>"
    )

    # Kirim pengumuman
    try:
        await client.send_message(chat_id=chat_id, text=announcement, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"[auto_title] weekly_title_reset: gagal kirim pengumuman ke {chat_id}: {e}")

    # Hapus semua custom title admin di grup (jika fitur aktif)
    if cfg.get("enabled", False):
        await _do_clear_all_titles(client, chat_id)

    # Reset semua skor di grup ini
    await typing_stats_col.delete_many({"chat_id": chat_id})
    print(f"[auto_title] Skor & title direset untuk grup {chat_id}.")


async def _do_clear_all_titles(client: Client, chat_id: int) -> int:
    """
    Hapus custom title semua admin di grup.
    Return jumlah admin yang berhasil dihapus titlenya.
    """
    cleared = 0
    try:
        async for member in client.get_chat_members(
            chat_id, filter=filters.ChatMembersFilter.ADMINISTRATORS
        ):
            if member.user and not member.user.is_bot:
                ok = await _clear_title(client, chat_id, member.user.id)
                if ok:
                    cleared += 1
                await asyncio.sleep(0.3)  # hindari flood
    except Exception as e:
        print(f"[auto_title] _do_clear_all_titles error chat={chat_id}: {e}")
    return cleared


# ─────────────────────────────────────────────────────────────────────────────
#  3. Halaman Panel Auto Title (untuk dipanggil dari pages.py / handlers_dm.py)
# ─────────────────────────────────────────────────────────────────────────────
async def page_autotitle(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """
    Buat konten halaman panel Auto Title.
    Dipanggil dari handler callback autotitle_panel_{chat_id}.
    """
    cfg     = await _get_title_config(chat_id)
    enabled = cfg.get("enabled", False)
    custom  = cfg.get("custom_titles", [])
    has_custom = bool(custom and len(custom) == 10)

    flag     = "🟢 AKTIF" if enabled else "🔴 NONAKTIF"
    src_type = "🎨 Custom" if has_custom else "⚔️ Default RPG"
    titles   = _resolve_titles(cfg)

    # Preview 3 tingkat teratas & terendah
    preview_lines = ""
    for i, t in enumerate(titles):
        threshold = _SCORE_THRESHOLDS[i]
        if i < 3 or i >= 7:
            preview_lines += f"   {'🔝' if i == 0 else '▸'} <code>{t}</code>  ≥{threshold} poin\n"
        elif i == 3:
            preview_lines += "   ⋮\n"

    text = (
        f"🏅 <b>AUTO TITLE</b>\n"
        f"<code>Grup: {chat_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>📌 APA INI?</b>\n"
        f"Bot memberi <b>gelar (custom title)</b> kepada admin grup\n"
        f"berdasarkan seberapa sering mereka mengetik di grup ini.\n"
        f"Semakin aktif → semakin tinggi gelarnya.\n\n"
        f"<b>⚙️ STATUS:</b>\n"
        f"  {'✅' if enabled else '❌'} Auto Title  : <code>{flag}</code>\n"
        f"  📋 Sumber Title : <code>{src_type}</code>\n\n"
        f"<b>🏆 PREVIEW GELAR:</b>\n"
        f"{preview_lines}\n"
        f"<b>⚠️ CATATAN:</b>\n"
        f"◈ Gelar hanya terpasang pada <b>admin grup</b>.\n"
        f"◈ Member biasa skornya tetap terekam, gelar terpasang\n"
        f"   otomatis begitu mereka dijadikan admin.\n"
        f"◈ Gelar akan direset setiap <b>Minggu 23:59 WIB</b>.\n"
        f"◈ Panjang gelar maks. <b>16 karakter</b> (batas Telegram).\n"
    )

    btn_toggle = (
        InlineKeyboardButton("🔴  Nonaktifkan", callback_data=f"autotitle_off_{chat_id}")
        if enabled else
        InlineKeyboardButton("🟢  Aktifkan", callback_data=f"autotitle_on_{chat_id}")
    )

    keyboard = InlineKeyboardMarkup([
        [btn_toggle],
        [InlineKeyboardButton("🎨  Custom Title",        callback_data=f"autotitle_custom_{chat_id}")],
        [InlineKeyboardButton("🔄  Reset Title Default", callback_data=f"autotitle_reset_{chat_id}")],
        [InlineKeyboardButton("🗑️  Hapus Semua Title Member", callback_data=f"autotitle_cleartitles_{chat_id}")],
        [InlineKeyboardButton("🔙  Kembali ke Panel Grup", callback_data=f"manage_{chat_id}")],
    ])
    return text, keyboard


# ─────────────────────────────────────────────────────────────────────────────
#  4. Callback Handlers — Panel Auto Title
# ─────────────────────────────────────────────────────────────────────────────

def _extract_chat_id(cb_data: str) -> int:
    return int(re.search(r"(-?\d+)$", cb_data).group(1))


async def _safe_edit(msg, text: str, keyboard=None):
    try:
        await msg.edit(
            text, reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except (MessageNotModified, MessageIdInvalid):
        pass
    except Exception as e:
        print(f"[auto_title] safe_edit error: {e}")


# ── Buka panel ─────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^autotitle_panel_(-?\d+)$"))
async def cb_autotitle_panel(client: Client, cb: CallbackQuery):
    await cb.answer()
    chat_id = _extract_chat_id(cb.data)
    user_id = cb.from_user.id

    if not await _adm_sess.verify_admin_session(client, user_id, chat_id):
        from plugins.ui.handlers_dm import _deny_session
        return await _deny_session(cb)

    text, keyboard = await page_autotitle(chat_id)
    await _safe_edit(cb.message, text, keyboard)


# ── Aktifkan ───────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^autotitle_on_(-?\d+)$"))
async def cb_autotitle_on(client: Client, cb: CallbackQuery):
    await cb.answer("Mengaktifkan Auto Title...")
    chat_id = _extract_chat_id(cb.data)
    user_id = cb.from_user.id

    if not await _adm_sess.verify_admin_session(client, user_id, chat_id):
        from plugins.ui.handlers_dm import _deny_session
        return await _deny_session(cb)

    await _set_title_config(chat_id, enabled=True)

    text, keyboard = await page_autotitle(chat_id)
    header = "✅ <b>Auto Title diaktifkan!</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    await _safe_edit(cb.message, header + text, keyboard)


# ── Nonaktifkan ────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^autotitle_off_(-?\d+)$"))
async def cb_autotitle_off(client: Client, cb: CallbackQuery):
    await cb.answer("Menonaktifkan Auto Title...")
    chat_id = _extract_chat_id(cb.data)
    user_id = cb.from_user.id

    if not await _adm_sess.verify_admin_session(client, user_id, chat_id):
        from plugins.ui.handlers_dm import _deny_session
        return await _deny_session(cb)

    await _set_title_config(chat_id, enabled=False)

    text, keyboard = await page_autotitle(chat_id)
    header = "🔴 <b>Auto Title dinonaktifkan.</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    await _safe_edit(cb.message, header + text, keyboard)


# ── Reset ke title default ─────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^autotitle_reset_(-?\d+)$"))
async def cb_autotitle_reset(client: Client, cb: CallbackQuery):
    await cb.answer("Mereset title ke default RPG...")
    chat_id = _extract_chat_id(cb.data)
    user_id = cb.from_user.id

    if not await _adm_sess.verify_admin_session(client, user_id, chat_id):
        from plugins.ui.handlers_dm import _deny_session
        return await _deny_session(cb)

    # Hapus custom_titles sehingga kembali ke default
    await _set_title_config(chat_id, custom_titles=[])

    text, keyboard = await page_autotitle(chat_id)
    header = (
        "🔄 <b>Title berhasil direset ke Default RPG!</b>\n"
        "<i>10 gelar RPG bawaan kembali digunakan.</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    await _safe_edit(cb.message, header + text, keyboard)


# ── Hapus semua title member di grup ──────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^autotitle_cleartitles_(-?\d+)$"))
async def cb_autotitle_cleartitles(client: Client, cb: CallbackQuery):
    await cb.answer("Menghapus semua title, harap tunggu...")
    chat_id = _extract_chat_id(cb.data)
    user_id = cb.from_user.id

    if not await _adm_sess.verify_admin_session(client, user_id, chat_id):
        from plugins.ui.handlers_dm import _deny_session
        return await _deny_session(cb)

    # Tampilkan loading sementara
    await _safe_edit(
        cb.message,
        f"⏳ <b>Menghapus semua custom title admin di grup...</b>\n\n"
        f"<i>Proses ini mungkin membutuhkan beberapa detik.</i>",
    )

    cleared = await _do_clear_all_titles(client, chat_id)

    text, keyboard = await page_autotitle(chat_id)
    header = (
        f"🗑️ <b>Selesai! {cleared} title admin berhasil dihapus.</b>\n"
        f"<i>Semua admin kembali tanpa gelar khusus.</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    await _safe_edit(cb.message, header + text, keyboard)


# ── Mulai FSM input custom title ───────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^autotitle_custom_(-?\d+)$"))
async def cb_autotitle_custom(client: Client, cb: CallbackQuery):
    await cb.answer()
    chat_id = _extract_chat_id(cb.data)
    user_id = cb.from_user.id

    if not await _adm_sess.verify_admin_session(client, user_id, chat_id):
        from plugins.ui.handlers_dm import _deny_session
        return await _deny_session(cb)

    # Batalkan FSM lama jika ada
    _cancel_custom_task(user_id)

    _pending_custom_title[user_id] = {
        "chat_id": chat_id,
        "msg_id":  cb.message.id,
        "_task":   None,
    }

    await _safe_edit(
        cb.message,
        f"🎨 <b>CUSTOM TITLE</b>\n"
        f"<code>Grup: {chat_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Kirim <b>10 nama gelar</b> — satu per baris,\n"
        f"dari yang <b>tertinggi</b> ke <b>terendah</b>.\n\n"
        f"<b>📋 FORMAT:</b>\n"
        f"<code>Nama Gelar 1\n"
        f"Nama Gelar 2\n"
        f"Nama Gelar 3\n"
        f"...\n"
        f"Nama Gelar 10</code>\n\n"
        f"<b>📌 CONTOH:</b>\n"
        f"<code>💎 Legenda\n"
        f"🌟 Bintang\n"
        f"🔥 Api\n"
        f"⚡ Kilat\n"
        f"🌊 Ombak\n"
        f"🍃 Angin\n"
        f"🪨 Batu\n"
        f"🌱 Tunas\n"
        f"🐣 Telur\n"
        f"🐛 Ulat</code>\n\n"
        f"<b>⚠️ CATATAN:</b>\n"
        f"◈ Harus tepat <b>10 baris</b>.\n"
        f"◈ Tiap gelar maks. <b>16 karakter</b> (batas Telegram).\n"
        f"◈ Baris kosong tidak dihitung.\n\n"
        f"<i>⏳ Batas waktu: {_CUSTOM_TITLE_TIMEOUT // 60} menit.</i>\n"
        f"<i>Kirim /batal untuk membatalkan.</i>",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🚫  Batalkan", callback_data=f"autotitle_panel_{chat_id}")]
        ]),
    )

    task = asyncio.create_task(
        _custom_title_timeout(user_id, chat_id, cb.message, client)
    )
    if user_id in _pending_custom_title:
        _pending_custom_title[user_id]["_task"] = task


async def _custom_title_timeout(user_id: int, chat_id: int, msg, client: Client):
    await asyncio.sleep(_CUSTOM_TITLE_TIMEOUT)
    if user_id not in _pending_custom_title:
        return
    _pending_custom_title.pop(user_id, None)
    try:
        text, keyboard = await page_autotitle(chat_id)
        await _safe_edit(
            msg,
            "⏰ <b>Timeout.</b> Input custom title dibatalkan.\n\n" + text,
            keyboard,
        )
    except Exception:
        pass


def _cancel_custom_task(user_id: int):
    state = _pending_custom_title.pop(user_id, None)
    if state and state.get("_task"):
        task = state["_task"]
        if not task.done():
            task.cancel()


# ─────────────────────────────────────────────────────────────────────────────
#  5. FSM Handler — tangkap input 10 custom title dari DM admin
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_message(
    filters.private & filters.text,
    group=52,   # setelah handler FSM lain (50, 51), tidak bentrok
)
async def handle_autotitle_custom_input(client: Client, message: Message):
    user_id = message.from_user.id

    state = _pending_custom_title.get(user_id)
    if not state:
        return   # bukan dalam mode custom title — lewati

    text    = (message.text or "").strip()
    chat_id = state["chat_id"]
    msg_id  = state["msg_id"]

    # /batal — batalkan
    if text.lower() in ("/batal", "/cancel"):
        _cancel_custom_task(user_id)
        try:
            page_text, keyboard = await page_autotitle(chat_id)
            await client.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg_id,
                text="✅ <b>Dibatalkan.</b>\n\n" + page_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        try:
            await message.delete()
        except Exception:
            pass
        return

    # Parse baris — hapus baris kosong
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # Validasi: harus tepat 10 baris
    if len(lines) != 10:
        try:
            err = await message.reply(
                f"❌ <b>Jumlah gelar tidak tepat.</b>\n\n"
                f"Kamu mengirim <b>{len(lines)} baris</b>, "
                f"harus tepat <b>10 baris</b>.\n\n"
                f"<i>Coba lagi atau kirim /batal untuk membatalkan.</i>",
                parse_mode=ParseMode.HTML,
            )
            await asyncio.sleep(6)
            await err.delete()
        except Exception:
            pass
        return

    # Validasi panjang tiap gelar (maks 16 karakter Telegram)
    too_long = [
        (i + 1, line) for i, line in enumerate(lines) if len(line) > 16
    ]
    if too_long:
        detail = "\n".join(
            f"  Baris {n}: <code>{l}</code> ({len(l)} karakter)"
            for n, l in too_long
        )
        try:
            err = await message.reply(
                f"❌ <b>Beberapa gelar terlalu panjang (maks. 16 karakter):</b>\n\n"
                f"{detail}\n\n"
                f"<i>Persingkat lalu kirim ulang, atau /batal untuk membatalkan.</i>",
                parse_mode=ParseMode.HTML,
            )
            await asyncio.sleep(8)
            await err.delete()
        except Exception:
            pass
        return

    # Semua valid — simpan
    _cancel_custom_task(user_id)

    await _set_title_config(chat_id, custom_titles=lines)

    # Tampilkan hasil
    preview = "\n".join(
        f"  {i + 1}. <code>{l}</code>" for i, l in enumerate(lines)
    )
    try:
        page_text, keyboard = await page_autotitle(chat_id)
        await client.edit_message_text(
            chat_id=message.chat.id,
            message_id=msg_id,
            text=(
                f"✅ <b>10 Custom Title berhasil disimpan!</b>\n\n"
                f"{preview}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{page_text}"
            ),
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        print(f"[auto_title] handle_autotitle_custom_input edit error: {e}")

    try:
        await message.delete()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  6. Leaderboard manual — /typing_stats di grup
#     (Dipertahankan dari script asli, dikombinasikan dengan UI bot ini)
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("typing_stats") & filters.group)
async def cmd_typing_stats(client: Client, message: Message):
    """Tampilkan leaderboard typing minggu ini di grup."""
    chat_id = message.chat.id

    cfg    = await _get_title_config(chat_id)
    titles = _resolve_titles(cfg)

    cursor     = typing_stats_col.find({"chat_id": chat_id}).sort("score", -1).limit(10)
    top_members = await cursor.to_list(length=10)

    if not top_members:
        await message.reply_text(
            "📭 <b>Belum ada data mengetik yang terekam minggu ini.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    response = "📊 <b>PAPAN PERINGKAT TYPING MINGGU INI:</b>\n\n"
    for rank, member in enumerate(top_members, start=1):
        score = member.get("score", 0)
        title = _get_title_for_score(score, titles)
        response += (
            f"{rank}. <b>{member['user_name']}</b>\n"
            f"   {title} — <code>{score} poin</code>\n\n"
        )

    await message.reply_text(response, parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────────────────────────────────────
#  7. Cron scheduler — loop Minggu 23:59 WIB (dipanggil dari antigcast.py)
# ─────────────────────────────────────────────────────────────────────────────
async def cron_weekly_title_reset(client: Client) -> None:
    """
    Loop tak terbatas yang berjalan sebagai asyncio.Task sejak bot start.
    Setiap Minggu (weekday=6) pukul 23:59 WIB → jalankan weekly_title_reset
    untuk semua grup yang punya Auto Title aktif.

    Dipanggil di antigcast.py:
        from plugins.filters.title import cron_weekly_title_reset
        asyncio.create_task(cron_weekly_title_reset(app))
    """
    import pytz
    from datetime import datetime as _dt

    TZ_WIB = pytz.timezone("Asia/Jakarta")

    print("[AutoTitle] ⏰ Weekly title reset scheduler aktif (Setiap Minggu 23:59 WIB).")

    try:
        while True:
            now = _dt.now(TZ_WIB)
            # weekday() → 0=Senin … 6=Minggu
            if now.weekday() == 6 and now.hour == 23 and now.minute == 59:
                print(f"[AutoTitle] 🔄 Memulai weekly reset semua grup...")

                # Ambil semua grup yang Auto Title-nya aktif
                cursor = title_config_col.find({"enabled": True})
                async for cfg in cursor:
                    chat_id = cfg.get("chat_id")
                    if not chat_id:
                        continue
                    try:
                        await weekly_title_reset(client, chat_id)
                    except Exception as e:
                        print(f"[AutoTitle] Error reset grup {chat_id}: {e}")
                    await asyncio.sleep(1)  # jeda antar grup

                # Tunggu 61 detik agar tidak trigger dua kali dalam menit yang sama
                await asyncio.sleep(61)

            await asyncio.sleep(30)

    except asyncio.CancelledError:
        print("[AutoTitle] 💤 Weekly title scheduler dihentikan.")
        raise
    except Exception as e:
        print(f"[AutoTitle] Error tak terduga di scheduler: {e}")
