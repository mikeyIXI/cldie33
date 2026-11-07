"""Modern, minimal inline keyboards with Back & Cancel navigation everywhere."""
from typing import Any, Dict, List

from bot.config import ACCESS_MODES, DURATION_PRESETS, OS_CATALOG, OWNER_ID, calc_points


def btn(text: str, callback_data: str = "", url: str = "", copy_text: str = "") -> Dict[str, Any]:
    if url:
        return {"text": text, "url": url}
    if copy_text:
        return {"text": text, "copy_text": {"text": copy_text}}
    return {"text": text, "callback_data": callback_data}


def kb(rows: List[List[Dict[str, Any]]]) -> Dict[str, Any]:
    return {"inline_keyboard": rows}


# ── Main User Menu (Strictly 2 Primary Buttons + Owner Button for Admin) ─────
def main_menu_kb(user_id: int) -> Dict[str, Any]:
    rows = [
        [btn("⚡ Deploy Server", "nav:deploy"), btn("👤 My Account", "nav:account")],
    ]
    if user_id == OWNER_ID:
        rows.append([btn("🛡 Admin Panel", "adm:home")])
    return kb(rows)


def cancel_kb(back_cb: str = "nav:home") -> Dict[str, Any]:
    return kb([[btn("‹ Back", back_cb), btn("✕ Cancel", "nav:cancel")]])


def only_cancel_kb(cb: str = "nav:cancel") -> Dict[str, Any]:
    return kb([[btn("✕ Cancel", cb)]])


