"""
MRI breathing instruction animation.

A settings window opens first (protocol, tasks, display); click Start (or press
Enter) to open the fullscreen animation. After each run the settings window comes
back for the next one. "Save as default" writes config.json.

A protocol is:  REST AT START | [ task 1 | task 2 | ... ] x N | REST AT END (minimum)
The cycle of tasks is repeated as many times as fits in SCAN_DURATION; the remaining
time is added to the final rest.

Each task has a type, a text shown on screen, a time (s) and, for breathing, a frequency:
  - "breath": breathing task or rest, freq in breaths/min (0 = free breathing, no pacing)
  - "hold"  : breath hold (ring emptying + remaining seconds)
Breathing tasks are guided either by a pulsing circle or by a sine curve scrolling
right -> left (the subject follows the dot: going up = breathe IN, down = breathe OUT),
chosen per protocol ("guide").

- WARNING_TIME seconds before every change of instruction a "Get ready" banner with
  a countdown is shown.
- Fullscreen. Press S or Space to start, S or Space again to stop, ESC to quit.
- A timer (elapsed / total) is shown at the bottom of the screen.
- A CSV log of the real onsets is written next to the program.

Settings can be edited below, or overridden without recompiling by placing a
`config.json` next to the executable (see config_example.json).
"""

import bisect
import csv
import json
import math
import os
import sys
import time
from datetime import datetime

import pygame

# =============================================================================
# SETTINGS
# =============================================================================
CONFIG = {
    "PROTOCOL": "breathhold",    # key of PROTOCOLS below
    "SHOW_LAUNCHER": True,       # settings window before each run (False = run directly)
    "SCAN_DURATION": 300,        # s, total duration of the animation (scan)
    "WARNING_TIME": 5,           # s, warning shown before each change of instruction
    "SCREEN_WIDTH": 0,           # px, 0 = auto (native resolution of the chosen screen)
    "SCREEN_HEIGHT": 0,          # px, 0 = auto
    "FULLSCREEN": True,
    "DISPLAY_INDEX": 0,          # 0 = main screen, 1 = second screen (projector) ...
    "MIRROR": False,             # flip horizontally (for mirror-based MRI setups)
    "START_STOP_KEYS": ["s", "space"],   # start the animation, and stop it when running
    "SHOW_TIMER": True,          # elapsed / total time at the bottom of the screen
    "FPS": 60,
    "BACKGROUND": [255, 255, 255],
    "TEXT_COLOR": [30, 30, 30],
    "WARNING_COLOR": [230, 130, 0],
    "LOG": True,
    # --- sine curve (anim "sine") ---
    "SINE_WINDOW": 12,           # s of breathing visible across the screen width
    "SINE_DOT_X": 0.3,           # dot position, fraction of screen width from the left
    "SINE_AMPLITUDE": 0.2,       # curve amplitude, fraction of screen height
    "SINE_RAMP": 2.0,            # s, smooth fade of the curve before a flat (rest) phase
    "EXHALE_TIME": 3.0,          # s, free breathing glides to full expiration before a breath hold
    "SHOW_IN_OUT": True,         # show "IN" / "OUT" next to the dot
}

# Colors, chosen automatically from the task type and breathing frequency.
COLORS = {
    "hold":   [210, 50, 50],
    "free":   [60, 160, 110],     # freq 0
    "slow":   [130, 60, 180],     # freq < SLOW_BELOW
    "normal": [40, 110, 200],
    "fast":   [220, 120, 0],      # freq > FAST_ABOVE
}
SLOW_BELOW, FAST_ABOVE = 10, 20   # breaths/min

FREE = "RELAX\nbreathe freely"

# guide: "circle" (pulsing circle) or "sine" (scrolling curve) for the breathing tasks
# rest_start / rest_end (minimum): breathing periods before the first / after the last cycle
# cycle: list of tasks, repeated as many times as fits in SCAN_DURATION
PROTOCOLS = {
    "breathhold": {
        "guide": "circle",
        "rest_start": {"name": FREE, "time": 30, "freq": 0},
        "cycle": [
            {"type": "breath", "name": "BREATHE FAST", "time": 10, "freq": 30},
            {"type": "hold", "name": "HOLD YOUR BREATH", "time": 20},
            {"type": "breath", "name": FREE, "time": 30, "freq": 0},
        ],
        "rest_end": {"name": FREE, "time": 30, "freq": 0},
    },
    "paced": {
        "guide": "sine",
        "rest_start": {"name": FREE, "time": 30, "freq": 0},
        "cycle": [
            {"type": "breath", "name": "BREATHE FAST", "time": 20, "freq": 30},
            {"type": "breath", "name": "BREATHE NORMALLY", "time": 20, "freq": 12},
            {"type": "breath", "name": "BREATHE SLOWLY", "time": 20, "freq": 6},
        ],
        "rest_end": {"name": FREE, "time": 30, "freq": 0},
    },
}

