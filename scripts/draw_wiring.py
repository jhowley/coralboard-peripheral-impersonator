#!/usr/bin/env python3
"""Deterministic SVG generator for the two-Pico I2C wiring graphic.

Pin data verified against Raspberry Pi Pico datasheet RP-008307-DS section 2.1
and the official "Raspberry Pi Pico Pinout" A4 poster (Pico-R3-A4-Pinout.pdf).
No generative imagery; all geometry is explicit so pin positions are exact.
"""
import os
from PIL import ImageFont

W, H = 2000, 2820
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "images")
os.makedirs(OUT_DIR, exist_ok=True)

_FONT_CACHE = {}
def text_w(s, size, weight="normal"):
    """Exact advance width from the same DejaVu faces Chromium will use."""
    key = (size, weight)
    if key not in _FONT_CACHE:
        bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        reg = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        _FONT_CACHE[key] = ImageFont.truetype(bold if weight == "bold" else reg, size)
    return _FONT_CACHE[key].getlength(s)

# ---------------------------------------------------------------- palette
INK      = "#101418"
SUB      = "#4a5560"
MUTED    = "#78828c"
PAPER    = "#ffffff"
PANEL    = "#f6f8fa"
PANEL_ST = "#d3dae1"
BOARD    = "#2f6d4f"
BOARD_D  = "#1d4a35"
BOARD_L  = "#3d8563"
PAD      = "#e0bf6a"
PAD_ST   = "#8a6a22"
RED      = "#c62828"
RED_BG   = "#fdecec"
AMBER    = "#b26a00"
AMBER_BG = "#fff6e5"
GREEN    = "#1b5e20"
GREEN_BG = "#e8f5e9"

C_SDA = "#1259b8"   # arbitrary
C_SCL = "#c25a00"   # arbitrary
C_GND = "#37474f"
C_3V3 = "#c62828"

parts = []
def add(s): parts.append(s)

def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

def T(x, y, s, size=18, fill=INK, anchor="start", weight="normal",
      family="DejaVu Sans", opacity=1.0, ls=0):
    add(f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}" '
        f'fill="{fill}" text-anchor="{anchor}" font-weight="{weight}" '
        f'opacity="{opacity}" letter-spacing="{ls}">{esc(s)}</text>')

def R(x, y, w, h, fill="none", stroke="none", sw=1, rx=0, dash=None, opacity=1.0):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    add(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d} opacity="{opacity}"/>')

def L(x1, y1, x2, y2, stroke=INK, sw=2, dash=None, cap="round", opacity=1.0):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    add(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="{stroke}" stroke-width="{sw}" stroke-linecap="{cap}"{d} opacity="{opacity}"/>')

def PATH(d, stroke=INK, sw=2, fill="none", cap="round", dash=None):
    da = f' stroke-dasharray="{dash}"' if dash else ""
    add(f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}" '
        f'stroke-linecap="{cap}" stroke-linejoin="round"{da}/>')

def C(cx, cy, r, fill="none", stroke="none", sw=1):
    add(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="{sw}"/>')

def dot(cx, cy, color):
    C(cx, cy, 8, fill=color)
    C(cx, cy, 8, fill="none", stroke="#ffffff", sw=2.2)

# ---------------------------------------------------------------- page
add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
    f'viewBox="0 0 {W} {H}">')
R(0, 0, W, H, fill=PAPER)

# ================================================================ HEADER
T(90, 92, "Two Raspberry Pi Pico (RP2040) boards  ·  I²C tester-to-sensor wiring",
  42, INK, weight="bold")
T(90, 134, "Original Raspberry Pi Pico (not Pico W)  ·  top / component view  ·  "
           "both micro-USB sockets point UP", 21, SUB)

# safety banner
R(90, 168, W - 180, 108, fill=RED_BG, stroke=RED, sw=3, rx=12)
R(90, 168, 12, 108, fill=RED, rx=6)
T(124, 212, "STOP  —  UNPLUG BOTH USB CABLES BEFORE YOU CONNECT ANYTHING.",
  30, RED, weight="bold")
T(124, 252, "Keep BOTH boards unplugged until every wire and both resistors have been "
            "checked. Each board is then powered only by its own USB cable.", 20, "#7f1d1d")

