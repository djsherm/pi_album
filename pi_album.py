#!/usr/bin/env python3
"""
Inky Impression photo frame + Final Jeopardy viewer.

Buttons:
  A (GPIO 5)  - show a random photo from ./album
  B (GPIO 6)  - show the "random" Final Jeopardy clue
  C (GPIO 16) - show the answer to that clue

Logging:
  * Console: INFO (set FRAME_DEBUG=1 for DEBUG)
  * File:    frame.log next to the album folder, always DEBUG
             (includes selenium/urllib3 traffic, so you can see the last
             command sent to the browser before any hang)
  * chromedriver.log + debug_page.html/.png are written to the jeopardy
    folder when the scrape fails.
"""

import io
import json
import logging
import logging.handlers
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

import RPi.GPIO as GPIO
from inky.auto import auto
from PIL import Image, ImageDraw, ImageFont
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

"""
CONFIG
"""

PATH = '/home/daniel/Pimoroni/inky/examples/7color'
JEOPARDY_PATH = os.path.join(PATH, "jeopardy")
PHOTO_ALBUM = os.path.join(PATH, 'album')
LOG_FILE = os.path.join(PATH, 'frame.log')

JARCHIVE_URL = "https://j-archive.com/"
JEOPARDY_FILES = ("recent.jpg", "random.jpg", "recent_answer.jpg", "random_answer.jpg")
STATE_FILE = os.path.join(JEOPARDY_PATH, "scrape_state.json")

# Be gentle with the site: scrape at most once per REFRESH_INTERVAL, and back off
# after failures (6h, 12h, 24h, then 48h) instead of retrying on every restart.
# Set FRAME_FORCE_SCRAPE=1 to bypass the limits for one run (for testing).
REFRESH_INTERVAL = 24 * 3600
BACKOFF_BASE = 6 * 3600
BACKOFF_MAX = 48 * 3600
PREFLIGHT_TIMEOUT = 10      # seconds for the quick TCP check before launching Chromium

CANVAS_SIZE = (640, 400)
JPEG_QUALITY = 90
SATURATION = 0.5

# Scraper timeouts (seconds)
PAGE_LOAD_TIMEOUT = 45      # max time for driver.get()
SCRIPT_TIMEOUT = 20         # max time for execute_script
ELEMENT_WAIT_SECONDS = 12   # wait for table.final_round
SCRAPE_TIMEOUT = 240        # hard cap for the whole scrape; browser is killed after this
HEARTBEAT_SECONDS = 10      # "still running" log interval

# If Chromium hangs on startup, try: export JEOPARDY_HEADLESS_FLAG=--headless
HEADLESS_FLAG = os.environ.get("JEOPARDY_HEADLESS_FLAG", "--headless=new")

# Gpio pins for each button (from top to bottom)
BUTTONS = [5, 6, 16, 24]
# These correspond to buttons A, B, C and D respectively
LABELS = ['A', 'B', 'C', 'D']

"""
LOGGING
"""


def setup_logging() -> logging.Logger:
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s [%(threadName)s] %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stdout)  # StreamHandler flushes every record
    console.setLevel(logging.DEBUG if os.environ.get("FRAME_DEBUG") else logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        root.warning("Could not open log file %s: %s", LOG_FILE, e)

    return logging.getLogger("frame")


log = setup_logging()


@contextmanager
def step(name: str):
    """Log start / end / duration of a block, and the traceback if it fails."""
    t0 = time.monotonic()
    log.info(">> %s", name)
    try:
        yield
    except Exception:
        log.exception("!! %s FAILED after %.1fs", name, time.monotonic() - t0)
        raise
    else:
        log.info("OK %s (%.1fs)", name, time.monotonic() - t0)


"""
SET UP INKY, GPIO AND PHOTOS
"""

log.info("path: %s", PATH)

photos = []
if os.path.isdir(PHOTO_ALBUM):
    photos = [
        f for f in sorted(os.listdir(PHOTO_ALBUM))
        if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".gif"))
    ]
