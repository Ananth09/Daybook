#!/usr/bin/env python3
"""Daybook: a private work log that fills your timesheet.

Runs as a small local server for one macOS account. Notes are stored in
~/Library/Application Support/Daybook (private to that account). Plain-text
notes are sorted by the `claude` command (Claude Code, signed in with this
account's own Claude login), with simple keyword rules as a fallback. Every
day at the fill time (default 04:07) the previous day's row is written into
the Excel timesheet found in your timesheet folder.

Usage:
  daybook.py serve            run the app (the LaunchAgent does this)
  daybook.py open             open the app window
  daybook.py add "note text"  log a note from Terminal
  daybook.py fill [YYYY-MM-DD] fill a day now (default: yesterday)
"""
import csv
import datetime as dt
import http.server
import io
import json
import os
import queue
import re
import secrets
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

VERSION = "1.0"
HOME = Path.home()
APP_DIR = Path(os.environ.get("DAYBOOK_HOME") or HOME / "Library" / "Application Support" / "Daybook")
DAYS_DIR = APP_DIR / "days"
CONFIG_FILE = APP_DIR / "config.json"
STATE_FILE = APP_DIR / "state.json"
LOG_FILE = APP_DIR / "daybook.log"
UI_FILE = Path(__file__).with_name("ui.html")

MODES = ["WFH", "WFO", "Leave", "Holiday", "Other"]
KINDS = ["plan", "doing", "done", "note"]
HEAD = ["Date", "Task Description/Feature link", "WFO/WFH/Other", "Start Time", "End Time",
        "Total Working Hours", "Remarks / Comments", "Approval (Y/N)", "Additional Comments"]
WDL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

LOCK = threading.RLock()


# ---------------------------------------------------------------- utilities
def log(*a):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} " + " ".join(str(x) for x in a)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def key_of(d):
    return d.strftime("%Y-%m-%d")


def from_key(k):
    return dt.datetime.strptime(k, "%Y-%m-%d").date()


def valid_key(k):
    try:
        return isinstance(k, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", k)) and bool(from_key(k))
    except ValueError:
        return False


def add_days(k, n):
    return key_of(from_key(k) + dt.timedelta(days=n))


def dmy(k):
    y, m, d = k.split("-")
    return f"{d}/{m}/{y}"


def workday(now=None):
    """The work day a moment belongs to: the day rolls over at 04:00."""
    n = now or dt.datetime.now()
    if n.hour < 4:
        n = n - dt.timedelta(days=1)
    return key_of(n)


def norm_time(t):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})", str(t or ""))
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return ""


def hours(s, e):
    def mins(t):
        t = norm_time(t)
        return int(t[:2]) * 60 + int(t[3:]) if t else None
    a, b = mins(s), mins(e)
    if a is None or b is None:
        return ""
    d = b - a
    if d <= 0:
        d += 1440
    return f"{d // 60}:{d % 60:02d}"


def atomic_write(path, text):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ------------------------------------------------------------------ config
def default_config():
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return {
        "folder": str(HOME / "Daybook" / "Timesheets"),
        "port": 47000 + (uid % 1000),
        "fill_time": "04:07",
        "default_mode": "WFH",
        "claude_path": "",
        "token": secrets.token_urlsafe(24),
    }


def load_config():
    cfg = default_config()
    stored = read_json(CONFIG_FILE, {})
    cfg.update({k: v for k, v in stored.items() if v not in (None,)})
    if stored != cfg:
        save_config(cfg)
    return cfg


def save_config(cfg):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write(CONFIG_FILE, json.dumps(cfg, indent=2))
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


CFG = None


def cfg():
    global CFG
    if CFG is None:
        CFG = load_config()
    return CFG


def load_state():
    return read_json(STATE_FILE, {"written": {}, "last_fill_check": ""})


def save_state(st):
    atomic_write(STATE_FILE, json.dumps(st))


# ------------------------------------------------------------------- store
def day_path(k):
    return DAYS_DIR / f"{k}.json"


def load_day(k):
    with LOCK:
        d = read_json(day_path(k), None)
        if not d:
            d = {"date": k, "entries": []}
        d.setdefault("entries", [])
        return d


def save_day(k, d):
    with LOCK:
        d["date"] = k
        d["updatedAt"] = now_iso()
        if not d.get("entries") and not d.get("sheet"):
            try:
                day_path(k).unlink()
            except FileNotFoundError:
                pass
            return
        atomic_write(day_path(k), json.dumps(d, ensure_ascii=False, indent=1))