# ── Forced Subscription Prompt ───────────────────────────────────────────────
def forced_sub_kb(missing_channels: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = []
    for ch in missing_channels:
        rows.append([btn(f"📢 Join {ch['title']}", url=ch["invite_link"])])
    rows.append([btn("✅ Verify Subscription", "sub:verify")])
    return kb(rows)


# ── Account & Second Panel ───────────────────────────────────────────────────
def account_kb(active_count: int) -> Dict[str, Any]:
    srv_label = f"🖥 My Servers ({active_count})" if active_count else "🖥 My Servers"
    return kb([
        [btn(srv_label, "srv:mine"), btn("🎁 Invite & Earn", "acc:referral")],
        [btn("🎟 Redeem Code", "acc:redeem"), btn("💎 Add Points", "acc:topup")],
        [btn("‹ Back", "nav:home")],
    ])


# ── Deploy Server Wizard (Every step has ‹ Back and ✕ Cancel) ────────────────
def wizard_type_kb(active_count: int) -> Dict[str, Any]:
    rows = [
        [btn("🖥 Cloud RDP (GUI)", "wiz:type:rdp"), btn("⌨️ Cloud VPS (CLI)", "wiz:type:vps")],
    ]
    if active_count > 0:
        rows.append([btn(f"📦 Active Servers ({active_count})", "srv:mine")])
    rows.append([btn("‹ Back", "nav:home"), btn("✕ Cancel", "nav:cancel")])
    return kb(rows)


def wizard_os_kb(srv_type: str) -> Dict[str, Any]:
    items = [(k, v) for k, v in OS_CATALOG.items() if v["type"] == srv_type]
    rows: List[List[Dict[str, Any]]] = []
    pair: List[Dict[str, Any]] = []
    for key, info in items:
        pair.append(btn(f"{info['emoji']} {info['name']}", f"wiz:os:{key}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([btn("‹ Back", "nav:deploy"), btn("✕ Cancel", "nav:cancel")])
    return kb(rows)


def wizard_access_kb(srv_type: str) -> Dict[str, Any]:
    rows = [[btn(label, f"wiz:acc:{code}")] for code, label in ACCESS_MODES.get(srv_type, [])]
    rows.append([btn("‹ Back", f"wiz:type:{srv_type}"), btn("✕ Cancel", "nav:cancel")])
    return kb(rows)


def wizard_duration_kb(user_id: int, os_key: str) -> Dict[str, Any]:
    rows: List[List[Dict[str, Any]]] = []
    pair: List[Dict[str, Any]] = []
    for hrs in DURATION_PRESETS:
        cost = calc_points(hrs, user_id)
        cost_txt = "Free" if cost == 0 else f"{cost} pts"
        pair.append(btn(f"⏱ {hrs}h · {cost_txt}", f"wiz:dur:{hrs}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([btn("✏️ Custom Duration", "wiz:dur:custom")])
    rows.append([btn("‹ Back", f"wiz:os:{os_key}"), btn("✕ Cancel", "nav:cancel")])
    return kb(rows)


def wizard_password_kb(prev_hours: int) -> Dict[str, Any]:
    return kb([
        [btn("🎲 Generate Strong Password", "wiz:pw:auto")],
        [btn("‹ Back", "wiz:back_dur"), btn("✕ Cancel", "nav:cancel")],
    ])


def wizard_confirm_kb() -> Dict[str, Any]:
    return kb([
        [btn("🚀 Launch Server Now", "wiz:launch")],
        [btn("‹ Back", "wiz:back_pw"), btn("✕ Cancel", "nav:cancel")],
    ])


# ── Server Management Card (User & Admin) ────────────────────────────────────
def server_card_kb(srv: Dict[str, Any], viewer_id: int, from_admin: bool = False) -> Dict[str, Any]:
    sid = srv["server_id"]
    status = srv["status"]
    ext_cost = calc_points(6, viewer_id)
    ext_lbl = "➕ Extend +6h (Free)" if ext_cost == 0 else f"➕ Extend +6h ({ext_cost} pts)"
    prefix = "admsrv" if from_admin else "srv"
    back_cb = "adm:servers:0" if from_admin else "srv:mine"

    rows: List[List[Dict[str, Any]]] = []
    if status in ("provisioning", "online", "renewing"):
        if srv.get("static_ip") and srv.get("static_port"):
            addr = f"{srv['static_ip']}:{srv['static_port']}"
            rows.append([
                btn("📋 Copy IP:Port", copy_text=addr),
                btn("🔑 Copy Password", copy_text=srv["password"]),
            ])
        if srv.get("web_url"):
            rows.append([btn("🌐 Open Web Console", url=srv["web_url"])])
        if status == "online" and srv["os_key"] in ("win2022", "win2025", "ubuntu_gui"):
            rows.append([btn("📥 Download .RDP File", f"{prefix}:rdp:{sid}")])
        rows.append([btn(ext_lbl, f"{prefix}:ext:{sid}"), btn("🔄 Refresh", f"{prefix}:view:{sid}")])
        rows.append([btn("🔁 Reboot", f"{prefix}:reboot:{sid}"), btn("⏹ Terminate", f"{prefix}:askstop:{sid}")])

    if from_admin:
        rows.append([btn(f"👤 Owner ({srv['user_id']})", f"adm:uview:{srv['user_id']}")])
    rows.append([btn("‹ Back", back_cb)])
    return kb(rows)


def confirm_terminate_kb(sid: str, from_admin: bool = False) -> Dict[str, Any]:
    prefix = "admsrv" if from_admin else "srv"
    return kb([
        [btn("⏹ Yes, Terminate Server", f"{prefix}:stop:{sid}")],
        [btn("‹ Back", f"{prefix}:view:{sid}"), btn("✕ Cancel", f"{prefix}:view:{sid}")],
    ])


# ── Admin Control Panel (Owner 7320078662 ONLY) ──────────────────────────────
def admin_home_kb(maint: bool) -> Dict[str, Any]:
    m_lbl = "🔴 Maintenance: ON (Tap to Open)" if maint else "🟢 Maintenance: OFF (Tap to Lock)"
    return kb([
        [btn("📊 Statistics", "adm:stats"), btn("📡 Broadcast", "adm:bc")],
        [btn("👥 Users & Bans", "adm:users:0"), btn("💰 Points & Vouchers", "adm:pts")],
        [btn("🖥 All Servers", "adm:servers:0"), btn("📢 Forced Channels", "adm:chans")],
        [btn(m_lbl, "adm:maint_toggle")],
        [btn("‹ User Panel", "nav:home")],
    ])


def admin_broadcast_mode_kb() -> Dict[str, Any]:
    return kb([
        [btn("📋 Copy Message (No Forward Tag)", "adm:bc_mode:copy")],
        [btn("↪️ Forward Message (With Tag)", "adm:bc_mode:forward")],
        [btn("‹ Back", "adm:home"), btn("✕ Cancel", "adm:cancel")],
    ])


def admin_broadcast_confirm_kb(total_targets: int) -> Dict[str, Any]:
    return kb([
        [btn(f"✅ Send to {total_targets} Users", "adm:bc_go")],
        [btn("‹ Back", "adm:bc"), btn("✕ Cancel", "adm:cancel")],
    ])


def admin_points_kb() -> Dict[str, Any]:
    return kb([
        [btn("➕ Add Points to User", "adm:pts_add"), btn("➖ Deduct Points", "adm:pts_sub")],
        [btn("🎁 Gift All Users", "adm:pts_all"), btn("🎟 Create Voucher", "adm:vouch_new")],
        [btn("📋 Active Vouchers", "adm:vouch_list")],
        [btn("‹ Back", "adm:home")],
    ])


def admin_user_card_kb(u: Dict[str, Any]) -> Dict[str, Any]:
    uid = u["user_id"]
    ban_btn = btn("✅ Unban User", f"adm:unban:{uid}") if u["is_banned"] else btn("🚫 Ban User", f"adm:ban:{uid}")
    return kb([
        [btn("➕ Add Pts", f"adm:uadd:{uid}"), btn("➖ Deduct Pts", f"adm:usub:{uid}")],
        [ban_btn, btn("✉️ Direct Message", f"adm:udm:{uid}")],
        [btn("🖥 User Servers", f"adm:usrvs:{uid}")],
        [btn("‹ Back to Users", "adm:users:0"), btn("🛡 Admin Home", "adm:home")],
    ])