log.info("Found %d photos in %s", len(photos), PHOTO_ALBUM)
log.debug("photos: %s", photos)

# NOTE: ask_user=True will wait for keyboard input if the display EEPROM can't
# be detected. That looks like a hang when run from systemd/cron.
log.info("Detecting Inky display...")
inky = auto(ask_user=True, verbose=True)
log.info("Inky detected: resolution=%s", inky.resolution)

# Set up RPi.GPIO with the "BCM" numbering scheme
GPIO.setmode(GPIO.BCM)
# Buttons connect to ground when pressed, so use a PULL UP.
GPIO.setup(BUTTONS, GPIO.IN, pull_up_down=GPIO.PUD_UP)

display_lock = threading.Lock()
scrape_running = threading.Event()

"""
IMAGE HELPERS
"""


def _save_png_bytes_to_jpg_with_padding(
    png_bytes: bytes,
    out_path: str,
    target_size: Tuple[int, int] = CANVAS_SIZE,
) -> None:
    """
    Convert PNG bytes to a JPEG at `out_path`, scaling so the height is 400px
    (preserving aspect ratio), centering horizontally and padding with white
    so the final image is 640x400.
    """
    if not isinstance(png_bytes, (bytes, bytearray)):
        raise TypeError("png_bytes must be bytes or bytearray")

    parent = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(parent, exist_ok=True)

    try:
        with Image.open(io.BytesIO(png_bytes)) as im:
            im = im.convert("RGBA")
            canvas_w, canvas_h = target_size
            iw, ih = im.size
            log.debug("Source screenshot size: %dx%d (%d bytes)", iw, ih, len(png_bytes))
            if ih == 0:
                raise ValueError("Source image has zero height")

            scale = canvas_h / ih
            new_w = max(1, int(round(iw * scale)))
            resized = im.resize((new_w, canvas_h), Image.LANCZOS)

            bg = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
            bg.paste(resized, ((canvas_w - new_w) // 2, 0), resized)
            bg.convert("RGB").save(out_path, format="JPEG", quality=JPEG_QUALITY)
            log.debug("Saved %s (scaled to %dx%d)", out_path, new_w, canvas_h)
    except Exception as e:
        raise RuntimeError(f"Failed to save image to {out_path}: {e}") from e


"""
SCRAPER
"""


def _version(binary: str) -> str:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15)
        return (out.stdout or out.stderr).strip()
    except Exception as e:
        return f"<unknown: {e!r}>"


def _kill_browser_processes(service, reason: str) -> None:
    """Kill chromedriver and the Chromium it spawned. Unblocks any hung selenium call."""
    proc = getattr(service, "process", None)
    if proc is None:
        log.warning("Cannot kill browser (%s): chromedriver was never started", reason)
        return
    if proc.poll() is not None:
        log.debug("chromedriver pid=%s already exited", proc.pid)
        return
    log.warning("Killing chromedriver pid=%s and its children (%s)", proc.pid, reason)
    try:
        subprocess.run(["pkill", "-9", "-P", str(proc.pid)], timeout=10, check=False)
    except Exception:
        log.exception("pkill of chromedriver children failed")
    try:
        proc.kill()
    except Exception:
        log.exception("Killing chromedriver failed")


def _quit_driver(driver, service) -> None:
    """driver.quit() can itself hang, so give it a deadline and then force-kill."""
    t = threading.Thread(target=driver.quit, name="driver-quit", daemon=True)
    t.start()
    t.join(15)
    if t.is_alive():
        log.warning("driver.quit() did not return within 15s")
    _kill_browser_processes(service, "cleanup")


def _dump_debug(driver, debug_dir: str) -> None:
    """Save the page HTML and a screenshot to help figure out why selectors failed."""
    if driver is None:
        return
    try:
        os.makedirs(debug_dir, exist_ok=True)
        html_path = os.path.join(debug_dir, "debug_page.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        png_path = os.path.join(debug_dir, "debug_page.png")
        driver.save_screenshot(png_path)
        log.info("Wrote debug files: %s, %s", html_path, png_path)
    except Exception:
        log.exception("Could not write debug files")


def save_jarchive_questions_and_answers(
    out_dir: str,
    chromium_path: Optional[str] = None,
    chromedriver_path: Optional[str] = None,
    headless: bool = True,
    wait_seconds: int = ELEMENT_WAIT_SECONDS,
    overall_timeout: int = SCRAPE_TIMEOUT,
    debug_dir: Optional[str] = None,
) -> Tuple[Tuple[str, Dict[str, str]], Tuple[str, Dict[str, str]]]:
    """
    Save Recent/Random question images and corresponding answer images.

    Returns:
      ((recent_question_path, recent_meta), (random_question_path, random_meta))
      where meta = {"category": "...", "clue": "...", "answer": "..."}
    """
    out_dir = os.path.abspath(out_dir)
    debug_dir = os.path.abspath(debug_dir or out_dir)
    if not os.path.isdir(out_dir):
        raise FileNotFoundError(f"Directory does not exist: {out_dir}")

    recent_q = os.path.join(out_dir, "recent.jpg")
    random_q = os.path.join(out_dir, "random.jpg")
    recent_a = os.path.join(out_dir, "recent_answer.jpg")
    random_a = os.path.join(out_dir, "random_answer.jpg")

    default_chromium_bins = ["/usr/bin/chromium-browser", "/usr/bin/chromium", "/snap/bin/chromium"]
    default_chromedriver_bins = [
        "/usr/bin/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
        "/usr/local/bin/chromedriver",
    ]
    chromium = chromium_path or next((p for p in default_chromium_bins if os.path.isfile(p)), None)
    chromedriver = chromedriver_path or next((p for p in default_chromedriver_bins if os.path.isfile(p)), None)

    if chromium is None:
        raise FileNotFoundError("Chromium binary not found. Install chromium-browser or set chromium_path.")
    if chromedriver is None:
        raise FileNotFoundError("chromedriver binary not found. Install chromium-chromedriver or set chromedriver_path.")

    log.info("Chromium:      %s (%s)", chromium, _version(chromium))
    log.info("chromedriver:  %s (%s)", chromedriver, _version(chromedriver))
    log.info("Headless flag: %s", HEADLESS_FLAG if headless else "(off)")

    options = Options()
    if headless:
        options.add_argument(HEADLESS_FLAG)
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--no-first-run")
    options.add_argument("--mute-audio")
    options.add_argument("--window-size=1600,1400")
    # Return from get() at DOMContentLoaded instead of waiting for every ad/tracker
    # on the page to finish loading (a very common cause of "stuck" scrapes).
    options.page_load_strategy = "eager"
    options.binary_location = chromium

    chromedriver_log = os.path.join(debug_dir, "chromedriver.log")
    try:
        service = Service(chromedriver, log_output=chromedriver_log)
    except TypeError:  # older selenium
        service = Service(chromedriver, log_path=chromedriver_log)
    log.info("chromedriver log: %s", chromedriver_log)

    driver = None
    t_start = time.monotonic()
    progress = {"stage": "starting", "since": t_start}

    def set_stage(name: str) -> None:
        progress["stage"] = name
        progress["since"] = time.monotonic()
        log.debug("stage -> %s", name)

    # Heartbeat: shows which stage we are stuck in
    stop_heartbeat = threading.Event()

    def heartbeat() -> None:
        while not stop_heartbeat.wait(HEARTBEAT_SECONDS):
            now = time.monotonic()
            log.info(
                "... still running: stage=%r (%.0fs in stage, %.0fs total)",
                progress["stage"], now - progress["since"], now - t_start,
            )

    threading.Thread(target=heartbeat, name="scrape-heartbeat", daemon=True).start()

    # Watchdog: kill the browser if the whole scrape takes too long
    timed_out = threading.Event()

    def on_timeout() -> None:
        timed_out.set()
        log.error(
            "Scrape exceeded %ss overall (stuck in stage %r). Killing browser.",
            overall_timeout, progress["stage"],
        )
        _kill_browser_processes(service, "overall timeout")

    watchdog = threading.Timer(overall_timeout, on_timeout)
    watchdog.daemon = True
    watchdog.start()

    try:
        set_stage("launch browser")
        with step("Launch Chromium via chromedriver"):
            driver = webdriver.Chrome(service=service, options=options)
            driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
            driver.set_script_timeout(SCRIPT_TIMEOUT)

        set_stage("load page")
        with step(f"Load {JARCHIVE_URL}"):
            try:
                driver.get(JARCHIVE_URL)
            except TimeoutException:
                log.warning(
                    "Page load exceeded %ss; stopping the load and continuing with what we have",
                    PAGE_LOAD_TIMEOUT,
                )
                driver.execute_script("window.stop();")
            log.info(
                "Page: title=%r url=%s readyState=%s",
                driver.title, driver.current_url,
                driver.execute_script("return document.readyState"),
            )

        set_stage("wait for clue tables")
        wait = WebDriverWait(driver, wait_seconds)
        with step(f"Wait up to {wait_seconds}s for a clue table"):
            try:
                wait.until(EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "table.final_round, td.clue_text, td.category_name")))
            except TimeoutException:
                log.warning("No clue table appeared within %ss; continuing best-effort", wait_seconds)

        set_stage("find tables")
        # Try several strategies, most specific first. The later ones key off the
        # cells the rest of the code already relies on (td.clue_text / td.category_name),
        # so they still work if the table's own class name has changed.
        strategies = [
            ("css table.final_round", By.CSS_SELECTOR, "table.final_round"),
            ("xpath table containing td.clue_text", By.XPATH,
             "//td[contains(concat(' ', normalize-space(@class), ' '), ' clue_text ')]/ancestor::table[1]"),
            ("xpath table containing td.category_name", By.XPATH,
             "//td[contains(concat(' ', normalize-space(@class), ' '), ' category_name ')]/ancestor::table[1]"),
        ]
        tables = []
        for label, by, selector in strategies:
            found = driver.find_elements(by, selector)
            unique, seen = [], set()
            for el in found:
                if el.id not in seen:
                    seen.add(el.id)
                    unique.append(el)
            log.info("Strategy %-40s -> %d table(s)", label, len(unique))
            if unique:
                tables = unique
                break

        if not tables:
            all_tables = driver.find_elements(By.TAG_NAME, "table")
            log.error("No clue tables found. title=%r url=%s", driver.title, driver.current_url)
            log.error("Page has %d <table>, %d <iframe>, %d td.clue_text, %d td.category_name",
                      len(all_tables),
                      len(driver.find_elements(By.TAG_NAME, "iframe")),
                      len(driver.find_elements(By.CSS_SELECTOR, "td.clue_text")),
                      len(driver.find_elements(By.CSS_SELECTOR, "td.category_name")))
            for i, t in enumerate(all_tables[:15]):
                log.error("  table[%d] class=%r id=%r", i, t.get_attribute("class"), t.get_attribute("id"))
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
                log.error("Visible page text (first 600 chars): %r", body_text[:600])
            except Exception:
                log.exception("Could not read page body text")
            _dump_debug(driver, debug_dir)
            raise Exception(
                "No clue tables found on the page (see debug_page.html / debug_page.png in "
                f"{debug_dir})")

        def extract_meta_from_table(elem) -> Dict[str, str]:
            cat = ""
            try:
                cat = elem.find_element(By.CSS_SELECTOR, "td.category_name").text.strip()
            except Exception:
                log.debug("No td.category_name in table")
            clue = ""
            try:
                clue_cells = elem.find_elements(By.CSS_SELECTOR, "td.clue_text")
                log.debug("Table has %d td.clue_text cell(s)", len(clue_cells))
                for c in clue_cells:
                    try:
                        if c.is_displayed() and c.text.strip():
                            clue = c.text.strip()
                            break
                    except Exception:
                        pass
                if not clue and clue_cells:
                    clue = clue_cells[0].text.strip()
            except Exception:
                log.debug("Could not extract clue text", exc_info=True)
            return {"category": cat, "clue": clue, "answer": ""}

        def capture_question(elem, out_path: str, label: str) -> Dict[str, str]:
            set_stage(f"capture {label} question")
            with step(f"Capture {label} question -> {os.path.basename(out_path)}"):
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', inline: 'center'});", elem)
                time.sleep(0.2)
                log.debug("Taking element screenshot (%s)", label)
                png = elem.screenshot_as_png
                _save_png_bytes_to_jpg_with_padding(png, out_path, target_size=CANVAS_SIZE)
                meta = extract_meta_from_table(elem)
                log.info("%s: category=%r clue=%r", label, meta["category"], meta["clue"][:80])
                return meta

        def reveal_answer_and_capture(elem, out_path: str, label: str) -> str:
            set_stage(f"capture {label} answer")
            with step(f"Reveal + capture {label} answer -> {os.path.basename(out_path)}"):
                answer_cells = elem.find_elements(By.CSS_SELECTOR, "td.clue_text[id$='_r']")
                if answer_cells:
                    log.debug("Answer cell found via id suffix '_r'")

                if not answer_cells:
                    for c in elem.find_elements(By.CSS_SELECTOR, "td.clue_text"):
                        try:
                            inner = c.get_attribute("innerHTML") or ""
                            if "correct_response" in inner:
                                answer_cells = [c]
                                log.debug("Answer cell found via 'correct_response' in innerHTML")
                                break
                        except Exception:
                            continue

                if not answer_cells:
                    for c in elem.find_elements(By.CSS_SELECTOR, "td.clue_text"):
                        try:
                            style = (c.get_attribute("style") or "").replace(" ", "")
                            if "display:none" in style or not c.is_displayed():
                                answer_cells = [c]
                                log.debug("Answer cell found via hidden-cell heuristic")
                                break
                        except Exception:
                            continue

                if not answer_cells:
                    raise Exception("Answer cell not found inside the provided final_round table")

                ans_elem = answer_cells[0]
                driver.execute_script("""
                    var el = arguments[0];
                    el.style.display = 'table-cell';
                    el.style.visibility = 'visible';
                    el.style.opacity = '1';
                    var em = el.querySelector('em.correct_response');
                    if (em) { em.style.display = 'inline'; em.style.visibility = 'visible'; }
                """, ans_elem)
                time.sleep(0.15)

                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', inline: 'center'});", elem)
                time.sleep(0.15)
                png = elem.screenshot_as_png
                _save_png_bytes_to_jpg_with_padding(png, out_path, target_size=CANVAS_SIZE)

                try:
                    revealed = ans_elem.text.strip()
                except Exception:
                    revealed = (ans_elem.get_attribute("innerText") or "").strip()
                log.info("%s answer: %r", label, revealed)
                return revealed

        if len(tables) >= 2:
            recent_table, random_table = tables[0], tables[1]
        else:
            log.warning("Only one table found; using it for both recent and random")
            recent_table = random_table = tables[0]

        recent_meta = capture_question(recent_table, recent_q, "recent")
        random_meta = capture_question(random_table, random_q, "random")
        recent_meta["answer"] = reveal_answer_and_capture(recent_table, recent_a, "recent")
        random_meta["answer"] = reveal_answer_and_capture(random_table, random_a, "random")

        set_stage("done")
        log.info("Scrape finished in %.1fs", time.monotonic() - t_start)
        return (recent_q, recent_meta), (random_q, random_meta)

    except Exception as e:
        if timed_out.is_set():
            raise TimeoutError(
                f"Scrape exceeded {overall_timeout}s (stuck in stage {progress['stage']!r})") from e
        log.error("Scrape failed during stage %r: %r", progress["stage"], e)
        _dump_debug(driver, debug_dir)
        if isinstance(e, WebDriverException):
            raise Exception(f"Selenium/WebDriver error: {e}") from e
        raise
    finally:
        watchdog.cancel()
        stop_heartbeat.set()
        if driver is not None:
            _quit_driver(driver, service)
        else:
            _kill_browser_processes(service, "driver never started")


