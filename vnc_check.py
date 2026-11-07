#!/usr/bin/env python3
"""Grab frames from a VNC server using plain VNC-password auth (security type 2),
exactly like RealVNC Viewer / noVNC do, and report whether the frame is black.
Optionally types the macOS account password (+Enter) to unlock the lock screen
that macOS shows to VNC-password viewers, then grabs a second frame.

Usage: vnc_check.py <vnc_password> <out.png> [unlock_password] [after.png]
"""
import socket
import struct
import sys
import time

from Crypto.Cipher import DES
from PIL import Image, ImageStat

pw, out = sys.argv[1], sys.argv[2]
unlock_pw = sys.argv[3] if len(sys.argv) > 3 else None
out_after = sys.argv[4] if len(sys.argv) > 4 else "after_unlock.png"

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
if 2 not in types:
    sys.exit("VNC password auth (type 2) not offered")
s.sendall(bytes([2]))
challenge = rx(16)
key = (pw.encode() + b"\0" * 8)[:8]
key = bytes(int("{:08b}".format(b)[::-1], 2) for b in key)  # VNC bit-reversed DES key
s.sendall(DES.new(key, DES.MODE_ECB).encrypt(challenge))
if struct.unpack(">I", rx(4))[0] != 0:
    sys.exit("VNC authentication FAILED")
print("VNC authentication OK")

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
                pass  # bell
            elif t == 3:
                rx(3)
                rx(struct.unpack(">I", rx(4))[0])
            elif t == 1:
                rx(1)
                _, n = struct.unpack(">HH", rx(4))
                rx(n * 6)
            else:
                raise ValueError(f"unknown message {t}")
    except Exception as e:  # keep whatever arrived
        print("warning:", e)
    img.save(path)
    st = ImageStat.Stat(img.convert("L"))
    black = st.stddev[0] < 3
    print(f"{path}: mean={st.mean[0]:.1f} stddev={st.stddev[0]:.1f} ->",
          "BLACK SCREEN" if black else "IMAGE VISIBLE")
    return black


def key(sym):
    s.sendall(struct.pack(">BBxxI", 4, 1, sym))
    time.sleep(0.05)
    s.sendall(struct.pack(">BBxxI", 4, 0, sym))
    time.sleep(0.05)


def click(x, y):
    s.sendall(struct.pack(">BBHH", 5, 1, x, y))
    time.sleep(0.1)
    s.sendall(struct.pack(">BBHH", 5, 0, x, y))


black = grab(out)

if unlock_pw:
    time.sleep(2)
    click(w // 2, int(h * 0.895))  # password field on the macOS lock screen
    time.sleep(1)
    for ch in unlock_pw:
        key(ord(ch))
    key(0xFF0D)  # Return
    time.sleep(8)
    black = grab(out_after)

s.close()
sys.exit(2 if black else 0)