# ================================================================ PANEL 1
PY = 330
R(60, PY, W - 120, 1010, fill=PANEL, stroke=PANEL_ST, sw=2, rx=16)
T(90, PY + 46, "PANEL 1  ·  WHERE THE PINS ARE", 27, INK, weight="bold")
T(W - 90, PY + 46, "Both boards seen from above, USB at the top.  40 pads each.",
  19, SUB, anchor="end")

BW, BH = 300, 737
PITCH = 33
BOARD_TOP = PY + 196          # 526
PAD0 = BOARD_TOP + 55         # first pad centre
LX, RX = 250, 1450            # board left edges

LEFT_PINS = [(1,"GP0"),(2,"GP1"),(3,"GND"),(4,"GP2"),(5,"GP3"),(6,"GP4"),(7,"GP5"),
             (8,"GND"),(9,"GP6"),(10,"GP7"),(11,"GP8"),(12,"GP9"),(13,"GND"),
             (14,"GP10"),(15,"GP11"),(16,"GP12"),(17,"GP13"),(18,"GND"),
             (19,"GP14"),(20,"GP15")]
RIGHT_PINS = [(40,"VBUS"),(39,"VSYS"),(38,"GND"),(37,"3V3_EN"),(36,"3V3(OUT)"),
              (35,"ADC_VREF"),(34,"GP28"),(33,"GND"),(32,"GP27"),(31,"GP26"),
              (30,"RUN"),(29,"GP22"),(28,"GND"),(27,"GP21"),(26,"GP20"),
              (25,"GP19"),(24,"GP18"),(23,"GND"),(22,"GP17"),(21,"GP16")]

def pad_y(i):          # i = row index 0..19 from the top of the board
    return PAD0 + i * PITCH

def board_art(bx):
    R(bx, BOARD_TOP, BW, BH, fill=BOARD, stroke=BOARD_D, sw=3, rx=14)
    # micro-USB socket, overhanging the top edge
    R(bx + BW/2 - 48, BOARD_TOP - 26, 96, 58, fill="#c3c8cd", stroke="#7d858c", sw=2, rx=5)
    R(bx + BW/2 - 34, BOARD_TOP - 12, 68, 12, fill="#8f979e", rx=3)
    T(bx + BW/2, BOARD_TOP + 62, "USB", 14, "#d7e6dd", anchor="middle", weight="bold")
    # BOOTSEL
    R(bx + BW - 92, BOARD_TOP + 22, 58, 26, fill="#e9edf0", stroke="#7d858c", sw=1.5, rx=4)
    T(bx + BW - 63, BOARD_TOP + 40, "BOOTSEL", 9.5, "#3f474d", anchor="middle")
    # chip
    R(bx + BW/2 - 56, BOARD_TOP + 300, 112, 112, fill="#22262b", stroke="#0d1013", sw=2, rx=4)
    T(bx + BW/2, BOARD_TOP + 352, "RP2040", 15, "#e8ecef", anchor="middle", weight="bold")
    T(bx + BW/2, BOARD_TOP + 374, "Pico", 13, "#9aa4ad", anchor="middle")
    # little LED
    C(bx + BW/2 + 92, BOARD_TOP + 250, 5, fill="#9be7a5", stroke="#0d1013", sw=1)
    T(bx + BW/2 + 92, BOARD_TOP + 276, "LED", 9, "#bfe9c6", anchor="middle")
    T(bx + BW/2, BOARD_TOP + BH - 22, "Raspberry Pi", 11, "#bfe9c6", anchor="middle")

