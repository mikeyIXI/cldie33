#!/usr/bin/env python3
"""Grab frames from macOS Screen Sharing exactly like a remote viewer does.

Modes:
  vnc  : plain VNC-password auth (security type 2) - RealVNC "VNC password" / noVNC
  ard  : Apple Remote Desktop auth (type 30) with macOS username + password
If an unlock password is given, it is typed at the macOS lock screen (with proper
Shift handling) and a second frame is captured.

Usage: vnc_check.py vnc <vnc_pw> <out.png> [unlock_pw] [after.png]
       vnc_check.py ard <user> <pw> <out.png>
"""
import hashlib
import os
import socket
import struct
import sys
import time

from Crypto.Cipher import AES, DES
from PIL import Image, ImageStat

mode = sys.argv[1]
s = socket.create_connection(("127.0.0.1", 5900), 20)
s.settimeout(60)


def rx(n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


print("Server version:", rx(12).strip().decode())
s.sendall(b"RFB 003.008\n")
types = list(rx(rx(1)[0]))
print("Security types:", types)

if mode == "vnc":
    pw, out = sys.argv[2], sys.argv[3]
    unlock_pw = sys.argv[4] if len(sys.argv) > 4 else None
    out_after = sys.argv[5] if len(sys.argv) > 5 else "after_unlock.png"
    s.sendall(bytes([2]))
    challenge = rx(16)
    k = (pw.encode() + b"\0" * 8)[:8]
    k = bytes(int("{:08b}".format(b)[::-1], 2) for b in k)  # VNC bit-reversed DES key
    s.sendall(DES.new(k, DES.MODE_ECB).encrypt(challenge))
else:
    user, pw, out = sys.argv[2], sys.argv[3], sys.argv[4]
    unlock_pw = None
    s.sendall(bytes([30]))
    gen = struct.unpack(">H", rx(2))[0]
    klen = struct.unpack(">H", rx(2))[0]
    prime = int.from_bytes(rx(klen), "big")
    peer = int.from_bytes(rx(klen), "big")
    priv = int.from_bytes(os.urandom(klen), "big") % prime
    pub = pow(gen, priv, prime).to_bytes(klen, "big")
    secret = pow(peer, priv, prime).to_bytes(klen, "big")
    aes_key = hashlib.md5(secret).digest()
    creds = bytearray(os.urandom(128))
    u, p = user.encode()[:63] + b"\0", pw.encode()[:63] + b"\0"
    creds[0:len(u)] = u
    creds[64:64 + len(p)] = p
    s.sendall(AES.new(aes_key, AES.MODE_ECB).encrypt(bytes(creds)) + pub)

res = struct.unpack(">I", rx(4))[0]
if res != 0:
    try:
        print("reason:", rx(struct.unpack(">I", rx(4))[0]).decode(errors="replace"))
    except Exception:
        pass
    sys.exit(f"{mode.upper()} authentication FAILED")
print(f"{mode.upper()} authentication OK")

s.sendall(b"\x01")  # shared session
w, h = struct.unpack(">HH", rx(4))
rx(16)
name = rx(struct.unpack(">I", rx(4))[0]).decode(errors="replace")
print(f"Desktop '{name}' {w}x{h}")

# 32bpp true colour, little-endian, 0x00RRGGBB -> bytes B,G,R,X
s.sendall(struct.pack(">B3xBBBBHHHBBB3x", 0, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
s.sendall(struct.pack(">BxHi", 2, 1, 0))  # Raw encoding only


def grab(path):
    s.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
    img = Image.new("RGB", (w, h))
    got = 0
    try:
        while got < w * h:
            t = rx(1)[0]
            if t == 0:
                rx(1)
                for _ in range(struct.unpack(">H", rx(2))[0]):
                    x, y, rw, rh, enc = struct.unpack(">HHHHi", rx(12))
                    if enc != 0:
                        raise ValueError(f"unexpected encoding {enc}")
                    data = rx(rw * rh * 4)
                    img.paste(Image.frombytes("RGB", (rw, rh), data, "raw", "BGRX"), (x, y))
                    got += rw * rh
            elif t == 2:
                pass
            elif t == 3:
                rx(3)
                rx(struct.unpack(">I", rx(4))[0])
            elif t == 1:
                rx(1)
                _, n = struct.unpack(">HH", rx(4))
                rx(n * 6)
            else:
                raise ValueError(f"unknown message {t}")
    except Exception as e:
        print("warning:", e)
    img.save(path)
    st = ImageStat.Stat(img.convert("L"))
    black = st.stddev[0] < 3
    print(f"{path}: mean={st.mean[0]:.1f} stddev={st.stddev[0]:.1f} ->",
          "BLACK SCREEN" if black else "IMAGE VISIBLE")
    return black


SHIFTED = '~!@#$%^&*()_+{}|:"<>?'


def key(sym, down):
    s.sendall(struct.pack(">BBxxI", 4, 1 if down else 0, sym))
    time.sleep(0.04)


def type_text(text):
    for ch in text:
        shift = ch.isupper() or ch in SHIFTED
        if shift:
            key(0xFFE1, True)
        key(ord(ch), True)
        key(ord(ch), False)
        if shift:
            key(0xFFE1, False)


def click(x, y):
    s.sendall(struct.pack(">BBHH", 5, 1, x, y))
    time.sleep(0.1)
    s.sendall(struct.pack(">BBHH", 5, 0, x, y))


time.sleep(3)  # let the first transition (black fade) finish
black = grab(out)

if unlock_pw:
    click(w // 2, int(h * 0.895))  # password field on the macOS lock screen
    time.sleep(1)
    type_text(unlock_pw)
    key(0xFF0D, True)
    key(0xFF0D, False)
    time.sleep(10)
    black = grab(out_after)

s.close()
sys.exit(2 if black else 0)