NEW_TASK = {
    "breath": {"type": "breath", "name": "BREATHE NORMALLY", "time": 20, "freq": 12},
    "hold": {"type": "hold", "name": "HOLD YOUR BREATH", "time": 20},
}

WAITING_TEXT = "Please stay still\nThe scan will start soon"
END_TEXT = "Scan finished\nThank you"


# =============================================================================
# HELPERS
# =============================================================================
def base_dir():
    """Folder containing the exe / the .app (frozen) or this script."""
    if getattr(sys, "frozen", False):
        path = os.path.dirname(sys.executable)
        if path.endswith(".app/Contents/MacOS"):        # macOS app bundle
            path = os.path.dirname(os.path.dirname(os.path.dirname(path)))
        return path
    return os.path.dirname(os.path.abspath(__file__))


def load_overrides():
    """Override the settings above with an optional config.json."""
    path = os.path.join(base_dir(), "config.json")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    CONFIG.update(data.get("CONFIG", {}))
    for name, proto in data.get("PROTOCOLS", {}).items():
        if valid_protocol(proto):
            PROTOCOLS[name] = proto
        else:
            print(f"config.json: protocol '{name}' has an old format, ignored")
    if CONFIG["PROTOCOL"] not in PROTOCOLS:
        CONFIG["PROTOCOL"] = next(iter(PROTOCOLS))
    print(f"Loaded overrides from {path}")


def valid_protocol(proto):
    return (isinstance(proto, dict)
            and isinstance(proto.get("rest_start"), dict) and isinstance(proto.get("rest_end"), dict)
            and isinstance(proto.get("cycle"), list) and all(isinstance(t, dict) for t in proto["cycle"]))


def task_color(task_type, freq):
    if task_type == "hold":
        return COLORS["hold"]
    if freq <= 0:
        return COLORS["free"]
    if freq < SLOW_BELOW:
        return COLORS["slow"]
    return COLORS["fast"] if freq > FAST_ABOVE else COLORS["normal"]


def blend(color, amount):
    """Mix `color` with the background (amount 0 = background, 1 = color)."""
    return [int(b + (c - b) * amount) for c, b in zip(color, CONFIG["BACKGROUND"])]


def first_line(text):
    return text.split("\n")[0]


def build_timeline(protocol):
    """Returns (segments, number of cycles).
    Segment: dict(type, name, freq, anim, color, start, end). Consecutive identical
    tasks are merged (e.g. the rest at the end of the cycle + the final rest)."""
    proto = PROTOCOLS[protocol]
    guide = proto.get("guide", "circle")
    total = CONFIG["SCAN_DURATION"]
    cycle = proto["cycle"]
    cycle_dur = sum(t["time"] for t in cycle)
    timeline = []

    def end():
        return timeline[-1]["end"] if timeline else 0.0

    def add(task, dur):
        if dur <= 0:
            return
        kind = task.get("type", "breath")
        freq = task.get("freq", 0) if kind == "breath" else 0
        if timeline and (timeline[-1]["type"], timeline[-1]["name"], timeline[-1]["freq"]) == (kind, task["name"], freq):
            timeline[-1]["end"] += dur
            return
        timeline.append(dict(type=kind, name=task["name"], freq=freq,
                             anim="hold" if kind == "hold" else guide,
                             color=task_color(kind, freq), start=end(), end=end() + dur))

    add(proto["rest_start"], proto["rest_start"]["time"])
    n = 0
    while cycle_dur > 0 and end() + cycle_dur + proto["rest_end"]["time"] <= total:
        for task in cycle:
            add(task, task["time"])
        n += 1
    add(proto["rest_end"], total - end())
    return timeline, n