def draw_board(bx, hi, hi36_state):
    """hi: {pin: color}; hi36_state: 'use' | 'x'"""
    board_art(bx)
    for i, (num, name) in enumerate(LEFT_PINS):
        y = pad_y(i)
        px = bx + 32
        col = hi.get(num)
        if col:
            C(px, y, 15, fill="none", stroke=col, sw=3)
        C(px, y, 9, fill=PAD, stroke=PAD_ST, sw=1.6)
        C(px, y, 3.6, fill=BOARD_D)
        is_hi = num in col_set
        nw = text_w(name, 15)
        T(px - 30, y + 5.5, name, 15, INK if is_hi else MUTED,
          anchor="end", weight="bold" if is_hi else "normal")
        bw = 30 if num >= 10 else 22
        bcx = px - 30 - nw - 10 - bw / 2
        badge(bcx, y, num, col)
        if col:
            pill(bcx - bw / 2 - 8 - 62, y - 11, 62, 22, col, NETTAG[num])
    for i, (num, name) in enumerate(RIGHT_PINS):
        y = pad_y(i)
        px = bx + BW - 32
        col = hi.get(num)
        if col:
            C(px, y, 15, fill="none", stroke=col, sw=3)
        C(px, y, 9, fill=PAD, stroke=PAD_ST, sw=1.6)
        C(px, y, 3.6, fill=BOARD_D)
        is_hi = num in col_set
        nw = text_w(name, 15)
        T(px + 30, y + 5.5, name, 15, INK if is_hi else MUTED,
          anchor="start", weight="bold" if is_hi else "normal")
        bw = 30 if num >= 10 else 22
        bcx = px + 30 + nw + 10 + bw / 2
        badge(bcx, y, num, col)
        if col and num != 36:
            pill(bcx + bw / 2 + 8, y - 11, 62, 22, col, NETTAG[num])
    if hi36_state == "x":
        # red X over pin 36 pad plus a dashed box
        px, y = bx + BW - 32, pad_y(4)
        R(px - 19, y - 19, 38, 38, fill="none", stroke=RED, sw=2, rx=4, dash="5 4")
        L(px - 12, y - 12, px + 12, y + 12, RED, 3.2)
        L(px - 12, y + 12, px + 12, y - 12, RED, 3.2)

def badge(cx, y, num, color=None):
    w = 30 if num >= 10 else 22
    fill = color if color else "#e6eaee"
    txt = "#ffffff" if color else "#39424a"
    R(cx - w/2, y - 12, w, 24, fill=fill, stroke="none", rx=5)
    T(cx, y + 5.5, str(num), 13, txt, anchor="middle", weight="bold", family="DejaVu Sans Mono")

def pill(x, y, w, h, color, label, size=12):
    R(x, y, w, h, fill=color, rx=11)
    T(x + w/2, y + h/2 + 4.2, label, size, "#ffffff", anchor="middle", weight="bold")

NETTAG = {6: "SDA", 7: "SCL", 8: "GND"}

# --- tester board
col_set = {6, 7, 8, 36}
T(LX + BW/2, PY + 112, "TESTER", 34, INK, anchor="middle", weight="bold")
T(LX + BW/2, PY + 144, "I²C controller — sends test requests", 16, SUB, anchor="middle")
draw_board(LX,
           {6: C_SDA, 7: C_SCL, 8: C_GND, 36: C_3V3}, "use")

# --- sensor board
T(RX + BW/2, PY + 112, "SENSOR", 34, INK, anchor="middle", weight="bold")
T(RX + BW/2, PY + 144, "I²C peripheral — impersonates a sensor", 16, SUB, anchor="middle")
draw_board(RX,
           {6: C_SDA, 7: C_SCL, 8: C_GND}, "x")

# the tester 3V3 pill
_px = LX + BW - 32
_bcx = _px + 30 + text_w("3V3(OUT)", 15, "bold") + 10 + 15
pill(_bcx + 15 + 8, pad_y(4) - 11, 118, 22, C_3V3, "to R1 + R2")

# --- wiring-order box in the gap (box is sized to its content)
GX, GY, GW = 820, PY + 190, 390
steps = [
    ("1", "Unplug both USB cables.", INK),
    ("2", "Wire pin 6 to pin 6  (SDA).", C_SDA),
    ("3", "Wire pin 7 to pin 7  (SCL).", C_SCL),
    ("4", "Wire pin 8 to pin 8  (GND).", C_GND),
    ("5", "R1 from TESTER pin 36 to the SDA wire.", C_3V3),
    ("6", "R2 from TESTER pin 36 to the SCL wire.", C_3V3),
    ("7", "Check with a meter, then plug both in.", INK),
]

def wrap(s, size, width):
    words, lines, cur = s.split(), [], ""
    for wd in words:
        t = (cur + " " + wd).strip()
        if text_w(t, size) > width and cur:
            lines.append(cur); cur = wd
        else:
            cur = t
    lines.append(cur)
    return lines