def _fmt_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _load_state() -> Dict[str, float]:
    state = {"last_success": 0.0, "last_attempt": 0.0, "failures": 0}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        state["last_success"] = float(data.get("last_success", 0))
        state["last_attempt"] = float(data.get("last_attempt", 0))
        state["failures"] = int(data.get("failures", 0))
    except FileNotFoundError:
        log.debug("No scrape state file yet (%s)", STATE_FILE)
    except Exception:
        log.warning("Could not read %s; starting with fresh state", STATE_FILE, exc_info=True)
    return state


def _save_state(state: Dict[str, float]) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
        log.debug("Saved scrape state: %s", state)
    except Exception:
        log.exception("Could not save scrape state to %s", STATE_FILE)


def _have_cached_images() -> bool:
    return all(os.path.isfile(os.path.join(JEOPARDY_PATH, n)) for n in JEOPARDY_FILES)


def scrape_due(state: Dict[str, float]) -> Tuple[bool, str]:
    """Decide whether a scrape is allowed right now. Returns (due, human-readable reason)."""
    now = time.time()
    have_images = _have_cached_images()
    last_success = state["last_success"]
    if have_images and not last_success:
        # Images that predate the state file: trust their timestamp.
        try:
            last_success = os.path.getmtime(os.path.join(JEOPARDY_PATH, "random.jpg"))
        except OSError:
            pass

    if have_images and last_success and now - last_success < REFRESH_INTERVAL:
        return False, (f"cached clues are only {_fmt_age(now - last_success)} old "
                       f"(refreshing every {_fmt_age(REFRESH_INTERVAL)})")

    if state["failures"] > 0 and state["last_attempt"]:
        wait = min(BACKOFF_BASE * 2 ** (state["failures"] - 1), BACKOFF_MAX)
        since = now - state["last_attempt"]
        if since < wait:
            return False, (f"last attempt was {_fmt_age(since)} ago and {state['failures']} "
                           f"attempt(s) in a row have not succeeded; backing off for another "
                           f"{_fmt_age(wait - since)}")

    if not have_images:
        return True, "no cached clues yet"
    if last_success:
        return True, f"cached clues are {_fmt_age(now - last_success)} old"
    return True, "cached clues have unknown age"


