"""CloudForge Telegram Server Creation Bot — Configuration & Catalog."""
import os

BOT_TOKEN = os.environ.get("CLOUDBOT_TOKEN", "").strip()
OWNER_ID = int(os.environ.get("CLOUDBOT_OWNER_ID", "7320078662").strip() or "7320078662")
GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()
GH_REPO = os.environ.get("GH_REPO", "").strip()
GH_REF = os.environ.get("GH_REF", "main").strip() or "main"
DB_KEY = os.environ.get("CLOUDBOT_DB_KEY", "").strip() or f"{GH_REPO}-default-aes-key"

STATE_RELEASE_TAG = "cloudbot-state"
NODES_RELEASE_TAG = "cloudbot-nodes"

POINTS_PER_6H = 5
HOURS_PER_BLOCK = 6
REFERRAL_REWARD = 1

# Server Catalog: Type -> OS Options
SERVER_TYPES = {
    "rdp": {
        "label": "Cloud RDP (GUI)",
        "emoji": "🖥",
        "desc": "Full graphical desktop environment",
    },
    "vps": {
        "label": "Cloud VPS (CLI)",
        "emoji": "⌨️",
        "desc": "High-speed terminal & root server",
    },
}

OS_CATALOG = {
    # RDP (Desktop GUI) systems
    "win2022": {
        "type": "rdp",
        "name": "Windows Server 2022",
        "emoji": "🪟",
        "runner": "windows-2022",
        "specs": "4 vCPU · 16 GB RAM · SSD",
        "default_user": "CloudAdmin",
        "proto": "Microsoft RDP",
    },
    "win2025": {
        "type": "rdp",
        "name": "Windows Server 2025",
        "emoji": "🪟",
        "runner": "windows-2025",
        "specs": "4 vCPU · 16 GB RAM · SSD",
        "default_user": "CloudAdmin",
        "proto": "Microsoft RDP",
    },
    "macos_gui": {
        "type": "rdp",
        "name": "macOS Tahoe (Apple M1)",
        "emoji": "🍎",
        "runner": "macos-latest",
        "specs": "Apple M1 · 7 GB RAM · ARM64",
        "default_user": "runner",
        "proto": "RustDesk Direct / VNC",
    },
    "ubuntu_gui": {
        "type": "rdp",
        "name": "Ubuntu 24.04 Desktop",
        "emoji": "🐧",
        "runner": "ubuntu-24.04",
        "specs": "4 vCPU · 16 GB RAM · XFCE4",
        "default_user": "cloud",
        "proto": "Microsoft RDP / Browser VNC",
    },
    # VPS (Terminal / CLI) systems
    "ubuntu24": {
        "type": "vps",
        "name": "Ubuntu 24.04 LTS",
        "emoji": "🐧",
        "runner": "ubuntu-24.04",
        "specs": "4 vCPU · 16 GB RAM · Root sudo",
        "default_user": "cloud",
        "proto": "SSH / Web Console",
    },
    "debian12": {
        "type": "vps",
        "name": "Debian 12 Bookworm",
        "emoji": "🌀",
        "runner": "ubuntu-24.04",
        "specs": "4 vCPU · 16 GB RAM · Root",
        "default_user": "root",
        "proto": "SSH / Web Console",
    },
    "macos_vps": {
        "type": "vps",
        "name": "macOS ARM64 Shell",
        "emoji": "🍎",
        "runner": "macos-latest",
        "specs": "Apple M1 · 7 GB RAM · Zsh",
        "default_user": "runner",
        "proto": "SSH / Web Console",
    },
    "win_vps": {
        "type": "vps",
        "name": "Windows PowerShell SSH",
        "emoji": "🪟",
        "runner": "windows-2022",
        "specs": "4 vCPU · 16 GB RAM · Admin",
        "default_user": "CloudAdmin",
        "proto": "OpenSSH PowerShell",
    },
}

ACCESS_MODES = {
    "rdp": [
        ("remote", "🖥 Remote App (RDP / RustDesk)"),
        ("browser", "🌐 Web Browser (HTTPS)"),
        ("ssh_rdp", "💻 Remote App + SSH Terminal"),
        ("all", "🚀 Full Suite (Remote + Web + SSH)"),
    ],
    "vps": [
        ("ssh", "💻 SSH Client (Terminal / PuTTY)"),
        ("webtty", "🌐 Web Terminal (HTTPS Browser)"),
        ("all", "🚀 Full Suite (SSH + Web Terminal)"),
    ],
}

ACCESS_LABELS = {
    "remote": "Remote Client",
    "browser": "Web Browser (HTTPS)",
    "ssh_rdp": "Remote Client + SSH",
    "ssh": "SSH Terminal",
    "webtty": "HTTPS Web Terminal",
    "all": "All Access Methods",
}

DURATION_PRESETS = [6, 12, 24, 48, 72]


def calc_points(hours: int, user_id: int) -> int:
    """Calculate point cost (5 pts per 6 hours; 0 for Admin)."""
    if user_id == OWNER_ID:
        return 0
    blocks = max(1, (hours + HOURS_PER_BLOCK - 1) // HOURS_PER_BLOCK)
    return blocks * POINTS_PER_6H
