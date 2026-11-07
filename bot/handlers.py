"""CloudForge Update Router — Clean English UI, Wizard, Referrals, Forced Sub & Owner Admin."""
import html
import json
import secrets
import string
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional

from bot.config import (
    ACCESS_LABELS,
    HOURS_PER_BLOCK,
    OS_CATALOG,
    OWNER_ID,
    POINTS_PER_6H,
    REFERRAL_REWARD,
    SERVER_TYPES,
    calc_points,
)
from bot.database import Database
from bot.keyboards import (
    account_kb,
    admin_broadcast_confirm_kb,
    admin_broadcast_mode_kb,
    admin_home_kb,
    admin_points_kb,
    admin_user_card_kb,
    btn,
    cancel_kb,
    confirm_terminate_kb,
    forced_sub_kb,
    kb,
    main_menu_kb,
    only_cancel_kb,
    server_card_kb,
    wizard_access_kb,
    wizard_confirm_kb,
    wizard_duration_kb,
    wizard_os_kb,
    wizard_password_kb,
    wizard_type_kb,
)
from bot.provisioner import Provisioner, build_rdp_file, format_server_card, format_time_left
from bot.tg_api import BotAPI, TelegramAPIError


def _gen_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def _state_data(user: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(user.get("state_data") or "{}")
    except Exception:
        return {}


class BotRouter:
    def __init__(self, db: Database, api: BotAPI, provisioner: Provisioner):
        self.db = db
        self.api = api
        self.prov = provisioner
        self._bc_cancel = threading.Event()
        self._bc_running = False

    # ── Entry Point ──────────────────────────────────────────────────────────
    def handle_update(self, upd: Dict[str, Any]) -> None:
        if "message" in upd:
            self._on_message(upd["message"])
        elif "callback_query" in upd:
            self._on_callback(upd["callback_query"])

    # ── Shared Helpers & Guards ──────────────────────────────────────────────
    def _extract_referrer(self, text: str) -> Optional[int]:
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2 and parts[1].startswith("ref_"):
                raw = parts[1][4:].strip()
                if raw.isdigit():
                    return int(raw)
        return None

    def _register_and_notify(self, tg_user: Dict[str, Any], start_text: str = "") -> Dict[str, Any]:
        uid = int(tg_user["id"])
        first = tg_user.get("first_name", "") or ""
        last = tg_user.get("last_name", "") or ""
        full_name = f"{first} {last}".strip() or "User"
        username = tg_user.get("username", "") or ""
        lang = tg_user.get("language_code", "en") or "en"
        ref_id = self._extract_referrer(start_text)

        user, is_new = self.db.upsert_user(uid, full_name, username, lang, referrer_id=ref_id)
        if is_new and uid != OWNER_ID:
            total = self.db.total_users_count()
            uname_disp = f"@{html.escape(username)}" if username else "None"
            join_card = (
                "✨ <b>New User Joined</b>\n\n"
                "👤 <b>Member Info:</b>\n"
                f"• Name: {html.escape(full_name)}\n"
                f"• Username: {uname_disp}\n"
                f"• ID: <code>{uid}</code>\n"
                f"• Language: <code>{html.escape(lang)}</code>\n\n"
                f"📊 <b>Total Users:</b> {total:,}"
            )
            try:
                self.api.send_message(
                    OWNER_ID,
                    join_card,
                    reply_markup=kb([[btn("👤 Manage User", f"adm:uview:{uid}")]]),
                )
            except Exception:
                pass
        return user

    def _missing_channels(self, user_id: int) -> List[Dict[str, Any]]:
        if user_id == OWNER_ID:
            return []
        channels = self.db.list_channels()
        if not channels:
            return []
        missing: List[Dict[str, Any]] = []
        for ch in channels:
            try:
                member = self.api.call("getChatMember", {"chat_id": ch["chat_id"], "user_id": user_id})
                st = member.get("status", "left")
                if st in ("creator", "administrator", "member"):
                    continue
                if st == "restricted" and member.get("is_member"):
                    continue
                missing.append(ch)
            except TelegramAPIError:
                # If channel was deleted or bot lost access, don't deadlock users
                continue
        return missing

    def _complete_referral_if_pending(self, user_id: int) -> None:
        res = self.db.try_complete_referral(user_id)
        if res:
            ref_id, new_bal = res
            try:
                self.api.send_message(
                    ref_id,
                    f"🎁 <b>Referral Bonus!</b>\n"
                    f"A new member joined via your link. You earned <b>+{REFERRAL_REWARD} pt</b>.\n"
                    f"• <b>Current Balance:</b> {new_bal} pts",
                )
            except Exception:
                pass

    def _check_guards(
        self,
        user: Dict[str, Any],
        chat_id: int,
        cb_id: Optional[str] = None,
        msg_id: Optional[int] = None,
        is_verify_tap: bool = False,
    ) -> bool:
        uid = int(user["user_id"])
        if uid == OWNER_ID:
            return True

        if user.get("is_banned"):
            txt = "🚫 <b>Access Suspended</b>\nYour account has been restricted from using this service."
            if cb_id:
                self.api.answer_callback(cb_id, "🚫 Your account is suspended.", show_alert=True)
            else:
                self.api.send_message(chat_id, txt)
            return False

        if self.db.is_maintenance():
            txt = (
                "🔧 <b>Maintenance Mode</b>\n\n"
                "We are upgrading our cloud nodes for better performance.\n"
                "Service will resume shortly — thank you for your patience."
            )
            if cb_id:
                self.api.answer_callback(cb_id, "🔧 Bot is currently under maintenance.", show_alert=True)
                if msg_id:
                    self.api.edit_message(chat_id, msg_id, txt)
            else:
                self.api.send_message(chat_id, txt)
            return False

        missing = self._missing_channels(uid)
        if missing:
            sub_txt = (
                "📢 <b>Subscription Required</b>\n\n"
                "Please join our official channel(s) below, then tap <b>Verify Subscription</b> to continue:"
            )
            markup = forced_sub_kb(missing)
            if cb_id:
                if is_verify_tap:
                    self.api.answer_callback(cb_id, "⚠️ Please join all listed channels first!", show_alert=True)
                else:
                    self.api.answer_callback(cb_id)
                if msg_id:
                    self.api.edit_message(chat_id, msg_id, sub_txt, reply_markup=markup)
                else:
                    self.api.send_message(chat_id, sub_txt, reply_markup=markup)
            else:
                self.api.send_message(chat_id, sub_txt, reply_markup=markup)
            return False

        self._complete_referral_if_pending(uid)
        return True

    # ── Renderers ────────────────────────────────────────────────────────────
    def _home_text(self, user: Dict[str, Any]) -> str:
        uid = int(user["user_id"])
        bal = "Unlimited (Admin)" if uid == OWNER_ID else f"{user['points']} pts"
        active = len(self.db.list_user_servers(uid, active_only=True))
        maint_note = "\n<i>🔴 Note: Maintenance lock is currently ON for users.</i>" if (uid == OWNER_ID and self.db.is_maintenance()) else ""
        return (
            "☁️ <b>CloudForge</b>\n"
            "Deploy high-speed dedicated <b>RDP</b> &amp; <b>VPS</b> cloud servers with persistent static IPs.\n\n"
            f"• <b>Balance:</b> <code>{bal}</code>\n"
            f"• <b>Active Servers:</b> <code>{active}</code>"
            f"{maint_note}"
        )

    def _show_home(self, chat_id: int, user: Dict[str, Any], msg_id: Optional[int] = None) -> None:
        uid = int(user["user_id"])
        self.db.clear_state(uid)
        fresh = self.db.get_user(uid) or user
        txt = self._home_text(fresh)
        markup = main_menu_kb(uid)
        if msg_id:
            self.api.edit_message(chat_id, msg_id, txt, reply_markup=markup)
        else:
            self.api.send_message(chat_id, txt, reply_markup=markup)

    def _show_account(self, chat_id: int, user: Dict[str, Any], msg_id: Optional[int] = None) -> None:
        uid = int(user["user_id"])
        self.db.clear_state(uid)
        fresh = self.db.get_user(uid) or user
        active = len(self.db.list_user_servers(uid, active_only=True))
        total_srv = len(self.db.list_user_servers(uid, active_only=False))
        bal = "Unlimited (Owner)" if uid == OWNER_ID else f"{fresh['points']} pts"
        txt = (
            "👤 <b>My Account</b>\n\n"
            f"• <b>Account ID:</b> <code>{uid}</code>\n"
            f"• <b>Balance:</b> <code>{bal}</code>\n"
            f"• <b>Invited Users:</b> <code>{fresh['referrals_count']}</code>\n"
            f"• <b>Servers:</b> <code>{active} active</code> / <code>{total_srv} total</code>\n\n"
            f"<i>Rate: {POINTS_PER_6H} pts per {HOURS_PER_BLOCK} hours • +{REFERRAL_REWARD} pt per invite</i>"
        )
        markup = account_kb(active)
        if msg_id:
            self.api.edit_message(chat_id, msg_id, txt, reply_markup=markup)
        else:
            self.api.send_message(chat_id, txt, reply_markup=markup)

    def _show_admin_home(self, chat_id: int, msg_id: Optional[int] = None) -> None:
        self.db.clear_state(OWNER_ID)
        st = self.db.get_stats()
        maint = self.db.is_maintenance()
        m_status = "🔴 ON (Locked for users)" if maint else "🟢 OFF (Live)"
        txt = (
            "🛡 <b>Owner Control Panel</b>\n\n"
            f"• <b>Bot Status:</b> {m_status}\n"
            f"• <b>Total Users:</b> <code>{st['total_users']:,}</code> (+{st['joined_24h']} today)\n"
            f"• <b>Active Servers:</b> <code>{st['active_servers']}</code>\n"
            f"• <b>Forced Channels:</b> <code>{st['forced_channels']}</code>"
        )
        markup = admin_home_kb(maint)
        if msg_id:
            self.api.edit_message(chat_id, msg_id, txt, reply_markup=markup)
        else:
            self.api.send_message(chat_id, txt, reply_markup=markup)

    # ── Incoming Messages ────────────────────────────────────────────────────
    def _on_message(self, msg: Dict[str, Any]) -> None:
        chat = msg.get("chat", {})
        if chat.get("type") != "private":
            return
        tg_user = msg.get("from")
        if not tg_user or tg_user.get("is_bot"):
            return

        chat_id = int(chat["id"])
        uid = int(tg_user["id"])
        text = (msg.get("text") or "").strip()

        user = self._register_and_notify(tg_user, start_text=text)
        if not self._check_guards(user, chat_id):
            return

        # Commands always interrupt active input states cleanly
        if text.startswith("/start"):
            self._show_home(chat_id, user)
            return
        if text.startswith("/cancel"):
            self.db.clear_state(uid)
            self._show_home(chat_id, user)
            return
        if text.startswith("/admin") and uid == OWNER_ID:
            self._show_admin_home(chat_id)
            return

        state = user.get("state", "")
        sdata = _state_data(user)

        # Stateful user inputs
        if state == "wait_voucher":
            if not text:
                self.api.send_message(chat_id, "🎟 Please send a valid text voucher code:", reply_markup=cancel_kb("nav:account"))
                return
            ok, note, _ = self.db.redeem_voucher(uid, text)
            if ok:
                self.db.clear_state(uid)
                fresh = self.db.get_user(uid) or user
                self.api.send_message(
                    chat_id,
                    f"✅ <b>{html.escape(note)}</b>\n• <b>New Balance:</b> <code>{fresh['points']} pts</code>",
                    reply_markup=kb([[btn("⚡ Deploy Server", "nav:deploy"), btn("👤 My Account", "nav:account")]]),
                )
            else:
                self.api.send_message(chat_id, f"⚠️ {html.escape(note)}", reply_markup=cancel_kb("nav:account"))
            return

        if state == "wait_custom_dur":
            if not text.isdigit():
                self.api.send_message(
                    chat_id,
                    "⚠️ Send duration in hours as a number (e.g. <code>18</code>, <code>36</code>, <code>96</code>):",
                    reply_markup=cancel_kb(f"wiz:acc:{sdata.get('access_mode', 'remote')}"),
                )
                return
            hrs = int(text)
            if hrs < 6 or hrs > 720:
                self.api.send_message(
                    chat_id,
                    "⚠️ Duration must be between <b>6</b> and <b>720</b> hours.",
                    reply_markup=cancel_kb(f"wiz:acc:{sdata.get('access_mode', 'remote')}"),
                )
                return
            # Align up to 6-hour billing blocks so user gets full value for points spent
            aligned_hrs = ((hrs + HOURS_PER_BLOCK - 1) // HOURS_PER_BLOCK) * HOURS_PER_BLOCK
            self._proceed_to_password_step(chat_id, user, sdata, aligned_hrs, msg_id=None)
            return

        if state == "wait_srv_pw":
            if not text or len(text) < 4 or len(text) > 32 or any(c.isspace() for c in text):
                self.api.send_message(
                    chat_id,
                    "⚠️ Password must be <b>4–32 characters</b> with no spaces.\nSend another password or tap <b>Generate Strong Password</b>:",
                    reply_markup=wizard_password_kb(int(sdata.get("hours", 6))),
                )
                return
            sdata["password"] = text
            self.db.set_state(uid, "wiz_confirm", sdata)
            self._send_wizard_confirm(chat_id, uid, sdata, msg_id=None)
            return

        # Stateful Owner Admin inputs
        if uid == OWNER_ID and state.startswith("adm_"):
            self._handle_admin_state_message(chat_id, msg, text, state, sdata)
            return

        # Default fallback -> concise main menu
        self._show_home(chat_id, user)

    # ── Wizard Helpers ───────────────────────────────────────────────────────
    def _proceed_to_password_step(
        self,
        chat_id: int,
        user: Dict[str, Any],
        sdata: Dict[str, Any],
        hours: int,
        msg_id: Optional[int],
    ) -> None:
        uid = int(user["user_id"])
        cost = calc_points(hours, uid)
        fresh = self.db.get_user(uid) or user
        if uid != OWNER_ID and int(fresh["points"]) < cost:
            short = cost - int(fresh["points"])
            txt = (
                "⚠️ <b>Insufficient Balance</b>\n\n"
                f"• <b>Selected Duration:</b> {hours} hours\n"
                f"• <b>Required:</b> <code>{cost} pts</code>\n"
                f"• <b>Your Balance:</b> <code>{fresh['points']} pts</code> (need {short} more pts)"
            )
            markup = kb([
                [btn("🎁 Invite & Earn (+1 pt)", "acc:referral"), btn("🎟 Redeem Code", "acc:redeem")],
                [btn("💎 Add Points", "acc:topup")],
                [btn("‹ Back", f"wiz:acc:{sdata.get('access_mode', 'remote')}"), btn("✕ Cancel", "nav:cancel")],
            ])
            if msg_id:
                self.api.edit_message(chat_id, msg_id, txt, reply_markup=markup)
            else:
                self.api.send_message(chat_id, txt, reply_markup=markup)
            return

        sdata["hours"] = hours
        self.db.set_state(uid, "wait_srv_pw", sdata)
        prompt = (
            "🔑 <b>Step 5/5 — Set Server Password</b>\n\n"
            "Type the custom password you want for your server (4–32 chars, no spaces), "
            "or tap below to generate a strong one automatically:"
        )
        markup = wizard_password_kb(hours)
        if msg_id:
            self.api.edit_message(chat_id, msg_id, prompt, reply_markup=markup)
        else:
            self.api.send_message(chat_id, prompt, reply_markup=markup)

    def _send_wizard_confirm(self, chat_id: int, uid: int, sdata: Dict[str, Any], msg_id: Optional[int]) -> None:
        os_key = sdata["os_key"]
        os_info = OS_CATALOG[os_key]
        hrs = int(sdata["hours"])
        cost = calc_points(hrs, uid)
        cost_str = "0 pts (Admin Exempt)" if uid == OWNER_ID else f"{cost} pts"
        acc_lbl = ACCESS_LABELS.get(sdata["access_mode"], sdata["access_mode"])
        static_note = "Static IP:Port (Auto-renews seamlessly)" if hrs > 6 else "Static IP:Port"

        txt = (
            "⚡ <b>Confirm Server Deployment</b>\n\n"
            f"• <b>Category:</b> {SERVER_TYPES[sdata['srv_type']]['label']}\n"
            f"• <b>System:</b> {os_info['emoji']} {os_info['name']}\n"
            f"• <b>Hardware:</b> {os_info['specs']}\n"
            f"• <b>Access Mode:</b> {acc_lbl}\n"
            f"• <b>Duration:</b> {hrs} Hours\n"
            f"• <b>Network:</b> {static_note}\n"
            f"• <b>Username:</b> <code>{os_info['default_user']}</code>\n"
            f"• <b>Password:</b> <code>{html.escape(sdata['password'])}</code>\n\n"
            f"💳 <b>Total Cost:</b> <code>{cost_str}</code>"
        )
        markup = wizard_confirm_kb()
        if msg_id:
            self.api.edit_message(chat_id, msg_id, txt, reply_markup=markup)
        else:
            self.api.send_message(chat_id, txt, reply_markup=markup)

    # ── Callback Queries ─────────────────────────────────────────────────────
    def _on_callback(self, cq: Dict[str, Any]) -> None:
        cb_id = cq["id"]
        tg_user = cq.get("from", {})
        msg = cq.get("message") or {}
        chat_id = int(msg.get("chat", {}).get("id") or tg_user["id"])
        msg_id = msg.get("message_id")
        data = cq.get("data") or ""
        uid = int(tg_user["id"])

        user = self._register_and_notify(tg_user)
        if not self._check_guards(user, chat_id, cb_id=cb_id, msg_id=msg_id, is_verify_tap=(data == "sub:verify")):
            return

        if data == "sub:verify":
            self.api.answer_callback(cb_id, "✅ Subscription verified!")
            self._show_home(chat_id, user, msg_id=msg_id)
            return

        # Navigation & Cancel
        if data in ("nav:home", "nav:cancel"):
            toast = "Cancelled." if data == "nav:cancel" else None
            self.api.answer_callback(cb_id, toast)
            self._show_home(chat_id, user, msg_id=msg_id)
            return

        if data == "nav:account":
            self.api.answer_callback(cb_id)
            self._show_account(chat_id, user, msg_id=msg_id)
            return

        # Account Sub-screens
        if data == "acc:referral":
            self.api.answer_callback(cb_id)
            link = f"https://t.me/{self.api.username}?start=ref_{uid}"
            share_text = urllib.parse.quote("Deploy instant Cloud RDP & VPS servers on Telegram!")
            share_url = f"https://t.me/share/url?url={urllib.parse.quote(link)}&text={share_text}"
            fresh = self.db.get_user(uid) or user
            txt = (
                "🎁 <b>Referral Program</b>\n\n"
                f"Invite friends using your personal link and earn <b>+{REFERRAL_REWARD} point</b> for every verified user.\n\n"
                f"• <b>Invited Users:</b> <code>{fresh['referrals_count']}</code>\n"
                f"• <b>Your Link:</b>\n<code>{link}</code>"
            )
            markup = kb([
                [btn("📋 Copy Link", copy_text=link), btn("📤 Share Link", url=share_url)],
                [btn("‹ Back", "nav:account")],
            ])
            self.api.edit_message(chat_id, msg_id, txt, reply_markup=markup)
            return

        if data == "acc:redeem":
            self.api.answer_callback(cb_id)
            self.db.set_state(uid, "wait_voucher", {})
            self.api.edit_message(
                chat_id,
                msg_id,
                "🎟 <b>Redeem Voucher</b>\n\nSend your voucher code below (e.g. <code>CF-A1B2C3</code>):",
                reply_markup=cancel_kb("nav:account"),
            )
            return

        if data == "acc:topup":
            self.api.answer_callback(cb_id)
            txt = (
                "💎 <b>Add Points</b>\n\n"
                f"• <b>Pricing:</b> <code>{POINTS_PER_6H} pts</code> = <b>{HOURS_PER_BLOCK} Hours</b> Dedicated Server\n"
                f"• <b>Free Points:</b> Earn <code>+{REFERRAL_REWARD} pt</code> per invited friend\n\n"
                f"To purchase points or voucher packs, contact the administrator with your ID (<code>{uid}</code>):"
            )
            markup = kb([
                [btn("💬 Contact Support / Buy Points", url=f"tg://user?id={OWNER_ID}")],
                [btn("🎁 Invite Friends", "acc:referral"), btn("🎟 Redeem Code", "acc:redeem")],
                [btn("‹ Back", "nav:account")],
            ])
            self.api.edit_message(chat_id, msg_id, txt, reply_markup=markup)
            return

        # Deploy Wizard
        if data == "nav:deploy":
            self.api.answer_callback(cb_id)
            self.db.clear_state(uid)
            active_cnt = len(self.db.list_user_servers(uid, active_only=True))
            txt = (
                "⚡ <b>Step 1/5 — Choose Server Type</b>\n\n"
                "• <b>Cloud RDP:</b> Full graphical desktop (Windows, macOS, Ubuntu XFCE)\n"
                "• <b>Cloud VPS:</b> High-speed root terminal &amp; SSH server"
            )
            self.api.edit_message(chat_id, msg_id, txt, reply_markup=wizard_type_kb(active_cnt))
            return

        if data.startswith("wiz:"):
            self._handle_wizard_callback(cb_id, chat_id, msg_id, user, data)
            return

        # User Server Management
        if data.startswith("srv:"):
            self._handle_server_callback(cb_id, chat_id, msg_id, user, data, from_admin=False)
            return

        # Owner-Only Admin Panel
        if data.startswith("adm:") or data.startswith("admsrv:"):
            if uid != OWNER_ID:
                self.api.answer_callback(cb_id, "⛔ Owner access only.", show_alert=True)
                return
            if data.startswith("admsrv:"):
                self._handle_server_callback(cb_id, chat_id, msg_id, user, data, from_admin=True)
            else:
                self._handle_admin_callback(cb_id, chat_id, msg_id, data)
            return

        self.api.answer_callback(cb_id)

    # ── Deploy Wizard Callbacks ──────────────────────────────────────────────
    def _handle_wizard_callback(
        self,
        cb_id: str,
        chat_id: int,
        msg_id: int,
        user: Dict[str, Any],
        data: str,
    ) -> None:
        uid = int(user["user_id"])
        sdata = _state_data(user)

        if data.startswith("wiz:type:"):
            stype = data.split(":", 2)[2]
            if stype not in SERVER_TYPES:
                self.api.answer_callback(cb_id, "Invalid type.")
                return
            self.api.answer_callback(cb_id)
            sdata = {"srv_type": stype}
            self.db.set_state(uid, "wiz_os", sdata)
            txt = (
                f"{SERVER_TYPES[stype]['emoji']} <b>Step 2/5 — Select Operating System</b>\n\n"
                f"Category: <b>{SERVER_TYPES[stype]['label']}</b>"
            )
            self.api.edit_message(chat_id, msg_id, txt, reply_markup=wizard_os_kb(stype))
            return

        if data.startswith("wiz:os:"):
            os_key = data.split(":", 2)[2]
            if os_key not in OS_CATALOG:
                self.api.answer_callback(cb_id, "Invalid OS.")
                return
            self.api.answer_callback(cb_id)
            os_info = OS_CATALOG[os_key]
            sdata["srv_type"] = os_info["type"]
            sdata["os_key"] = os_key
            self.db.set_state(uid, "wiz_acc", sdata)
            txt = (
                f"{os_info['emoji']} <b>Step 3/5 — Select Access Method</b>\n\n"
                f"• <b>System:</b> {os_info['name']}\n"
                f"• <b>Specs:</b> {os_info['specs']}\n\n"
                "How would you like to connect to your server?"
            )
            self.api.edit_message(chat_id, msg_id, txt, reply_markup=wizard_access_kb(os_info["type"]))
            return

        if data.startswith("wiz:acc:"):
            acc = data.split(":", 2)[2]
            os_key = sdata.get("os_key", "ubuntu24")
            if os_key not in OS_CATALOG:
                self._show_home(chat_id, user, msg_id=msg_id)
                return
            self.api.answer_callback(cb_id)
            sdata["access_mode"] = acc
            self.db.set_state(uid, "wiz_dur", sdata)
            bal_str = "Unlimited" if uid == OWNER_ID else f"{user['points']} pts"
            txt = (
                "⏱ <b>Step 4/5 — Select Server Duration</b>\n\n"
                f"• <b>Rate:</b> {POINTS_PER_6H} pts per {HOURS_PER_BLOCK} hours\n"
                f"• <b>Your Balance:</b> <code>{bal_str}</code>\n\n"
                "<i>Multi-cycle servers (>6h) keep the exact same static IP:Port and files automatically.</i>"
            )
            self.api.edit_message(chat_id, msg_id, txt, reply_markup=wizard_duration_kb(uid, os_key))
            return

        if data == "wiz:back_dur":
            os_key = sdata.get("os_key", "ubuntu24")
            self.api.answer_callback(cb_id)
            self.db.set_state(uid, "wiz_dur", sdata)
            bal_str = "Unlimited" if uid == OWNER_ID else f"{user['points']} pts"
            txt = (
                "⏱ <b>Step 4/5 — Select Server Duration</b>\n\n"
                f"• <b>Rate:</b> {POINTS_PER_6H} pts per {HOURS_PER_BLOCK} hours\n"
                f"• <b>Your Balance:</b> <code>{bal_str}</code>"
            )
            self.api.edit_message(chat_id, msg_id, txt, reply_markup=wizard_duration_kb(uid, os_key))
            return

        if data == "wiz:dur:custom":
            self.api.answer_callback(cb_id)
            self.db.set_state(uid, "wait_custom_dur", sdata)
            self.api.edit_message(
                chat_id,
                msg_id,
                "✏️ <b>Custom Duration</b>\n\n"
                "Send the number of hours you want (from <b>6</b> to <b>720</b> hours, billed in 6h blocks):",
                reply_markup=cancel_kb(f"wiz:acc:{sdata.get('access_mode', 'remote')}"),
            )
            return

        if data.startswith("wiz:dur:"):
            hrs = int(data.split(":", 2)[2])
            self.api.answer_callback(cb_id)
            self._proceed_to_password_step(chat_id, user, sdata, hrs, msg_id=msg_id)
            return

        if data == "wiz:back_pw":
            self.api.answer_callback(cb_id)
            hrs = int(sdata.get("hours", 6))
            self._proceed_to_password_step(chat_id, user, sdata, hrs, msg_id=msg_id)
            return

        if data == "wiz:pw:auto":
            self.api.answer_callback(cb_id, "🎲 Strong password generated!")
            sdata["password"] = _gen_password(12)
            self.db.set_state(uid, "wiz_confirm", sdata)
            self._send_wizard_confirm(chat_id, uid, sdata, msg_id=msg_id)
            return

        if data == "wiz:launch":
            if not all(k in sdata for k in ("srv_type", "os_key", "access_mode", "hours", "password")):
                self.api.answer_callback(cb_id, "Session expired. Please start again.", show_alert=True)
                self._show_home(chat_id, user, msg_id=msg_id)
                return

            hrs = int(sdata["hours"])
            cost = calc_points(hrs, uid)
            fresh = self.db.get_user(uid) or user
            if uid != OWNER_ID and int(fresh["points"]) < cost:
                self.api.answer_callback(cb_id, "⚠️ Not enough points!", show_alert=True)
                return

            self.api.answer_callback(cb_id, "🚀 Provisioning your cloud server...")
            self.db.clear_state(uid)
            os_info = OS_CATALOG[sdata["os_key"]]
            srv = self.db.create_server(
                user_id=uid,
                srv_type=sdata["srv_type"],
                os_key=sdata["os_key"],
                access_mode=sdata["access_mode"],
                password=sdata["password"],
                hours=hrs,
                points_spent=cost,
                default_user=os_info["default_user"],
            )
            self.api.edit_message(
                chat_id,
                msg_id,
                "⏳ <b>Dispatching Dedicated Cloud Node...</b>\nAllocating static port &amp; encrypting credentials...",
            )
            dispatched = self.prov.dispatch_node(srv)
            if not dispatched:
                if cost > 0:
                    self.db.add_points(uid, cost)
                self.db.update_server(srv["server_id"], status="terminated")
                self.api.edit_message(
                    chat_id,
                    msg_id,
                    "⚠️ <b>Cloud Dispatch Temporarily Busy</b>\nYour points were not deducted. Please try again in a moment.",
                    reply_markup=main_menu_kb(uid),
                )
                return

            threading.Thread(target=self.db.sync_to_cloud, kwargs={"force": True}, daemon=True).start()
            self.api.edit_message(
                chat_id,
                msg_id,
                format_server_card(srv),
                reply_markup=server_card_kb(srv, uid, from_admin=False),
            )
            return

    # ── Server List & Actions (User + Admin) ─────────────────────────────────
    def _handle_server_callback(
        self,
        cb_id: str,
        chat_id: int,
        msg_id: int,
        user: Dict[str, Any],
        data: str,
        from_admin: bool,
    ) -> None:
        uid = int(user["user_id"])

        if data == "srv:mine":
            self.api.answer_callback(cb_id)
            servers = self.db.list_user_servers(uid, active_only=False)
            if not servers:
                txt = "🖥 <b>My Servers</b>\n\nYou have not deployed any servers yet."
                markup = kb([
                    [btn("⚡ Deploy Server", "nav:deploy")],
                    [btn("‹ Back", "nav:account")],
                ])
                self.api.edit_message(chat_id, msg_id, txt, reply_markup=markup)
                return

            rows: List[List[Dict[str, Any]]] = []
            dots = {"online": "🟢", "provisioning": "🟡", "renewing": "🔄", "terminated": "🔴", "expired": "⏰"}
            for s in servers[:10]:
                dot = dots.get(s["status"], "⚪")
                os_name = OS_CATALOG.get(s["os_key"], {}).get("name", s["os_key"])
                rem = format_time_left(s["expires_at"]) if s["status"] in ("online", "provisioning", "renewing") else s["status"].capitalize()
                rows.append([btn(f"{dot} {s['server_id']} · {os_name} ({rem})", f"srv:view:{s['server_id']}")])
            rows.append([btn("⚡ Deploy New Server", "nav:deploy")])
            rows.append([btn("‹ Back", "nav:account")])
            self.api.edit_message(
                chat_id,
                msg_id,
                "🖥 <b>My Servers</b>\nSelect a server below to view credentials or manage power:",
                reply_markup=kb(rows),
            )
            return

        parts = data.split(":", 2)
        if len(parts) < 3:
            self.api.answer_callback(cb_id)
            return
        _, action, sid = parts
        srv = self.db.get_server(sid)
        if not srv or (not from_admin and int(srv["user_id"]) != uid and uid != OWNER_ID):
            self.api.answer_callback(cb_id, "Server not found.", show_alert=True)
            return

        if action == "view":
            self.api.answer_callback(cb_id, "Updated.")
            self.api.edit_message(
                chat_id,
                msg_id,
                format_server_card(srv, include_owner=from_admin),
                reply_markup=server_card_kb(srv, uid, from_admin=from_admin),
            )
            return

        if action == "rdp":
            if not srv.get("static_ip") or not srv.get("static_port"):
                self.api.answer_callback(cb_id, "⏳ Endpoint is still provisioning.", show_alert=True)
                return
            self.api.answer_callback(cb_id, "📥 Sending .RDP file...")
            rdp_bytes = build_rdp_file(srv)
            self.api.send_document_bytes(
                chat_id,
                f"{sid}.rdp",
                rdp_bytes,
                caption=f"🖥 <b>Remote Desktop File</b> — <code>{sid}</code>\nPassword: <code>{html.escape(srv['password'])}</code>",
            )
            return

        if action == "ext":
            if srv["status"] not in ("provisioning", "online", "renewing"):
                self.api.answer_callback(cb_id, "Cannot extend an inactive server.", show_alert=True)
                return
            cost = calc_points(6, uid)
            fresh = self.db.get_user(uid) or user
            if uid != OWNER_ID and int(fresh["points"]) < cost:
                self.api.answer_callback(cb_id, f"⚠️ Need {cost} pts to extend +6h.", show_alert=True)
                return
            updated = self.db.extend_server(sid, 6, cost, uid)
            if updated:
                threading.Thread(target=self.prov.extend_node, args=(updated,), daemon=True).start()
                self.api.answer_callback(cb_id, "✅ Extended +6h! Static IP:Port preserved.", show_alert=True)
                self.api.edit_message(
                    chat_id,
                    msg_id,
                    format_server_card(updated, include_owner=from_admin),
                    reply_markup=server_card_kb(updated, uid, from_admin=from_admin),
                )
            return

        if action == "reboot":
            if srv["status"] not in ("provisioning", "online", "renewing"):
                self.api.answer_callback(cb_id, "Server is no longer active.", show_alert=True)
                return
            self.api.answer_callback(cb_id, "🔁 Rebooting server services...", show_alert=True)
            threading.Thread(target=self.prov.reboot_node, args=(srv,), daemon=True).start()
            return

        if action == "askstop":
            self.api.answer_callback(cb_id)
            self.api.edit_message(
                chat_id,
                msg_id,
                f"⚠️ <b>Terminate Server <code>{sid}</code>?</b>\n\n"
                "This will permanently shut down the cloud instance and release its static port.",
                reply_markup=confirm_terminate_kb(sid, from_admin=from_admin),
            )
            return

        if action == "stop":
            self.api.answer_callback(cb_id, "⏹ Server terminated.")
            threading.Thread(target=self.prov.terminate_node, args=(srv, "terminated"), daemon=True).start()
            srv["status"] = "terminated"
            self.api.edit_message(
                chat_id,
                msg_id,
                format_server_card(srv, include_owner=from_admin),
                reply_markup=server_card_kb(srv, uid, from_admin=from_admin),
            )
            return

    # ── Owner Admin Panel Callbacks (7320078662 ONLY) ────────────────────────
    def _handle_admin_callback(self, cb_id: str, chat_id: int, msg_id: int, data: str) -> None:
        if data in ("adm:home", "adm:cancel"):
            self.api.answer_callback(cb_id, "Cancelled." if data == "adm:cancel" else None)
            self._show_admin_home(chat_id, msg_id=msg_id)
            return

        if data == "adm:maint_toggle":
            new_state = self.db.toggle_maintenance()
            toast = "🔴 Maintenance ON — Bot locked for users." if new_state else "🟢 Maintenance OFF — Bot open to all users."
            self.api.answer_callback(cb_id, toast, show_alert=True)
            self._show_admin_home(chat_id, msg_id=msg_id)
            return

        if data == "adm:stats":
            self.api.answer_callback(cb_id)
            st = self.db.get_stats()
            txt = (
                "📊 <b>Platform Statistics</b>\n\n"
                f"👥 <b>Total Users:</b> <code>{st['total_users']:,}</code>\n"
                f"🆕 <b>Joined (24h):</b> <code>{st['joined_24h']:,}</code>\n"
                f"🚫 <b>Banned Users:</b> <code>{st['banned_users']:,}</code>\n"
                f"🎁 <b>Verified Referrals:</b> <code>{st['total_referrals']:,}</code>\n"
                f"💰 <b>User Points Held:</b> <code>{st['total_points']:,} pts</code>\n\n"
                f"🟢 <b>Active Servers:</b> <code>{st['active_servers']}</code>\n"
                f"🖥 <b>Total Servers Created:</b> <code>{st['total_servers']:,}</code>\n"
                f"📢 <b>Forced Channels:</b> <code>{st['forced_channels']}</code>"
            )
            markup = kb([[btn("🔄 Refresh", "adm:stats"), btn("‹ Back", "adm:home")]])
            self.api.edit_message(chat_id, msg_id, txt, reply_markup=markup)
            return

        # Universal Broadcast
        if data == "adm:bc":
            if self._bc_running:
                self.api.answer_callback(cb_id, "📡 A broadcast is currently running.", show_alert=True)
                return
            self.api.answer_callback(cb_id)
            self.db.clear_state(OWNER_ID)
            txt = (
                "📡 <b>Universal Broadcast</b>\n\n"
                "Supports <b>all Telegram content</b>: text, photos, videos, documents, voice, audio, GIFs, stickers, polls &amp; inline buttons.\n\n"
                "Choose delivery mode:"
            )
            self.api.edit_message(chat_id, msg_id, txt, reply_markup=admin_broadcast_mode_kb())
            return

        if data.startswith("adm:bc_mode:"):
            mode = data.split(":", 2)[2]
            self.api.answer_callback(cb_id)
            self.db.set_state(OWNER_ID, "adm_wait_bc_msg", {"mode": mode})
            mode_lbl = "Copy (Without Forward Tag)" if mode == "copy" else "Forward (With Original Tag)"
            self.api.edit_message(
                chat_id,
                msg_id,
                f"📡 <b>Send Broadcast Content</b>\n• <b>Mode:</b> {mode_lbl}\n\n"
                "Send or forward <b>any message or media</b> now:",
                reply_markup=cancel_kb("adm:bc"),
            )
            return

        if data == "adm:bc_go":
            owner = self.db.get_user(OWNER_ID) or {}
            sdata = _state_data(owner)
            if "msg_id" not in sdata:
                self.api.answer_callback(cb_id, "Broadcast expired.", show_alert=True)
                self._show_admin_home(chat_id, msg_id=msg_id)
                return
            self.api.answer_callback(cb_id, "📡 Broadcast started!")
            self.db.clear_state(OWNER_ID)
            threading.Thread(
                target=self._run_broadcast,
                args=(chat_id, msg_id, sdata),
                daemon=True,
            ).start()
            return

        if data == "adm:bc_abort":
            self._bc_cancel.set()
            self.api.answer_callback(cb_id, "⏹ Stopping broadcast...", show_alert=True)
            return

        # Points & Vouchers
        if data == "adm:pts":
            self.api.answer_callback(cb_id)
            self.db.clear_state(OWNER_ID)
            self.api.edit_message(
                chat_id,
                msg_id,
                "💰 <b>Points &amp; Vouchers Management</b>\nSelect an action below:",
                reply_markup=admin_points_kb(),
            )
            return

        if data in ("adm:pts_add", "adm:pts_sub"):
            self.api.answer_callback(cb_id)
            op = "add" if data == "adm:pts_add" else "sub"
            self.db.set_state(OWNER_ID, "adm_wait_pts_user", {"op": op})
            lbl = "Add Points" if op == "add" else "Deduct Points"
            self.api.edit_message(
                chat_id,
                msg_id,
                f"💰 <b>{lbl}</b>\n\nSend the target user's <b>Telegram ID</b> or <b>@username</b>:",
                reply_markup=cancel_kb("adm:pts"),
            )
            return

        if data == "adm:pts_all":
            self.api.answer_callback(cb_id)
            self.db.set_state(OWNER_ID, "adm_wait_pts_all", {})
            self.api.edit_message(
                chat_id,
                msg_id,
                "🎁 <b>Gift Points to All Users</b>\n\nSend the number of points to add to every active user:",
                reply_markup=cancel_kb("adm:pts"),
            )
            return

        if data == "adm:vouch_new":
            self.api.answer_callback(cb_id)
            self.db.set_state(OWNER_ID, "adm_wait_vouch_pts", {})
            self.api.edit_message(
                chat_id,
                msg_id,
                "🎟 <b>Create Voucher (Step 1/2)</b>\n\nHow many <b>points</b> should this voucher grant?",
                reply_markup=cancel_kb("adm:pts"),
            )
            return

        if data == "adm:vouch_list":
            self.api.answer_callback(cb_id)
            vouchers = self.db.list_vouchers()
            if not vouchers:
                txt = "🎟 <b>Active Vouchers</b>\n\nNo active vouchers available."
            else:
                lines = ["🎟 <b>Active Vouchers</b>\n"]
                for v in vouchers:
                    lines.append(f"• <code>{v['code']}</code> — <b>{v['points']} pts</b> ({v['used_count']}/{v['max_uses']} used)")
                txt = "\n".join(lines)
            self.api.edit_message(
                chat_id,
                msg_id,
                txt,
                reply_markup=kb([[btn("➕ Create Voucher", "adm:vouch_new")], [btn("‹ Back", "adm:pts")]]),
            )
            return

        # Users & Bans
        if data.startswith("adm:users:") or data.startswith("adm:ubanned:"):
            self.api.answer_callback(cb_id)
            banned_only = data.startswith("adm:ubanned:")
            offset = int(data.split(":", 2)[2])
            users, total = self.db.list_users(offset=offset, limit=8, banned_only=banned_only)
            prefix = "adm:ubanned" if banned_only else "adm:users"
            title = "🚫 <b>Banned Users</b>" if banned_only else "👥 <b>Users Directory</b>"
            rows: List[List[Dict[str, Any]]] = [
                [
                    btn("🔍 Search User", "adm:usearch"),
                    btn("👥 All Users" if banned_only else "🚫 Banned Only", "adm:users:0" if banned_only else "adm:ubanned:0"),
                ]
            ]
            for u in users:
                flag = "🚫 " if u["is_banned"] else "👤 "
                uname = f"@{u['username']}" if u["username"] else str(u["user_id"])
                rows.append([btn(f"{flag}{u['full_name'][:18]} ({uname}) · {u['points']}p", f"adm:uview:{u['user_id']}")])
            nav_row: List[Dict[str, Any]] = []
            if offset > 0:
                nav_row.append(btn("‹ Prev", f"{prefix}:{max(0, offset - 8)}"))
            if offset + 8 < total:
                nav_row.append(btn("Next ›", f"{prefix}:{offset + 8}"))
            if nav_row:
                rows.append(nav_row)
            rows.append([btn("‹ Back", "adm:home")])
            self.api.edit_message(chat_id, msg_id, f"{title} — Total: <code>{total}</code>", reply_markup=kb(rows))
            return

        if data == "adm:usearch":
            self.api.answer_callback(cb_id)
            self.db.set_state(OWNER_ID, "adm_wait_usearch", {})
            self.api.edit_message(
                chat_id,
                msg_id,
                "🔍 <b>Search User</b>\n\nSend a user's <b>Telegram ID</b> or <b>@username</b>:",
                reply_markup=cancel_kb("adm:users:0"),
            )
            return

        if data.startswith("adm:uview:"):
            target_id = int(data.split(":", 2)[2])
            self.api.answer_callback(cb_id)
            self._render_admin_user_card(chat_id, target_id, msg_id=msg_id)
            return

        if data.startswith("adm:ban:") or data.startswith("adm:unban:"):
            ban = data.startswith("adm:ban:")
            target_id = int(data.split(":", 2)[2])
            if target_id == OWNER_ID:
                self.api.answer_callback(cb_id, "Cannot ban Owner.", show_alert=True)
                return
            self.db.set_ban(target_id, ban)
            self.api.answer_callback(cb_id, "🚫 User banned." if ban else "✅ User unbanned.")
            self._render_admin_user_card(chat_id, target_id, msg_id=msg_id)
            return

        if data.startswith("adm:uadd:") or data.startswith("adm:usub:"):
            op = "add" if data.startswith("adm:uadd:") else "sub"
            target_id = int(data.split(":", 2)[2])
            self.api.answer_callback(cb_id)
            self.db.set_state(OWNER_ID, "adm_wait_pts_amount", {"op": op, "target_id": target_id})
            lbl = "Add Points to" if op == "add" else "Deduct Points from"
            self.api.edit_message(
                chat_id,
                msg_id,
                f"💰 <b>{lbl} <code>{target_id}</code></b>\n\nSend point amount:",
                reply_markup=cancel_kb(f"adm:uview:{target_id}"),
            )
            return

        if data.startswith("adm:udm:"):
            target_id = int(data.split(":", 2)[2])
            self.api.answer_callback(cb_id)
            self.db.set_state(OWNER_ID, "adm_wait_udm", {"target_id": target_id})
            self.api.edit_message(
                chat_id,
                msg_id,
                f"✉️ <b>Direct Message to <code>{target_id}</code></b>\n\nSend any text or media message to deliver:",
                reply_markup=cancel_kb(f"adm:uview:{target_id}"),
            )
            return

        if data.startswith("adm:usrvs:"):
            target_id = int(data.split(":", 2)[2])
            self.api.answer_callback(cb_id)
            srvs = self.db.list_user_servers(target_id, active_only=False)
            rows = []
            dots = {"online": "🟢", "provisioning": "🟡", "renewing": "🔄", "terminated": "🔴", "expired": "⏰"}
            for s in srvs[:10]:
                dot = dots.get(s["status"], "⚪")
                rows.append([btn(f"{dot} {s['server_id']} · {OS_CATALOG[s['os_key']]['name']}", f"admsrv:view:{s['server_id']}")])
            rows.append([btn("‹ Back to User", f"adm:uview:{target_id}")])
            txt = f"🖥 <b>Servers of User <code>{target_id}</code></b> ({len(srvs)})"
            self.api.edit_message(chat_id, msg_id, txt, reply_markup=kb(rows))
            return

        # All Servers Management
        if data.startswith("adm:servers:") or data.startswith("adm:srvactive:"):
            self.api.answer_callback(cb_id)
            active_only = data.startswith("adm:srvactive:")
            offset = int(data.split(":", 2)[2])
            srvs, total = self.db.list_all_servers(offset=offset, limit=8, active_only=active_only)
            prefix = "adm:srvactive" if active_only else "adm:servers"
            rows = [[
                btn("🖥 All History" if active_only else "🟢 Active Only", "adm:servers:0" if active_only else "adm:srvactive:0"),
                btn("🔄 Refresh", f"{prefix}:{offset}"),
            ]]
            dots = {"online": "🟢", "provisioning": "🟡", "renewing": "🔄", "terminated": "🔴", "expired": "⏰"}
            for s in srvs:
                dot = dots.get(s["status"], "⚪")
                os_short = OS_CATALOG.get(s["os_key"], {}).get("name", s["os_key"])[:16]
                rows.append([btn(f"{dot} {s['server_id']} · {os_short} · U:{s['user_id']}", f"admsrv:view:{s['server_id']}")])
            nav = []
            if offset > 0:
                nav.append(btn("‹ Prev", f"{prefix}:{max(0, offset - 8)}"))
            if offset + 8 < total:
                nav.append(btn("Next ›", f"{prefix}:{offset + 8}"))
            if nav:
                rows.append(nav)
            rows.append([btn("‹ Back", "adm:home")])
            hdr = "🟢 <b>Active Cloud Servers</b>" if active_only else "🖥 <b>All Cloud Servers</b>"
            self.api.edit_message(chat_id, msg_id, f"{hdr} — Total: <code>{total}</code>", reply_markup=kb(rows))
            return

        # Forced Subscription Channels
        if data == "adm:chans":
            self.api.answer_callback(cb_id)
            self.db.clear_state(OWNER_ID)
            self._render_admin_channels(chat_id, msg_id=msg_id)
            return

        if data == "adm:chan_add":
            self.api.answer_callback(cb_id)
            self.db.set_state(OWNER_ID, "adm_wait_chan", {})
            txt = (
                "📢 <b>Add Mandatory Channel</b>\n\n"
                f"1. Promote <b>@{self.api.username}</b> as an <b>Admin</b> in your channel.\n"
                "2. Send the channel's <code>@username</code>, <code>-100...</code> ID, or <b>forward any post</b> from the channel here:"
            )
            self.api.edit_message(chat_id, msg_id, txt, reply_markup=cancel_kb("adm:chans"))
            return

        if data.startswith("adm:chan_del:"):
            cid = data.split(":", 2)[2]
            self.db.remove_channel(cid)
            self.api.answer_callback(cb_id, "🗑 Channel removed.")
            self._render_admin_channels(chat_id, msg_id=msg_id)
            return

    def _render_admin_user_card(self, chat_id: int, target_id: int, msg_id: Optional[int] = None) -> None:
        u = self.db.get_user(target_id)
        if not u:
            txt = "⚠️ User not found in database."
            if msg_id:
                self.api.edit_message(chat_id, msg_id, txt, reply_markup=kb([[btn("‹ Back", "adm:users:0")]]))
            else:
                self.api.send_message(chat_id, txt, reply_markup=kb([[btn("‹ Back", "adm:users:0")]]))
            return
        uname = f"@{html.escape(u['username'])}" if u["username"] else "None"
        st_lbl = "🚫 Banned" if u["is_banned"] else "🟢 Active"
        srvs = self.db.list_user_servers(target_id)
        joined_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(u["joined_at"])))
        txt = (
            "👤 <b>User Profile</b>\n\n"
            f"• <b>Name:</b> {html.escape(u['full_name'])}\n"
            f"• <b>Username:</b> {uname}\n"
            f"• <b>ID:</b> <code>{u['user_id']}</code>\n"
            f"• <b>Language:</b> <code>{html.escape(u['lang'])}</code>\n"
            f"• <b>Status:</b> {st_lbl}\n"
            f"• <b>Balance:</b> <code>{u['points']} pts</code>\n"
            f"• <b>Referrals:</b> <code>{u['referrals_count']}</code>\n"
            f"• <b>Servers:</b> <code>{len(srvs)}</code>\n"
            f"• <b>Joined:</b> {joined_str}"
        )
        markup = admin_user_card_kb(u)
        if msg_id:
            self.api.edit_message(chat_id, msg_id, txt, reply_markup=markup)
        else:
            self.api.send_message(chat_id, txt, reply_markup=markup)

    def _render_admin_channels(self, chat_id: int, msg_id: Optional[int] = None) -> None:
        chans = self.db.list_channels()
        rows: List[List[Dict[str, Any]]] = []
        if not chans:
            txt = "📢 <b>Forced Subscription Channels</b>\n\nNo mandatory channels configured. New users can enter freely."
        else:
            lines = ["📢 <b>Forced Subscription Channels</b>\nUsers must join all channels below to use the bot:\n"]
            for ch in chans:
                lines.append(f"• <b>{html.escape(ch['title'])}</b> (<code>{ch['chat_id']}</code>)")
                rows.append([
                    btn(f"🔗 {ch['title'][:20]}", url=ch["invite_link"]),
                    btn("🗑 Remove", f"adm:chan_del:{ch['chat_id']}"),
                ])
            txt = "\n".join(lines)
        rows.append([btn("➕ Add Channel", "adm:chan_add")])
        rows.append([btn("‹ Back", "adm:home")])
        if msg_id:
            self.api.edit_message(chat_id, msg_id, txt, reply_markup=kb(rows))
        else:
            self.api.send_message(chat_id, txt, reply_markup=kb(rows))

    # ── Owner Admin Stateful Inputs ──────────────────────────────────────────
    def _handle_admin_state_message(
        self,
        chat_id: int,
        msg: Dict[str, Any],
        text: str,
        state: str,
        sdata: Dict[str, Any],
    ) -> None:
        if state == "adm_wait_bc_msg":
            mode = sdata.get("mode", "copy")
            targets = self.db.all_user_ids()
            new_data = {
                "mode": mode,
                "from_chat_id": chat_id,
                "msg_id": msg["message_id"],
                "reply_markup": msg.get("reply_markup"),
            }
            self.db.set_state(OWNER_ID, "adm_bc_confirm", new_data)
            mode_lbl = "Copy (No Forward Tag)" if mode == "copy" else "Forward (With Tag)"
            self.api.send_message(
                chat_id,
                f"📡 <b>Broadcast Ready</b>\n\n"
                f"• <b>Mode:</b> {mode_lbl}\n"
                f"• <b>Recipients:</b> <code>{len(targets):,}</code> active users\n\n"
                "Confirm sending now?",
                reply_markup=admin_broadcast_confirm_kb(len(targets)),
            )
            return

        if state == "adm_wait_usearch":
            u = self.db.find_user(text)
            if not u:
                self.api.send_message(chat_id, "⚠️ No user found matching that ID or @username.", reply_markup=cancel_kb("adm:users:0"))
                return
            self.db.clear_state(OWNER_ID)
            self._render_admin_user_card(chat_id, int(u["user_id"]))
            return

        if state == "adm_wait_pts_user":
            u = self.db.find_user(text)
            if not u:
                self.api.send_message(chat_id, "⚠️ User not found. Send a valid ID or @username:", reply_markup=cancel_kb("adm:pts"))
                return
            sdata["target_id"] = int(u["user_id"])
            self.db.set_state(OWNER_ID, "adm_wait_pts_amount", sdata)
            self.api.send_message(
                chat_id,
                f"👤 Target: <b>{html.escape(u['full_name'])}</b> (<code>{u['user_id']}</code>) • Balance: <code>{u['points']} pts</code>\n\n"
                "Send the number of points:",
                reply_markup=cancel_kb("adm:pts"),
            )
            return

        if state == "adm_wait_pts_amount":
            if not text.isdigit() or int(text) <= 0:
                self.api.send_message(chat_id, "⚠️ Send a positive integer amount:", reply_markup=only_cancel_kb("adm:cancel"))
                return
            amt = int(text)
            op = sdata.get("op", "add")
            target_id = int(sdata["target_id"])
            delta = amt if op == "add" else -amt
            new_bal = self.db.add_points(target_id, delta)
            self.db.clear_state(OWNER_ID)
            verb = "Added" if op == "add" else "Deducted"
            self.api.send_message(
                chat_id,
                f"✅ <b>{verb} {amt} pts</b> • User <code>{target_id}</code> now has <code>{new_bal} pts</code>.",
                reply_markup=kb([[btn("👤 View User", f"adm:uview:{target_id}"), btn("🛡 Admin Home", "adm:home")]]),
            )
            try:
                user_note = (
                    f"💎 <b>Balance Updated</b>\n<b>+{amt} pts</b> were added to your account!\n• <b>New Balance:</b> <code>{new_bal} pts</code>"
                    if op == "add"
                    else f"ℹ️ <b>Balance Adjusted</b>\n<b>-{amt} pts</b> were deducted from your account.\n• <b>New Balance:</b> <code>{new_bal} pts</code>"
                )
                self.api.send_message(target_id, user_note)
            except Exception:
                pass
            return

        if state == "adm_wait_pts_all":
            if not text.isdigit() or int(text) <= 0:
                self.api.send_message(chat_id, "⚠️ Send a positive integer amount:", reply_markup=cancel_kb("adm:pts"))
                return
            amt = int(text)
            count = self.db.add_points_all(amt)
            self.db.clear_state(OWNER_ID)
            self.api.send_message(
                chat_id,
                f"🎁 <b>+{amt} pts</b> credited to <code>{count:,}</code> active users!",
                reply_markup=kb([[btn("‹ Back to Points", "adm:pts"), btn("🛡 Admin Home", "adm:home")]]),
            )
            return

        if state == "adm_wait_vouch_pts":
            if not text.isdigit() or int(text) <= 0:
                self.api.send_message(chat_id, "⚠️ Send a positive number of points:", reply_markup=cancel_kb("adm:pts"))
                return
            self.db.set_state(OWNER_ID, "adm_wait_vouch_uses", {"points": int(text)})
            self.api.send_message(
                chat_id,
                "🎟 <b>Create Voucher (Step 2/2)</b>\n\nHow many <b>total users</b> can redeem this code? (e.g. <code>1</code>, <code>10</code>, <code>100</code>):",
                reply_markup=cancel_kb("adm:pts"),
            )
            return

        if state == "adm_wait_vouch_uses":
            if not text.isdigit() or int(text) <= 0:
                self.api.send_message(chat_id, "⚠️ Send a positive integer for max uses:", reply_markup=cancel_kb("adm:pts"))
                return
            pts = int(sdata["points"])
            uses = int(text)
            code = self.db.create_voucher(pts, uses)
            self.db.clear_state(OWNER_ID)
            self.api.send_message(
                chat_id,
                f"🎟 <b>Voucher Created!</b>\n\n"
                f"• <b>Code:</b> <code>{code}</code>\n"
                f"• <b>Value:</b> <code>{pts} pts</code>\n"
                f"• <b>Max Claims:</b> <code>{uses}</code>",
                reply_markup=kb([
                    [btn("📋 Copy Voucher Code", copy_text=code)],
                    [btn("‹ Back", "adm:pts")],
                ]),
            )
            return

        if state == "adm_wait_udm":
            target_id = int(sdata["target_id"])
            self.db.clear_state(OWNER_ID)
            try:
                self.api.copy_message(target_id, chat_id, msg["message_id"], reply_markup=msg.get("reply_markup"))
                self.api.send_message(
                    chat_id,
                    f"✅ Message delivered to <code>{target_id}</code>.",
                    reply_markup=kb([[btn("👤 Back to User", f"adm:uview:{target_id}")]]),
                )
            except Exception as e:
                self.api.send_message(
                    chat_id,
                    f"⚠️ Could not deliver message: <code>{html.escape(str(e))}</code>",
                    reply_markup=kb([[btn("👤 Back to User", f"adm:uview:{target_id}")]]),
                )
            return

        if state == "adm_wait_chan":
            target_chat: Optional[Any] = None
            fwd_origin = msg.get("forward_origin") or {}
            if fwd_origin.get("chat"):
                target_chat = fwd_origin["chat"]["id"]
            elif msg.get("forward_from_chat"):
                target_chat = msg["forward_from_chat"]["id"]
            elif text:
                cleaned = text.strip()
                if "t.me/" in cleaned:
                    slug = cleaned.split("t.me/", 1)[1].split("/")[0].split("?")[0].strip()
                    if slug and not slug.startswith("+"):
                        cleaned = f"@{slug}"
                if cleaned.startswith("@") or cleaned.lstrip("-").isdigit():
                    target_chat = cleaned

            if not target_chat:
                self.api.send_message(
                    chat_id,
                    "⚠️ Send a public <code>@channel</code>, a numeric <code>-100...</code> ID, or forward a post from the channel:",
                    reply_markup=cancel_kb("adm:chans"),
                )
                return

            try:
                chat_info = self.api.call("getChat", {"chat_id": target_chat})
                cid = str(chat_info["id"])
                title = chat_info.get("title") or str(target_chat)
                uname = chat_info.get("username") or ""

                bot_mem = self.api.call("getChatMember", {"chat_id": cid, "user_id": self.api.id})
                if bot_mem.get("status") != "administrator":
                    self.api.send_message(
                        chat_id,
                        f"⚠️ <b>Admin Permission Required</b>\n\n"
                        f"Please promote <b>@{self.api.username}</b> as an <b>Administrator</b> in <b>{html.escape(title)}</b> first, then try again.",
                        reply_markup=cancel_kb("adm:chans"),
                    )
                    return

                if uname:
                    inv_link = f"https://t.me/{uname}"
                else:
                    inv_link = chat_info.get("invite_link") or ""
                    if not inv_link:
                        inv_link = self.api.call("exportChatInviteLink", {"chat_id": cid})

                self.db.add_channel(cid, title, uname, inv_link)
                self.db.clear_state(OWNER_ID)
                self.api.send_message(chat_id, f"✅ Added mandatory channel: <b>{html.escape(title)}</b>")
                self._render_admin_channels(chat_id)
            except Exception as e:
                self.api.send_message(
                    chat_id,
                    f"⚠️ Could not verify channel (make sure @{self.api.username} is an Admin in the channel):\n<code>{html.escape(str(e))}</code>",
                    reply_markup=cancel_kb("adm:chans"),
                )
            return

    # ── Background Universal Broadcast Worker ────────────────────────────────
    def _run_broadcast(self, admin_chat_id: int, status_msg_id: int, sdata: Dict[str, Any]) -> None:
        self._bc_running = True
        self._bc_cancel.clear()
        try:
            targets = self.db.all_user_ids()
            total = len(targets)
            mode = sdata.get("mode", "copy")
            src_chat = sdata["from_chat_id"]
            src_msg = int(sdata["msg_id"])
            rm = sdata.get("reply_markup")

            sent = 0
            failed = 0
            last_edit = time.time()

            for idx, uid in enumerate(targets, start=1):
                if self._bc_cancel.is_set():
                    break
                try:
                    if mode == "forward":
                        self.api.forward_message(uid, src_chat, src_msg)
                    else:
                        self.api.copy_message(uid, src_chat, src_msg, reply_markup=rm)
                    sent += 1
                except Exception:
                    failed += 1

                time.sleep(0.04)  # ~25 msgs/sec safe throughput
                if time.time() - last_edit >= 3.0:
                    last_edit = time.time()
                    try:
                        self.api.edit_message(
                            admin_chat_id,
                            status_msg_id,
                            f"📡 <b>Broadcasting...</b> ({idx}/{total})\n\n"
                            f"• <b>Delivered:</b> <code>{sent}</code>\n"
                            f"• <b>Blocked / Skipped:</b> <code>{failed}</code>",
                            reply_markup=kb([[btn("⏹ Stop Broadcast", "adm:bc_abort")]]),
                        )
                    except Exception:
                        pass

            state_title = "⏹ <b>Broadcast Stopped</b>" if self._bc_cancel.is_set() else "✅ <b>Broadcast Complete</b>"
            self.api.edit_message(
                admin_chat_id,
                status_msg_id,
                f"{state_title}\n\n"
                f"• <b>Delivered:</b> <code>{sent:,}</code>\n"
                f"• <b>Blocked / Unreachable:</b> <code>{failed:,}</code>\n"
                f"• <b>Total Targeted:</b> <code>{total:,}</code>",
                reply_markup=kb([[btn("🛡 Back to Admin Panel", "adm:home")]]),
            )
        finally:
            self._bc_running = False