def _site_reachable() -> bool:
    """Cheap TCP check so we don't launch Chromium (and wait ~minutes) when the site is unreachable."""
    host = urlparse(JARCHIVE_URL).hostname
    try:
        with socket.create_connection((host, 443), timeout=PREFLIGHT_TIMEOUT):
            pass
        log.info("Preflight OK: TCP connection to %s:443 succeeded", host)
        return True
    except OSError as e:
        log.error(
            "Preflight FAILED: could not connect to %s:443 within %ss (%s). "
            "The site is unreachable from this network (blocked, down, or a routing problem).",
            host, PREFLIGHT_TIMEOUT, e)
        return False


def refresh_jeopardy(force: bool = False) -> None:
    """
    Scrape at most once per REFRESH_INTERVAL (with backoff after failures).
    Scrapes into a temp folder and only replaces the existing images on success,
    so a failed scrape never leaves the frame without a clue to show.
    """
    force = force or bool(os.environ.get("FRAME_FORCE_SCRAPE"))
    state = _load_state()
    due, reason = scrape_due(state)
    if not due and not force:
        log.info("Skipping Jeopardy scrape: %s", reason)
        return
    if not due and force:
        log.warning("FRAME_FORCE_SCRAPE set; ignoring limit (%s)", reason)

    log.info("=== Jeopardy refresh starting (%s) ===", reason)
    # Record the attempt up front and count it as a failure until it succeeds, so a
    # crash, kill or power cut mid-scrape can't cause a scrape on every restart.
    state["last_attempt"] = time.time()
    state["failures"] += 1
    _save_state(state)

    scrape_running.set()
    t0 = time.monotonic()
    tmp_dir = None
    try:
        os.makedirs(JEOPARDY_PATH, exist_ok=True)
        if not _site_reachable():
            raise ConnectionError(f"{JARCHIVE_URL} is unreachable from this network")
        tmp_dir = tempfile.mkdtemp(prefix=".scrape_", dir=JEOPARDY_PATH)
        log.debug("Scraping into temp dir %s", tmp_dir)

        (_, recent_meta), (_, random_meta) = save_jarchive_questions_and_answers(
            tmp_dir, debug_dir=JEOPARDY_PATH)

        # Only touch the existing images once ALL four new ones exist and are non-empty,
        # so a partial scrape can never replace a good set with a mixed one.
        for name in JEOPARDY_FILES:
            src = os.path.join(tmp_dir, name)
            if not os.path.isfile(src) or os.path.getsize(src) == 0:
                raise FileNotFoundError(f"Scraper did not produce a valid {src}; keeping old images")
        for name in JEOPARDY_FILES:
            dst = os.path.join(JEOPARDY_PATH, name)
            os.replace(os.path.join(tmp_dir, name), dst)
            log.info("Installed %s", dst)

        state["last_success"] = time.time()
        state["failures"] = 0
        _save_state(state)
        log.info("Recent clue: %s", recent_meta)
        log.info("Random clue: %s", random_meta)
        log.info("=== Jeopardy refresh complete (%.1fs) ===", time.monotonic() - t0)
    except Exception:
        wait = min(BACKOFF_BASE * 2 ** (state["failures"] - 1), BACKOFF_MAX)
        log.exception("=== Jeopardy refresh FAILED after %.1fs; keeping any previous images ===",
                      time.monotonic() - t0)
        log.error("Failure #%d in a row. No further attempt for at least %s (until about %s).",
                  state["failures"], _fmt_age(wait),
                  time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + wait)))
    finally:
        scrape_running.clear()
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


