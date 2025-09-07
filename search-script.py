#!/usr/bin/env python3
"""
email-searcher.py
Search .eml trees quickly with age-colored dates, attachment flagging, and
a live status line.

Key features
- ripgrep/grep prefilter (fast) + Python verifier (precise)
- Skips base64 blocks and <html>…</html> bodies to reduce noise
- Shows Date header (colored by age) and BIJLAGE when attachments are present
- Different colors for INBOX vs OUTBOX paths
- Immediate "prefiltering…" banner + spinner, then one-line progress bar
- Optional log to output.txt

Customize paths in the CONFIG block or via CLI flags.
"""

import os, sys, time, argparse, subprocess, threading, shutil
from pathlib import Path
from datetime import datetime, timezone
from email import utils

# =========================
# CONFIG (safe defaults)
# =========================
# By default we assume:
#   BASE/
#     ├─ inbox/      <-- .eml input directory (INBOX)
#     └─ outbox/     <-- .eml input directory (OUTBOX)
#
# Customize here, OR override using CLI flags: --inbox, --outbox, --base
#
DEFAULT_BASE   = os.environ.get("EMAIL_SEARCH_BASE",  os.path.expanduser("~/mail_archive"))
DEFAULT_INBOX  = os.environ.get("EMAIL_SEARCH_INBOX", "inbox")
DEFAULT_OUTBOX = os.environ.get("EMAIL_SEARCH_OUTBOX","outbox")
DEFAULT_WORKERS = 6   # threads used for verify phase (prefilter is external)

# =========================
# ANSI colors (no deps)
# =========================
RESET="\033[0m"; BOLD="\033[1m"; DIM="\033[2m"
GREEN="\033[32m"; CYAN="\033[36m"; YELLOW="\033[33m"
WHITE="\033[97m"; GREY="\033[37m"; MAGENTA="\033[35m"

SPINCHARS = ["-", "\\", "|", "/"]

# ---------- color helpers ----------
def color_path(p: Path, outbox_token: str="outbox") -> str:
    """
    Colorize absolute path. OUTBOX paths cyan, otherwise green.
    We match using the last directory name (robust to absolute roots).
    """
    s = str(p)
    return (CYAN if f"/{outbox_token}/" in s or f"\\{outbox_token}\\" in s else GREEN) + s + RESET

def color_date_obvious(dt: datetime|None) -> str:
    """Default scheme: <1y white, 1–2y yellow, >2y magenta."""
    if dt is None: return GREY + "(Date not found)" + RESET
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    age = (datetime.now(dt.tzinfo) - dt).days
    txt = dt.strftime("%a, %d %b %Y %H:%M:%S %z")
    if age < 365:     return WHITE   + txt + RESET
    elif age < 730:   return YELLOW  + txt + RESET
    else:             return MAGENTA + txt + RESET

def color_date_fade(dt: datetime|None) -> str:
    """Optional subtle fade: <1y white, 1–2y grey, >2y dim."""
    if dt is None: return GREY + "(Date not found)" + RESET
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    age = (datetime.now(dt.tzinfo) - dt).days
    txt = dt.strftime("%a, %d %b %Y %H:%M:%S %z")
    if age < 365:     return WHITE + txt + RESET
    elif age < 730:   return GREY  + txt + RESET
    else:             return DIM   + txt + RESET

# ---------- parsing helpers ----------
def parse_date_line(s: str) -> datetime|None:
    """Parse an RFC 2822 style date into a timezone-aware datetime (UTC)."""
    try:
        tup = utils.parsedate_tz(s.strip())
        if tup: return datetime.fromtimestamp(utils.mktime_tz(tup), tz=timezone.utc)
    except Exception:
        pass
    return None

