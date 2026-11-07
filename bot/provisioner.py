"""CloudForge Provisioner — Dispatches, monitors, renews, and controls cloud nodes."""
import html
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from bot.config import (
    ACCESS_LABELS,
    DB_KEY,
    GH_REF,
    GH_REPO,
    GH_TOKEN,
    NODES_RELEASE_TAG,
    OS_CATALOG,
)
from bot.database import Database
from bot.keyboards import server_card_kb
from bot.tg_api import BotAPI


class Provisioner:
    def __init__(self, db: Database, api: BotAPI):
        self.db = db
        self.api = api
        self._release_ready = False
        self._asset_versions: Dict[str, str] = {}

    def _gh_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        env["GH_TOKEN"] = GH_TOKEN
        env["DB_PASS"] = DB_KEY
        return env

    def ensure_nodes_release(self) -> None:
        if self._release_ready or not GH_TOKEN or not GH_REPO:
            return
        self.db.ensure_release(NODES_RELEASE_TAG, "CloudForge Encrypted Node Bus")
        self._release_ready = True

    def gh_api(self, method: str, endpoint: str, payload: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        if not GH_TOKEN or not GH_REPO:
            return 0, {}
        url = f"https://api.github.com{endpoint}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {GH_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "CloudForge-Bot/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                body = json.loads(raw) if raw.strip() else {}
                return resp.status, body
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw)
            except Exception:
                body = {"message": raw}
            return e.code, body
        except Exception as e:
            return 500, {"message": str(e)}

    def upload_encrypted_json(self, asset_name: str, data: Dict[str, Any]) -> bool:
        if not GH_TOKEN or not GH_REPO:
            return False
        self.ensure_nodes_release()
        env = self._gh_env()
        with tempfile.TemporaryDirectory() as d:
            raw_path = os.path.join(d, "payload.json")
            enc_path = os.path.join(d, asset_name)
            with open(raw_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            enc = subprocess.run(
                [
                    "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "100000", "-salt",
                    "-pass", "env:DB_PASS", "-in", raw_path, "-out", enc_path,
                ],
                env=env,
                capture_output=True,
            )
            if enc.returncode != 0:
                return False
            up = subprocess.run(
                ["gh", "release", "upload", NODES_RELEASE_TAG, enc_path, "--clobber", "-R", GH_REPO],
                env=env,
                capture_output=True,
            )
            return up.returncode == 0

    def download_encrypted_json(self, asset_name: str) -> Optional[Dict[str, Any]]:
        if not GH_TOKEN or not GH_REPO:
            return None
        env = self._gh_env()
        with tempfile.TemporaryDirectory() as d:
            dl = subprocess.run(
                ["gh", "release", "download", NODES_RELEASE_TAG, "-R", GH_REPO, "-p", asset_name, "-D", d],
                env=env,
                capture_output=True,
            )
            enc_path = os.path.join(d, asset_name)
            raw_path = os.path.join(d, "payload.json")
            if dl.returncode != 0 or not os.path.exists(enc_path):
                return None
            dec = subprocess.run(
                [
                    "openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "100000",
                    "-pass", "env:DB_PASS", "-in", enc_path, "-out", raw_path,
                ],
                env=env,
                capture_output=True,
            )
            if dec.returncode != 0 or not os.path.exists(raw_path):
                return None
            try:
                with open(raw_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return None

    def _build_spec(self, srv: Dict[str, Any]) -> Dict[str, Any]:
        os_info = OS_CATALOG[srv["os_key"]]
        return {
            "server_id": srv["server_id"],
            "user_id": srv["user_id"],
            "srv_type": srv["srv_type"],
            "os_key": srv["os_key"],
            "runner": os_info["runner"],
            "access_mode": srv["access_mode"],
            "username": srv["username"],
            "password": srv["password"],
            "static_port": srv["static_port"],
            "cycle": srv["cycle"],
            "created_at": srv["created_at"],
            "expires_at": srv["expires_at"],
        }

    def dispatch_node(self, srv: Dict[str, Any]) -> bool:
        """Uploads encrypted spec and dispatches cloud-node.yml on GitHub Actions."""
        sid = srv["server_id"]
        os_info = OS_CATALOG[srv["os_key"]]
        if not self.upload_encrypted_json(f"srv-spec-{sid}.enc", self._build_spec(srv)):
            return False
        self.upload_encrypted_json(
            f"srv-cmd-{sid}.enc",
            {"cmd_id": int(time.time()), "action": "none", "expires_at": srv["expires_at"]},
        )
        code, _ = self.gh_api(
            "POST",
            f"/repos/{GH_REPO}/actions/workflows/cloud-node.yml/dispatches",
            {
                "ref": GH_REF,
                "inputs": {
                    "server_id": sid,
                    "runner_os": os_info["runner"],
                    "cycle": str(srv["cycle"]),
                },
            },
        )
        return code in (200, 201, 204)

    def extend_node(self, srv: Dict[str, Any]) -> bool:
        sid = srv["server_id"]
        self.upload_encrypted_json(f"srv-spec-{sid}.enc", self._build_spec(srv))
        return self.upload_encrypted_json(
            f"srv-cmd-{sid}.enc",
            {
                "cmd_id": int(time.time()),
                "action": "extend",
                "expires_at": srv["expires_at"],
            },
        )

    def reboot_node(self, srv: Dict[str, Any]) -> bool:
        sid = srv["server_id"]
        # If run is active, signal fast in-place service restart; if dead, re-dispatch on same static port
        run_id = int(srv.get("gh_run_id") or 0)
        if run_id > 0:
            code, run_info = self.gh_api("GET", f"/repos/{GH_REPO}/actions/runs/{run_id}")
            if code == 200 and run_info.get("status") in ("queued", "in_progress"):
                return self.upload_encrypted_json(
                    f"srv-cmd-{sid}.enc",
                    {
                        "cmd_id": int(time.time()),
                        "action": "reboot",
                        "expires_at": srv["expires_at"],
                    },
                )
        new_cycle = int(srv["cycle"]) + 1
        self.db.update_server(sid, cycle=new_cycle, status="provisioning")
        updated = self.db.get_server(sid)
        return self.dispatch_node(updated) if updated else False

    def terminate_node(self, srv: Dict[str, Any], final_status: str = "terminated") -> None:
        sid = srv["server_id"]
        self.db.update_server(sid, status=final_status)
        self.upload_encrypted_json(
            f"srv-cmd-{sid}.enc",
            {
                "cmd_id": int(time.time()),
                "action": "terminate",
                "expires_at": int(time.time()),
            },
        )
        run_id = int(srv.get("gh_run_id") or 0)
        if run_id > 0:
            self.gh_api("POST", f"/repos/{GH_REPO}/actions/runs/{run_id}/cancel")

    def poll_once(self) -> None:
        active = self.db.active_servers()
        if not active:
            return

        now = int(time.time())
        # Fetch release asset metadata in a single API call
        code, rel = self.gh_api("GET", f"/repos/{GH_REPO}/releases/tags/{NODES_RELEASE_TAG}")
        assets_map: Dict[str, str] = {}
        if code == 200 and isinstance(rel, dict):
            for a in rel.get("assets", []):
                name = a.get("name", "")
                ver = f"{a.get('id')}:{a.get('updated_at')}"
                assets_map[name] = ver

        for srv in active:
            sid = srv["server_id"]
            if now >= int(srv["expires_at"]):
                self.terminate_node(srv, final_status="expired")
                try:
                    self.api.send_message(
                        srv["user_id"],
                        f"⏰ <b>Server Expired</b>\n\n"
                        f"Your server <code>{sid}</code> ({OS_CATALOG[srv['os_key']]['name']}) has reached the end of its rental period.",
                    )
                except Exception:
                    pass
                continue

            status_asset = f"srv-status-{sid}.enc"
            ver = assets_map.get(status_asset)
            if not ver:
                continue
            if self._asset_versions.get(status_asset) == ver and srv.get("notified_online"):
                continue

            st = self.download_encrypted_json(status_asset)
            if not st:
                continue
            self._asset_versions[status_asset] = ver

            updates: Dict[str, Any] = {}
            for field in ("status", "static_ip", "static_port", "ssh_port", "web_url", "username", "gh_run_id", "cycle"):
                if field in st and st[field] is not None and st[field] != srv.get(field):
                    updates[field] = st[field]

            if updates:
                self.db.update_server(sid, **updates)
                srv = self.db.get_server(sid) or srv

            if srv["status"] == "online" and not srv.get("notified_online") and srv.get("static_ip") and srv.get("static_port"):
                self.db.update_server(sid, notified_online=1)
                srv["notified_online"] = 1
                self._notify_server_ready(srv)

    def _notify_server_ready(self, srv: Dict[str, Any]) -> None:
        uid = srv["user_id"]
        sid = srv["server_id"]
        text = "🎉 <b>Your Cloud Server is Ready!</b>\n\n" + format_server_card(srv)
        try:
            self.api.send_message(uid, text, reply_markup=server_card_kb(srv, uid, from_admin=False))
            if srv["os_key"] in ("win2022", "win2025", "ubuntu_gui"):
                rdp_bytes = build_rdp_file(srv)
                self.api.send_document_bytes(
                    uid,
                    f"{sid}.rdp",
                    rdp_bytes,
                    caption=f"🖥 <b>One-Click Remote Desktop</b> — <code>{sid}</code>\nPassword: <code>{html.escape(srv['password'])}</code>",
                )
        except Exception:
            pass

    def poll_loop(self, stop_event: Any) -> None:
        while not stop_event.is_set():
            try:
                self.poll_once()
            except Exception:
                pass
            stop_event.wait(6.0)


def build_rdp_file(srv: Dict[str, Any]) -> bytes:
    addr = f"{srv['static_ip']}:{srv['static_port']}"
    user = srv.get("username") or OS_CATALOG[srv["os_key"]]["default_user"]
    lines = [
        f"full address:s:{addr}",
        f"username:s:{user}",
        "prompt for credentials:i:1",
        "administrative session:i:1",
        "screen mode id:i:2",
        "desktopwidth:i:1920",
        "desktopheight:i:1080",
        "session bpp:i:32",
        "compression:i:1",
        "clipboard:i:1",
        "audiomode:i:0",
        "authentication level:i:0",
        "enablecredsspsupport:i:1",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def format_time_left(expires_at: int) -> str:
    rem = max(0, int(expires_at) - int(time.time()))
    if rem <= 0:
        return "Expired"
    hrs = rem // 3600
    mins = (rem % 3600) // 60
    if hrs >= 24:
        days = hrs // 24
        rem_h = hrs % 24
        return f"{days}d {rem_h}h {mins}m"
    return f"{hrs}h {mins}m"


def format_server_card(srv: Dict[str, Any], include_owner: bool = False) -> str:
    os_info = OS_CATALOG.get(srv["os_key"], {"name": srv["os_key"], "emoji": "🖥", "specs": ""})
    st = srv["status"]
    badges = {
        "provisioning": "🟡 <b>Status:</b> Provisioning (~60–90s)",
        "online": "🟢 <b>Status:</b> Online (Static Endpoint)",
        "renewing": "🔄 <b>Status:</b> Auto-Renewing (Keeping Static IP)",
        "terminated": "🔴 <b>Status:</b> Terminated",
        "expired": "⏰ <b>Status:</b> Expired",
    }
    badge = badges.get(st, f"⚪ <b>Status:</b> {html.escape(st)}")
    acc_lbl = ACCESS_LABELS.get(srv["access_mode"], srv["access_mode"])
    t_left = format_time_left(srv["expires_at"]) if st in ("provisioning", "online", "renewing") else "—"

    lines = [
        f"🖥 <b>Server</b> <code>{srv['server_id']}</code>",
        badge,
        "",
        f"• <b>System:</b> {os_info['emoji']} {os_info['name']}",
        f"• <b>Specs:</b> {os_info['specs']}",
        f"• <b>Access:</b> {acc_lbl}",
        f"• <b>Time Left:</b> {t_left}" + (f" (Cycle #{srv['cycle']})" if int(srv.get("cycle", 1)) > 1 else ""),
    ]
    if include_owner:
        lines.append(f"• <b>Owner ID:</b> <code>{srv['user_id']}</code>")

    ip = srv.get("static_ip", "")
    port = int(srv.get("static_port") or 0)
    user = html.escape(srv.get("username") or os_info.get("default_user", "root"))
    pw = html.escape(srv["password"])

    lines.append("")
    if ip and port:
        addr = f"{ip}:{port}"
        if srv["srv_type"] == "vps":
            lines.append("🔐 <b>Connection Details:</b>")
            lines.append(f"• <b>Static Host:</b> <code>{addr}</code>")
            lines.append(f"• <b>SSH Command:</b> <code>ssh {user}@{ip} -p {port}</code>")
            lines.append(f"• <b>Username:</b> <code>{user}</code>")
            lines.append(f"• <b>Password:</b> <code>{pw}</code>")
        elif srv["os_key"] == "macos_gui":
            lines.append("🔐 <b>Connection Details:</b>")
            lines.append(f"• <b>RustDesk Direct IP:</b> <code>{addr}</code>")
            lines.append(f"• <b>Username:</b> <code>{user}</code>")
            lines.append(f"• <b>Password:</b> <code>{pw}</code>")
            if srv.get("ssh_port"):
                lines.append(f"• <b>SSH:</b> <code>ssh {user}@{ip} -p {srv['ssh_port']}</code>")
        else:
            lines.append("🔐 <b>Connection Details:</b>")
            lines.append(f"• <b>Static RDP Address:</b> <code>{addr}</code>")
            lines.append(f"• <b>Username:</b> <code>{user}</code>")
            lines.append(f"• <b>Password:</b> <code>{pw}</code>")
            if srv.get("ssh_port"):
                lines.append(f"• <b>SSH:</b> <code>ssh {user}@{ip} -p {srv['ssh_port']}</code>")
        if srv.get("web_url"):
            lines.append(f"• <b>Web Console:</b> {html.escape(srv['web_url'])}")
    else:
        lines.append("🔐 <b>Configured Credentials:</b>")
        lines.append(f"• <b>Reserved Port:</b> <code>{port}</code> (Static)")
        lines.append(f"• <b>Username:</b> <code>{user}</code>")
        lines.append(f"• <b>Password:</b> <code>{pw}</code>")
        if st == "provisioning":
            lines.append("\n<i>⏳ Setting up dedicated cloud node. You will receive an instant alert once ready.</i>")

    return "\n".join(lines)