HH = 0
for _, s, _c in steps:
    HH += 21 * len(wrap(s, 16, GW - 100)) + 22
GH = 84 + HH + 120
R(GX, GY, GW, GH, fill="#ffffff", stroke=PANEL_ST, sw=2, rx=12)
T(GX + 26, GY + 38, "WIRING ORDER", 19, INK, weight="bold")

yy = GY + 84
for n, s, col in steps:
    C(GX + 36, yy - 5, 13, fill=col)
    T(GX + 36, yy, n, 13, "#ffffff", anchor="middle", weight="bold")
    lines = wrap(s, 16, GW - 100)
    for j, ln in enumerate(lines):
        T(GX + 58, yy + j * 21, ln, 16, INK)
    yy += 21 * len(lines) + 22

# free-choice note inside the wiring box
L(GX + 24, GY + GH - 92, GX + GW - 24, GY + GH - 92, PANEL_ST, 1.5)
T(GX + 26, GY + GH - 64, "Wire colours: your choice.", 15, SUB, weight="bold")
T(GX + 26, GY + GH - 42, "Resistor orientation: irrelevant", 15, SUB, weight="bold")
T(GX + 26, GY + GH - 20, "(5.1 kΩ is not polarised).", 15, SUB)

# --- legend under the boards
ly = BOARD_TOP + BH + 26
C(LX + 40, ly - 5, 9, fill=PAD, stroke=PAD_ST, sw=1.6); C(LX + 40, ly - 5, 3.6, fill=BOARD_D)
T(LX + 58, ly, "solder pad / hole  (physical pin number in the dark box)", 15, SUB)
C(LX + 520, ly - 5, 14, fill="none", stroke=C_SDA, sw=3)
T(LX + 540, ly, "coloured ring = pin used in this wiring", 15, SUB)
L(LX + 900, ly - 12, LX + 920, ly + 2, RED, 3); L(LX + 900, ly + 2, LX + 920, ly - 12, RED, 3)
T(LX + 932, ly, "✕ = pin 36 on SENSOR must stay EMPTY", 15, SUB)
T(LX + 40, ly + 26, "Pin numbers are not printed on a real Pico: pin 1 is the top-left pad at the USB end. Count 1→20 down the left side, then 21→40 up the right side.",
  14, MUTED)

# ================================================================ PANEL 2
QY = 1372
R(60, QY, W - 120, 900, fill="#ffffff", stroke=PANEL_ST, sw=2, rx=16)
T(90, QY + 46, "PANEL 2  ·  EXACT CONNECTIONS", 27, INK, weight="bold")
T(W - 90, QY + 46, "Electrical view — wire length and physical routing are not to scale.",
  19, SUB, anchor="end")

RAIL, Y_SDA, Y_SCL, Y_GND = QY + 268, QY + 368, QY + 458, QY + 548
BLK_L, BLK_R = 200, 1520
BLK_W = 280
BLK_TOP, BLK_BOT = QY + 183, QY + 613

def term_block(x, title, sub, rows, bad_row=None):
    R(x, BLK_TOP, BLK_W, BLK_BOT - BLK_TOP, fill="#fbfcfd", stroke="#9aa4ad", sw=2, rx=10)
    T(x + BLK_W/2, BLK_TOP + 34, title, 20, INK, anchor="middle", weight="bold")
    T(x + BLK_W/2, BLK_TOP + 56, sub, 13, MUTED, anchor="middle")
    for y, num, name, col, label in rows:
        px = x + 34
        C(px, y, 9, fill=PAD, stroke=PAD_ST, sw=1.6)
        C(px, y, 3.6, fill="#fbfcfd")
        T(px + 22, y + 6, f"pin {num}", 17, INK if num in (6,7,8,36) else MUTED, weight="bold")
        T(px + 22, y + 28, name, 14, MUTED)
    T(x + BLK_W/2, BLK_BOT - 18, "schematic terminals — see panel 1", 12, MUTED, anchor="middle")