def list_days(a, b):
    out = {}
    with LOCK:
        for p in sorted(DAYS_DIR.glob("*.json")):
            k = p.stem
            if valid_key(k) and a <= k <= b:
                d = read_json(p, None)
                if d:
                    out[k] = d
    return out


def mutate_day(k, fn, entries_changed=True):
    with LOCK:
        d = load_day(k)
        fn(d)
        if entries_changed:
            d["entriesUpdatedAt"] = now_iso()
        save_day(k, d)
        return d


# ------------------------------------------------------------------ claude
_claude_cache = {"path": None, "checked": 0}


def find_claude(force=False):
    if not force and _claude_cache["path"] and time.time() - _claude_cache["checked"] < 600:
        return _claude_cache["path"]
    found = ""
    cands = [cfg().get("claude_path") or "",
             str(HOME / ".local" / "bin" / "claude"),
             str(HOME / ".claude" / "local" / "claude"),
             "/opt/homebrew/bin/claude", "/usr/local/bin/claude",
             str(HOME / ".npm-global" / "bin" / "claude"),
             str(HOME / ".bun" / "bin" / "claude")]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            found = c
            break
    if not found:
        for sh in ("/bin/zsh", "/bin/bash"):
            if os.path.exists(sh):
                try:
                    r = subprocess.run([sh, "-lc", "command -v claude"], capture_output=True, text=True, timeout=15)
                    p = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
                    if p and os.path.isfile(p):
                        found = p
                        break
                except Exception:
                    pass
    _claude_cache.update(path=found, checked=time.time())
    return found