"""
DISPLAY
"""


def _fmt_stale(seconds: float) -> str:
    hours = max(0, int(seconds)) // 3600
    if hours < 1:
        return "<1h old"
    if hours < 48:
        return f"{hours}h old"
    return f"{hours // 24}d old"


def _load_font(size: int):
    for font_path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                      "/usr/share/fonts/truetype/freefont/FreeSans.ttf"):
        try:
            return ImageFont.truetype(font_path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)  # newer Pillow
    except TypeError:
        return ImageFont.load_default()


def _stamp_age(image: Image.Image, path: str) -> Image.Image:
    """
    Return a copy of `image` with a small "Scraped <date> - <age>" label in the
    bottom-right corner, based on when the file was saved. The file on disk is not changed.
    """
    try:
        mtime = os.path.getmtime(path)
        text = (f"Scraped {time.strftime('%Y-%m-%d', time.localtime(mtime))} - "
                f"{_fmt_stale(time.time() - mtime)}")
        img = image.convert("RGB")
        draw = ImageDraw.Draw(img)
        font = _load_font(13)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        w, h = right - left, bottom - top
        pad = 3
        x1, y1 = img.width - (w + 2 * pad) - 1, img.height - (h + 2 * pad) - 1
        draw.rectangle([x1, y1, img.width - 1, img.height - 1], fill=(255, 255, 255), outline=(0, 0, 0))
        draw.text((x1 + pad - left, y1 + pad - top), text, font=font, fill=(0, 0, 0))
        log.debug("Stamped image with %r", text)
        return img
    except Exception:
        log.exception("Could not stamp age on %s; showing it unmodified", path)
        return image


