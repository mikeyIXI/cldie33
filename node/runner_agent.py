#!/usr/bin/env python3
"""CloudForge Cross-Platform Node Agent (Linux / Windows / macOS).

Provisions RDP or VPS services, locks a persistent static IP:Port via bore,
handles live bot commands (extend, reboot, terminate), and performs zero-collision
state + static-port handovers across 6-hour GitHub Actions runner renewals.
"""
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from typing import Any, Dict, List, Optional, Tuple

SERVER_ID = os.environ.get("SERVER_ID", "").strip()
GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()
GH_REPO = os.environ.get("GH_REPO", "").strip()
GH_REF = os.environ.get("GH_REF", "main").strip() or "main"
DB_KEY = os.environ.get("CLOUDBOT_DB_KEY", "").strip() or f"{GH_REPO}-default-aes-key"
RUN_ID = int(os.environ.get("GITHUB_RUN_ID", "0") or "0")
NODES_TAG = "cloudbot-nodes"

IS_WIN = platform.system().lower().startswith("win")
IS_MAC = platform.system().lower() == "darwin"
IS_LINUX = platform.system().lower() == "linux"
ARCH = platform.machine().lower()
IS_ARM = ARCH in ("arm64", "aarch64")


def log(msg: str) -> None:
    print(f"[Node {SERVER_ID}] {msg}", flush=True)