def claude_env(path):
    env = dict(os.environ)
    extra = [os.path.dirname(path), str(HOME / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin",
             "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    env["PATH"] = ":".join(dict.fromkeys(extra + env.get("PATH", "").split(":")))
    env.setdefault("HOME", str(HOME))
    return env


def run_claude(prompt, model):
    """Ask Claude Code (this account's own login). Returns text or raises."""
    path = find_claude()
    if not path:
        raise RuntimeError("Claude Code is not installed for this Mac account")
    base = [path, "-p"]
    for cmd in (base + ["--model", model], base):
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=240,
                           cwd=str(APP_DIR), env=claude_env(path))
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
        err = (r.stderr or r.stdout or "").strip()
        if "model" not in err.lower():
            break
    raise RuntimeError(f"claude exited {r.returncode}: {err[:300]}")


def extract_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in reply")
    return json.loads(m.group(0))


# ----------------------------------------------------------------- parsing
def local_parse(text):
    t = text.lower()
    links = re.findall(r"https?://\S+|\b[A-Z][A-Z0-9]+-\d+\b", text)
    kind = "note"
    if re.search(r"\b(will|plan|planning|todo|to do|need to|have to|tomorrow)\b", t):
        kind = "plan"
    elif re.search(r"\b(working on|in progress|currently)\b", t):
        kind = "doing"
    elif re.search(r"\b(done|did|finished|completed|fixed|built|wrote|shipped|deployed|reviewed|worked on|merged)\b", t):
        kind = "done"
    mode = "WFO" if re.search(r"\bwfo\b|\boffice\b", t) else "WFH" if re.search(r"\bwfh\b|from home", t) else None

    def conv(m):
        if not m:
            return None
        h = int(m.group(1))
        ap = (m.group(3) or "").lower()
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        return f"{h:02d}:{m.group(2) or '00'}" if h < 24 else None

    st = re.search(r"(?:start(?:ed)?|began|logged in)\D{0,8}(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text, re.I)
    en = re.search(r"(?:logged off|log off|end(?:ed)?|stopped|wrapped up|till|until)\D{0,8}(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text, re.I)
    remark = text.strip() if re.search(r"\bblock(ed|er)\b|\bmeeting\b|\bleave\b", t) else None
    summary = re.sub(r"https?://\S+", "", text).strip()
    return [{"kind": kind, "summary": summary, "links": links, "start": conv(st), "end": conv(en),
             "mode": mode, "remark": remark}]


def norm_items(arr):
    if not isinstance(arr, list) or not arr:
        return None
    out = []
    for i in arr:
        i = i if isinstance(i, dict) else {}
        out.append({
            "kind": i.get("kind") if i.get("kind") in KINDS else "note",
            "summary": str(i.get("summary") or "").strip(),
            "links": [str(x) for x in (i.get("links") or []) if x] if isinstance(i.get("links"), list) else [],
            "start": norm_time(i.get("start")) or None,
            "end": norm_time(i.get("end")) or None,
            "mode": i.get("mode") if i.get("mode") in MODES else None,
            "remark": str(i.get("remark")).strip() if i.get("remark") else None,
        })
    return out


def with_ids(eid, items):
    return [dict(it, id=f"{eid}-{n}") for n, it in enumerate(items)]


def parse_prompt(text, k, at):
    early = at.hour < 4 and k == workday(at)
    wd = WDL[from_key(k).weekday()]
    early_note = (" It is before 4 AM, so unstated work belongs to %s." % k) if early else ""
    return f"""You turn one plain-text work-log note into JSON for a work timesheet.
Current local time: {at:%Y-%m-%d %H:%M} ({WDL[at.weekday()]}).
The user has the log open on {k} ({wd}).{early_note}

Note:
\"\"\"{text}\"\"\"

Reply with ONLY this JSON, nothing else:
{{"date":"YYYY-MM-DD","items":[{{"kind":"plan|doing|done|note","summary":"","links":[],"start":null,"end":null,"mode":null,"remark":null}}]}}

Rules:
- date: the work day the note is about. Default {k}. Use another date only if the note says so ("yesterday", "tomorrow", "on Monday", a date).
- Split the note into separate items when it mentions separate tasks or facts. Keep one item if it is one thing.
- kind: plan = intends to do later; doing = in progress now; done = completed or worked on; note = anything else (meeting, blocker, a time-only note like "logged off at 7").
- summary: short, timesheet-ready phrase in the user's own wording. Past tense for done. No times and no URLs in it. Empty string for a time-only item.
- links: every URL or ticket id (like ABC-123) in that item.
- start / end: "HH:MM" 24-hour, only when the note says when the work DAY began or ended ("started 10", "logged off at 7pm", "worked till 11:30 pm"). Never meeting times. Otherwise null.
- mode: "WFH", "WFO", "Leave", "Holiday" or "Other", only if stated (office = WFO, home = WFH, sick/leave = Leave). Otherwise null.
- remark: blockers, meetings, leave or dependencies as one short phrase. Otherwise null."""


def compile_prompt(k, d):
    items = []
    for e in d.get("entries", []):
        for i in e.get("items", []):
            items.append(dict(i, loggedAt=(e.get("at") or "")[11:16]))
    return f"""Write one timesheet row from this work log for {dmy(k)} ({WDL[from_key(k).weekday()]}).
Log items (JSON): {json.dumps(items, ensure_ascii=False)}

Reply with ONLY this JSON: {{"task":"","remarks":"","mode":"WFH","start":"","end":""}}
Rules:
- task: what was worked on that day, from done and in-progress items only (skip unfinished plans and plain notes). One line per piece of work, separated by "\\n". Concise, past tense, keep ticket ids and links at the end of their line.
- remarks: meetings, blockers, leave notes and dependencies, joined with "; ". Empty string if none.
- mode: the last stated mode that day, else "{cfg().get('default_mode', 'WFH')}".
- start: the earliest stated start time; end: the last stated end time, both "HH:MM". Use "" when not stated. Never invent times."""


def local_compile(d):
    items = [i for e in (d or {}).get("entries", []) for i in e.get("items", [])]
    work = [i for i in items if i.get("kind") in ("done", "doing")]
    lines = []
    for i in work:
        s = i.get("summary") or ""
        extra = [l for l in i.get("links", []) if l not in s]
        line = " ".join(x for x in [s] + extra if x)
        if line:
            lines.append(line)
    modes = [i["mode"] for i in items if i.get("mode")]
    starts = sorted(i["start"] for i in items if i.get("start"))
    ends = [i["end"] for i in items if i.get("end")]
    return {"task": "\n".join(lines), "remarks": "; ".join(i["remark"] for i in items if i.get("remark")),
            "mode": modes[-1] if modes else cfg().get("default_mode", "WFH"),
            "start": starts[0] if starts else "", "end": ends[-1] if ends else "", "additional": ""}


def mode_stated(d):
    return any(i.get("mode") for e in d.get("entries", []) for i in e.get("items", []))


# --------------------------------------------------------- note pipeline
JOBS = queue.Queue()


def add_note(k, text):
    eid = uuid.uuid4().hex[:10]
    entry = {"id": eid, "text": text, "at": now_iso(), "status": "pending",
             "items": with_ids(eid, local_parse(text))}
    mutate_day(k, lambda d: d["entries"].append(entry))
    JOBS.put(("parse", k, eid))
    return eid


def parse_job(k, eid):
    d = load_day(k)
    entry = next((e for e in d["entries"] if e["id"] == eid), None)
    if not entry:
        return
    at = dt.datetime.fromisoformat(entry["at"]).replace(tzinfo=None)
    try:
        r = extract_json(run_claude(parse_prompt(entry["text"], k, at), "haiku"))
        items = norm_items(r.get("items"))
        target = r.get("date") if valid_key(r.get("date")) else k
        done = dict(entry, status="parsed", items=with_ids(eid, items) if items else entry["items"])
    except Exception as ex:
        log("parse fell back to rules:", ex)
        target, done = k, dict(entry, status="local")

    def replace(dd):
        dd["entries"] = [done if e["id"] == eid else e for e in dd["entries"]]

    if target != k:
        mutate_day(k, lambda dd: dd.__setitem__("entries", [e for e in dd["entries"] if e["id"] != eid]))
        mutate_day(target, lambda dd: dd["entries"].append(dict(done, movedFrom=k)))
    else:
        mutate_day(k, replace)


def worker():
    while True:
        job = JOBS.get()
        try:
            if job[0] == "parse":
                parse_job(job[1], job[2])
            elif job[0] == "fill":
                fill_day(job[1], job[2])
            elif job[0] == "excel":
                write_excel(job[1])
        except Exception:
            log("job failed", job, traceback.format_exc())


# -------------------------------------------------------------- fill rows
def fill_day(k, by="schedule"):
    d = load_day(k)
    if not d.get("entries"):
        return False
    row = local_compile(d)
    try:
        r = extract_json(run_claude(compile_prompt(k, d), "sonnet"))
        row = {"task": str(r.get("task", row["task"])), "remarks": str(r.get("remarks", row["remarks"])),
               "mode": r.get("mode") if r.get("mode") in MODES else row["mode"],
               "start": norm_time(r.get("start")), "end": norm_time(r.get("end"))}
    except Exception as ex:
        log("fill used rules:", ex)
    stated = mode_stated(d)

    def apply(dd):
        s = dd.get("sheet") or {}
        keep = set(s.get("editedFields") or [])
        for f in ("task", "remarks", "mode", "start", "end"):
            if f not in keep:
                s[f] = row.get(f, "")
        s["hours"] = hours(s.get("start"), s.get("end"))
        s["modeStated"] = stated or "mode" in keep
        s["filledAt"] = now_iso()
        s["filledBy"] = by
        dd["sheet"] = s

    mutate_day(k, apply, entries_changed=False)
    log(f"filled {k} ({by})")
    write_excel(k)
    return True


def due_days(now=None):
    """Days whose row should be (re)filled now."""
    now = now or dt.datetime.now()
    fh, fm = (int(x) for x in (cfg().get("fill_time") or "04:07").split(":"))
    today = workday(now)
    passed_today = (now.hour, now.minute) >= (fh, fm) or now.hour < 4
    out = []
    for n in range(1, 9):
        k = add_days(today, -n)
        if n == 1 and not passed_today:
            continue
        d = read_json(day_path(k), None)
        if not d or not d.get("entries"):
            continue
        s = d.get("sheet") or {}
        if not s.get("filledAt"):
            out.append(k)
        elif (d.get("entriesUpdatedAt") or "") > s["filledAt"]:
            try:
                changed = dt.datetime.fromisoformat(d["entriesUpdatedAt"])
                if (dt.datetime.now(changed.tzinfo) - changed).total_seconds() > 600:
                    out.append(k)
            except ValueError:
                out.append(k)
    return out


def excel_pending():
    out = []
    today = workday()
    for n in range(1, 40):
        k = add_days(today, -n)
        d = read_json(day_path(k), None)
        s = (d or {}).get("sheet") or {}
        if s.get("filledAt") and (s.get("excel") or {}).get("status") != "ok":
            out.append(k)
    return out


def scheduler():
    time.sleep(5)
    while True:
        try:
            for k in due_days():
                fill_day(k, "schedule")
            for k in excel_pending():
                write_excel(k, quiet=True)
        except Exception:
            log("scheduler error", traceback.format_exc())
        time.sleep(60)


# -------------------------------------------------------------- Excel
def _need_openpyxl():
    try:
        import openpyxl  # noqa: F401
        return openpyxl
    except ImportError:
        raise RuntimeError("openpyxl is missing. Run install.sh again.")


def _cell_date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, (int, float)) and 30000 < v < 80000:
        return (dt.datetime(1899, 12, 30) + dt.timedelta(days=float(v))).date()
    s = str(v or "").strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _colmap(ws):
    for r in range(1, min(ws.max_row, 15) + 1):
        cols = {}
        for c in range(1, min(ws.max_column, 30) + 1):
            h = str(ws.cell(r, c).value or "").strip().lower()
            if not h:
                continue
            if h.startswith("date") and "date" not in cols:
                cols["date"] = c
            elif "task" in h:
                cols["task"] = c
            elif "wfo" in h or "wfh" in h:
                cols["mode"] = c
            elif "start" in h:
                cols["start"] = c
            elif h.startswith("end") or "end time" in h:
                cols["end"] = c
            elif "hour" in h:
                cols["hours"] = c
            elif "remark" in h:
                cols["remarks"] = c
            elif "approv" in h:
                cols["approval"] = c
            elif "additional" in h:
                cols["additional"] = c
        if "date" in cols and len(cols) >= 3:
            return r, cols
    return None, None


def _xlsx_files(folder):
    files = [p for p in Path(folder).glob("*.xlsx") if not p.name.startswith(("~$", "."))]
    # the user's own files first, Daybook's generated monthly files last
    return sorted(files, key=lambda p: (p.name.startswith("Daybook_"), p.name.lower()))


def find_row(folder, k):
    openpyxl = _need_openpyxl()
    target = from_key(k)
    for p in _xlsx_files(folder):
        try:
            wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        except Exception as ex:
            log("can't read", p.name, ex)
            continue
        try:
            for ws in wb.worksheets:
                hr, cols = _colmap(ws)
                if not cols:
                    continue
                for r, row in enumerate(ws.iter_rows(min_row=hr + 1, max_row=hr + 400,
                                                     min_col=cols["date"], max_col=cols["date"],
                                                     values_only=True), start=hr + 1):
                    if _cell_date(row[0]) == target:
                        return p, ws.title, r, cols
        finally:
            wb.close()
    return None


def create_month_file(folder, k):
    openpyxl = _need_openpyxl()
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    d0 = from_key(k).replace(day=1)
    p = Path(folder) / f"Daybook_Timesheet_{d0:%Y-%m}.xlsx"
    if p.exists():
        return p
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = d0.strftime("%b %Y")
    ws.append(HEAD)
    green = PatternFill("solid", fgColor="6AA84F")
    orange = PatternFill("solid", fgColor="F1A13B")
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for c in ws[1]:
        c.fill, c.font = green, Font(name="Times New Roman", bold=True, size=12)
        c.alignment, c.border = Alignment(horizontal="center", vertical="center"), border
    day = d0
    while day.month == d0.month:
        ws.append([day.strftime("%d/%m/%Y"), "", cfg().get("default_mode", "WFH"), "", "", "", "", "", ""])
        for i, c in enumerate(ws[ws.max_row], start=1):
            c.border = border
            c.font = Font(name="Arial", size=10)
            c.alignment = Alignment(vertical="top", wrap_text=i in (2, 7))
            if day.weekday() == 6 and i != 3:
                c.fill = orange
        day += dt.timedelta(days=1)
    for i, w in enumerate([14, 48, 16, 12, 12, 20, 34, 15, 26], start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    wb.save(p)
    log("created", p.name)
    return p


def _backup(p):
    bdir = p.parent / ".daybook-backups"
    bdir.mkdir(exist_ok=True)
    dest = bdir / f"{p.stem}.{dt.date.today():%Y%m%d}{p.suffix}"
    if not dest.exists():
        shutil.copy2(p, dest)
        olds = sorted(bdir.glob(f"{p.stem}.*{p.suffix}"))
        for o in olds[:-14]:
            o.unlink(missing_ok=True)


def write_excel(k, quiet=False):
    """Write a day's filled row into the timesheet file. Never overwrites
    a cell you typed in Excel yourself."""
    d = load_day(k)
    s = d.get("sheet") or {}
    if not s.get("filledAt") and not s.get("editedFields"):
        return

    def set_status(status, file=None, note=""):
        def ap(dd):
            sh = dd.get("sheet") or {}
            sh["excel"] = {"status": status, "file": file, "at": now_iso(), "note": note}
            dd["sheet"] = sh
        mutate_day(k, ap, entries_changed=False)

    try:
        openpyxl = _need_openpyxl()
        from openpyxl.styles import Alignment
        folder = Path(cfg()["folder"]).expanduser()
        if not folder.is_dir():
            set_status("error", note=f"Timesheet folder not found: {folder}")
            return
        hit = find_row(folder, k)
        if not hit:
            create_month_file(folder, k)
            hit = find_row(folder, k)
        if not hit:
            set_status("error", note="No row for this date in any timesheet file")
            return
        p, sheet, r, cols = hit
        if (p.parent / f"~${p.name}").exists() or (p.parent / f"~${p.name[2:]}").exists():
            set_status("waiting", p.name, "The file is open in Excel. It will be filled after you close it.")
            return
        st = load_state()
        written = st.setdefault("written", {})
        wb = openpyxl.load_workbook(p)
        ws = wb[sheet]
        values = {"task": s.get("task", ""), "start": s.get("start", ""), "end": s.get("end", ""),
                  "hours": s.get("hours", ""), "remarks": s.get("remarks", ""),
                  "additional": s.get("additional", ""), "mode": s.get("mode", "")}
        changed, skipped = 0, 0
        for f, v in values.items():
            c = cols.get(f)
            if not c or v in (None, ""):
                continue
            cell = ws.cell(r, c)
            cur = "" if cell.value is None else str(cell.value)
            wkey = f"{p.name}|{sheet}|{r}|{c}"
            prev = written.get(wkey)
            ok = cur.strip() == "" or cur == prev or cur == str(v)
            # a pre-filled "WFH" in the template gives way only to a mode you actually stated
            if f == "mode" and cur in MODES and s.get("modeStated"):
                ok = True
            if not ok:
                skipped += 1
                continue
            if cur != str(v):
                cell.value = v
                if "\n" in str(v):
                    al = cell.alignment
                    cell.alignment = Alignment(horizontal=al.horizontal, vertical=al.vertical or "top", wrap_text=True)
                changed += 1
            written[wkey] = str(v)
        if changed:
            _backup(p)
            wb.save(p)
        wb.close()
        save_state(st)
        note = f"{skipped} cell(s) left alone because you edited them in Excel" if skipped else ""
        set_status("ok", p.name, note)
        log(f"excel {k}: {changed} cell(s) written to {p.name} row {r}" + (f", {skipped} skipped" if skipped else ""))
    except PermissionError:
        set_status("error", note="macOS blocked access to the timesheet folder. Use a folder outside Documents/Desktop/Downloads, or allow access.")
    except Exception as ex:
        if not quiet:
            log("excel error", k, traceback.format_exc())
        set_status("error", note=str(ex)[:200])


# ------------------------------------------------------------- exports
def row_values(k, d):
    s = (d or {}).get("sheet") or {}
    p = local_compile(d) if (not s.get("filledAt") and (d or {}).get("entries")) else {}
    g = lambda f: s.get(f) or p.get(f) or ""
    st, en = g("start"), g("end")
    return [dmy(k), g("task"), g("mode") or cfg().get("default_mode", "WFH"), st, en,
            s.get("hours") or hours(st, en), g("remarks"), s.get("approval") or "", g("additional")]


def range_keys(a, b):
    if a > b:
        a, b = b, a
    out, k = [], a
    while k <= b and len(out) < 400:
        out.append(k)
        k = add_days(k, 1)
    return out


def export_xlsx(a, b):
    openpyxl = _need_openpyxl()
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    days = list_days(a, b)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Timesheet"
    ws.append(HEAD)
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for c in ws[1]:
        c.fill, c.font = PatternFill("solid", fgColor="6AA84F"), Font(name="Times New Roman", bold=True, size=12)
        c.alignment, c.border = Alignment(horizontal="center", vertical="center"), border
    for k in range_keys(a, b):
        ws.append(row_values(k, days.get(k)))
        for i, c in enumerate(ws[ws.max_row], start=1):
            c.border, c.font = border, Font(name="Arial", size=10)
            c.alignment = Alignment(vertical="top", wrap_text=i in (2, 7), horizontal="center" if 4 <= i <= 6 else "left")
            if from_key(k).weekday() == 6 and i != 3:
                c.fill = PatternFill("solid", fgColor="F1A13B")
    for i, w in enumerate([14, 48, 16, 12, 12, 20, 34, 15, 26], start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_csv(a, b):
    days = list_days(a, b)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEAD)
    for k in range_keys(a, b):
        w.writerow(row_values(k, days.get(k)))
    return ("﻿" + buf.getvalue()).encode("utf-8")


# ---------------------------------------------------------------- server
def status():
    c = cfg()
    folder = Path(c["folder"]).expanduser()
    files = [p.name for p in _xlsx_files(folder)] if folder.is_dir() else []
    return {"version": VERSION, "claude": bool(find_claude()), "claudePath": find_claude(),
            "folder": str(folder), "folderExists": folder.is_dir(), "files": files,
            "fillTime": c.get("fill_time"), "defaultMode": c.get("default_mode"),
            "workday": workday(), "user": os.environ.get("USER", "")}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Daybook"

    def log_message(self, *a):
        pass

    def _host_ok(self):
        port = cfg()["port"]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _authed(self):
        cookie = self.headers.get("Cookie", "")
        m = re.search(r"(?:^|;\s*)daybook=([^;]+)", cookie)
        return bool(m) and secrets.compare_digest(m.group(1), cfg()["token"])

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _locked(self):
        self._send(403, "<!doctype html><meta charset=utf-8><title>Daybook</title><body style='font:16px system-ui;padding:40px'>"
                        "<h2>Open Daybook from its app icon</h2><p>This page is private to one Mac account. "
                        "Open <b>Daybook</b> from Applications or Spotlight in your own Mac account.</p>", "text/html; charset=utf-8")

    def do_GET(self):
        if not self._host_ok():
            return self._send(400, {"error": "bad host"})
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/":
            if q.get("t") and secrets.compare_digest(q["t"], cfg()["token"]):
                cookie = f"daybook={cfg()['token']}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"
                return self._send(302, b"", "text/plain", {"Location": "/", "Set-Cookie": cookie})
            if not self._authed():
                return self._locked()
            return self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
        if not self._authed():
            return self._send(401, {"error": "Open Daybook from its app icon."})
        if u.path == "/api/days":
            a, b = q.get("from", ""), q.get("to", "")
            if not (valid_key(a) and valid_key(b)):
                return self._send(400, {"error": "bad range"})
            return self._send(200, {"days": list_days(min(a, b), max(a, b)), "status": status()})
        if u.path == "/api/status":
            return self._send(200, status())
        if u.path in ("/export.xlsx", "/export.csv"):
            a, b = q.get("from", ""), q.get("to", "")
            if not (valid_key(a) and valid_key(b)):
                return self._send(400, {"error": "bad range"})
            name = f"Timesheet_{dmy(min(a, b)).replace('/', '-')}_to_{dmy(max(a, b)).replace('/', '-')}"
            try:
                if u.path.endswith("xlsx"):
                    data, ct, ext = export_xlsx(a, b), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
                else:
                    data, ct, ext = export_csv(a, b), "text/csv; charset=utf-8", "csv"
            except Exception as ex:
                return self._send(500, {"error": str(ex)})
            return self._send(200, data, ct, {"Content-Disposition": f'attachment; filename="{name}.{ext}"'})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok() or not self._authed():
            return self._send(401, {"error": "Open Daybook from its app icon."})
        if self.headers.get("X-Daybook") != "1":
            return self._send(400, {"error": "missing header"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 1_000_000)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        path = urllib.parse.urlparse(self.path).path
        try:
            res = self.route(path, body)
        except ValueError as ex:
            return self._send(400, {"error": str(ex)})
        except Exception as ex:
            log("api error", path, traceback.format_exc())
            return self._send(500, {"error": str(ex)})
        return self._send(200, res if res is not None else {"ok": True})

    def route(self, path, b):
        k = b.get("date")
        if path == "/api/note":
            text = str(b.get("text") or "").strip()
            if not valid_key(k) or not text:
                raise ValueError("date and text required")
            return {"id": add_note(k, text[:5000])}
        if path == "/api/item":
            if not valid_key(k):
                raise ValueError("bad date")
            iid = b.get("id")

            def fn(d):
                for e in d["entries"]:
                    if b.get("delete"):
                        e["items"] = [i for i in e.get("items", []) if i["id"] != iid]
                    else:
                        for i in e.get("items", []):
                            if i["id"] == iid and b.get("kind") in KINDS:
                                i["kind"] = b["kind"]
                d["entries"] = [e for e in d["entries"] if e.get("items") or e.get("status") == "pending"]
            mutate_day(k, fn)
            return None
        if path == "/api/carry":
            if not valid_key(k):
                raise ValueError("bad date")
            iid = b.get("id")
            d = load_day(k)
            item = next((i for e in d["entries"] for i in e.get("items", []) if i["id"] == iid), None)
            if not item:
                raise ValueError("item not found")
            if b.get("action") == "done":
                eid = uuid.uuid4().hex[:10]
                today = workday()
                mutate_day(today, lambda dd: dd["entries"].append(
                    {"id": eid, "text": item.get("summary", ""), "at": now_iso(), "status": "parsed", "carriedFrom": k,
                     "items": [dict(item, kind="done", id=f"{eid}-0")]}))

            def rm(dd):
                for e in dd["entries"]:
                    e["items"] = [i for i in e.get("items", []) if i["id"] != iid]
                dd["entries"] = [e for e in dd["entries"] if e.get("items")]
            mutate_day(k, rm)
            return None
        if path == "/api/sheet":
            f = b.get("field")
            if not valid_key(k) or f not in ("task", "mode", "start", "end", "remarks", "additional"):
                raise ValueError("bad field")
            v = str(b.get("value") or "").strip()
            if f in ("start", "end"):
                v = norm_time(v)

            def ap(d):
                s = d.get("sheet") or {}
                s[f] = v
                s["editedFields"] = sorted(set(s.get("editedFields") or []) | {f})
                s["hours"] = hours(s.get("start"), s.get("end"))
                if f == "mode":
                    s["modeStated"] = True
                d["sheet"] = s
            d = mutate_day(k, ap, entries_changed=False)
            if (d.get("sheet") or {}).get("filledAt"):
                JOBS.put(("excel", k))
            return None
        if path == "/api/fill":
            if not valid_key(k):
                raise ValueError("bad date")
            if not load_day(k).get("entries"):
                raise ValueError("Nothing logged on this day yet.")
            JOBS.put(("fill", k, "you"))
            return None
        if path == "/api/settings":
            c = cfg()
            if "folder" in b:
                folder = Path(str(b["folder"]).strip()).expanduser()
                folder.mkdir(parents=True, exist_ok=True)
                c["folder"] = str(folder)
            if "fill_time" in b and norm_time(b["fill_time"]):
                c["fill_time"] = norm_time(b["fill_time"])
            if b.get("default_mode") in MODES:
                c["default_mode"] = b["default_mode"]
            if "claude_path" in b:
                c["claude_path"] = str(b["claude_path"]).strip()
                find_claude(force=True)
            save_config(c)
            return status()
        if path == "/api/test-claude":
            find_claude(force=True)
            try:
                out = run_claude('Reply with only this JSON: {"ok":true}', "haiku")
                return {"ok": bool(extract_json(out).get("ok")), "path": find_claude()}
            except Exception as ex:
                return {"ok": False, "error": str(ex)[:300], "path": find_claude()}
        if path == "/api/open-folder":
            folder = Path(cfg()["folder"]).expanduser()
            folder.mkdir(parents=True, exist_ok=True)
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            return None
        raise ValueError("unknown action")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    DAYS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(APP_DIR, 0o700)
    except OSError:
        pass
    c = cfg()
    Path(c["folder"]).expanduser().mkdir(parents=True, exist_ok=True)
    # re-queue notes that were waiting when the app last stopped
    for p in DAYS_DIR.glob("*.json"):
        d = read_json(p, {})
        for e in d.get("entries", []):
            if e.get("status") == "pending":
                JOBS.put(("parse", p.stem, e["id"]))
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    httpd = Server(("127.0.0.1", c["port"]), Handler)
    log(f"Daybook {VERSION} on http://127.0.0.1:{c['port']} (claude: {find_claude() or 'not found'})")
    httpd.serve_forever()


def app_url():
    c = cfg()
    return f"http://127.0.0.1:{c['port']}/?t={c['token']}"


def _running():
    import socket
    try:
        with socket.create_connection(("127.0.0.1", cfg()["port"]), timeout=1):
            return True
    except OSError:
        return False


def open_app():
    if not _running() and sys.platform == "darwin":
        uid = os.getuid()
        subprocess.run(["launchctl", "kickstart", f"gui/{uid}/com.daybook.app"], capture_output=True)
        for _ in range(20):
            if _running():
                break
            time.sleep(0.25)
    url = app_url()
    for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
        if Path(f"/Applications/{app}.app").exists() or (HOME / "Applications" / f"{app}.app").exists():
            subprocess.Popen(["open", "-na", app, "--args", f"--app={url}", "--window-size=1200,860"])
            return
    subprocess.Popen(["open", url])


def cli_post(path, payload):
    c = cfg()
    req = urllib.request.Request(f"http://127.0.0.1:{c['port']}{path}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "X-Daybook": "1",
                                          "Cookie": f"daybook={c['token']}", "Host": f"127.0.0.1:{c['port']}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "open"
    if cmd == "serve":
        serve()
    elif cmd == "open":
        open_app()
    elif cmd == "url":
        print(app_url())
    elif cmd == "add":
        text = " ".join(argv[2:]).strip()
        if not text:
            print('Usage: daybook add "what you did"')
            return 1
        try:
            cli_post("/api/note", {"date": workday(), "text": text})
            print(f"Logged for {dmy(workday())}.")
        except Exception as ex:
            print(f"Daybook isn't running ({ex}). Open the Daybook app once, then try again.")
            return 1
    elif cmd == "fill":
        k = argv[2] if len(argv) > 2 else add_days(workday(), -1)
        if not valid_key(k):
            print("Use a date like 2026-10-27")
            return 1
        print("Filled." if fill_day(k, "you") else "Nothing logged on that day.")
        print(json.dumps((load_day(k).get("sheet") or {}).get("excel"), indent=1))
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv) or 0)
