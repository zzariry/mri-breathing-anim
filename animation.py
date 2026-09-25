"""
MRI breathing instruction animation.

A settings window opens first (protocol, timings, breathing rates, display);
click Start (or press Enter) to open the fullscreen animation. After each run the
settings window comes back for the next one. "Save as default" writes config.json.

Two protocols:

  "breathhold" : REST 30 s | [ NORMAL 20 s | FAST 20 s | HOLD 20 s ] x N | REST >= 30 s
  "paced"      : REST 30 s | [ FAST 20 s | NORMAL 20 s | SLOW 20 s ] x N | REST >= 30 s
                 guided by a sine curve scrolling right -> left; the subject follows the
                 dot (curve going up = breathe IN, going down = breathe OUT).

- The cycle is repeated as many times as fits between REST_START and REST_END; the
  remaining time is added to the final rest.
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
    "REST_START": 30,            # s, rest at the beginning
    "REST_END": 30,              # s, minimum rest at the end
    "DELAY_BETWEEN": 0,          # s, extra rest between two cycles (0 = cycles back to back)
    "WARNING_TIME": 5,           # s, warning shown before each change of instruction
    "SCREEN_WIDTH": 1920,
    "SCREEN_HEIGHT": 1080,
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
    "SHOW_IN_OUT": True,         # show "IN" / "OUT" next to the dot
}

# How each instruction looks. Timing is defined in PROTOCOLS.
#   anim "circle": circle pulsing at `rate` breaths/min
#   anim "hold"  : ring emptying + remaining seconds
#   anim "sine"  : scrolling sine curve at `rate` breaths/min (rate 0 = flat line)
ACTIONS = {
    # breath-hold protocol
    "rest":   {"text": "RELAX\nbreathe freely",   "anim": "circle", "rate": 12, "color": [60, 160, 110]},
    "normal": {"text": "BREATHE NORMALLY",        "anim": "circle", "rate": 12, "color": [40, 110, 200]},
    "fast":   {"text": "BREATHE FAST",            "anim": "circle", "rate": 30, "color": [220, 120, 0]},
    "hold":   {"text": "HOLD YOUR BREATH",        "anim": "hold",               "color": [210, 50, 50]},
    # paced (sine) protocol
    "paced_rest":   {"text": "RELAX\nbreathe freely", "anim": "sine", "rate": 0,  "color": [60, 160, 110]},
    "paced_fast":   {"text": "BREATHE FAST",          "anim": "sine", "rate": 30, "color": [220, 120, 0]},
    "paced_normal": {"text": "BREATHE NORMALLY",      "anim": "sine", "rate": 12, "color": [40, 110, 200]},
    "paced_slow":   {"text": "BREATHE SLOWLY",        "anim": "sine", "rate": 6,  "color": [130, 60, 180]},
}

# rest: action used for the rest periods; cycle: [action, duration in s], repeated.
PROTOCOLS = {
    "breathhold": {
        "rest": "rest",
        "cycle": [["normal", 20], ["fast", 20], ["hold", 20]],
    },
    "paced": {
        "rest": "paced_rest",
        "cycle": [["paced_fast", 20], ["paced_normal", 20], ["paced_slow", 20]],
    },
}

WAITING_TEXT = "Please stay still\nThe scan will start soon"
END_TEXT = "Scan finished\nThank you"


# =============================================================================
# HELPERS
# =============================================================================
def base_dir():
    """Folder containing the exe (frozen) or this script."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_overrides():
    """Override the settings above with an optional config.json."""
    path = os.path.join(base_dir(), "config.json")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    CONFIG.update(data.get("CONFIG", {}))
    for name, act in data.get("ACTIONS", {}).items():
        ACTIONS.setdefault(name, {}).update(act)
    PROTOCOLS.update(data.get("PROTOCOLS", {}))
    print(f"Loaded overrides from {path}")


def blend(color, amount):
    """Mix `color` with the background (amount 0 = background, 1 = color)."""
    return [int(b + (c - b) * amount) for c, b in zip(color, CONFIG["BACKGROUND"])]


def first_line(text):
    return text.split("\n")[0]


def build_timeline(protocol):
    """List of segments: dict(name, start, end, **action)."""
    proto = PROTOCOLS[protocol]
    total = CONFIG["SCAN_DURATION"]
    cycle = proto["cycle"]
    cycle_dur = sum(d for _, d in cycle)
    timeline = []

    def add(name, dur):
        start = timeline[-1]["end"] if timeline else 0.0
        timeline.append(dict(ACTIONS[name], name=name, start=start, end=start + dur))

    add(proto["rest"], CONFIG["REST_START"])
    n = 0
    while True:
        gap = CONFIG["DELAY_BETWEEN"] if n else 0
        if timeline[-1]["end"] + gap + cycle_dur + CONFIG["REST_END"] > total:
            break
        if gap:
            add(proto["rest"], gap)
        for name, dur in cycle:
            add(name, dur)
        n += 1
    if timeline[-1]["end"] < total:
        add(proto["rest"], total - timeline[-1]["end"])
    return timeline