def _push_to_display(inky, image: Image.Image, label: str) -> bool:
    """Send an image to the e-ink panel. Skips if a refresh is already in progress."""
    if not display_lock.acquire(blocking=False):
        log.warning("Display busy (refresh in progress); ignoring request for %s", label)
        return False
    try:
        t0 = time.monotonic()
        log.info("Refreshing e-ink display with %s (takes ~30s)...", label)
        inky.set_image(image.rotate(180), saturation=SATURATION)
        inky.show()
        log.info("Display refresh done (%.1fs)", time.monotonic() - t0)
        return True
    finally:
        display_lock.release()


def Show_Image(inky, path, album):
    """
    inky = inky object representing the eInk display
    path = folder (relative to PATH) the photos are in
    album = list containing strings of photo names
    """
    if not album:
        log.warning("No photos available in %s", path)
        return None

    name = random.choice(album)
    full_path = os.path.join(PATH, path, name)
    log.info("Selected photo: %s", full_path)
    with Image.open(full_path) as image:
        resized = image.resize(inky.resolution)
        _push_to_display(inky, resized, f"photo {name}")
    return None


def Show_Image_On_Pimoroni(inky, path):
    """
    inky = inky object representing the eInk display
    path = path to the actual image
    """
    if not os.path.isfile(path):
        if scrape_running.is_set():
            log.warning("%s not ready yet: Jeopardy scrape is still running", path)
        else:
            log.warning("No cached Jeopardy clue yet (%s missing). The last scrape failed or "
                        "was skipped; see the log lines at startup.", path)
        return
    log.info("Showing cached %s (scraped %s ago)", os.path.basename(path),
             _fmt_age(time.time() - os.path.getmtime(path)))
    with Image.open(path) as image:
        if image.size != tuple(inky.resolution):
            log.debug("Resizing %s from %s to %s", path, image.size, inky.resolution)
            image = image.resize(inky.resolution)
        _push_to_display(inky, _stamp_age(image, path), os.path.basename(path))