term_block(BLK_L, "TESTER", "controller / request sender", [
    (RAIL, 36, "3V3(OUT)", C_3V3, ""),
    (Y_SDA, 6, "GP4", C_SDA, ""),
    (Y_SCL, 7, "GP5", C_SCL, ""),
    (Y_GND, 8, "GND", C_GND, ""),
])
term_block(BLK_R, "SENSOR", "peripheral / impersonator", [
    (Y_SDA, 6, "GP4", C_SDA, ""),
    (Y_SCL, 7, "GP5", C_SCL, ""),
    (Y_GND, 8, "GND", C_GND, ""),
])
# sensor pin 36 with X
sx, sy = BLK_R + 34, RAIL
C(sx, sy, 9, fill=PAD, stroke=PAD_ST, sw=1.6)
C(sx, sy, 3.6, fill="#fbfcfd")
R(sx - 19, sy - 19, 38, 38, fill="none", stroke=RED, sw=2, rx=4, dash="5 4")
L(sx - 12, sy - 12, sx + 12, sy + 12, RED, 3.2)
L(sx - 12, sy + 12, sx + 12, sy - 12, RED, 3.2)
T(sx + 30, sy - 2, "pin 36", 17, RED, weight="bold")
T(sx + 30, sy + 20, "3V3(OUT) — leave", 13, RED)
T(sx + 30, sy + 36, "empty. Never link.", 13, RED)

X0, X1 = BLK_L + BLK_W, BLK_R     # 480 .. 1520

def wire(y, color, label, sub):
    L(X0, y, X1, y, color, 5)
    R(X1 - 250, y - 46, 246, 40, fill="#ffffff", rx=6)
    T(X1 - 14, y - 26, label, 19, color, anchor="end", weight="bold")
    T(X1 - 14, y - 8, sub, 13, MUTED, anchor="end")

# 3V3 rail first (behind resistors)
L(X0, RAIL, 1030, RAIL, C_3V3, 5)
wire(Y_SDA, C_SDA, "SDA", "wire 1  ·  pin 6 to pin 6")
wire(Y_SCL, C_SCL, "SCL", "wire 2  ·  pin 7 to pin 7")
wire(Y_GND, C_GND, "GND", "wire 3  ·  pin 8 to pin 8")

def resistor_v(cx, y_top, y_bot, name, side):
    """vertical resistor body between two y's, leads included"""
    body_t, body_b = y_top + 34, y_top + 84
    L(cx, y_top, cx, body_t, C_3V3, 5)
    R(cx - 16, body_t, 32, body_b - body_t, fill="#ffffff", stroke=C_3V3, sw=3.4, rx=3)
    L(cx, body_b, cx, y_bot, C_3V3, 5)
    if side == "right":
        T(cx + 26, body_t + 14, name, 17, C_3V3, weight="bold")
        T(cx + 26, body_t + 36, "5.1 kΩ", 17, C_3V3)
    else:
        T(cx - 26, body_t + 14, name, 17, C_3V3, anchor="end", weight="bold")
        T(cx - 26, body_t + 36, "5.1 kΩ", 17, C_3V3, anchor="end")

# R1: rail -> SDA (no crossing)
resistor_v(700, RAIL, Y_SDA, "R1", "right")
dot(700, RAIL, C_3V3)
dot(700, Y_SDA, C_SDA)
T(690, Y_SDA + 29, "dot = joined", 13, MUTED, anchor="end")

# R2: rail -> SCL, hops over SDA
body_t2, body_b2 = RAIL + 30, RAIL + 70
L(1030, RAIL, 1030, body_t2, C_3V3, 5)
R(1030 - 16, body_t2, 32, body_b2 - body_t2, fill="#ffffff", stroke=C_3V3, sw=3.4, rx=3)
L(1030, body_b2, 1030, Y_SDA - 16, C_3V3, 5)
hop = f"M 1030 {Y_SDA - 16} C 1054 {Y_SDA - 16} 1054 {Y_SDA + 16} 1030 {Y_SDA + 16}"
PATH(hop, PAPER, 11)  # white clearance prevents a false junction at the crossing
PATH(hop, C_3V3, 5)   # continuous vertical lead, hop, no connection
L(1030, Y_SDA + 16, 1030, Y_SCL, C_3V3, 5)
T(1052, body_t2 + 14, "R2", 17, C_3V3, weight="bold")
T(1052, body_t2 + 36, "5.1 kΩ", 17, C_3V3)
dot(1030, Y_SCL, C_SCL)

# annotation for the hop
T(1048, Y_SDA + 31, "hop = no connection", 13, C_3V3)

