"""CloudForge 24/7 Telegram Bot Engine — Entry Point & Self-Renewing Loop."""
import json
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from bot.config import BOT_TOKEN, DB_KEY, GH_REF, GH_REPO, OWNER_ID
from bot.database import Database
from bot.handlers import BotRouter
from bot.provisioner import Provisioner
from bot.tg_api import BotAPI, TelegramAPIError


def _setup_commands(api: BotAPI) -> None:
    user_cmds = [
        {"command": "start", "description": "Open CloudForge Dashboard"},
        {"command": "cancel", "description": "Cancel current action"},
    ]
    owner_cmds = [
        {"command": "start", "description": "Open CloudForge Dashboard"},
        {"command": "admin", "description": "Owner Control Panel"},
        {"command": "cancel", "description": "Cancel current action"},
    ]
    try:
        api.call("setMyCommands", {"commands": user_cmds})
        api.call(
            "setMyCommands",
            {
                "commands": owner_cmds,
                "scope": {"type": "chat", "chat_id": OWNER_ID},
            },
        )
    except Exception:
        pass


def main() -> int:
    if not BOT_TOKEN:
        print("ERROR: CLOUDBOT_TOKEN environment variable is missing.", file=sys.stderr)
        return 1

    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{BOT_TOKEN}")
        if DB_KEY:
            print(f"::add-mask::{DB_KEY}")

    api = BotAPI(BOT_TOKEN)
    me = api.me
    try:
        api.call("deleteWebhook", {"drop_pending_updates": False})
    except Exception:
        pass
    _setup_commands(api)

    db = Database()
    prov = Provisioner(db, api)
    prov.ensure_nodes_release()
    router = BotRouter(db, api, prov)

    diag = {
        "ok": True,
        "bot_id": api.id,
        "bot_username": api.username,
        "bot_name": me.get("first_name", ""),
        "owner_id": OWNER_ID,
        "started_at": int(time.time()),
    }
    with open("/tmp/bot_diag.json", "w", encoding="utf-8") as f:
        json.dump(diag, f, indent=2)
    print(f"[CloudForge] Online as @{api.username} (ID: {api.id}) | Owner: {OWNER_ID}", flush=True)

    stop_event = threading.Event()

    def _on_signal(signum: int, _frame: object) -> None:
        print(f"[CloudForge] Signal {signum} received — syncing state and shutting down cleanly...", flush=True)
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    def _sync_worker() -> None:
        while not stop_event.is_set():
            stop_event.wait(10.0)
            try:
                db.sync_to_cloud(force=False)
            except Exception:
                pass

    t_sync = threading.Thread(target=_sync_worker, daemon=True)
    t_prov = threading.Thread(target=prov.poll_loop, args=(stop_event,), daemon=True)
    t_sync.start()
    t_prov.start()

    max_runtime = int(os.environ.get("BOT_MAX_RUNTIME_SEC", str(345 * 60)))
    boot_ts = time.time()
    offset = int(db.get_setting("tg_offset", "0") or "0")
    conflict_streak = 0

    executor = ThreadPoolExecutor(max_workers=8)
    should_renew = False

    try:
        while not stop_event.is_set():
            if time.time() - boot_ts >= max_runtime:
                print("[CloudForge] Cycle runtime reached — triggering seamless self-renewal...", flush=True)
                should_renew = True
                break

            try:
                updates = api.call(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": 20,
                        "allowed_updates": ["message", "callback_query"],
                    },
                    timeout=28,
                    retries=1,
                )
                conflict_streak = 0
            except TelegramAPIError as e:
                if e.code == 409:
                    conflict_streak += 1
                    if conflict_streak >= 3:
                        print("[CloudForge] Newer polling instance detected (409) — yielding cleanly.", flush=True)
                        break
                    time.sleep(2.0)
                    continue
                time.sleep(2.0)
                continue
            except Exception:
                time.sleep(2.0)
                continue

            if updates:
                for upd in updates:
                    offset = int(upd["update_id"]) + 1
                    executor.submit(router.handle_update, upd)
                db.set_setting("tg_offset", str(offset))
    finally:
        stop_event.set()
        executor.shutdown(wait=True, cancel_futures=False)
        db.set_setting("tg_offset", str(offset))
        db.sync_to_cloud(force=True)
        if should_renew and GH_REPO:
            prov.gh_api(
                "POST",
                f"/repos/{GH_REPO}/actions/workflows/bot-engine.yml/dispatches",
                {"ref": GH_REF, "inputs": {}},
            )
        print("[CloudForge] State encrypted & synced. Bye.", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
