"""curses front-end. Reads from a background collector thread, never blocks."""

import curses
import signal
import threading
import time

from . import render as R

STATE_COLORS = {
    "RUN": "green",
    "WORK": "cyan",
    "WAIT": "yellow",
    "IDLE": "blue",
    "ORPHAN": "red",
    "EXIT": "dim",
}


class DataWorker(threading.Thread):
    def __init__(self, collector, interval=2.0):
        threading.Thread.__init__(self)
        self.daemon = True
        self.collector = collector
        self.interval = interval
        self.lock = threading.Lock()
        self.snapshot = None
        self.error = None
        self.last_cost = 0.0
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            started = time.time()
            try:
                snap = self.collector.snapshot()
                snap["refresh_ms"] = int((time.time() - started) * 1000)
                with self.lock:
                    self.snapshot = snap
                    self.error = None
            except Exception as exc:
                with self.lock:
                    self.error = "%s: %s" % (type(exc).__name__, exc)
            self.last_cost = time.time() - started
            self._stop.wait(max(0.3, self.interval - self.last_cost))

    def read(self):
        with self.lock:
            return self.snapshot, self.error

    def stop(self):
        self._stop.set()


class App(object):
    def __init__(self, collector, interval=2.0, project_dir="."):
        self.collector = collector
        self.interval = interval
        self.worker = DataWorker(collector, interval=interval)
        self.sel = 0
        self.scroll = 0
        self.paused = False
        self.show_help = False
        self.status = ""
        self.pairs = {}

    # -- lifecycle -------------------------------------------------------
    def run(self):
        # ncurses chains to whatever handler existed at initscr; installing ours
        # *before* the wrapper is what makes SIGINT/SIGTERM exit cleanly.
        def _bail(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGINT, _bail)
        signal.signal(signal.SIGTERM, _bail)
        self.worker.start()
        try:
            curses.wrapper(self._loop)
        except KeyboardInterrupt:
            pass
        finally:
            self.worker.stop()
            self.collector.stop()

    def _loop(self, stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(120)
        self._init_colors()
        try:
            curses.mousemask(0)
        except curses.error:
            pass
        while True:
            snap, err = self.worker.read()
            if snap is not None:
                self._draw(stdscr, snap, err)
            ch = stdscr.getch()
            if not self._handle_key(ch, snap):
                break
            if ch == curses.KEY_RESIZE:
                stdscr.clear()

    def _init_colors(self):
        if not curses.has_colors():
            return
        try:
            curses.start_color()
            curses.use_default_colors()
            names = {
                "green": curses.COLOR_GREEN,
                "cyan": curses.COLOR_CYAN,
                "yellow": curses.COLOR_YELLOW,
                "blue": curses.COLOR_BLUE,
                "red": curses.COLOR_RED,
                "magenta": curses.COLOR_MAGENTA,
                "dim": curses.COLOR_WHITE,
                "bold": curses.COLOR_WHITE,
            }
            for idx, (name, color) in enumerate(names.items(), start=1):
                curses.init_pair(idx, color, -1)
                self.pairs[name] = curses.color_pair(idx)
        except curses.error:
            self.pairs = {}

    def _attr(self, name, base=0):
        if name == "dim":
            return base | curses.A_DIM
        return base | self.pairs.get(name, 0)

    # -- keys ------------------------------------------------------------
    def _handle_key(self, ch, snap):
        agents = (snap or {}).get("agents") or []
        if ch in (ord("q"), ord("Q"), 27, 3):  # 3 = Ctrl+C inside curses
            if self.show_help:
                self.show_help = False
                return True
            return False
        if ch in (curses.KEY_UP, ord("k")):
            self.sel = max(0, self.sel - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.sel = min(max(0, len(agents) - 1), self.sel + 1)
        elif ch == curses.KEY_PPAGE:
            self.sel = max(0, self.sel - 8)
        elif ch == curses.KEY_NPAGE:
            self.sel = min(max(0, len(agents) - 1), self.sel + 8)
        elif ch == ord("g"):
            self.sel = 0
        elif ch == ord("G"):
            self.sel = max(0, len(agents) - 1)
        elif ch in (ord("r"), ord("R")):
            self.worker.collector.usage.refresh_now()
            self.status = "refresh requested"
        elif ch in (ord("p"), ord("P"), ord(" ")):
            self.paused = not self.paused
            self.status = "paused" if self.paused else "resumed"
        elif ch in (ord("?"), ord("h")):
            self.show_help = not self.show_help

        if self.sel >= len(agents) and agents:
            self.sel = len(agents) - 1
        return True

    # -- drawing ---------------------------------------------------------
    def _draw(self, stdscr, snap, err):
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        if h < 12 or w < 40:
            _put(stdscr, 0, 0, "terminal too small for codexmon (%dx%d)" % (w, h), curses.A_BOLD)
            stdscr.refresh()
            return

        footer_h = 1
        header_h = 2
        plan_h = self._plan_height(snap)
        if h < 24:
            plan_h = min(plan_h, 5)
        events_h = 6 if h >= 28 else (4 if h >= 22 else 0)
        agents_h = h - footer_h - header_h - events_h - plan_h
        while agents_h < 6 and events_h > 0:
            events_h -= 1
            agents_h += 1
        while agents_h < 6 and plan_h > 0:
            plan_h -= 1
            agents_h += 1
        top = 0
        self._draw_header(stdscr, snap, w, top)
        top += header_h
        used = self._draw_agents(stdscr, snap, top, w, agents_h)
        top = used
        if plan_h:
            top = self._draw_plan(stdscr, snap, top, w, plan_h)
        if events_h:
            self._draw_events(stdscr, snap, top, w, h - footer_h - top)
        self._draw_footer(stdscr, snap, err, h, w)
        if self.show_help:
            self._draw_help(stdscr, h, w)
        stdscr.refresh()

    def _draw_header(self, stdscr, snap, w, y):
        title = " codexmon "
        _put(stdscr, y, 0, R.clip(title, w), curses.A_BOLD | curses.A_REVERSE)
        x = R.disp_w(title)
        stamp = time.strftime("%H:%M:%S")
        totals = snap.get("totals") or {}
        info = "%s  %d agents  %d live  %d sub  %s tok  homes %s" % (
            stamp,
            totals.get("agents", 0),
            totals.get("live", 0),
            totals.get("subs", 0),
            R.fmt_tokens(totals.get("tokens")) + ("+" if totals.get("tokens_partial") else ""),
            ",".join(snap.get("homes") or []),
        )
        if totals.get("spinning"):
            info += "  %d SPIN" % totals["spinning"]
        x = _put(stdscr, y, x, R.clip(info, max(0, w - x - 1)), curses.A_BOLD)
        right = "%ss%s" % (self.interval, "  PAUSED" if self.paused else "")
        _put(stdscr, y + 1, max(0, w - R.disp_w(right) - 1), right, curses.A_DIM)
        _put(stdscr, y + 1, 0, "─" * (w - 1), curses.A_DIM)

    def _columns(self, agent, now, w):
        """Column spec: (label, width, text, attr_name, align)."""
        state = agent["state"]
        # SESSION is the primary identifier; the hex id lives in the detail line
        name_w = 34 if w >= 165 else 30 if w >= 145 else 26 if w >= 118 else 22 if w >= 104 else 18 if w >= 90 else 14
        model_w = 19 if w >= 112 else 12 if w >= 90 else 0
        cols = [
            ("ST", 6, R.STATE_LABEL.get(state, state), STATE_COLORS.get(state, "dim"), "left"),
            ("ROLE", 9, R.role_label(agent), "dim", "left"),
            ("SESSION", name_w, R.session_label(agent), "", "left"),
        ]
        if model_w:
            cols.append(("MODEL", model_w, R.model_label(agent), "", "left"))
        cols += [
            ("CTX", 11, R.ctx_label(agent), "", "left"),
            ("TOKENS", 8, R.fmt_tokens(agent.get("tokens_total")), "", "right"),
            ("AGE", 8, R.fmt_age(agent.get("age_s")), "", "left"),
        ]
        if w >= 82:
            cols.append(("LAST", 7, R.fmt_ago(agent.get("last_seen"), now), "", "left"))
        if w >= 125:
            cols.append(("PID", 6, str(agent.get("pid") or "—"), "", "left"))
        return cols

    def _draw_agents(self, stdscr, snap, y, w, height):
        agents = snap.get("agents") or []
        now = snap["ts"]
        sample = agents[0] if agents else {"state": "IDLE", "tokens_total": 0}
        head_cols = self._columns(sample, now, w)
        header = " ".join(
            R.pad(label, width, align) for label, width, _t, _a, align in head_cols
        )
        tools_w = max(0, w - R.disp_w(header) - 2)
        if tools_w >= 12:
            header += " TOOLS"
        label = "AGENTS"
        room = w - 1
        if R.disp_w(header) + R.disp_w(label) + 2 <= w:
            room = w - R.disp_w(label) - 2
            _put(stdscr, y, max(0, w - R.disp_w(label) - 1), label, curses.A_BOLD)
        _put(stdscr, y, 0, R.clip(header, max(0, room)), curses.A_DIM)
        y += 1
        rows = max(0, height - 2)
        if self.sel < self.scroll:
            self.scroll = self.sel
        if self.sel >= self.scroll + rows:
            self.scroll = self.sel - rows + 1
        self.scroll = max(0, min(self.scroll, max(0, len(agents) - rows)))
        visible = agents[self.scroll:self.scroll + rows]
        if not agents:
            _put(stdscr, y, 1, "no codex thread in the selected window", curses.A_DIM)
        for idx, agent in enumerate(visible):
            row_y = y + idx
            selected = (self.scroll + idx) == self.sel
            self._draw_agent_row(stdscr, agent, row_y, w, selected, now, tools_w)
        y += rows
        # detail line for the selected agent
        if agents:
            agent = agents[min(self.sel, len(agents) - 1)]
            bits = [agent.get("id") or agent.get("short")]
            turn = R.turn_label(agent)
            if turn:
                bits.append(turn)
            if agent.get("title"):
                bits.append(agent["title"])
            elif agent.get("error"):
                bits.append(agent["error"])
            if agent.get("cwd"):
                bits.append("[" + _tail_path(agent["cwd"], 24) + "]")
            text = R.clip("  ".join(bits), max(0, w - 2))
            _put(stdscr, y, 1, text, curses.A_DIM)
        return y + 1

    def _draw_agent_row(self, stdscr, agent, y, w, selected, now, tools_w):
        base = curses.A_REVERSE if selected else 0
        x = 0
        for label, width, text, color, align in self._columns(agent, now, w):
            attr = base
            if color:
                attr = self._attr(color, base)
                if color in ("green", "red"):
                    attr |= curses.A_BOLD
            if label == "CTX" and agent.get("ctx_percent") and agent["ctx_percent"] >= 80:
                attr = self._attr("red", base) | curses.A_BOLD
            x = _put(stdscr, y, x, R.pad(text, width, align) + " ", attr)
        if tools_w >= 12:
            attr = base
            if agent.get("spin"):
                attr = self._attr("red", base) | curses.A_BOLD
            _put(stdscr, y, x, R.tools_label(agent, tools_w), attr)

    def _plan_rows(self, snap):
        """Build (text, attr, align) tuples for the quota panel."""
        now = snap["ts"]
        rows = []
        cg = snap.get("chatgpt") or {}
        active = cg.get("active")
        credits = cg.get("credits") or {}
        src = cg.get("source") or "none"
        age = " age " + R.fmt_dur(cg.get("cache_age_s")) if cg.get("cache_age_s") is not None else ""
        head = "CHATGPT  source=%s%s  credits %s  plan %s" % (
            src,
            age,
            "unlimited" if credits.get("unlimited") else credits.get("balance", "—"),
            cg.get("plan_type") or "—",
        )
        rows.append((head, "green" if active else "dim", "left", "● 使用中" if active else "○ 空闲"))
        if not cg.get("available"):
            rows.append(("  unavailable: %s" % (cg.get("error") or "no data"), "dim", "left", ""))
        else:
            for key, name in (("primary", "5h  "), ("secondary", "week")):
                win = cg.get(key) or {}
                used = win.get("used_percent")
                text = "  %s %s %6s  left %6s  %s" % (
                    name,
                    R.bar(used, 16),
                    R.pct(used, 1),
                    R.pct(100 - used if used is not None else None, 1),
                    R.fmt_reset(win.get("resets_at"), now),
                )
                rows.append((text, "red" if (used or 0) >= 90 else "", "left", ""))

        oc = snap.get("opencode") or {}
        providers = snap.get("providers") or {}
        oc_active = bool((providers.get("opencode-go") or {}).get("active"))
        oc_head = "OPENCODE GO  %s (%s)" % (oc.get("key") or "no key", oc.get("key_source") or "—")
        rows.append((oc_head, "green" if oc_active else "dim", "left", "● 调用中" if oc_active else "○ 空闲"))
        if oc.get("configured") is None:
            rows.append(("  --no-usage：未联网查询额度", "dim", "left", ""))
        elif not oc.get("configured"):
            rows.append(("  未配置 API key：在 .env 写入 OPENCODE_GO_API_KEY=... 后按 r", "dim", "left", ""))
        elif not oc.get("ok"):
            rows.append(("  query failed: %s" % (oc.get("error") or "?"), "dim", "left", ""))
        else:
            for win in oc.get("windows") or []:
                used = win.get("percent")
                text = "  %-8s %s %6s  left %6s  %s  [%s]" % (
                    win.get("name"),
                    R.bar(used, 16),
                    R.pct(used, 0),
                    R.pct(100 - used if used is not None else None, 0),
                    R.fmt_reset(win.get("resets_at"), now),
                    win.get("status") or "?",
                )
                rows.append((text, "red" if (used or 0) >= 90 else "", "left", ""))
        return rows

    def _plan_height(self, snap):
        return 1 + len(self._plan_rows(snap))

    def _draw_plan(self, stdscr, snap, y, w, height):
        _put(stdscr, y, 0, "─" * (w - 1), curses.A_DIM)
        y += 1
        rows = self._plan_rows(snap)
        for text, color, _align, right in rows:
            if height <= 1:
                break
            height -= 1
            attr = self._attr(color) | curses.A_BOLD if color and color != "dim" else self._attr(color)
            avail = w - 1
            if right:
                avail -= R.disp_w(right) + 1
            _put(stdscr, y, 0, R.clip(text, max(0, avail)), attr)
            if right:
                _put(stdscr, y, max(0, w - R.disp_w(right) - 1), right, attr)
            y += 1
        return y

    def _draw_events(self, stdscr, snap, y, w, height):
        if height < 3:
            return y
        _put(stdscr, y, 0, "─" * (w - 1), curses.A_DIM)
        y += 1
        _put(stdscr, y, 0, "EVENTS", curses.A_BOLD)
        _put(stdscr, y, 7, "recent tool calls across all threads", curses.A_DIM)
        y += 1
        rows = max(0, height - 2)
        events = (snap.get("events") or [])[:rows]
        if not events:
            _put(stdscr, y, 2, "no tool activity in window", curses.A_DIM)
        for ev in events:
            theme = self._attr("red") if not ev.get("ok", True) else 0
            x = _put(stdscr, y, 1, R.fmt_clock(ev.get("ts")), curses.A_DIM)
            x = _put(stdscr, y, x + 1, (ev.get("thread") or "?")[:8], curses.A_DIM)
            x = _put(stdscr, y, x + 1, R.pad(ev.get("tool") or "?", 20), theme | curses.A_BOLD)
            x = _put(stdscr, y, x, R.pad(R.fmt_ms(ev.get("ms")), 8, "right"), theme)
            if not ev.get("ok", True):
                _put(stdscr, y, x + 2, R.clip(ev.get("text") or "", max(0, w - x - 3)), theme)
            y += 1
        return y

    def _draw_footer(self, stdscr, snap, err, h, w):
        line = " q quit   r refresh   p pause   ↑↓ scroll   g/G top/bottom   ? help"
        if err:
            line = " collector error: %s" % err
        elif self.status:
            line = " " + self.status
        errors = (snap.get("sources") or {}).get("errors") or []
        if errors and not err:
            line += "   ! src:" + R.clip(";".join(errors[:2]), 40)
        procs = []
        for row in snap.get("processes") or []:
            if row.get("kind") in ("daemon", "tui"):
                procs.append("%s:%s" % (row["kind"], row["pid"]))
        right = "  pid " + " ".join(procs[:3]) + " " if procs else ""
        avail = max(0, w - 1 - R.disp_w(right))
        _put(stdscr, h - 1, 0, R.pad(R.clip(line, avail), avail), curses.A_REVERSE)
        if right:
            _put(stdscr, h - 1, max(0, w - 1 - R.disp_w(right)), right, curses.A_REVERSE)

    def _draw_help(self, stdscr, h, w):
        stdscr.erase()
        lines = [
            "codexmon keys",
            "",
            "  q / Esc   quit",
            "  r         refresh quota now",
            "  p / space pause auto refresh",
            "  ↑ ↓ / j k move agent selection",
            "  PgUp/Dn   jump 8 agents",
            "  g / G     first / last agent",
            "  ?         toggle this help",
            "",
            "states: RUN=requesting  WORK=recent  WAIT=waiting",
            "        IDLE=idle main  EXIT=loop exited",
            "        ORPHAN=subagent alive but never exited (needs a look)",
            "SPIN  = wait_agent timeouts with zero close_agent",
        ]
        box_w = min(max(len(x) for x in lines) + 4, w - 4)
        box_h = min(len(lines) + 2, h - 2)
        top = max(0, (h - box_h) // 2)
        left = max(0, (w - box_w) // 2)
        for i in range(box_h):
            _put(stdscr, top + i, left, " " * box_w, curses.A_REVERSE)
        for idx, text in enumerate(lines[: box_h - 2]):
            attr = curses.A_REVERSE
            if idx == 0:
                attr |= curses.A_BOLD
            _put(stdscr, top + 1 + idx, left + 2, R.clip(text, box_w - 4), attr)
        _put(stdscr, h - 1, 0, R.pad(" ? / any key  close", w - 1), curses.A_REVERSE)


def _put(win, y, x, text, attr=0):
    """Write text clipped to the screen; never raises."""
    if not text:
        return x
    try:
        h, w = win.getmaxyx()
    except curses.error:
        return x
    if y < 0 or y >= h or x >= w - 1:
        return x
    text = R.clip(text, w - 1 - x)
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass
    return x + R.disp_w(text)


def _tail_path(path, width):
    if len(path) <= width:
        return path
    return "…" + path[-width:]