# bus caption
T(X0 + 6, RAIL - 18, "3V3 (OUT) node — TESTER pin 36 only", 15, C_3V3, weight="bold")

# callout under the circuit
CY = QY + 650
R(150, CY, 1700, 150, fill=AMBER_BG, stroke=AMBER, sw=2.5, rx=12)
T(180, CY + 34, "Both resistors start at the same point: TESTER pin 36 (3V3 OUT).", 21, AMBER, weight="bold")
T(180, CY + 66, "That pin supplies ONLY the pull-ups. One resistor's free end goes to the "
                "SDA wire, the other's to the SCL wire —", 17, "#6b4400")
T(180, CY + 92, "separately. The resistors are not in series with the board-to-board wires, and the SENSOR's "
                "pin 36 is never connected to anything.", 17, "#6b4400")
T(180, CY + 124, "Resistor orientation does not matter (5.1 kΩ is not polarised). Wire colours are arbitrary — "
                 "pick your own and stay consistent.", 16, "#6b4400")

# ================================================================ FOOTER
FY = 2320
T(90, FY, "BEFORE YOU APPLY POWER", 22, INK, weight="bold")
checks = [
    ("Continuity, both unplugged:", "pin 6 ↔ pin 6, pin 7 ↔ pin 7, pin 8 ↔ pin 8 all beep (≈0 Ω)."),
    ("Visual check:", "Leave SENSOR pin 36 and both boards' pins 39/40 unwired."),
    ("Measure resistors separately:", "Each ≈5.1 kΩ before fitting. Connected boards can alter readings."),
    ("No 5 V on signals:", "VBUS (40) and VSYS (39) are not used. Only 3V3(OUT) feeds the resistors."),
    ("Polarity:", "GP4→GP4, GP5→GP5. Swapping SDA and SCL will simply not communicate."),
]
yy = FY + 40
for a, b in checks:
    C(120, yy - 6, 4, fill=INK)
    T(136, yy, a, 17, INK, weight="bold")
    T(136 + text_w(a, 17, "bold") + 8, yy, b, 17, SUB)
    yy += 34

T(1080, FY, "NEVER DO THESE", 22, RED, weight="bold")
nevers = [
    "Join the two 3V3(OUT) pins (36) together.",
    "Join VBUS, VSYS, 3V3_EN, RUN or SWD between boards.",
    "Connect SENSOR pin 36. This diagram uses TESTER pin 36 only.",
    "Add a 5 V wire. Neither board needs one and it can destroy an input.",
]
yy = FY + 40
for s in nevers:
    T(1100, yy, "✕", 17, RED, weight="bold")
    T(1124, yy, s, 17, SUB)
    yy += 30

# bus correction note
BY = FY + 190
R(1080, BY, 780, 118, fill=GREEN_BG, stroke=GREEN, sw=2, rx=10)
T(1104, BY + 30, "I²C is bidirectional — both boards drive SDA.", 18, GREEN, weight="bold")
T(1104, BY + 56, "The TESTER makes the clock and starts each transfer, but the SENSOR also", 15, "#1d3b1f")
T(1104, BY + 78, "pulls SDA low to acknowledge bytes and to send its replies. Never let both", 15, "#1d3b1f")
T(1104, BY + 100, "boards act as bus master at the same time.", 15, "#1d3b1f")

# sources
SY = BY + 150
T(90, SY, "Pinout verified against Raspberry Pi Ltd: Pico datasheet RP-008307-DS §2.1 and the official "
          "“Raspberry Pi Pico Pinout” poster", 13, MUTED)
T(90, SY + 20, "https://datasheets.raspberrypi.com/pico/Pico-R3-A4-Pinout.pdf  ·  "
               "https://pip.raspberrypi.com/documents/RP-008307-DS-pico-datasheet.pdf", 13, MUTED)
T(90, SY + 44, "Illustrative diagram, not a breadboard layout and not to scale. No hardware was connected or "
               "powered to make it.", 13, MUTED)

add("</svg>")

svg = "\n".join(parts)
with open(os.path.join(OUT_DIR, "pico-i2c-wiring.svg"), "w") as f:
    f.write('<?xml version="1.0" encoding="UTF-8"?>\n' + svg)
print("svg written", len(svg))