class BreathCurve:
    """Continuous breathing curve y(t) in [-1, 1] built from the "sine" segments.

    The phase is continuous across rate changes. After a flat part (rate 0 or
    other animation) it restarts at 0 so the curve starts smoothly by going up
    (inspiration); before a flat part the amplitude fades out over SINE_RAMP s.
    """

    def __init__(self, timeline):
        self.starts, self.segs = [], []
        phase = 0.0
        for k, seg in enumerate(timeline):
            f = seg.get("rate", 0) / 60 if seg["anim"] == "sine" else 0.0
            nxt = timeline[k + 1] if k + 1 < len(timeline) else None
            fade = f > 0 and not (nxt and nxt["anim"] == "sine" and nxt.get("rate", 0) > 0)
            if f == 0:
                phase = 0.0
            self.starts.append(seg["start"])
            self.segs.append((seg["start"], seg["end"], f, phase, fade))
            phase += 2 * math.pi * f * (seg["end"] - seg["start"])

    def __call__(self, t):
        k = bisect.bisect_right(self.starts, t) - 1
        if k < 0 or t >= self.segs[-1][1]:
            return 0.0
        start, end, f, phase0, fade = self.segs[k]
        if f == 0:
            return 0.0
        amp = 1.0
        if fade:
            x = min(1.0, (end - t) / CONFIG["SINE_RAMP"])
            amp = x * x * (3 - 2 * x)                     # smoothstep
        return amp * math.sin(phase0 + 2 * math.pi * f * (t - start))


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
    def draw_circle(self, seg, elapsed):
        rate = seg.get("rate", 12)
        phase = (1 - math.cos(2 * math.pi * rate / 60 * elapsed)) / 2 if rate else 0.5
        r = int(self.radius * (0.5 + 0.45 * phase))
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

        if CONFIG["SHOW_IN_OUT"] and seg.get("rate", 0) > 0:
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
        msg = f"Get ready:  {first_line(next_seg['text'])}   {int(math.ceil(remaining))}"
        img = self.font_mid.render(msg, True, CONFIG["TEXT_COLOR"])
        self.s.blit(img, img.get_rect(center=(rect.centerx, rect.centery - rect.h // 12)))

    def draw_segment(self, seg, now, next_seg):
        remaining = max(0.0, seg["end"] - now)
        if seg["anim"] == "sine":
            self.draw_sine(seg, now)
        elif seg["anim"] == "hold":
            self.draw_hold(seg, remaining)
        else:
            self.draw_circle(seg, now - seg["start"])
        self.text(seg["text"], self.font_text, seg["color"], int(self.h * 0.13))
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
    """Settings window. Applies the values to CONFIG / PROTOCOLS / ACTIONS.
    Returns True to start a run, False to quit."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("MRI breathing instructions")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=12)
    frm.grid()
    fields = {}                  # CONFIG key -> (tk variable, type)
    phase_vars = []              # (index in cycle, action name, duration var, rate var or None)
    result = {"start": False}

    def fmt(v):
        return f"{v:g}" if isinstance(v, float) else str(v)

    def section(title, row):
        box = ttk.LabelFrame(frm, text=title, padding=8)
        box.grid(row=row, column=0, sticky="ew", pady=4)
        return box

    def entry(box, row, label, value, unit, col=0):
        var = tk.StringVar(value=fmt(value))
        ttk.Label(box, text=label).grid(row=row, column=col, sticky="w", padx=(0, 6))
        ttk.Entry(box, textvariable=var, width=7).grid(row=row, column=col + 1)
        ttk.Label(box, text=unit).grid(row=row, column=col + 2, sticky="w", padx=(4, 12))
        var.trace_add("write", lambda *_: refresh())
        return var

    def number(box, row, label, key, kind=float, unit="s", col=0):
        fields[key] = (entry(box, row, label, CONFIG[key], unit, col), kind)

    def check(box, row, label, key, col=0):
        var = tk.BooleanVar(value=CONFIG[key])
        ttk.Checkbutton(box, text=label, variable=var).grid(row=row, column=col, columnspan=3, sticky="w")
        fields[key] = (var, bool)

    # ---- protocol & timing ----
    box = section("Protocol and timing", 0)
    proto_var = tk.StringVar(value=CONFIG["PROTOCOL"])
    ttk.Label(box, text="Protocol").grid(row=0, column=0, sticky="w")
    combo = ttk.Combobox(box, textvariable=proto_var, values=list(PROTOCOLS), state="readonly", width=12)
    combo.grid(row=0, column=1, columnspan=3, sticky="w", pady=(0, 6))
    combo.bind("<<ComboboxSelected>>", lambda e: build_phases())
    number(box, 1, "Scan duration", "SCAN_DURATION")
    number(box, 1, "Warning before change", "WARNING_TIME", col=3)
    number(box, 2, "Rest at start", "REST_START")
    number(box, 2, "Rest at end (min)", "REST_END", col=3)
    number(box, 3, "Rest between cycles", "DELAY_BETWEEN")

    # ---- phases of the selected protocol ----
    phase_box = section("Phases (one cycle, repeated)", 1)

    def build_phases():
        for w in phase_box.winfo_children():
            w.destroy()
        phase_vars.clear()
        for i, (name, dur) in enumerate(PROTOCOLS[proto_var.get()]["cycle"]):
            act = ACTIONS[name]
            ttk.Label(phase_box, text=f"{i + 1}. {first_line(act['text'])}", width=22).grid(
                row=i, column=0, sticky="w")
            dur_var = entry(phase_box, i, "duration", dur, "s", col=1)
            rate_var = None
            if act["anim"] != "hold":
                rate_var = entry(phase_box, i, "rate", act.get("rate", 12), "breaths/min", col=4)
            phase_vars.append((i, name, dur_var, rate_var))
        refresh()

    # ---- sine curve ----
    box = section("Paced curve", 2)
    number(box, 0, "Visible time window", "SINE_WINDOW")
    check(box, 0, 'Show "IN" / "OUT"', "SHOW_IN_OUT", col=3)

    # ---- display ----
    box = section("Display", 3)
    number(box, 0, "Width", "SCREEN_WIDTH", int, "px")
    number(box, 0, "Height", "SCREEN_HEIGHT", int, "px", col=3)
    number(box, 1, "Screen number", "DISPLAY_INDEX", int, "(0 = main)")
    check(box, 1, "Show timer", "SHOW_TIMER", col=3)
    check(box, 2, "Fullscreen", "FULLSCREEN")
    check(box, 2, "Mirror (flip left-right)", "MIRROR", col=3)

    info = ttk.Label(frm, text="")
    info.grid(row=4, column=0, sticky="w", pady=(6, 2))

    def apply():
        """Copy the window values into the settings. Raises ValueError if invalid."""
        CONFIG["PROTOCOL"] = proto_var.get()
        for key, (var, kind) in fields.items():
            value = var.get() if kind is bool else kind(var.get())
            if kind is not bool and value < 0:
                raise ValueError(key)
            CONFIG[key] = value
        cycle = PROTOCOLS[CONFIG["PROTOCOL"]]["cycle"]
        for i, name, dur_var, rate_var in phase_vars:
            cycle[i][1] = float(dur_var.get())
            if rate_var is not None:
                ACTIONS[name]["rate"] = float(rate_var.get())
        if CONFIG["SINE_WINDOW"] <= 0 or min(d for _, d in cycle) <= 0:
            raise ValueError("durations must be > 0")

    def refresh():
        if len(phase_vars) != len(PROTOCOLS[proto_var.get()]["cycle"]):
            return                                          # window still being built
        try:
            apply()
            proto = PROTOCOLS[CONFIG["PROTOCOL"]]
            tl = build_timeline(CONFIG["PROTOCOL"])
            n = sum(s["name"] != proto["rest"] for s in tl) // len(proto["cycle"])
            last = tl[-1]["end"] - tl[-1]["start"]
            if n == 0:
                info.config(text="No complete cycle fits in the scan duration", foreground="red")
            else:
                info.config(text=f"{n} cycle(s), final rest {last:g} s", foreground="")
        except (ValueError, KeyError, ZeroDivisionError):
            info.config(text="Invalid value", foreground="red")

    def start(*_):
        try:
            apply()
        except (ValueError, KeyError) as e:
            messagebox.showerror("Invalid settings", f"Please check the values ({e}).")
            return
        result["start"] = True
        root.destroy()

    def save():
        try:
            apply()
        except (ValueError, KeyError) as e:
            messagebox.showerror("Invalid settings", f"Please check the values ({e}).")
            return
        path = os.path.join(base_dir(), "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"CONFIG": CONFIG, "ACTIONS": ACTIONS, "PROTOCOLS": PROTOCOLS}, f, indent=2)
        messagebox.showinfo("Saved", f"Settings saved to\n{path}")

    buttons = ttk.Frame(frm)
    buttons.grid(row=5, column=0, sticky="e", pady=(6, 0))
    ttk.Button(buttons, text="Save as default", command=save).grid(row=0, column=0, padx=4)
    ttk.Button(buttons, text="Quit", command=root.destroy).grid(row=0, column=1, padx=4)
    ttk.Button(buttons, text="Start", command=start).grid(row=0, column=2, padx=4)
    root.bind("<Return>", start)
    root.start = start           # used by tests

    build_phases()
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
    pygame.init()
    size = (CONFIG["SCREEN_WIDTH"], CONFIG["SCREEN_HEIGHT"])
    flags = pygame.FULLSCREEN if CONFIG["FULLSCREEN"] else 0
    screen = pygame.display.set_mode(size, flags, display=CONFIG["DISPLAY_INDEX"])
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

    timeline = build_timeline(protocol)
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
            log.append([f"{now:.3f}", f"{seg['start']:.3f}", seg["name"],
                        seg.get("rate", ""), first_line(seg["text"])])
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
            w.writerow(["real_onset_s", "planned_onset_s", "action", "rate_bpm", "text"])
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