# "handle_button" is called every time a button is pressed.
# It receives one argument: the associated input pin.
def handle_button(pin):
    try:
        label = LABELS[BUTTONS.index(pin)]
        log.info("Button press detected on pin: %s label: %s", pin, label)
        if pin == 5:  # button furthest away from usb power plug
            Show_Image(inky, 'album', photos)
        elif pin == 6:
            Show_Image_On_Pimoroni(inky, os.path.join(JEOPARDY_PATH, "random.jpg"))
        elif pin == 16:
            Show_Image_On_Pimoroni(inky, os.path.join(JEOPARDY_PATH, "random_answer.jpg"))
        # can add more functionality for other buttons here
    except Exception:
        log.exception("Error while handling button on pin %s", pin)


"""
MAIN
"""


def main():
    # Buttons first, so they work even while the scrape is running.
    for pin in BUTTONS:
        GPIO.add_event_detect(pin, GPIO.FALLING, handle_button, bouncetime=250)
    log.info("Button handlers registered on pins %s", BUTTONS)

    try:
        Show_Image(inky, 'album', photos)  # random image on startup
    except Exception:
        log.exception("Failed to show startup image")

    # Scrape in the background so a slow/hung scrape can't block the frame.
    threading.Thread(target=refresh_jeopardy, name="scraper", daemon=True).start()

    log.info("Ready. Waiting for button presses.")
    try:
        signal.pause()
    except KeyboardInterrupt:
        log.info("Interrupted; shutting down")
    finally:
        GPIO.cleanup()


if __name__ == "__main__":
    main()