def find_openssl() -> str:
    found = shutil.which("openssl")
    if found:
        return found
    candidates = [
        r"C:\Program Files\Git\usr\bin\openssl.exe",
        r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe",
        r"C:\Program Files\OpenSSL\bin\openssl.exe",
        "/usr/bin/openssl",
        "/opt/homebrew/bin/openssl",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return "openssl"


OPENSSL_BIN = find_openssl()


def gh_env() -> Dict[str, str]:
    env = os.environ.copy()
    env["GH_TOKEN"] = GH_TOKEN
    env["DB_PASS"] = DB_KEY
    return env


def gh_api(method: str, endpoint: str, payload: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
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
            "User-Agent": "CloudForge-Node/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 500, {}


def upload_enc_json(asset_name: str, payload: Dict[str, Any]) -> bool:
    env = gh_env()
    with tempfile.TemporaryDirectory() as d:
        raw_p = os.path.join(d, "data.json")
        enc_p = os.path.join(d, asset_name)
        with open(raw_p, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        r = subprocess.run(
            [
                OPENSSL_BIN, "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "100000", "-salt",
                "-pass", "env:DB_PASS", "-in", raw_p, "-out", enc_p,
            ],
            env=env,
            capture_output=True,
        )
        if r.returncode != 0:
            return False
        up = subprocess.run(
            ["gh", "release", "upload", NODES_TAG, enc_p, "--clobber", "-R", GH_REPO],
            env=env,
            capture_output=True,
        )
        return up.returncode == 0


def download_enc_json(asset_name: str) -> Optional[Dict[str, Any]]:
    env = gh_env()
    with tempfile.TemporaryDirectory() as d:
        dl = subprocess.run(
            ["gh", "release", "download", NODES_TAG, "-R", GH_REPO, "-p", asset_name, "-D", d],
            env=env,
            capture_output=True,
        )
        enc_p = os.path.join(d, asset_name)
        raw_p = os.path.join(d, "data.json")
        if dl.returncode != 0 or not os.path.exists(enc_p):
            return None
        dec = subprocess.run(
            [
                OPENSSL_BIN, "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "100000",
                "-pass", "env:DB_PASS", "-in", enc_p, "-out", raw_p,
            ],
            env=env,
            capture_output=True,
        )
        if dec.returncode != 0 or not os.path.exists(raw_p):
            return None
        try:
            with open(raw_p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None


def download_file(url: str, dest: str, retries: int = 4) -> bool:
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CloudForge-Node/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
                shutil.copyfileobj(resp, out)
            return True
        except Exception:
            time.sleep(2 * (i + 1))
    return False


# ── Binary Installers (bore, cloudflared, ttyd) ──────────────────────────────
def install_bore(tools_dir: str) -> str:
    exe = os.path.join(tools_dir, "bore.exe" if IS_WIN else "bore")
    if os.path.exists(exe):
        return exe
    if IS_WIN:
        url = "https://github.com/ekzhang/bore/releases/download/v0.5.2/bore-v0.5.2-x86_64-pc-windows-msvc.zip"
        arc = os.path.join(tools_dir, "bore.zip")
        download_file(url, arc)
        with zipfile.ZipFile(arc, "r") as zf:
            zf.extractall(tools_dir)
    elif IS_MAC:
        arch_tag = "aarch64" if IS_ARM else "x86_64"
        url = f"https://github.com/ekzhang/bore/releases/download/v0.5.2/bore-v0.5.2-{arch_tag}-apple-darwin.tar.gz"
        arc = os.path.join(tools_dir, "bore.tar.gz")
        download_file(url, arc)
        with tarfile.open(arc, "r:gz") as tf:
            tf.extractall(tools_dir)
    else:
        arch_tag = "aarch64" if IS_ARM else "x86_64"
        url = f"https://github.com/ekzhang/bore/releases/download/v0.5.2/bore-v0.5.2-{arch_tag}-unknown-linux-musl.tar.gz"
        arc = os.path.join(tools_dir, "bore.tar.gz")
        download_file(url, arc)
        with tarfile.open(arc, "r:gz") as tf:
            tf.extractall(tools_dir)
    if not IS_WIN and os.path.exists(exe):
        os.chmod(exe, 0o755)
    return exe


def install_cloudflared(tools_dir: str) -> str:
    exe = os.path.join(tools_dir, "cloudflared.exe" if IS_WIN else "cloudflared")
    if os.path.exists(exe):
        return exe
    if IS_WIN:
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
        download_file(url, exe)
    elif IS_MAC:
        arch_tag = "arm64" if IS_ARM else "amd64"
        url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-{arch_tag}.tgz"
        arc = os.path.join(tools_dir, "cf.tgz")
        download_file(url, arc)
        with tarfile.open(arc, "r:gz") as tf:
            tf.extractall(tools_dir)
    else:
        arch_tag = "arm64" if IS_ARM else "amd64"
        url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch_tag}"
        download_file(url, exe)
    if not IS_WIN and os.path.exists(exe):
        os.chmod(exe, 0o755)
    return exe


def install_ttyd(tools_dir: str) -> str:
    if IS_MAC:
        found = shutil.which("ttyd")
        if found:
            return found
        subprocess.run(["brew", "install", "--quiet", "ttyd"], capture_output=True)
        return shutil.which("ttyd") or "ttyd"
    exe = os.path.join(tools_dir, "ttyd.exe" if IS_WIN else "ttyd")
    if os.path.exists(exe):
        return exe
    if IS_WIN:
        download_file("https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.win32.exe", exe)
    else:
        arch_tag = "aarch64" if IS_ARM else "x86_64"
        download_file(f"https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.{arch_tag}", exe)
        if os.path.exists(exe):
            os.chmod(exe, 0o755)
    return exe


# ── OS Provisioners ──────────────────────────────────────────────────────────
def provision_linux(spec: Dict[str, Any], tools_dir: str) -> Tuple[int, Optional[int], Optional[int]]:
    """Returns (primary_local_port, secondary_ssh_local_port, web_local_port)."""
    user = spec["username"]
    pw = spec["password"]
    os_key = spec["os_key"]
    acc = spec["access_mode"]

    if user != "root":
        r = subprocess.run(["id", user], capture_output=True)
        if r.returncode != 0:
            subprocess.run(["sudo", "useradd", "-m", "-s", "/bin/bash", user], check=False)
        sudoers_line = f"{user} ALL=(ALL) NOPASSWD:ALL\n"
        p = subprocess.run(["sudo", "tee", "/etc/sudoers.d/99-cloudforge"], input=sudoers_line.encode(), capture_output=True)
        subprocess.run(["sudo", "chmod", "440", "/etc/sudoers.d/99-cloudforge"], check=False)

    chpw_input = f"root:{pw}\n" + (f"{user}:{pw}\n" if user != "root" else "")
    subprocess.run(["sudo", "chpasswd"], input=chpw_input.encode("utf-8"), check=False)

    sshd_cfg = (
        "PasswordAuthentication yes\n"
        "PermitRootLogin yes\n"
        "KbdInteractiveAuthentication yes\n"
        "UsePAM yes\n"
    )
    subprocess.run(["sudo", "mkdir", "-p", "/etc/ssh/sshd_config.d"], check=False)
    subprocess.run(["sudo", "tee", "/etc/ssh/sshd_config.d/99-cloudforge.conf"], input=sshd_cfg.encode(), capture_output=True)
    subprocess.run(["sudo", "systemctl", "enable", "ssh"], capture_output=True)
    if subprocess.run(["sudo", "systemctl", "restart", "ssh"], capture_output=True).returncode != 0:
        subprocess.run(["sudo", "systemctl", "restart", "sshd"], capture_output=True)

    if os_key == "ubuntu_gui":
        env = os.environ.copy()
        env["DEBIAN_FRONTEND"] = "noninteractive"
        subprocess.run(["sudo", "-E", "apt-get", "update", "-qq"], env=env, capture_output=True)
        pkgs = ["xfce4", "xfce4-goodies", "dbus-x11", "xrdp", "xorgxrdp"]
        if acc in ("browser", "all"):
            pkgs += ["tigervnc-standalone-server", "novnc", "websockify"]
        subprocess.run(["sudo", "-E", "apt-get", "install", "-y", "-qq"] + pkgs, env=env, capture_output=True)
        subprocess.run(["sudo", "usermod", "-aG", "ssl-cert", "xrdp"], capture_output=True)
        home_dir = f"/home/{user}" if user != "root" else "/root"
        xsess = os.path.join(home_dir, ".xsession")
        subprocess.run(["sudo", "tee", xsess], input=b"xfce4-session\n", capture_output=True)
        subprocess.run(["sudo", "chown", f"{user}:{user}", xsess], capture_output=True)
        subprocess.run(["sudo", "systemctl", "enable", "xrdp"], capture_output=True)
        subprocess.run(["sudo", "systemctl", "restart", "xrdp"], capture_output=True)

        web_port = None
        if acc in ("browser", "all"):
            vnc_dir = os.path.join(home_dir, ".vnc")
            subprocess.run(["sudo", "-u", user, "mkdir", "-p", vnc_dir], capture_output=True)
            p_res = subprocess.run(["vncpasswd", "-f"], input=(pw + "\n").encode(), capture_output=True)
            if p_res.stdout:
                pw_file = os.path.join(vnc_dir, "passwd")
                subprocess.run(["sudo", "tee", pw_file], input=p_res.stdout, capture_output=True)
                subprocess.run(["sudo", "chown", f"{user}:{user}", pw_file], capture_output=True)
                subprocess.run(["sudo", "chmod", "600", pw_file], capture_output=True)
            xstartup = os.path.join(vnc_dir, "xstartup")
            xstart_sh = b"#!/bin/sh\nunset SESSION_MANAGER\nunset DBUS_SESSION_BUS_ADDRESS\nexec startxfce4\n"
            subprocess.run(["sudo", "tee", xstartup], input=xstart_sh, capture_output=True)
            subprocess.run(["sudo", "chown", f"{user}:{user}", xstartup], capture_output=True)
            subprocess.run(["sudo", "chmod", "755", xstartup], capture_output=True)
            subprocess.run(["sudo", "-u", user, "vncserver", ":1", "-geometry", "1920x1080", "-depth", "24", "-localhost", "yes"], capture_output=True)
            subprocess.Popen(["websockify", "--web=/usr/share/novnc/", "6080", "127.0.0.1:5901"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            web_port = 6080

        sec_ssh = 22 if acc in ("ssh_rdp", "all") else None
        return 3389, sec_ssh, web_port

    # Linux VPS (ubuntu24 / debian12)
    web_port = None
    if acc in ("webtty", "all"):
        ttyd_bin = install_ttyd(tools_dir)
        shell_cmd = ["sudo", "-u", user, "-i"] if user != "root" else ["/bin/bash", "-l"]
        subprocess.Popen(
            [ttyd_bin, "-W", "-p", "7681", "-c", f"{user}:{pw}"] + shell_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        web_port = 7681
    return 22, None, web_port


def provision_windows(spec: Dict[str, Any], tools_dir: str) -> Tuple[int, Optional[int], Optional[int]]:
    user = spec["username"]
    pw = spec["password"]
    os_key = spec["os_key"]
    acc = spec["access_mode"]

    ps_script = r"""
$ErrorActionPreference = 'Continue'
secedit /export /cfg C:\secpol.cfg | Out-Null
$cfg = Get-Content C:\secpol.cfg -Encoding Unicode
$cfg = $cfg -replace "PasswordComplexity = 1","PasswordComplexity = 0" -replace "MinimumPasswordLength = \d+","MinimumPasswordLength = 0"
$cfg | Set-Content C:\secpol.cfg -Encoding Unicode
secedit /configure /db C:\windows\security\local.sdb /cfg C:\secpol.cfg /areas SECURITYPOLICY | Out-Null
Remove-Item -Force C:\secpol.cfg -ErrorAction SilentlyContinue

$u = $env:CF_USER_NAME
$p = $env:CF_USER_PASS
$secPass = ConvertTo-SecureString $p -AsPlainText -Force

if (Get-LocalUser -Name $u -ErrorAction SilentlyContinue) {
    Set-LocalUser -Name $u -Password $secPass -PasswordNeverExpires $true
    Enable-LocalUser -Name $u
} else {
    New-LocalUser -Name $u -Password $secPass -PasswordNeverExpires -AccountNeverExpires | Out-Null
}
Add-LocalGroupMember -Group "Administrators" -Member $u -ErrorAction SilentlyContinue
Add-LocalGroupMember -Group "Remote Desktop Users" -Member $u -ErrorAction SilentlyContinue

if ($env:CF_ENABLE_RDP -eq "1") {
    Set-ItemProperty -Path "HKLM:\System\CurrentControlSet\Control\Terminal Server" -Name "fDenyTSConnections" -Value 0 -Force
    Set-ItemProperty -Path "HKLM:\System\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp" -Name "UserAuthentication" -Value 0 -Force
    Set-ItemProperty -Path "HKLM:\System\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp" -Name "SecurityLayer" -Value 1 -Force
    New-Item -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services" -Force -ErrorAction SilentlyContinue | Out-Null
    Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services" -Name "fDisableUDP" -Value 1 -Force -ErrorAction SilentlyContinue
    Set-Service -Name "TermService" -StartupType Automatic
    Restart-Service -Name "TermService" -Force
    netsh advfirewall firewall add rule name="CloudForge-RDP" dir=in action=allow protocol=TCP localport=3389 | Out-Null
}

if ($env:CF_ENABLE_SSH -eq "1") {
    Set-Service -Name "sshd" -StartupType Automatic -ErrorAction SilentlyContinue
    Start-Service -Name "sshd" -ErrorAction SilentlyContinue
    $sshdCfg = "C:\ProgramData\ssh\sshd_config"
    if (Test-Path $sshdCfg) {
        $c = Get-Content $sshdCfg
        $c = $c -replace "^#?PasswordAuthentication\s+.*","PasswordAuthentication yes"
        $c | Set-Content $sshdCfg
    }
    New-Item -Path "HKLM:\SOFTWARE\OpenSSH" -Force -ErrorAction SilentlyContinue | Out-Null
    New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force -ErrorAction SilentlyContinue | Out-Null
    Restart-Service -Name "sshd" -Force -ErrorAction SilentlyContinue
    netsh advfirewall firewall add rule name="CloudForge-SSH" dir=in action=allow protocol=TCP localport=22 | Out-Null
}
New-Item -ItemType Directory -Force -Path "C:\Workspace" | Out-Null
"""
    env = os.environ.copy()
    env["CF_USER_NAME"] = user
    env["CF_USER_PASS"] = pw
    env["CF_ENABLE_RDP"] = "1" if os_key in ("win2022", "win2025") else "0"
    env["CF_ENABLE_SSH"] = "1" if (os_key == "win_vps" or acc in ("ssh_rdp", "all")) else "0"

    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as f:
        f.write(ps_script)
        ps_path = f.name
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_path], env=env, capture_output=True)
    finally:
        try:
            os.remove(ps_path)
        except Exception:
            pass

    web_port = None
    if acc in ("webtty", "browser", "all"):
        ttyd_bin = install_ttyd(tools_dir)
        if os.path.exists(ttyd_bin):
            subprocess.Popen(
                [ttyd_bin, "-W", "-p", "7681", "-c", f"{user}:{pw}", "powershell.exe"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            web_port = 7681

    if os_key == "win_vps":
        return 22, None, web_port
    sec_ssh = 22 if acc in ("ssh_rdp", "all") else None
    return 3389, sec_ssh, web_port


def provision_macos(spec: Dict[str, Any], tools_dir: str) -> Tuple[int, Optional[int], Optional[int]]:
    user = spec["username"]
    pw = spec["password"]
    os_key = spec["os_key"]
    acc = spec["access_mode"]

    subprocess.run(["sudo", "sysadminctl", "-resetPasswordFor", user, "-newPassword", pw], capture_output=True)
    subprocess.run(["sudo", "systemsetup", "-setcomputersleep", "Never", "-setdisplaysleep", "Never"], capture_output=True)
    subprocess.run(["sudo", "pmset", "-a", "displaysleep", "0", "sleep", "0", "disksleep", "0"], capture_output=True)
    subprocess.Popen(["caffeinate", "-dimsu"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Enable SSH daemon
    subprocess.run(["sudo", "mkdir", "-p", "/etc/ssh/sshd_config.d"], capture_output=True)
    subprocess.run(
        ["sudo", "tee", "/etc/ssh/sshd_config.d/99-cloudforge.conf"],
        input=b"PasswordAuthentication yes\nKbdInteractiveAuthentication yes\n",
        capture_output=True,
    )
    subprocess.run(["sudo", "launchctl", "load", "-w", "/System/Library/LaunchDaemons/ssh.plist"], capture_output=True)

    if os_key == "macos_vps":
        web_port = None
        if acc in ("webtty", "all"):
            ttyd_bin = install_ttyd(tools_dir)
            subprocess.Popen(
                [ttyd_bin, "-W", "-p", "7681", "-c", f"{user}:{pw}", "login", "-f", user],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            web_port = 7681
        return 22, None, web_port

    # macOS GUI Desktop (RustDesk Direct IP on 21118 + TCC permissions)
    mac_setup_sh = r"""
set -e
CONSOLE_USER="$CF_USER_NAME"
MAC_PASSWORD="$CF_USER_PASS"
ARCH_TAG=$( [ "$(uname -m)" = "arm64" ] && echo "aarch64" || echo "x86_64" )
curl -fsSL -o /tmp/rd.dmg "https://github.com/rustdesk/rustdesk/releases/download/1.5.1/rustdesk-1.5.1-${ARCH_TAG}.dmg"
hdiutil attach -nobrowse -quiet /tmp/rd.dmg -mountpoint /tmp/rdmnt
sudo cp -R /tmp/rdmnt/RustDesk.app /Applications/
hdiutil detach -quiet /tmp/rdmnt || true
sudo xattr -dr com.apple.quarantine /Applications/RustDesk.app || true
sudo chown -R root:wheel /Applications/RustDesk.app || true
BID=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' /Applications/RustDesk.app/Contents/Info.plist)
UIDN=$(id -u "$CONSOLE_USER")

COLS="service,client,client_type,auth_value,auth_reason,auth_version,csreq,policy_id,indirect_object_identifier_type,indirect_object_identifier,indirect_object_code_identity,flags,last_modified"
NOW=$(date +%s)
for DB in "/Library/Application Support/com.apple.TCC/TCC.db" "/Users/$CONSOLE_USER/Library/Application Support/com.apple.TCC/TCC.db"; do
  [ -f "$DB" ] || continue
  for BUNDLE in "$BID" "com.carriez.rustdesk" "com.carriez.RustDesk"; do
    for SVC in kTCCServiceScreenCapture kTCCServiceAccessibility kTCCServicePostEvent kTCCServiceListenEvent; do
      sudo sqlite3 "$DB" "INSERT OR REPLACE INTO access($COLS) VALUES('$SVC','$BUNDLE',0,2,4,1,NULL,NULL,0,'UNUSED',NULL,0,$NOW);" 2>/dev/null || true
    done
  done
  for BIN in "/Applications/RustDesk.app/Contents/MacOS/RustDesk" "/Applications/RustDesk.app/Contents/MacOS/service" "/opt/hca/hosted-compute-agent" "/bin/bash" "/usr/bin/python3"; do
    for SVC in kTCCServiceScreenCapture kTCCServiceAccessibility kTCCServicePostEvent kTCCServiceListenEvent; do
      sudo sqlite3 "$DB" "INSERT OR REPLACE INTO access($COLS) VALUES('$SVC','$BIN',1,2,4,1,NULL,NULL,0,'UNUSED',NULL,0,$NOW);" 2>/dev/null || true
    done
  done
done
sudo killall tccd cfprefsd 2>/dev/null || true

USER_RD_CFG="/Users/$CONSOLE_USER/Library/Preferences/com.carriez.RustDesk"
ROOT_RD_CFG="/var/root/Library/Preferences/com.carriez.RustDesk"
sudo mkdir -p "$USER_RD_CFG" "$ROOT_RD_CFG"
printf "password = '%s'\n" "$MAC_PASSWORD" | sudo tee "$USER_RD_CFG/RustDesk.toml" >/dev/null
sudo cp -f "$USER_RD_CFG/RustDesk.toml" "$ROOT_RD_CFG/RustDesk.toml" || true
for CFG in "$USER_RD_CFG" "$ROOT_RD_CFG"; do
  sudo tee "$CFG/RustDesk2.toml" >/dev/null <<'TOML'
[options]
direct-server = 'Y'
direct-access-port = '21118'
access-mode = 'full'
enable-keyboard = 'Y'
enable-clipboard = 'Y'
enable-file-transfer = 'Y'
verification-method = 'use-permanent-password'
approve-mode = 'password'
TOML
done
sudo chown -R "$CONSOLE_USER":staff "$USER_RD_CFG" || true

sudo tee /Library/LaunchAgents/com.carriez.RustDesk_server.plist >/dev/null <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.carriez.RustDesk_server</string>
    <key>KeepAlive</key><true/>
    <key>LimitLoadToSessionType</key><array><string>Aqua</string></array>
    <key>ProcessType</key><string>Interactive</string>
    <key>ProgramArguments</key><array>
        <string>/Applications/RustDesk.app/Contents/MacOS/RustDesk</string>
        <string>--server</string>
    </array>
    <key>RunAtLoad</key><true/>
</dict>
</plist>
PLIST
sudo chown root:wheel /Library/LaunchAgents/com.carriez.RustDesk_server.plist
sudo chmod 644 /Library/LaunchAgents/com.carriez.RustDesk_server.plist
sudo launchctl bootstrap "gui/${UIDN}" /Library/LaunchAgents/com.carriez.RustDesk_server.plist 2>/dev/null || true
sudo launchctl kickstart -k "gui/${UIDN}/com.carriez.RustDesk_server" 2>/dev/null || true
sudo -u "$CONSOLE_USER" /Applications/RustDesk.app/Contents/MacOS/RustDesk --password "$MAC_PASSWORD" 2>/dev/null || true
"""
    env = os.environ.copy()
    env["CF_USER_NAME"] = user
    env["CF_USER_PASS"] = pw
    subprocess.run(["/bin/bash", "-c", mac_setup_sh], env=env, capture_output=True)
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", 21118)) == 0:
                break
        time.sleep(1.5)

    sec_ssh = 22 if acc in ("ssh_rdp", "all") else None
    web_port = None
    if acc in ("browser", "all"):
        ttyd_bin = install_ttyd(tools_dir)
        subprocess.Popen(
            [ttyd_bin, "-W", "-p", "7681", "-c", f"{user}:{pw}", "login", "-f", user],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        web_port = 7681
    return 21118, sec_ssh, web_port


# ── Workspace Persistence Across >6h Renewals ────────────────────────────────
def _workspace_paths(user: str) -> List[str]:
    if IS_WIN:
        base = rf"C:\Users\{user}"
        return [p for p in [os.path.join(base, "Desktop"), os.path.join(base, "Documents"), r"C:\Workspace"] if os.path.exists(p)]
    if IS_MAC:
        base = f"/Users/{user}"
        return [p for p in [os.path.join(base, "Desktop"), os.path.join(base, "Documents"), os.path.join(base, "Downloads")] if os.path.exists(p)]
    base = "/root" if user == "root" else f"/home/{user}"
    return [base] if os.path.exists(base) else []


def save_workspace(user: str) -> None:
    paths = _workspace_paths(user)
    if not paths:
        return
    env = gh_env()
    asset_name = f"srv-data-{SERVER_ID}.enc"
    with tempfile.TemporaryDirectory() as d:
        tar_p = os.path.join(d, "ws.tar.gz")
        enc_p = os.path.join(d, asset_name)
        try:
            with tarfile.open(tar_p, "w:gz") as tf:
                for p in paths:
                    rel_arc = os.path.basename(p.rstrip("/\\"))
                    tf.add(
                        p,
                        arcname=rel_arc,
                        filter=lambda ti: None if (".cache" in ti.name or "AppData" in ti.name or ti.size > 250 * 1024 * 1024) else ti,
                    )
            if os.path.getsize(tar_p) > 450 * 1024 * 1024:
                return
            r = subprocess.run(
                [
                    OPENSSL_BIN, "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "100000", "-salt",
                    "-pass", "env:DB_PASS", "-in", tar_p, "-out", enc_p,
                ],
                env=env,
                capture_output=True,
            )
            if r.returncode == 0:
                subprocess.run(["gh", "release", "upload", NODES_TAG, enc_p, "--clobber", "-R", GH_REPO], env=env, capture_output=True)
        except Exception as e:
            log(f"Workspace backup warning: {e}")


def restore_workspace(user: str) -> None:
    env = gh_env()
    asset_name = f"srv-data-{SERVER_ID}.enc"
    with tempfile.TemporaryDirectory() as d:
        dl = subprocess.run(
            ["gh", "release", "download", NODES_TAG, "-R", GH_REPO, "-p", asset_name, "-D", d],
            env=env,
            capture_output=True,
        )
        enc_p = os.path.join(d, asset_name)
        tar_p = os.path.join(d, "ws.tar.gz")
        if dl.returncode != 0 or not os.path.exists(enc_p):
            return
        dec = subprocess.run(
            [
                OPENSSL_BIN, "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "100000",
                "-pass", "env:DB_PASS", "-in", enc_p, "-out", tar_p,
            ],
            env=env,
            capture_output=True,
        )
        if dec.returncode != 0 or not os.path.exists(tar_p):
            return
        try:
            dest_root = (
                rf"C:\Users\{user}" if IS_WIN
                else (f"/Users/{user}" if IS_MAC else ("/" if user == "root" else "/home"))
            )
            if IS_WIN:
                stage = os.path.join(d, "stage")
                os.makedirs(stage, exist_ok=True)
                with tarfile.open(tar_p, "r:gz") as tf:
                    tf.extractall(stage)
                for item in os.listdir(stage):
                    src = os.path.join(stage, item)
                    dst = r"C:\Workspace" if item.lower() == "workspace" else os.path.join(dest_root, item)
                    shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                with tarfile.open(tar_p, "r:gz") as tf:
                    tf.extractall(dest_root)
                if IS_LINUX and user != "root":
                    subprocess.run(["sudo", "chown", "-R", f"{user}:{user}", f"/home/{user}"], capture_output=True)
                elif IS_MAC:
                    subprocess.run(["sudo", "chown", "-R", f"{user}:staff", f"/Users/{user}"], capture_output=True)
        except Exception as e:
            log(f"Workspace restore warning: {e}")


# ── Tunnel Managers (Static Bore Port + Optional Cloudflare HTTPS) ───────────
class BoreTunnel:
    def __init__(self, bore_bin: str, local_port: int, desired_remote_port: int = 0):
        self.bore_bin = bore_bin
        self.local_port = local_port
        self.desired_port = desired_remote_port
        self.bound_port = 0
        self.proc: Optional[subprocess.Popen[str]] = None
        self._log_lines: List[str] = []

    def _start_once(self, req_port: int) -> Optional[int]:
        cmd = [self.bore_bin, "local", str(self.local_port), "--to", "bore.pub"]
        if req_port > 0:
            cmd += ["--port", str(req_port)]
        self._log_lines = []
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.proc = proc

        def _reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                self._log_lines.append(line.strip())

        threading.Thread(target=_reader, daemon=True).start()
        deadline = time.time() + 10.0
        while time.time() < deadline:
            for ln in list(self._log_lines):
                m = re.search(r"bore\.pub:(\d+)", ln)
                if m:
                    return int(m.group(1))
            if proc.poll() is not None:
                return None
            time.sleep(0.2)
        if proc.poll() is None and req_port > 0:
            proc.terminate()
        return None

    def start_static(self, is_renewal: bool) -> int:
        # On renewal, retry exact static_port aggressively while previous cycle releases it
        attempts = 90 if is_renewal else 4
        for _ in range(attempts):
            p = self._start_once(self.desired_port)
            if p:
                self.desired_port = p
                self.bound_port = p
                return p
            time.sleep(0.5)

        # Only on initial Cycle 1 if random port was already occupied on bore.pub: pick next free static port
        for _ in range(15):
            cand = 32000 + secrets.randbelow(28000)
            p = self._start_once(cand)
            if p:
                self.desired_port = p
                self.bound_port = p
                return p
        # Fallback auto-port
        p = self._start_once(0)
        if p:
            self.desired_port = p
            self.bound_port = p
            return p
        raise RuntimeError("Could not establish bore tunnel")

    def ensure_alive(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            for _ in range(5):
                p = self._start_once(self.desired_port)
                if p:
                    self.bound_port = p
                    return
                time.sleep(1.0)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


def start_cloudflare_tunnel(cf_bin: str, local_port: int, suffix: str = "") -> Tuple[Optional[subprocess.Popen[str]], str]:
    proc = subprocess.Popen(
        [cf_bin, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{local_port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: List[str] = []

    def _read() -> None:
        assert proc.stdout is not None
        for ln in proc.stdout:
            lines.append(ln.strip())

    threading.Thread(target=_read, daemon=True).start()
    deadline = time.time() + 18.0
    while time.time() < deadline:
        for ln in list(lines):
            m = re.search(r"(https://[a-zA-Z0-9-]+\.trycloudflare\.com)", ln)
            if m:
                return proc, m.group(1) + suffix
        if proc.poll() is not None:
            break
        time.sleep(0.3)
    return proc, ""


def restart_local_services(os_key: str) -> None:
    if IS_LINUX:
        subprocess.run(["sudo", "systemctl", "restart", "ssh"], capture_output=True)
        if os_key == "ubuntu_gui":
            subprocess.run(["sudo", "systemctl", "restart", "xrdp"], capture_output=True)
    elif IS_WIN:
        if os_key in ("win2022", "win2025"):
            subprocess.run(["powershell", "-Command", "Restart-Service TermService -Force -ErrorAction SilentlyContinue"], capture_output=True)
        subprocess.run(["powershell", "-Command", "Restart-Service sshd -Force -ErrorAction SilentlyContinue"], capture_output=True)


# ── Main Lifecycle ───────────────────────────────────────────────────────────
def main() -> int:
    if not SERVER_ID or not GH_TOKEN or not GH_REPO:
        print("Missing required node environment variables.", file=sys.stderr)
        return 1

    print(f"::add-mask::{DB_KEY}", flush=True)
    cycle_env = int(os.environ.get("NODE_CYCLE") or "1")
    if SERVER_ID == "SELFTEST" and cycle_env == 1:
        test_spec = {
            "server_id": "SELFTEST",
            "user_id": 0,
            "srv_type": "vps",
            "os_key": "ubuntu24",
            "runner": "ubuntu-24.04",
            "access_mode": "all",
            "username": "cloud",
            "password": "Cf" + secrets.token_hex(6),
            "static_port": 32000 + secrets.randbelow(28000),
            "cycle": 1,
            "created_at": int(time.time()),
            "expires_at": int(time.time()) + 260,
        }
        upload_enc_json("srv-spec-SELFTEST.enc", test_spec)

    spec = None
    for _ in range(8):
        spec = download_enc_json(f"srv-spec-{SERVER_ID}.enc")
        if spec:
            break
        time.sleep(2.0)
    if not spec:
        log("Failed to load encrypted node specification.")
        return 1

    pw = spec["password"]
    print(f"::add-mask::{pw}", flush=True)
    cycle = int(os.environ.get("NODE_CYCLE") or spec.get("cycle", 1))
    expires_at = int(spec["expires_at"])
    user = spec["username"]
    os_key = spec["os_key"]
    expected_prev_port = int(spec.get("static_port") or 0)

    tools_dir = tempfile.mkdtemp(prefix="cf_tools_")
    bore_bin = install_bore(tools_dir)

    log(f"Provisioning OS ({os_key}) for cycle #{cycle}...")
    if IS_WIN:
        primary_local, sec_ssh_local, web_local = provision_windows(spec, tools_dir)
    elif IS_MAC:
        primary_local, sec_ssh_local, web_local = provision_macos(spec, tools_dir)
    else:
        primary_local, sec_ssh_local, web_local = provision_linux(spec, tools_dir)

    if SERVER_ID == "SELFTEST" and cycle == 1:
        subprocess.run(
            ["sudo", "-u", user, "tee", f"/home/{user}/persist_proof.txt"],
            input=b"CLOUDFORGE_PERSIST_OK\n",
            capture_output=True,
        )

    if cycle > 1:
        log("Restoring persistent workspace from previous cycle...")
        restore_workspace(user)
        # Signal outgoing cycle to release static port right now
        upload_enc_json(f"srv-ready-{SERVER_ID}-c{cycle}.enc", {"ready": True, "cycle": cycle, "ts": int(time.time())})

    try:
        static_ip = socket.gethostbyname("bore.pub")
    except Exception:
        static_ip = "bore.pub"

    primary_tunnel = BoreTunnel(bore_bin, primary_local, int(spec.get("static_port") or 0))
    bound_port = primary_tunnel.start_static(is_renewal=(cycle > 1))
    print(f"::add-mask::{bound_port}", flush=True)
    spec["static_port"] = bound_port

    if SERVER_ID == "SELFTEST" and cycle == 2:
        proof_ok = os.path.exists(f"/home/{user}/persist_proof.txt")
        port_match = (bound_port == expected_prev_port and bound_port > 0)
        log(f"[SELFTEST] Cycle #2 Verification -> port_match={port_match}, workspace_restored={proof_ok}")
        primary_tunnel.stop()
        env = gh_env()
        for a_name in [
            "srv-spec-SELFTEST.enc",
            "srv-status-SELFTEST.enc",
            "srv-data-SELFTEST.enc",
            "srv-ready-SELFTEST-c2.enc",
        ]:
            subprocess.run(["gh", "release", "delete-asset", NODES_TAG, a_name, "-y", "-R", GH_REPO], env=env, capture_output=True)
        return 0 if (port_match and proof_ok) else 1

    ssh_tunnel: Optional[BoreTunnel] = None
    ssh_remote_port = 0
    if sec_ssh_local:
        ssh_tunnel = BoreTunnel(bore_bin, sec_ssh_local, 0)
        try:
            ssh_remote_port = ssh_tunnel.start_static(is_renewal=False)
        except Exception:
            ssh_remote_port = 0

    cf_proc = None
    web_url = ""
    if web_local:
        cf_bin = install_cloudflared(tools_dir)
        suffix = "/vnc.html?autoconnect=true&resize=scale" if (os_key == "ubuntu_gui" and web_local == 6080) else ""
        cf_proc, web_url = start_cloudflare_tunnel(cf_bin, web_local, suffix=suffix)

    status_payload = {
        "server_id": SERVER_ID,
        "status": "online",
        "static_ip": static_ip,
        "static_port": bound_port,
        "ssh_port": ssh_remote_port,
        "web_url": web_url,
        "username": user,
        "gh_run_id": RUN_ID,
        "cycle": cycle,
        "updated_at": int(time.time()),
    }
    upload_enc_json(f"srv-status-{SERVER_ID}.enc", status_payload)
    log(f"Node is ONLINE (cycle #{cycle}). Entering watchdog & command loop.")

    boot_ts = time.time()
    max_cycle_sec = int(os.environ.get("NODE_MAX_CYCLE_SEC", str(340 * 60)))
    last_cmd_id = 0
    last_cmd_poll = 0.0

    try:
        while True:
            now = int(time.time())
            if now >= expires_at:
                log("Rental period completed. Shutting down cleanly.")
                status_payload["status"] = "expired"
                upload_enc_json(f"srv-status-{SERVER_ID}.enc", status_payload)
                break

            # Seamless Multi-Cycle Renewal (when >6h remaining)
            if time.time() - boot_ts >= max_cycle_sec and expires_at - now > 180:
                next_cycle = cycle + 1
                log(f"Approaching runner limit with {expires_at - now}s left — initiating seamless handover to cycle #{next_cycle}...")
                status_payload["status"] = "renewing"
                upload_enc_json(f"srv-status-{SERVER_ID}.enc", status_payload)

                save_workspace(user)
                spec["cycle"] = next_cycle
                spec["expires_at"] = expires_at
                spec["static_port"] = bound_port
                upload_enc_json(f"srv-spec-{SERVER_ID}.enc", spec)

                gh_api(
                    "POST",
                    f"/repos/{GH_REPO}/actions/workflows/cloud-node.yml/dispatches",
                    {
                        "ref": GH_REF,
                        "inputs": {
                            "server_id": SERVER_ID,
                            "runner_os": spec["runner"],
                            "cycle": str(next_cycle),
                        },
                    },
                )

                # Hold static port until Cycle N+1 signals ready (zero port theft window!)
                handover_deadline = time.time() + 240.0
                ready_asset = f"srv-ready-{SERVER_ID}-c{next_cycle}.enc"
                while time.time() < handover_deadline:
                    primary_tunnel.ensure_alive()
                    flag = download_enc_json(ready_asset)
                    if flag and flag.get("ready"):
                        log(f"Cycle #{next_cycle} ready! Releasing static port for instant takeover.")
                        primary_tunnel.stop()
                        break
                    time.sleep(3.0)
                break

            # Keep tunnels healthy
            primary_tunnel.ensure_alive()
            if ssh_tunnel:
                ssh_tunnel.ensure_alive()

            # Check incoming bot commands every 15s
            if time.time() - last_cmd_poll >= 15.0:
                last_cmd_poll = time.time()
                cmd = download_enc_json(f"srv-cmd-{SERVER_ID}.enc")
                if cmd and int(cmd.get("cmd_id", 0)) > last_cmd_id:
                    last_cmd_id = int(cmd["cmd_id"])
                    action = cmd.get("action", "none")
                    if "expires_at" in cmd:
                        expires_at = int(cmd["expires_at"])
                    if action == "terminate":
                        log("Terminate command received from bot.")
                        status_payload["status"] = "terminated"
                        upload_enc_json(f"srv-status-{SERVER_ID}.enc", status_payload)
                        break
                    elif action == "reboot":
                        log("Reboot command received — restarting local daemons & refreshing tunnel.")
                        restart_local_services(os_key)
                        primary_tunnel.ensure_alive()
                        status_payload["status"] = "online"
                        status_payload["updated_at"] = int(time.time())
                        upload_enc_json(f"srv-status-{SERVER_ID}.enc", status_payload)
                    elif action == "extend":
                        log(f"Extended rental — new expiry timestamp: {expires_at}")

            time.sleep(5.0)
    finally:
        primary_tunnel.stop()
        if ssh_tunnel:
            ssh_tunnel.stop()
        if cf_proc and cf_proc.poll() is None:
            try:
                cf_proc.terminate()
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
