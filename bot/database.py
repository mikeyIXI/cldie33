"""Thread-safe SQLite database with live AES-256 encrypted GitHub Release sync."""
import json
import os
import secrets
import sqlite3
import subprocess
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from bot.config import DB_KEY, GH_REPO, GH_TOKEN, OWNER_ID, REFERRAL_REWARD, STATE_RELEASE_TAG


class Database:
    def __init__(self, db_path: str = "/tmp/cloudbot.db"):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._dirty = False
        self._sync_lock = threading.Lock()
        self.restore_from_cloud()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _gh_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        env["GH_TOKEN"] = GH_TOKEN
        env["DB_PASS"] = DB_KEY
        return env

    def ensure_release(self, tag: str, title: str) -> None:
        if not GH_TOKEN or not GH_REPO:
            return
        env = self._gh_env()
        r = subprocess.run(["gh", "release", "view", tag, "-R", GH_REPO], env=env, capture_output=True)
        if r.returncode != 0:
            subprocess.run(
                ["gh", "release", "create", tag, "-R", GH_REPO, "--title", title, "--notes", "Encrypted CloudForge Storage"],
                env=env,
                capture_output=True,
            )

    def restore_from_cloud(self) -> bool:
        if not GH_TOKEN or not GH_REPO:
            return False
        self.ensure_release(STATE_RELEASE_TAG, "CloudForge Encrypted Database")
        env = self._gh_env()
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run(
                ["gh", "release", "download", STATE_RELEASE_TAG, "-R", GH_REPO, "-p", "cloudbot.db.enc", "-D", d],
                env=env,
                capture_output=True,
            )
            enc_path = os.path.join(d, "cloudbot.db.enc")
            if r.returncode != 0 or not os.path.exists(enc_path):
                return False
            dec = subprocess.run(
                [
                    "openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "100000",
                    "-pass", "env:DB_PASS", "-in", enc_path, "-out", self.db_path,
                ],
                env=env,
                capture_output=True,
            )
            return dec.returncode == 0 and os.path.exists(self.db_path)

    def sync_to_cloud(self, force: bool = False) -> bool:
        if not GH_TOKEN or not GH_REPO:
            return False
        if not force and not self._dirty:
            return True
        with self._sync_lock:
            if not force and not self._dirty:
                return True
            env = self._gh_env()
            with tempfile.TemporaryDirectory() as d:
                snap_db = os.path.join(d, "snap.db")
                enc_db = os.path.join(d, "cloudbot.db.enc")
                with self._lock:
                    bck = sqlite3.connect(snap_db)
                    self._conn.backup(bck)
                    bck.close()
                    self._dirty = False
                enc = subprocess.run(
                    [
                        "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "100000", "-salt",
                        "-pass", "env:DB_PASS", "-in", snap_db, "-out", enc_db,
                    ],
                    env=env,
                    capture_output=True,
                )
                if enc.returncode != 0:
                    self._dirty = True
                    return False
                up = subprocess.run(
                    ["gh", "release", "upload", STATE_RELEASE_TAG, enc_db, "--clobber", "-R", GH_REPO],
                    env=env,
                    capture_output=True,
                )
                if up.returncode != 0:
                    self._dirty = True
                    return False
                return True

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    full_name TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    lang TEXT NOT NULL DEFAULT 'en',
                    points INTEGER NOT NULL DEFAULT 0,
                    is_banned INTEGER NOT NULL DEFAULT 0,
                    referred_by INTEGER DEFAULT NULL,
                    referral_rewarded INTEGER NOT NULL DEFAULT 0,
                    referrals_count INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT '',
                    state_data TEXT NOT NULL DEFAULT '{}',
                    joined_at INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS servers (
                    server_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    srv_type TEXT NOT NULL,
                    os_key TEXT NOT NULL,
                    access_mode TEXT NOT NULL,
                    password TEXT NOT NULL,
                    hours INTEGER NOT NULL,
                    points_spent INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'provisioning',
                    static_ip TEXT NOT NULL DEFAULT '',
                    static_port INTEGER NOT NULL DEFAULT 0,
                    ssh_port INTEGER NOT NULL DEFAULT 0,
                    web_url TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    gh_run_id INTEGER NOT NULL DEFAULT 0,
                    cycle INTEGER NOT NULL DEFAULT 1,
                    notified_online INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS channels (
                    chat_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    invite_link TEXT NOT NULL,
                    added_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vouchers (
                    code TEXT PRIMARY KEY,
                    points INTEGER NOT NULL,
                    max_uses INTEGER NOT NULL DEFAULT 1,
                    used_count INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS voucher_claims (
                    code TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    claimed_at INTEGER NOT NULL,
                    PRIMARY KEY (code, user_id)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    # ── Settings ─────────────────────────────────────────────────────────────
    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()
            self._dirty = True

    def is_maintenance(self) -> bool:
        return self.get_setting("maintenance", "0") == "1"

    def toggle_maintenance(self) -> bool:
        new_val = "0" if self.is_maintenance() else "1"
        self.set_setting("maintenance", new_val)
        return new_val == "1"

    # ── Users & Referrals ────────────────────────────────────────────────────
    def upsert_user(
        self,
        user_id: int,
        full_name: str,
        username: str,
        lang: str,
        referrer_id: Optional[int] = None,
    ) -> Tuple[Dict[str, Any], bool]:
        """Returns (user_dict, is_new_user)."""
        now = int(time.time())
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE users SET full_name=?, username=?, lang=?, last_seen=? WHERE user_id=?",
                    (full_name, username or "", lang or "en", now, user_id),
                )
                self._conn.commit()
                updated = self._conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
                return dict(updated), False

            valid_ref = None
            if referrer_id and referrer_id != user_id:
                ref_row = self._conn.execute("SELECT user_id FROM users WHERE user_id=?", (referrer_id,)).fetchone()
                if ref_row:
                    valid_ref = referrer_id

            self._conn.execute(
                """
                INSERT INTO users(user_id, full_name, username, lang, points, referred_by, referral_rewarded, joined_at, last_seen)
                VALUES(?, ?, ?, ?, 0, ?, 0, ?, ?)
                """,
                (user_id, full_name, username or "", lang or "en", valid_ref, now, now),
            )
            self._conn.commit()
            self._dirty = True
            created = self._conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            return dict(created), True

    def try_complete_referral(self, user_id: int) -> Optional[Tuple[int, int]]:
        """Awards +1 pt to referrer if pending. Returns (referrer_id, new_balance) or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT referred_by, referral_rewarded FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            if not row or not row["referred_by"] or row["referral_rewarded"]:
                return None
            ref_id = int(row["referred_by"])
            self._conn.execute("UPDATE users SET referral_rewarded=1 WHERE user_id=?", (user_id,))
            self._conn.execute(
                "UPDATE users SET points = points + ?, referrals_count = referrals_count + 1 WHERE user_id=?",
                (REFERRAL_REWARD, ref_id),
            )
            self._conn.commit()
            self._dirty = True
            ref_user = self._conn.execute("SELECT points FROM users WHERE user_id=?", (ref_id,)).fetchone()
            return (ref_id, int(ref_user["points"])) if ref_user else None

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            return dict(row) if row else None

    def find_user(self, query: str) -> Optional[Dict[str, Any]]:
        q = query.strip().lstrip("@")
        with self._lock:
            if q.lstrip("-").isdigit():
                row = self._conn.execute("SELECT * FROM users WHERE user_id=?", (int(q),)).fetchone()
                if row:
                    return dict(row)
            row = self._conn.execute("SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (q,)).fetchone()
            return dict(row) if row else None

    def set_state(self, user_id: int, state: str, data: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            if data is not None:
                self._conn.execute(
                    "UPDATE users SET state=?, state_data=? WHERE user_id=?",
                    (state, json.dumps(data), user_id),
                )
            else:
                self._conn.execute("UPDATE users SET state=? WHERE user_id=?", (state, user_id))
            self._conn.commit()

    def clear_state(self, user_id: int) -> None:
        self.set_state(user_id, "", {})

    def add_points(self, user_id: int, delta: int) -> int:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET points = MAX(0, points + ?) WHERE user_id=?",
                (delta, user_id),
            )
            self._conn.commit()
            self._dirty = True
            row = self._conn.execute("SELECT points FROM users WHERE user_id=?", (user_id,)).fetchone()
            return int(row["points"]) if row else 0

    def add_points_all(self, delta: int) -> int:
        with self._lock:
            cur = self._conn.execute("UPDATE users SET points = MAX(0, points + ?) WHERE is_banned=0", (delta,))
            self._conn.commit()
            self._dirty = True
            return cur.rowcount

    def set_ban(self, user_id: int, banned: bool) -> None:
        if user_id == OWNER_ID:
            return
        with self._lock:
            self._conn.execute("UPDATE users SET is_banned=? WHERE user_id=?", (1 if banned else 0, user_id))
            self._conn.commit()
            self._dirty = True

    def list_users(self, offset: int = 0, limit: int = 8, banned_only: bool = False) -> Tuple[List[Dict[str, Any]], int]:
        where = "WHERE is_banned=1" if banned_only else ""
        with self._lock:
            total = self._conn.execute(f"SELECT COUNT(*) AS c FROM users {where}").fetchone()["c"]
            rows = self._conn.execute(
                f"SELECT * FROM users {where} ORDER BY joined_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [dict(r) for r in rows], int(total)

    def all_user_ids(self) -> List[int]:
        with self._lock:
            rows = self._conn.execute("SELECT user_id FROM users WHERE is_banned=0").fetchall()
            return [int(r["user_id"]) for r in rows]

    def total_users_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"])

    # ── Vouchers ─────────────────────────────────────────────────────────────
    def create_voucher(self, points: int, max_uses: int) -> str:
        code = "CF-" + secrets.token_hex(3).upper()
        with self._lock:
            self._conn.execute(
                "INSERT INTO vouchers(code, points, max_uses, used_count, created_at) VALUES(?, ?, ?, 0, ?)",
                (code, points, max_uses, int(time.time())),
            )
            self._conn.commit()
            self._dirty = True
        return code

    def redeem_voucher(self, user_id: int, code: str) -> Tuple[bool, str, int]:
        c = code.strip().upper()
        with self._lock:
            v = self._conn.execute("SELECT * FROM vouchers WHERE code=?", (c,)).fetchone()
            if not v:
                return False, "Invalid voucher code.", 0
            if v["used_count"] >= v["max_uses"]:
                return False, "This voucher has reached its usage limit.", 0
            claimed = self._conn.execute(
                "SELECT 1 FROM voucher_claims WHERE code=? AND user_id=?", (c, user_id)
            ).fetchone()
            if claimed:
                return False, "You have already redeemed this voucher.", 0
            pts = int(v["points"])
            self._conn.execute(
                "INSERT INTO voucher_claims(code, user_id, claimed_at) VALUES(?, ?, ?)",
                (c, user_id, int(time.time())),
            )
            self._conn.execute("UPDATE vouchers SET used_count = used_count + 1 WHERE code=?", (c,))
            self._conn.execute("UPDATE users SET points = points + ? WHERE user_id=?", (pts, user_id))
            self._conn.commit()
            self._dirty = True
            return True, f"Redeemed +{pts} pts!", pts

    def list_vouchers(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM vouchers WHERE used_count < max_uses ORDER BY created_at DESC LIMIT 15"
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Forced Subscription Channels ─────────────────────────────────────────
    def add_channel(self, chat_id: str, title: str, username: str, invite_link: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO channels(chat_id, title, username, invite_link, added_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, username=excluded.username, invite_link=excluded.invite_link
                """,
                (str(chat_id), title, username or "", invite_link, int(time.time())),
            )
            self._conn.commit()
            self._dirty = True

    def remove_channel(self, chat_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM channels WHERE chat_id=?", (str(chat_id),))
            self._conn.commit()
            self._dirty = True

    def list_channels(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM channels ORDER BY added_at ASC").fetchall()
            return [dict(r) for r in rows]

    # ── Servers ──────────────────────────────────────────────────────────────
    def allocate_static_port(self) -> int:
        with self._lock:
            used = {
                int(r["static_port"])
                for r in self._conn.execute(
                    "SELECT static_port FROM servers WHERE status IN ('provisioning', 'online', 'renewing')"
                ).fetchall()
            }
        while True:
            p = 32000 + secrets.randbelow(28000)
            if p not in used:
                return p

    def create_server(
        self,
        user_id: int,
        srv_type: str,
        os_key: str,
        access_mode: str,
        password: str,
        hours: int,
        points_spent: int,
        default_user: str,
    ) -> Dict[str, Any]:
        now = int(time.time())
        server_id = "SRV-" + secrets.token_hex(2).upper()
        while self.get_server(server_id):
            server_id = "SRV-" + secrets.token_hex(3).upper()
        port = self.allocate_static_port()
        expires_at = now + hours * 3600
        with self._lock:
            if points_spent > 0:
                self._conn.execute("UPDATE users SET points = MAX(0, points - ?) WHERE user_id=?", (points_spent, user_id))
            self._conn.execute(
                """
                INSERT INTO servers(
                    server_id, user_id, srv_type, os_key, access_mode, password, hours,
                    points_spent, status, static_port, username, created_at, expires_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'provisioning', ?, ?, ?, ?)
                """,
                (server_id, user_id, srv_type, os_key, access_mode, password, hours, points_spent, port, default_user, now, expires_at),
            )
            self._conn.commit()
            self._dirty = True
            return dict(self._conn.execute("SELECT * FROM servers WHERE server_id=?", (server_id,)).fetchone())

    def get_server(self, server_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM servers WHERE server_id=?", (server_id,)).fetchone()
            return dict(row) if row else None

    def update_server(self, server_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields.keys())
        vals = list(fields.values()) + [server_id]
        with self._lock:
            self._conn.execute(f"UPDATE servers SET {cols} WHERE server_id=?", vals)
            self._conn.commit()
            self._dirty = True

    def extend_server(self, server_id: str, add_hours: int, cost: int, payer_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            srv = self._conn.execute("SELECT * FROM servers WHERE server_id=?", (server_id,)).fetchone()
            if not srv:
                return None
            if cost > 0:
                self._conn.execute("UPDATE users SET points = MAX(0, points - ?) WHERE user_id=?", (cost, payer_id))
            new_exp = max(int(time.time()), int(srv["expires_at"])) + add_hours * 3600
            new_hrs = int(srv["hours"]) + add_hours
            self._conn.execute(
                "UPDATE servers SET expires_at=?, hours=?, points_spent = points_spent + ? WHERE server_id=?",
                (new_exp, new_hrs, cost, server_id),
            )
            self._conn.commit()
            self._dirty = True
            return dict(self._conn.execute("SELECT * FROM servers WHERE server_id=?", (server_id,)).fetchone())

    def list_user_servers(self, user_id: int, active_only: bool = False) -> List[Dict[str, Any]]:
        extra = "AND status IN ('provisioning', 'online', 'renewing')" if active_only else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM servers WHERE user_id=? {extra} ORDER BY created_at DESC LIMIT 15",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_all_servers(self, offset: int = 0, limit: int = 8, active_only: bool = False) -> Tuple[List[Dict[str, Any]], int]:
        where = "WHERE status IN ('provisioning', 'online', 'renewing')" if active_only else ""
        with self._lock:
            total = self._conn.execute(f"SELECT COUNT(*) AS c FROM servers {where}").fetchone()["c"]
            rows = self._conn.execute(
                f"SELECT * FROM servers {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [dict(r) for r in rows], int(total)

    def active_servers(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM servers WHERE status IN ('provisioning', 'online', 'renewing')"
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Stats ────────────────────────────────────────────────────────────────
    def get_stats(self) -> Dict[str, int]:
        now = int(time.time())
        day_ago = now - 86400
        with self._lock:
            total_u = self._conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            today_u = self._conn.execute("SELECT COUNT(*) AS c FROM users WHERE joined_at >= ?", (day_ago,)).fetchone()["c"]
            banned_u = self._conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_banned=1").fetchone()["c"]
            total_pts = self._conn.execute("SELECT COALESCE(SUM(points), 0) AS s FROM users").fetchone()["s"]
            total_refs = self._conn.execute("SELECT COALESCE(SUM(referrals_count), 0) AS s FROM users").fetchone()["s"]
            active_s = self._conn.execute(
                "SELECT COUNT(*) AS c FROM servers WHERE status IN ('provisioning', 'online', 'renewing')"
            ).fetchone()["c"]
            total_s = self._conn.execute("SELECT COUNT(*) AS c FROM servers").fetchone()["c"]
            chans = self._conn.execute("SELECT COUNT(*) AS c FROM channels").fetchone()["c"]
            return {
                "total_users": int(total_u),
                "joined_24h": int(today_u),
                "banned_users": int(banned_u),
                "total_points": int(total_pts),
                "total_referrals": int(total_refs),
                "active_servers": int(active_s),
                "total_servers": int(total_s),
                "forced_channels": int(chans),
            }