def quick_verify(path: Path, needle: str, case_sensitive: bool, spinner_cb=None):
    """
    Fast verifier per file:
      - skips base64 blocks
      - crudely skips HTML blocks
      - collects first Date header
      - detects attachments (Content-Disposition: attachment | Content-Type: *name=)
      - spinner_cb() is called occasionally during large files
    Returns (matched, date_dt, has_bijlage)
    """
    matched = False
    date_dt = None
    has_bijlage = False
    base64_mode = False
    html_mode = False
    boundary_prefix = "--"
    n_low = needle.lower()
    line_count = 0

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line_count += 1
                if spinner_cb and (line_count % 800 == 0):
                    spinner_cb()

                L = line.lower()

                # Skip base64 blocks until next MIME boundary
                if L.startswith("content-transfer-encoding: base64"):
                    base64_mode = True
                    continue
                if base64_mode and line.startswith(boundary_prefix):
                    base64_mode = False
                    continue

                # Skip crude HTML sections
                if "<html" in L:  html_mode = True
                if "</html" in L:
                    html_mode = False
                    continue
                if base64_mode or html_mode:
                    continue

                # First Date: header we see
                if date_dt is None and L.startswith("date:"):
                    date_dt = parse_date_line(line[5:])

                # Attachment heuristics
                if ("content-disposition:" in L and "attachment" in L) or \
                   ("content-type:" in L and "name=" in L):
                    has_bijlage = True

                # Match line
                hay = line if case_sensitive else L
                if (needle in hay) if case_sensitive else (n_low in hay):
                    matched = True

        return matched, date_dt, has_bijlage
    except Exception:
        return False, None, False

# ---------- printing/progress ----------
def handle_hit(path: Path, date_dt, has_bijlage, color_date_fn, logf, outbox_token: str):
    """Print a result block and also log to file when required."""
    sys.stdout.write("\r")
    abs_path = path.resolve()
    print(color_path(abs_path, outbox_token))
    print(color_date_fn(date_dt))
    if has_bijlage:
        print(BOLD + YELLOW + "BIJLAGE" + RESET)
    if logf:
        logf.write(str(abs_path) + "\n")
        logf.write((date_dt.strftime("%a, %d %b %Y %H:%M:%S %z") if date_dt else "(Date not found)") + "\n")
        if has_bijlage: logf.write("BIJLAGE\n")

def progress(done, total, start_ts):
    elapsed = int(time.time() - start_ts)
    bar = f"[{done}/{total}] | Elapsed: {elapsed}s"
    sys.stdout.write("\r" + GREY + bar + RESET)
    sys.stdout.flush()

# ---------- spinners ----------
def start_spinner(msg="prefiltering", interval=0.1):
    """
    Start a background spinner thread that prints '[msg] <spinner>' on one line.
    Returns a stop() function to terminate and clear the line.
    """
    stop_event = threading.Event()
    def run():
        i = 0
        while not stop_event.is_set():
            ch = SPINCHARS[i % len(SPINCHARS)]
            i += 1
            sys.stdout.write("\r" + GREY + f"[{msg}] {ch}" + RESET)
            sys.stdout.flush()
            time.sleep(interval)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    def stop():
        stop_event.set()
        t.join(timeout=0.2)
        sys.stdout.write("\r" + " " * 80 + "\r")
        sys.stdout.flush()
    return stop

def print_prefilter_banner():
    """Always show a banner immediately—even if the spinner is too brief to notice."""
    sys.stdout.write(GREY + "[prefiltering…]" + RESET + "\r")
    sys.stdout.flush()

# ---------- prefilter + counting ----------
def build_grep_cmd(term: str, cs: bool, nullsep: bool, inbox: Path, outbox: Path) -> list[str]:
    """
    Build ripgrep/grep command.
    - ripgrep (rg) preferred for speed; falls back to grep if rg not present.
    - nullsep=True uses -0/-Z for NUL-separated paths (for streaming).
    """
    if shutil.which("rg"):
        cmd = ["rg", "-Il", term, str(inbox), str(outbox)]
        if not cs: cmd.insert(1, "-i")
        if nullsep: cmd.append("-0")
        return cmd
    cmd = ["grep", "-r", "-I", "-l", "-F"]
    if nullsep: cmd.append("-Z")
    if not cs: cmd.append("-i")
    cmd += [term, str(inbox), str(outbox)]
    return cmd

def count_candidates(term: str, cs: bool, inbox: Path, outbox: Path) -> int:
    """
    Count candidates with a guaranteed visible banner + spinner so you
    always see activity *before* the main progress bar appears.
    """
    cmd = build_grep_cmd(term, cs, nullsep=False, inbox=inbox, outbox=outbox)
    print_prefilter_banner()
    stop_spin = start_spinner("prefiltering", interval=0.08)
    total = 0
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for chunk in iter(lambda: proc.stdout.read(65536), ""):
            if not chunk:
                break
            total += chunk.count("\n")
        proc.wait()
    except Exception:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, text=True)
        total = len([ln for ln in (res.stdout or "").splitlines() if ln.strip()])
    finally:
        stop_spin()
    return total