def smoothstep(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


class BreathCurve:
    """Breathing curve y(t) in [-1, 1] (+1 = full inspiration, -1 = full expiration),
    shared by the sine curve and the pulsing circle.

    - breathing task (freq > 0): y = sin(phase). The phase is continuous between
      consecutive breathing tasks; after free breathing it starts at 0 (going up),
      after a breath hold at the bottom.
    - free breathing (freq 0): y = 0.
    - breath hold: y = -1 (held after expiration).
    - Before a breath hold the previous task always ends fully exhaled (y = -1):
      a breathing task gets its frequency slightly adjusted so that its last breath
      ends at the bottom; free breathing glides down over EXHALE_TIME s.
    - Before free breathing, the curve fades to 0 over SINE_RAMP s.
    The adjusted frequency is stored in each segment as "freq_eff" (breaths/min).
    """

    def __init__(self, timeline):
        self.starts = [seg["start"] for seg in timeline]
        self.segs = []
        bottom = -math.pi / 2
        phase, prev = 0.0, None
        for k, seg in enumerate(timeline):
            nxt = timeline[k + 1] if k + 1 < len(timeline) else None
            dur = seg["end"] - seg["start"]
            hold_before = prev is not None and prev["type"] == "hold"
            hold_after = nxt is not None and nxt["type"] == "hold"
            seg["freq_eff"] = 0.0
            if seg["type"] == "hold":
                self.segs.append(("hold",))
            elif seg["freq"] > 0:
                if hold_before:
                    phase = bottom
                elif not (prev and prev["type"] == "breath" and prev["freq"] > 0):
                    phase = 0.0
                cycles = seg["freq"] / 60 * dur
                if hold_after:                              # whole number of breaths ending at the bottom
                    base = ((bottom - phase) / (2 * math.pi)) % 1.0
                    k_best = max(0 if base > 1e-9 else 1, round(cycles - base))
                    cycles = base + k_best
                f = cycles / dur
                fade = not hold_after and not (nxt and nxt["type"] == "breath" and nxt["freq"] > 0)
                self.segs.append(("sine", f, phase, fade))
                seg["freq_eff"] = f * 60
                phase += 2 * math.pi * cycles
            else:
                self.segs.append(("free", hold_before, hold_after))
            prev = seg

    def __call__(self, t):
        k = bisect.bisect_right(self.starts, t) - 1
        if k < 0 or not self.segs:
            return 0.0
        start = self.starts[k]
        end = self.starts[k + 1] if k + 1 < len(self.starts) else CONFIG["SCAN_DURATION"]
        if t >= end:
            return 0.0
        kind, *p = self.segs[k]
        if kind == "hold":
            return -1.0
        if kind == "sine":
            f, phase0, fade = p
            amp = smoothstep((end - t) / CONFIG["SINE_RAMP"]) if fade else 1.0
            return amp * math.sin(phase0 + 2 * math.pi * f * (t - start))
        hold_before, hold_after = p                         # free breathing
        half = (end - start) / 2
        y = 0.0
        if hold_before:                                     # come back up from the bottom
            y -= 1 - smoothstep((t - start) / min(CONFIG["SINE_RAMP"], half))
        if hold_after:                                      # breathe out before the hold
            y -= smoothstep(1 - (end - t) / min(CONFIG["EXHALE_TIME"], half))
        return max(-1.0, y)


# =============================================================================
# DRAWING
# =============================================================================
class Renderer:
    def __init__(self, surface):
        self.s = surface
        self.w, self.h = surface.get_size()
        self.cx, self.cy = self.w // 2, int(self.h * 0.55)
        self.radius = int(min(self.w, self.h) * 0.2)
        self.font_text = pygame.font.Font(None, int(self.h * 0.09))
        self.font_big = pygame.font.Font(None, int(self.h * 0.2))
        self.font_mid = pygame.font.Font(None, int(self.h * 0.065))
        self.font_small = pygame.font.Font(None, int(self.h * 0.04))
        self.curve = None

    # ---- primitives ----
    def text(self, msg, font, color, center_y, center_x=None):
        lines = msg.split("\n")
        lh = font.get_linesize()
        y0 = center_y - lh * (len(lines) - 1) / 2
        for k, line in enumerate(lines):
            img = font.render(line, True, color)
            self.s.blit(img, img.get_rect(center=(center_x or self.cx, int(y0 + k * lh))))

    def ring(self, color, frac, radius, width):
        """Arc starting at 12 o'clock, `frac` of full circle remaining."""
        if frac <= 0:
            return
        n = max(2, int(180 * frac))
        angles = [math.pi / 2 + 2 * math.pi * frac * k / n for k in range(n + 1)]
        outer = [(self.cx + radius * math.cos(a), self.cy - radius * math.sin(a)) for a in angles]
        r_in = radius - width
        inner = [(self.cx + r_in * math.cos(a), self.cy - r_in * math.sin(a)) for a in reversed(angles)]
        pygame.draw.polygon(self.s, color, outer + inner)

    # ---- animations ----
    def draw_circle(self, seg, now):
        size = (1 + self.curve(now)) / 2                    # 0 = exhaled, 1 = inhaled
        r = int(self.radius * (0.35 + 0.6 * size))
        pygame.draw.circle(self.s, blend(seg["color"], 0.25), (self.cx, self.cy), r)
        pygame.draw.circle(self.s, seg["color"], (self.cx, self.cy), r, max(3, self.h // 150))

    def draw_hold(self, seg, remaining):
        width = self.h // 40
        self.ring(blend(seg["color"], 0.2), 1, self.radius, width)
        self.ring(seg["color"], remaining / (seg["end"] - seg["start"]), self.radius, width)
        img = self.font_big.render(str(int(math.ceil(remaining))), True, seg["color"])
        self.s.blit(img, img.get_rect(center=(self.cx, self.cy)))

    def draw_sine(self, seg, now):
        speed = self.w / CONFIG["SINE_WINDOW"]           # px per second
        dot_x = int(self.w * CONFIG["SINE_DOT_X"])
        amp = self.h * CONFIG["SINE_AMPLITUDE"]
        step = max(2, self.w // 400)
        width = max(3, self.h // 180)

        def point(x):
            return (x, self.cy - amp * self.curve(now + (x - dot_x) / speed))

        past = [point(x) for x in range(0, dot_x + 1, step)] + [point(dot_x)]
        future = [point(dot_x)] + [point(x) for x in range(dot_x, self.w + step, step)]
        pygame.draw.lines(self.s, blend(CONFIG["TEXT_COLOR"], 0.2), False, past, width)
        pygame.draw.lines(self.s, blend(CONFIG["TEXT_COLOR"], 0.75), False, future, width)

        y = self.curve(now)
        dot = (dot_x, int(self.cy - amp * y))
        r = max(8, self.h // 28)
        pygame.draw.circle(self.s, seg["color"], dot, r)
        pygame.draw.circle(self.s, CONFIG["BACKGROUND"], dot, r // 3)

        if CONFIG["SHOW_IN_OUT"] and seg["freq"] > 0:
            dy = self.curve(now + 0.05) - y
            if abs(dy) > 1e-4:
                label = "IN" if dy > 0 else "OUT"
                self.text(label, self.font_mid, seg["color"], dot[1], dot_x - int(self.w * 0.09))

    def draw_warning(self, next_seg, remaining):
        """Banner at the bottom: 'Get ready: <next instruction>  3'."""
        color = CONFIG["WARNING_COLOR"]
        rect = pygame.Rect(0, 0, int(self.w * 0.7), int(self.h * 0.1))
        rect.center = (self.cx, int(self.h * 0.86))
        pygame.draw.rect(self.s, blend(color, 0.15), rect, border_radius=rect.h // 3)
        pygame.draw.rect(self.s, color, rect, max(3, self.h // 250), border_radius=rect.h // 3)
        frac = remaining / CONFIG["WARNING_TIME"]
        bar = pygame.Rect(rect.x + rect.h // 3, rect.bottom - rect.h // 6,
                          int((rect.w - 2 * rect.h // 3) * frac), rect.h // 12)
        pygame.draw.rect(self.s, color, bar)
        msg = f"Get ready:  {first_line(next_seg['name'])}   {int(math.ceil(remaining))}"
        img = self.font_mid.render(msg, True, CONFIG["TEXT_COLOR"])
        self.s.blit(img, img.get_rect(center=(rect.centerx, rect.centery - rect.h // 12)))

    def draw_segment(self, seg, now, next_seg):
        remaining = max(0.0, seg["end"] - now)
        if seg["anim"] == "sine":
            self.draw_sine(seg, now)
        elif seg["anim"] == "hold":
            self.draw_hold(seg, remaining)
        else:
            self.draw_circle(seg, now)
        self.text(seg["name"], self.font_text, seg["color"], int(self.h * 0.13))
        if next_seg and remaining <= CONFIG["WARNING_TIME"]:
            self.draw_warning(next_seg, remaining)

    def timer(self, now):
        fmt = lambda t: f"{int(t) // 60:02d}:{int(t) % 60:02d}"
        img = self.font_small.render(f"{fmt(now)} / {fmt(CONFIG['SCAN_DURATION'])}",
                                     True, blend(CONFIG["TEXT_COLOR"], 0.5))
        self.s.blit(img, img.get_rect(center=(self.cx, int(self.h * 0.965))))

    def draw_message(self, msg):
        self.text(msg, self.font_text, CONFIG["TEXT_COLOR"], self.h // 2)


# =============================================================================
# SETTINGS WINDOW
# =============================================================================
def launcher():
    """Settings window. Applies the values to CONFIG / PROTOCOLS.
    Returns True to start a run, False to quit."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("MRI breathing instructions")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=12)
    frm.grid()
    fields = {}                  # CONFIG key -> (tk variable, type)
    result = {"start": False}
    # task rows of the protocol being edited: dict(type, name, time, freq) of tk variables
    state = {"proto": CONFIG["PROTOCOL"], "start": None, "cycle": [], "end": None, "building": True}
    guides = {"Pulsing circle": "circle", "Sine curve": "sine"}

    def fmt(v):
        return f"{v:g}" if isinstance(v, (int, float)) else str(v)

    def section(title, row):
        box = ttk.LabelFrame(frm, text=title, padding=8)
        box.grid(row=row, column=0, sticky="ew", pady=4)
        return box

    def watched(value):
        var = tk.StringVar(value=value)
        var.trace_add("write", lambda *_: refresh())
        return var

    def entry(box, row, label, value, unit, col=0):
        var = watched(fmt(value))
        ttk.Label(box, text=label).grid(row=row, column=col, sticky="w", padx=(0, 6))
        ttk.Entry(box, textvariable=var, width=7).grid(row=row, column=col + 1)
        ttk.Label(box, text=unit).grid(row=row, column=col + 2, sticky="w", padx=(4, 12))
        return var

    def number(box, row, label, key, kind=float, unit="s", col=0):
        fields[key] = (entry(box, row, label, CONFIG[key], unit, col), kind)

    def check(box, row, label, key, col=0):
        var = tk.BooleanVar(value=CONFIG[key])
        ttk.Checkbutton(box, text=label, variable=var).grid(row=row, column=col, columnspan=3, sticky="w")
        fields[key] = (var, bool)

    def to_number(var, what, minimum=0.0, strict=False):
        try:
            value = float(var.get())
        except ValueError:
            raise ValueError(f"{what}: '{var.get()}' is not a number") from None
        if value < minimum or (strict and value == minimum):
            raise ValueError(f"{what} must be {'>' if strict else '>='} {minimum:g}")
        return value

    # ---- protocol & timing ----
    box = section("Protocol and timing", 0)
    proto_var = tk.StringVar(value=state["proto"])
    ttk.Label(box, text="Protocol").grid(row=0, column=0, sticky="w")
    combo = ttk.Combobox(box, textvariable=proto_var, values=list(PROTOCOLS), state="readonly", width=12)
    combo.grid(row=0, column=1, columnspan=2, sticky="w", pady=(0, 6))
    combo.bind("<<ComboboxSelected>>", lambda e: switch_protocol())
    ttk.Label(box, text="Breathing guide").grid(row=0, column=3, sticky="w")
    guide_var = watched("")
    ttk.Combobox(box, textvariable=guide_var, values=list(guides), state="readonly", width=14).grid(
        row=0, column=4, columnspan=2, sticky="w", pady=(0, 6))
    number(box, 1, "Scan duration", "SCAN_DURATION")
    number(box, 1, "Warning before change", "WARNING_TIME", col=3)
    cycles_var = tk.StringVar(value="")
    ttk.Label(box, text="Number of cycles").grid(row=2, column=0, sticky="w", padx=(0, 6))
    ttk.Entry(box, textvariable=cycles_var, width=7).grid(row=2, column=1)
    ttk.Button(box, text="Set scan duration from cycles", command=lambda: duration_from_cycles()).grid(
        row=2, column=2, columnspan=4, sticky="w", padx=(4, 0), pady=(4, 0))

    # ---- tasks of the selected protocol ----
    task_box = section("Tasks  (text: use \\n for a new line;  freq 0 = free breathing)", 1)

    def make_row(task, kind=None):
        kind = kind or task.get("type", "breath")
        return {
            "type": kind,
            "name": watched(task["name"].replace("\n", "\\n")),
            "time": watched(fmt(task["time"])),
            "freq": watched(fmt(task.get("freq", 0))) if kind == "breath" else None,
        }

    def load_protocol():
        proto = PROTOCOLS[state["proto"]]
        state["building"] = True
        state["start"] = make_row(proto["rest_start"], "breath")
        state["cycle"] = [make_row(t) for t in proto["cycle"]]
        state["end"] = make_row(proto["rest_end"], "breath")
        guide_var.set(next(k for k, v in guides.items() if v == proto.get("guide", "circle")))
        render_tasks()
        try:
            cycles_var.set(str(build_timeline(state["proto"])[1]))
        except (ValueError, KeyError):
            pass

    def switch_protocol():
        try:
            apply()                                        # keep the edits of the previous protocol
        except (ValueError, KeyError):
            pass
        state["proto"] = proto_var.get()
        load_protocol()

    def render_tasks():
        state["building"] = True
        for w in task_box.winfo_children():
            w.destroy()
        for col, title in enumerate(["", "Type", "Text on screen", "Time (s)", "Freq (breaths/min)"]):
            ttk.Label(task_box, text=title, foreground="gray").grid(row=0, column=col, sticky="w", padx=3)

        def row_widgets(r, grid_row, label):
            ttk.Label(task_box, text=label).grid(row=grid_row, column=0, sticky="w", padx=3)
            ttk.Label(task_box, text="Breath hold" if r["type"] == "hold" else "Breathing").grid(
                row=grid_row, column=1, sticky="w", padx=3)
            ttk.Entry(task_box, textvariable=r["name"], width=26).grid(row=grid_row, column=2, padx=3, pady=1)
            ttk.Entry(task_box, textvariable=r["time"], width=6).grid(row=grid_row, column=3, padx=3)
            if r["freq"] is not None:
                ttk.Entry(task_box, textvariable=r["freq"], width=6).grid(row=grid_row, column=4, padx=3, sticky="w")
            else:
                ttk.Label(task_box, text="  -").grid(row=grid_row, column=4, sticky="w", padx=3)

        row_widgets(state["start"], 1, "Rest at start")
        ttk.Label(task_box, text="Cycle (repeated):").grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 2))
        n = len(state["cycle"])
        for i, r in enumerate(state["cycle"]):
            row_widgets(r, i + 3, f"   {i + 1}.")
            btns = ttk.Frame(task_box)
            btns.grid(row=i + 3, column=5, sticky="w")
            for k, (txt, cmd, ok) in enumerate([("↑", lambda i=i: move(i, -1), i > 0),
                                                 ("↓", lambda i=i: move(i, 1), i < n - 1),
                                                 ("✕", lambda i=i: delete(i), True)]):
                ttk.Button(btns, text=txt, width=2, command=cmd,
                           state="normal" if ok else "disabled").grid(row=0, column=k)
        adds = ttk.Frame(task_box)
        adds.grid(row=n + 3, column=1, columnspan=5, sticky="w", pady=(4, 8))
        ttk.Button(adds, text="+ Breathing task / rest", command=lambda: add("breath")).grid(row=0, column=0, padx=3)
        ttk.Button(adds, text="+ Breath hold", command=lambda: add("hold")).grid(row=0, column=1, padx=3)
        row_widgets(state["end"], n + 4, "Rest at end (min)")
        state["building"] = False
        refresh()

    def move(i, d):
        c = state["cycle"]
        c[i], c[i + d] = c[i + d], c[i]
        render_tasks()

    def delete(i):
        del state["cycle"][i]
        render_tasks()

    def add(kind):
        state["cycle"].append(make_row(NEW_TASK[kind]))
        render_tasks()

    # ---- sine curve ----
    box = section("Breathing guide", 2)
    number(box, 0, "Sine: visible time window", "SINE_WINDOW")
    check(box, 0, 'Sine: show "IN" / "OUT"', "SHOW_IN_OUT", col=3)
    number(box, 1, "Free breathing: exhale before hold", "EXHALE_TIME")

    # ---- display ----
    box = section("Display", 3)
    number(box, 0, "Width", "SCREEN_WIDTH", int, "px (0 = auto)")
    number(box, 0, "Height", "SCREEN_HEIGHT", int, "px (0 = auto)", col=3)
    number(box, 1, "Screen number", "DISPLAY_INDEX", int, "(0 = main)")
    check(box, 1, "Show timer", "SHOW_TIMER", col=3)
    check(box, 2, "Fullscreen", "FULLSCREEN")
    check(box, 2, "Mirror (flip left-right)", "MIRROR", col=3)

    info = ttk.Label(frm, text="")
    info.grid(row=4, column=0, sticky="w", pady=(6, 2))

    def read_task(r, what, kind=None, strict=True):
        task = {"type": kind or r["type"], "name": r["name"].get().replace("\\n", "\n"),
                "time": to_number(r["time"], f"{what}: time", strict=strict)}
        if r["freq"] is not None:
            task["freq"] = to_number(r["freq"], f"{what}: freq")
        if kind:                                            # rest at start / end: no type key
            del task["type"]
        return task

    def apply():
        """Copy the window values into CONFIG / PROTOCOLS. Raises ValueError if invalid."""
        for key, (var, kind) in fields.items():
            if kind is bool:
                CONFIG[key] = var.get()
                continue
            try:
                value = kind(var.get())
            except ValueError:
                raise ValueError(f"{key}: '{var.get()}' is not a number") from None
            if value < 0:
                raise ValueError(f"{key} must be >= 0")
            CONFIG[key] = value
        if CONFIG["SINE_WINDOW"] <= 0:
            raise ValueError("Visible time window must be > 0")
        if not state["cycle"]:
            raise ValueError("add at least one task to the cycle")
        proto = PROTOCOLS[state["proto"]]
        proto["guide"] = guides.get(guide_var.get(), "circle")
        proto["rest_start"] = read_task(state["start"], "Rest at start", "breath", strict=False)
        proto["cycle"] = [read_task(r, f"Task {i + 1}") for i, r in enumerate(state["cycle"])]
        proto["rest_end"] = read_task(state["end"], "Rest at end", "breath", strict=False)
        CONFIG["PROTOCOL"] = state["proto"]

    def refresh():
        if state["building"]:
            return
        try:
            apply()
            proto = PROTOCOLS[state["proto"]]
            tl, n = build_timeline(state["proto"])
            cycle_dur = sum(t["time"] for t in proto["cycle"])
            last = tl[-1]["end"] - tl[-1]["start"]
            if n == 0:
                info.config(text="No complete cycle fits in the scan duration", foreground="red")
            else:
                info.config(text=f"{n} cycle(s) of {cycle_dur:g} s, rest after the last instruction: {last:g} s",
                            foreground="")
        except (ValueError, KeyError) as e:
            info.config(text=f"Invalid value: {e}", foreground="red")

    def duration_from_cycles():
        try:
            apply()
            n = int(to_number(cycles_var, "Number of cycles", 1))
        except (ValueError, KeyError) as e:
            messagebox.showerror("Invalid settings", str(e))
            return
        proto = PROTOCOLS[state["proto"]]
        total = (proto["rest_start"]["time"] + n * sum(t["time"] for t in proto["cycle"])
                 + proto["rest_end"]["time"])
        fields["SCAN_DURATION"][0].set(fmt(total))

    def checked(action):
        def run_it(*_):
            try:
                apply()
            except (ValueError, KeyError) as e:
                messagebox.showerror("Invalid settings", str(e))
                return
            action()
        return run_it

    def start():
        result["start"] = True
        root.destroy()

    def save():
        path = os.path.join(base_dir(), "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"CONFIG": CONFIG, "PROTOCOLS": PROTOCOLS}, f, indent=2)
        messagebox.showinfo("Saved", f"Settings saved to\n{path}")

    buttons = ttk.Frame(frm)
    buttons.grid(row=5, column=0, sticky="e", pady=(6, 0))
    ttk.Button(buttons, text="Save as default", command=checked(save)).grid(row=0, column=0, padx=4)
    ttk.Button(buttons, text="Quit", command=root.destroy).grid(row=0, column=1, padx=4)
    ttk.Button(buttons, text="Start", command=checked(start)).grid(row=0, column=2, padx=4)
    root.bind("<Return>", checked(start))
    root.start = checked(start)  # used by tests

    load_protocol()
    root.mainloop()
    return result["start"]


# =============================================================================
# MAIN
# =============================================================================
def main():
    load_overrides()
    while True:
        if CONFIG["SHOW_LAUNCHER"] and not launcher():
            return
        run(CONFIG["PROTOCOL"])
        if not CONFIG["SHOW_LAUNCHER"]:
            return


def run(protocol):
    if sys.platform == "win32":
        # real pixels instead of Windows display scaling (125 %, 150 % ...)
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    pygame.init()
    desktops = pygame.display.get_desktop_sizes()
    display = CONFIG["DISPLAY_INDEX"] if CONFIG["DISPLAY_INDEX"] < len(desktops) else 0
    native_w, native_h = desktops[display]
    size = (CONFIG["SCREEN_WIDTH"] or native_w, CONFIG["SCREEN_HEIGHT"] or native_h)
    print(f"screen {display}: native {native_w}x{native_h}, using {size[0]}x{size[1]}")
    flags = pygame.FULLSCREEN if CONFIG["FULLSCREEN"] else 0
    screen = pygame.display.set_mode(size, flags, display=display)
    pygame.display.set_caption("MRI instructions")
    pygame.mouse.set_visible(False)
    canvas = pygame.Surface(screen.get_size())
    rnd = Renderer(canvas)
    clock = pygame.time.Clock()
    start_stop_keys = {pygame.key.key_code(k) for k in CONFIG["START_STOP_KEYS"]}

    def start_stop_pressed(events):
        return any(e.type == pygame.KEYDOWN and e.key in start_stop_keys for e in events)

    def present():
        out = pygame.transform.flip(canvas, True, False) if CONFIG["MIRROR"] else canvas
        screen.blit(out, (0, 0))
        pygame.display.flip()

    def quit_requested(events):
        return any(e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE)
                   for e in events)

    # ---- wait for start key ----
    started = False
    while not started:
        events = pygame.event.get()
        if quit_requested(events):
            pygame.quit()
            return
        started = start_stop_pressed(events)
        canvas.fill(CONFIG["BACKGROUND"])
        rnd.draw_message(WAITING_TEXT)
        present()
        clock.tick(CONFIG["FPS"])

    timeline, _ = build_timeline(protocol)
    rnd.curve = BreathCurve(timeline)
    t0 = time.perf_counter()
    log, idx, aborted = [], -1, False

    # ---- run timeline ----
    while True:
        now = time.perf_counter() - t0
        if now >= CONFIG["SCAN_DURATION"]:
            break
        events = pygame.event.get()
        if quit_requested(events) or start_stop_pressed(events):
            aborted = True
            break
        while idx + 1 < len(timeline) and now >= timeline[idx + 1]["start"]:
            idx += 1
            seg = timeline[idx]
            breath = seg["type"] == "breath"
            log.append([f"{now:.3f}", f"{seg['start']:.3f}", seg["type"],
                        seg["freq"] if breath else "", f"{seg['freq_eff']:.2f}" if breath else "",
                        first_line(seg["name"])])
        next_seg = timeline[idx + 1] if idx + 1 < len(timeline) else None
        canvas.fill(CONFIG["BACKGROUND"])
        rnd.draw_segment(timeline[idx], now, next_seg)
        if CONFIG["SHOW_TIMER"]:
            rnd.timer(now)
        present()
        clock.tick(CONFIG["FPS"])

    # ---- write log ----
    if CONFIG["LOG"]:
        path = os.path.join(base_dir(), f"log_{datetime.now():%Y%m%d_%H%M%S}_{protocol}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["real_onset_s", "planned_onset_s", "type", "freq_bpm", "freq_shown_bpm", "text"])
            w.writerows(log)
            if aborted:
                w.writerow([f"{time.perf_counter() - t0:.3f}", "", "stopped", "", ""])

    # ---- end screen (ESC or 5 s) ----
    end_t = time.perf_counter()
    while not aborted and time.perf_counter() - end_t < 5:
        if quit_requested(pygame.event.get()):
            break
        canvas.fill(CONFIG["BACKGROUND"])
        rnd.draw_message(END_TEXT)
        present()
        clock.tick(CONFIG["FPS"])

    pygame.quit()


if __name__ == "__main__":
    main()