def stream_candidates(term: str, cs: bool, inbox: Path, outbox: Path):
    """Yield candidate files as Path objects (NUL-separated stream)."""
    cmd = build_grep_cmd(term, cs, nullsep=True, inbox=inbox, outbox=outbox)
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        buf = b""
        for chunk in iter(lambda: proc.stdout.read(65536), b""):
            buf += chunk
            while True:
                i = buf.find(b"\x00")
                if i == -1: break
                yield Path(buf[:i].decode("utf-8", errors="ignore"))
                buf = buf[i+1:]
        proc.wait()

# =========================
# Main
# =========================
def main():
    ap = argparse.ArgumentParser(description="Fast .eml search (rg/grep prefilter + Python verify).")
    ap.add_argument("-t", "--term", required=True, help="Search term")
    ap.add_argument("-cs","--casesensitive", action="store_true", help="Case-sensitive search (default insensitive)")
    ap.add_argument("-txt","--logtofile", action="store_true", help="Also write results to ./output.txt")
    ap.add_argument("-j1","--singlethread", action="store_true", help="Single-threaded verify phase")
    ap.add_argument("--fade-age", action="store_true", help="Use subtle fading for dates (default is obvious colors)")
    # Path overrides (portable):
    ap.add_argument("--base",  default=DEFAULT_BASE, help="Root directory that contains inbox/ and outbox/")
    ap.add_argument("--inbox", default=DEFAULT_INBOX, help="Relative or absolute path to INBOX (.eml) folder")
    ap.add_argument("--outbox",default=DEFAULT_OUTBOX,help="Relative or absolute path to OUTBOX (.eml) folder")
    args = ap.parse_args()

    # Resolve paths
    base = Path(os.path.expanduser(args.base)).resolve()
    inbox = Path(args.inbox)
    outbox = Path(args.outbox)
    # If relative, treat as base/<name>
    if not inbox.is_absolute():  inbox  = (base / inbox).resolve()
    if not outbox.is_absolute(): outbox = (base / outbox).resolve()

    # We use the leaf directory name of OUTBOX to colorize paths generically.
    outbox_token = outbox.name or "outbox"

    # Setup
    cs   = args.casesensitive
    term = args.term
    logf = open("output.txt","w",encoding="utf-8") if args.logtofile else None
    color_date_fn = color_date_fade if args.fade_age else color_date_obvious

    start = time.time()
    total = count_candidates(term, cs, inbox, outbox)  # for denominator
    done = hits = bij = 0

    # Worker pool for verification (Python stage)
    nworkers = 1 if args.singlethread else DEFAULT_WORKERS
    lock = threading.Lock()

    # spinner during per-file verify only in single-thread (avoids noisy interleaving)
    def start_verify_spinner():
        stop = start_spinner("scanning", interval=0.1)
        return lambda: None, stop   # tick (unused), stop

    def worker(paths):
        nonlocal done, hits, bij
        tick, stop = (lambda: None, lambda: None)
        if nworkers == 1:
            tick, stop = start_verify_spinner()
        try:
            for p in paths:
                m, ddt, hb = quick_verify(p, term, cs, spinner_cb=tick if nworkers == 1 else None)
                with lock:
                    done += 1
                    if m:
                        hits += 1
                        if hb: bij += 1
                        handle_hit(p, ddt, hb, color_date_fn, logf, outbox_token)
                    progress(done, total, start)
        finally:
            if nworkers == 1:
                stop()

    if nworkers == 1:
        if total == 0:
            progress(0, 0, start)
        else:
            tick, stop = start_verify_spinner()
            try:
                for p in stream_candidates(term, cs, inbox, outbox):
                    m, ddt, hb = quick_verify(p, term, cs, spinner_cb=tick)
                    done += 1
                    if m:
                        hits += 1
                        if hb: bij += 1
                        handle_hit(p, ddt, hb, color_date_fn, logf, outbox_token)
                    progress(done, total, start)
            finally:
                stop()
    else:
        candidates = list(stream_candidates(term, cs, inbox, outbox))
        if not candidates:
            progress(0, 0, start)
        else:
            chunk = max(1, len(candidates)//nworkers)
            threads = []
            for i in range(0, len(candidates), chunk):
                t = threading.Thread(target=worker, args=(candidates[i:i+chunk],), daemon=True)
                threads.append(t); t.start()
            for t in threads: t.join()

    print()
    elapsed = int(time.time() - start)
    print(f"{hits} items found, {bij} with attachments, time elapsed {elapsed}s")
    if logf:
        logf.write(f"{hits} items found, {bij} with attachments, time elapsed {elapsed}s\n")
        logf.close()

if __name__ == "__main__":
    main()
