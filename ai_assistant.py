"""
Ultron — personal desktop voice assistant.

Run:
    python assistant.py          # voice mode (falls back to typing if no mic)
    python assistant.py --text   # type commands instead of speaking

Settings live in ~/.desktop_assistant/config.json (created on first run).
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import imaplib
import json
import logging
import os
import platform
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from email import message_from_bytes
from email.header import decode_header
from pathlib import Path
from typing import Callable, Optional

# --------------------------------------------------------------------------
# Optional dependencies. The assistant degrades gracefully if any is missing.
# --------------------------------------------------------------------------
try:
    import speech_recognition as sr
    import pyaudio
except Exception as e:
    print(f"Audio system notice: {e}")
    sr = None

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

try:
    import pywhatkit
except ImportError:
    pywhatkit = None

try:
    import wikipedia
except ImportError:
    wikipedia = None

try:
    import psutil
except ImportError:
    psutil = None

try:
    import pyautogui
except ImportError:
    pyautogui = None

try:
    import requests
except ImportError:
    requests = None

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
except ImportError:
    webdriver = None

try:
    from deep_translator import GoogleTranslator
except ImportError:
    GoogleTranslator = None

try:
    import screen_brightness_control as sbc
except ImportError:
    sbc = None


APP_DIR = Path.home() / ".desktop_assistant"
APP_DIR.mkdir(exist_ok=True)
NOTES_FILE = APP_DIR / "notes.txt"
CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE = APP_DIR / "assistant.log"
BROWSER_PROFILE_DIR = APP_DIR / "browser_profile"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s | pid %(process)d | %(levelname)s | %(message)s",
)
log = logging.getLogger("assistant")

OS_NAME = platform.system()  # 'Windows' | 'Darwin' | 'Linux'


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
# NOTE on wake_word: "" = always on (no prefix needed on every command). It
# previously defaulted to "hello ultron", which meant EVERY command was
# silently ignored unless prefixed with that exact phrase. If you want an
# old-style per-command wake word, set "wake_word" explicitly in config.json.
#
# NOTE on activation_phrase / start_asleep: this is different from
# wake_word. It's meant for running the assistant as a background process
# (started automatically at login — see README) that sits quietly until you
# say the activation phrase, then stays fully awake and takes normal
# commands with no prefix needed, until you tell it to sleep or exit.
#
# NOTE on notifications: "email" and "whatsapp" support here is polling-based
# (IMAP for email, a shared Selenium WhatsApp Web tab for WhatsApp), NOT a
# hook into OS/app push notifications. Reading a real OS notification from an
# arbitrary app needs platform-specific accessibility APIs (a UWP toast
# listener on Windows, NSUserNotificationCenter on macOS, a D-Bus signal
# watcher on Linux) plus permissions this script can't assume. Polling gets
# you the same practical behaviour ("you've got a new message, want to
# reply?") without those platform-specific hooks.
DEFAULT_CONFIG = {
    "user_title": "Boss",
    "assistant_name": "Ultron",
    "voice_rate": 178,
    "voice_index": 0,          # 0 = first system voice, 1 = usually female on Windows
    "listen_timeout": 6,       # seconds to wait for you to start speaking
    "phrase_time_limit": 8,    # max seconds of a single command
    "wake_word": "",           # "" = always on. Set e.g. "hello ultron" to require it on every command.
    "activation_phrase": "ultron online",  # say this once to wake the assistant from standby
    "start_asleep": True,      # start in standby (waiting for activation_phrase) — good for autostart
    "contacts": {},            # name -> "+<countrycode><number>" for WhatsApp
    "default_country_code": "91",  # used when a 10-digit number is given without country code
    "hindi_support": True,     # translate Hindi/Hinglish speech to English before matching commands
    "browser_profile_dir": str(BROWSER_PROFILE_DIR),  # persistent Chrome profile so YouTube/WhatsApp stay logged in
    "notification_polling_enabled": False,   # background-check WhatsApp/email without being asked
    "notification_poll_seconds": 30,
    "email": {
        "enabled": False,
        "imap_server": "imap.gmail.com",
        "smtp_server": "smtp.gmail.com",
        "address": "",
        "app_password": "",   # use an app password, never your normal account password
    },
    "config_version": 5,
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy (nested "email" dict)
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg.update(saved)
            if "email" in saved:
                merged_email = dict(DEFAULT_CONFIG["email"])
                merged_email.update(saved["email"])
                cfg["email"] = merged_email

            dirty = False

            # One-time migration: older versions of this file shipped with
            # assistant_name "Nova" and an empty wake_word. If we detect that
            # untouched old default, upgrade it to the new "Ultron" / wake-word
            # setup instead of silently keeping the stale values forever.
            if saved.get("assistant_name") == "Nova" and not saved.get("wake_word"):
                cfg["assistant_name"] = "Ultron"
                cfg["wake_word"] = ""
                dirty = True

            # One-time migration: configs written before this fix have
            # wake_word="hello ultron" baked in from the old DEFAULT_CONFIG,
            # even though the person never asked for a wake word — they just
            # inherited the old default. If it still matches that old
            # default exactly (i.e. wasn't deliberately customized to
            # something else), clear it so commands work without a prefix.
            # config_version gates this so it only ever runs once.
            if saved.get("config_version", 1) < 2:
                if saved.get("wake_word", "hello ultron") == "hello ultron":
                    cfg["wake_word"] = ""
                dirty = True

            # config_version 3: introduces "contacts" for WhatsApp messaging.
            if saved.get("config_version", 1) < 3:
                cfg.setdefault("contacts", {})
                dirty = True

            # config_version 4: introduces the standby/activation-phrase
            # system for running as a background/autostart process.
            if saved.get("config_version", 1) < 4:
                cfg.setdefault("activation_phrase", DEFAULT_CONFIG["activation_phrase"])
                cfg.setdefault("start_asleep", DEFAULT_CONFIG["start_asleep"])
                dirty = True

            # config_version 5: persistent browser window, notification
            # polling (WhatsApp Web + email), and Hindi support toggle.
            if saved.get("config_version", 1) < 5:
                cfg.setdefault("hindi_support", DEFAULT_CONFIG["hindi_support"])
                cfg.setdefault("browser_profile_dir", DEFAULT_CONFIG["browser_profile_dir"])
                cfg.setdefault("notification_polling_enabled", DEFAULT_CONFIG["notification_polling_enabled"])
                cfg.setdefault("notification_poll_seconds", DEFAULT_CONFIG["notification_poll_seconds"])
                cfg.setdefault("email", DEFAULT_CONFIG["email"])
                dirty = True

            if dirty:
                cfg["config_version"] = DEFAULT_CONFIG["config_version"]
                CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception as exc:  # corrupted file shouldn't kill startup
            log.warning("Bad config, using defaults: %s", exc)
    else:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


CONFIG = load_config()


# --------------------------------------------------------------------------
# Single-instance guard.
# If two copies of the assistant run at once (e.g. the autostart one plus one
# you started by hand, or an old window that never closed), BOTH hear the same
# command, BOTH speak and BOTH do the work -> everything happens twice.
# A new copy now tells the older copy to quit, so only one is ever alive.
# --------------------------------------------------------------------------
_LOCK_PORT = 47653
_lock_sock = None
_ACTIVE = None  # the running Assistant (so the old copy can close its browser)


def _lock_listener(srv) -> None:
    while True:
        try:
            conn, _ = srv.accept()
            data = conn.recv(16)
            try:  # reset instead of a normal close, so the port isn't left in TIME_WAIT
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            except Exception:
                pass
            conn.close()
            if data.strip() == b"quit":
                log.info("A newer copy started; this copy is shutting down.")
                print("\nℹ️  A newer copy of the assistant started. Closing this older copy.")
                try:
                    if _ACTIVE is not None:
                        _ACTIVE.browser.close()
                except Exception:
                    pass
                os._exit(0)
        except Exception:
            return


def acquire_single_instance() -> bool:
    global _lock_sock
    for _ in range(2):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":  # Linux/macOS: allow re-binding right after the old copy closed
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", _LOCK_PORT))
            srv.listen(2)
            _lock_sock = srv
            threading.Thread(target=_lock_listener, args=(srv,), daemon=True).start()
            return True
        except OSError:
            srv.close()
            try:  # another copy is alive: ask it to quit, then try again
                c = socket.create_connection(("127.0.0.1", _LOCK_PORT), timeout=2)
                c.sendall(b"quit")
                c.close()
            except Exception:
                pass
            time.sleep(2.5)
    return False


# --------------------------------------------------------------------------
# Speech output
# --------------------------------------------------------------------------
class Speaker:
    """
    Text-to-speech.

    IMPORTANT: pyttsx3's SAPI5/NSSpeechSynthesizer/espeak drivers can only run
    `runAndWait()` reliably ONE time on a given engine instance in most
    processes. Reusing a single long-lived engine object is exactly why the
    assistant used to speak once and then go silent (falling back to
    print-only "text" output) for every command after the first. The fix is
    to build a brand-new engine for every utterance and dispose of it right
    after, which is the standard, battle-tested workaround for this bug.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._available = pyttsx3 is not None
        if self._available:
            probe = self._make_engine()
            if probe is None:
                self._available = False
            else:
                try:
                    probe.stop()
                except Exception:
                    pass

    def _make_engine(self):
        if pyttsx3 is None:
            return None
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", CONFIG["voice_rate"])
            voices = engine.getProperty("voices")
            idx = CONFIG["voice_index"]
            if voices and 0 <= idx < len(voices):
                engine.setProperty("voice", voices[idx].id)
            return engine
        except Exception as exc:
            log.error("TTS init failed: %s", exc)
            return None

    def say(self, text: str) -> None:
        print(f"\n🤖 {CONFIG['assistant_name']}: {text}")
        log.info("SAY: %s", text)
        if not self._available:
            return

        with self._lock:
            engine = self._make_engine()
            if engine is None:
                log.error("TTS engine could not be created for this utterance.")
                return
            try:
                engine.say(text)
                engine.runAndWait()
            except Exception as exc:
                log.error("TTS failed: %s", exc)
            finally:
                try:
                    engine.stop()
                except Exception:
                    pass
                try:
                    del engine
                except Exception:
                    pass
        time.sleep(0.35)  # let the room echo die down before the mic opens


# --------------------------------------------------------------------------
# Speech input
# --------------------------------------------------------------------------
class Ears:
    """Microphone listener. Falls back to keyboard input when unavailable."""

    def __init__(self, force_text: bool = False) -> None:
        self.text_mode = force_text or sr is None
        self.recognizer = None
        self.mic = None
        self.last_raw = ""  # last thing heard/typed, original casing (used for WhatsApp messages)

        if self.text_mode:
            return

        try:
            self.recognizer = sr.Recognizer()
            self.recognizer.dynamic_energy_threshold = True
            self.recognizer.pause_threshold = 0.8
            self.mic = sr.Microphone()
            with self.mic as source:
                print("🎚️  Calibrating microphone for background noise...")
                self.recognizer.adjust_for_ambient_noise(source, duration=1.2)
        except Exception as exc:
            log.error("Mic unavailable: %s", exc)
            print(f"⚠️  Microphone unavailable ({exc}). Switching to text mode.")
            self.text_mode = True

    def listen(self) -> str:
        if self.text_mode:
            try:
                raw = input("\n⌨️  You: ").strip()
            except (EOFError, KeyboardInterrupt):
                return "exit"
            self.last_raw = raw  # keep original capital letters for messages
            return raw.lower()

        try:
            with self.mic as source:
                print("\n🎤 Listening...")
                audio = self.recognizer.listen(
                    source,
                    timeout=CONFIG["listen_timeout"],
                    phrase_time_limit=CONFIG["phrase_time_limit"],
                )
        except sr.WaitTimeoutError:
            return ""
        except Exception as exc:
            log.error("Listen error: %s", exc)
            return ""

        print("🧠 Recognizing...")
        # Try English (India) first, then fall back to Hindi. This is what
        # lets the assistant understand commands spoken in Hindi — the
        # recognizer gives us Devanagari/Hinglish text, and Assistant later
        # translates it to English before matching commands.
        for lang in ("en-IN", "hi-IN"):
            try:
                text = self.recognizer.recognize_google(audio, language=lang)
                print(f"👤 You ({lang}): {text}")
                log.info("HEARD [%s]: %s", lang, text)
                self.last_raw = text.strip()
                return text.lower().strip()
            except sr.UnknownValueError:
                continue
            except sr.RequestError as exc:
                log.error("Speech API error: %s", exc)
                print("⚠️  Speech service unreachable (check internet).")
                return ""
        return ""


# --------------------------------------------------------------------------
# Synonyms — rewrites many ways of saying the same thing into the canonical
# words the command rules understand ("launch/start/fire up" -> "open",
# "put on/listen to" -> "play", "find/look for" -> "search", "text/ping/dm"
# -> "message", "louder" -> "volume up", Hindi word order, etc).
# Message bodies (after "saying ...") are never touched.
# --------------------------------------------------------------------------
_HINDI_ORDER = [
    (r"^(?P<o>.+?)\s+(?:kholo|khol do|khol|open karo|open kar do|chalu karo|start karo)$", "open {o}"),
    (r"^(?P<o>.+?)\s+(?:bajao|bajao na|chalao|chala do|play karo|play kar do)$", "play {o}"),
    (r"^(?P<o>.+?)\s+(?:band karo|band kar do|band kar|close karo|close kar do)$", "close {o}"),
    (r"^(?P<o>.+?)\s+(?:dhundo|dhoondo|khojo|search karo|search kar do)$", "search {o}"),
]

_PHRASES = [
    # play
    (r"\bstart(?:ing)? playing\b|\bput on\b|\blisten to\b|\bstream\b", "play"),
    # open
    (r"\b(?:launch|fire up|boot up|bring up|pull up|navigate to|take me to|visit|start)\b", "open"),
    (r"\bgo to(?!\s+sleep)\b", "open"),
    # close
    (r"\b(?:terminate|dismiss|shut(?!\s*down))\b", "close"),
    # search
    (r"\b(?:look for|lookup|find)\b", "search"),
    # messaging
    (r"\bsend\s+(?:a\s+|an\s+)?(?:whatsapp\s+|wa\s+)?(?:message|msg|text|sms)(?:\s+on\s+whatsapp)?\s+to\b", "message"),
    (r"\bsend\s+(?:a\s+)?whatsapp\s+to\b", "message"),
    (r"^(?:text|dm|ping|msg)\s+", "message "),
    # volume
    (r"\b(?:louder|sound up|(?:increase|raise|turn up)\s+(?:the\s+)?(?:volume|sound))\b", "volume up"),
    (r"\b(?:quieter|softer|sound down|(?:decrease|lower|reduce|turn down)\s+(?:the\s+)?(?:volume|sound))\b", "volume down"),
    (r"\bsilence\b", "mute"),
    # misc
    (r"\bscreen ?capture\b|\bscreen ?grab\b|\bsnap(?:shot)?\b", "screenshot"),
    (r"\bkitne baje\b|\bkya time\b|\btime kya hai\b|\bwhat(?:'s| is) the clock\b", "what is the time"),
    (r"\baaj ki tarikh\b|\bdate kya hai\b", "what is the date"),
    (r"\bpower level\b|\bcharge left\b|\bcharging status\b", "battery"),
    (r"\bgo dormant\b|\brest now\b|\bso jao\b|\bso ja\b", "go to sleep"),
    (r"\block down\b|\bsecure (?:the )?(?:pc|screen)\b", "lock the screen"),
]

_FILLERS = re.compile(
    r"\b(?:please|kindly|can you|could you|would you|will you|for me|zara|jara)\b\s*", re.I
)
_NOTE_START = re.compile(r"^(?:take a note|add a note|make a note|note|type|write)\b", re.I)
_MSG_SPLIT = re.compile(r"\s(?:saying|that says|to say)\s|:", re.I)


def normalize_synonyms(text: str) -> str:
    original = (text or "").strip()
    if not original:
        return original
    t = re.sub(r"^(?:jot down|write down|remember that|make a memo|memo)\b\s*", "note ", original, flags=re.I)
    if _NOTE_START.match(t.strip()):
        return t.strip()

    m = _MSG_SPLIT.search(t)
    head, tail = (t[: m.start()], t[m.start():]) if m else (t, "")

    head = _FILLERS.sub("", head).strip()
    for pat, repl in _HINDI_ORDER:
        hm = re.match(pat, head, re.I)
        if hm:
            head = repl.format(o=hm.group("o").strip())
            break
    for pat, repl in _PHRASES:
        head = re.sub(pat, repl, head, flags=re.I)
    head = re.sub(r"\s{2,}", " ", head).strip()

    out = (head + tail).strip()
    return out or original


# --------------------------------------------------------------------------
# Persistent browser window (Selenium) — lets "open youtube" then "search X"
# reuse the SAME window/tab instead of spawning a new one every time, and
# lets WhatsApp Web notification-checking share that same window.
# --------------------------------------------------------------------------

# YouTube: filter results to videos only, and force playback with JS.
_YT_SP = "&sp=EgIQAQ%253D%253D"

_PLAY_JS = """
const skip=document.querySelector('.ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern');
if(skip){skip.click();}
const v=document.querySelector('video.html5-main-video')||document.querySelector('video');
if(!v){return 'novideo';}
if(v.paused){
  v.play().catch(()=>{const b=document.querySelector('.ytp-play-button'); if(b){b.click();}});
  return 'resumed';
}
return 'playing';
"""


def _ensure_playing(driver, seconds: float = 14.0) -> bool:
    """Keep nudging the player until the video is really playing (also skips ads)."""
    end = time.time() + seconds
    seen_playing = 0
    while time.time() < end:
        try:
            state = driver.execute_script(_PLAY_JS)
        except Exception:
            state = None
        if state == "playing":
            seen_playing += 1
            if seen_playing >= 4:
                return True
        time.sleep(0.8)
    return seen_playing > 0


# WhatsApp Web selectors (WhatsApp changes its page now and then — if sending
# stops working, update these).
_WA_TEXTBOX = [
    "//footer//div[@contenteditable='true']",
    "//div[@id='main']//div[@contenteditable='true'][@data-tab]",
]
_WA_SEARCH = [
    "//div[@contenteditable='true'][@data-tab='3']",
    "//*[@id='side']//div[@contenteditable='true']",
    "//div[@role='textbox'][@aria-label='Search input textbox']",
]
_WA_SEND_BTN = "//span[@data-icon='send']|//button[@aria-label='Send']"
_WA_HEADER = "//div[@id='main']//header//span[@title]"


def _wait_xpath(driver, xpaths, timeout: float):
    end = time.time() + timeout
    while time.time() < end:
        for xp in xpaths:
            try:
                els = driver.find_elements(By.XPATH, xp)
                if els:
                    return els[0]
            except Exception:
                pass
        time.sleep(0.5)
    return None


def _safe_get(driver, url: str) -> None:
    try:
        driver.get(url)
    except Exception:
        driver.switch_to.alert.accept()  # "Leave site?" popup
        driver.get(url)


class BrowserSession:
    def __init__(self, profile_dir: str):
        self.profile_dir = profile_dir
        self.driver = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        return webdriver is not None

    def ensure_driver(self):
        if webdriver is None:
            return None
        with self._lock:
            if self.driver is not None:
                try:
                    handles = self.driver.window_handles  # liveness check
                    if not handles:
                        raise RuntimeError("no windows")
                    try:
                        _ = self.driver.current_url
                    except Exception:
                        self.driver.switch_to.window(handles[-1])
                    return self.driver
                except Exception:
                    self.driver = None
            try:
                opts = ChromeOptions()
                opts.add_argument(f"--user-data-dir={self.profile_dir}")
                opts.add_argument("--profile-directory=Default")
                # lets YouTube start playing without a manual click
                opts.add_argument("--autoplay-policy=no-user-gesture-required")
                opts.add_argument("--start-maximized")
                opts.add_experimental_option("excludeSwitches", ["enable-logging"])
                self.driver = webdriver.Chrome(options=opts)
                return self.driver
            except Exception as exc:
                log.error("Could not start Chrome/Selenium: %s", exc)
                self.driver = None
                return None

    def goto(self, url: str) -> bool:
        driver = self.ensure_driver()
        if driver is None:
            return False
        try:
            driver.get(url)
            return True
        except Exception as exc:
            log.error("Navigation failed: %s", exc)
            return False

    def on_youtube(self) -> bool:
        if self.driver is None:
            return False
        try:
            return "youtube.com" in (self.driver.current_url or "")
        except Exception:
            return False

    def youtube_search(self, query: str) -> bool:
        """Search inside the already-open YouTube tab instead of opening a
        new window. If no YouTube tab is open yet, opens exactly one."""
        driver = self.ensure_driver()
        if driver is None:
            return False
        try:
            if not self.on_youtube():
                driver.get("https://www.youtube.com")
                time.sleep(1.5)
            box = WebDriverWait(driver, 6).until(
                EC.presence_of_element_located((By.NAME, "search_query"))
            )
            box.clear()
            box.send_keys(query)
            box.send_keys(Keys.RETURN)
            return True
        except Exception as exc:
            log.error("YouTube in-window search failed: %s", exc)
            try:  # go to the results page in THIS window instead of failing over to a new one
                driver.get("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query))
                return True
            except Exception:
                return False

    def youtube_play_first_result(self, query: str) -> bool:
        """Opens the first real video and FORCES playback (no manual click)."""
        driver = self.ensure_driver()
        if driver is None:
            return False
        try:
            driver.get(
                "https://www.youtube.com/results?search_query="
                + urllib.parse.quote_plus(query) + _YT_SP
            )
        except Exception as exc:
            log.error("YouTube navigation failed: %s", exc)
            return False
        try:
            WebDriverWait(driver, 12).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "ytd-video-renderer a#video-title"))
            )
            href = driver.find_element(
                By.CSS_SELECTOR, "ytd-video-renderer a#video-title"
            ).get_attribute("href")
            driver.get(href)  # go straight to the video page
            _ensure_playing(driver)
        except Exception as exc:
            # The results page is already open in OUR window. Returning False here
            # made the caller open a SECOND window/tab -> the song "played twice".
            log.error("YouTube autoplay-first-result failed: %s", exc)
        return True

    def whatsapp_tab(self):
        """Switch to the (single, reused) WhatsApp Web tab, opening it if needed."""
        driver = self.ensure_driver()
        if driver is None:
            return None
        try:
            for handle in driver.window_handles:
                driver.switch_to.window(handle)
                if "web.whatsapp.com" in (driver.current_url or ""):
                    return driver
            driver.execute_script("window.open('https://web.whatsapp.com','_blank');")
            driver.switch_to.window(driver.window_handles[-1])
            time.sleep(2)
            return driver
        except Exception as exc:
            log.error("WhatsApp tab failed: %s", exc)
            return None

    def open_whatsapp_if_needed(self) -> bool:
        return self.whatsapp_tab() is not None

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None


def scrape_first_youtube_video_id(query: str) -> Optional[str]:
    """Fallback for when Selenium isn't installed: find the first real
    video id from YouTube's search results so opening a URL lands on a
    playing VIDEO, not a search-results page."""
    if requests is None:
        return None
    try:
        resp = requests.get(
            "https://www.youtube.com/results",
            params={"search_query": query, "sp": "EgIQAQ%3D%3D"},
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-IN,en;q=0.9"},
            timeout=8,
        )
        match = re.search(r'"videoRenderer":\{"videoId":"([\w-]{11})"', resp.text)
        if match:
            return match.group(1)
    except Exception as exc:
        log.error("YouTube scrape failed: %s", exc)
    return None


# --------------------------------------------------------------------------
# Notifications: WhatsApp (via the shared browser) + email (via IMAP/SMTP).
# Polling-based — see the NOTE at the top of DEFAULT_CONFIG for why.
# --------------------------------------------------------------------------
class NotificationCenter:
    def __init__(self, browser: BrowserSession, config: dict):
        self.browser = browser
        self.config = config
        self._seen_whatsapp: set[tuple[str, str]] = set()  # (chat, last message) already announced

    # ---- email ----
    def check_email(self) -> Optional[dict]:
        cfg = self.config.get("email", {})
        if not cfg.get("enabled") or not cfg.get("address") or not cfg.get("app_password"):
            return None
        try:
            imap = imaplib.IMAP4_SSL(cfg.get("imap_server", "imap.gmail.com"))
            imap.login(cfg["address"], cfg["app_password"])
            imap.select("INBOX")
            status, data = imap.search(None, "UNSEEN")
            if status != "OK" or not data or not data[0]:
                imap.logout()
                return None
            latest_id = data[0].split()[-1]
            status, msg_data = imap.fetch(latest_id, "(RFC822)")
            imap.logout()
            if status != "OK" or not msg_data or not msg_data[0]:
                return None
            msg = message_from_bytes(msg_data[0][1])
            raw_subject, enc = decode_header(msg.get("Subject", ""))[0]
            subject = raw_subject.decode(enc or "utf-8", errors="ignore") if isinstance(raw_subject, bytes) else raw_subject
            sender = msg.get("From", "an unknown sender")
            return {"channel": "email", "from": sender, "subject": subject or "(no subject)"}
        except Exception as exc:
            log.error("Email check failed: %s", exc)
            return None

    def send_email_reply(self, to_addr: str, subject: str, body: str) -> bool:
        cfg = self.config.get("email", {})
        if not cfg.get("enabled"):
            return False
        try:
            import smtplib
            from email.mime.text import MIMEText

            msg = MIMEText(body)
            msg["Subject"] = "Re: " + subject
            msg["From"] = cfg["address"]
            msg["To"] = to_addr
            with smtplib.SMTP_SSL(cfg.get("smtp_server", "smtp.gmail.com"), 465) as server:
                server.login(cfg["address"], cfg["app_password"])
                server.send_message(msg)
            return True
        except Exception as exc:
            log.error("Email send failed: %s", exc)
            return False

    # ---- whatsapp (shared Selenium window on web.whatsapp.com) ----
    def check_whatsapp(self) -> Optional[dict]:
        if not self.browser.available():
            return None
        driver = self.browser.ensure_driver()
        if driver is None or not self.browser.open_whatsapp_if_needed():
            return None
        try:
            unread_badges = driver.find_elements(
                By.XPATH,
                "//div[@aria-label='Chat list']//span[@aria-label and contains(@aria-label,'unread')]",
            )
            for badge in unread_badges:
                chat_row = badge.find_element(By.XPATH, "./ancestor::div[@role='listitem']")
                name_els = chat_row.find_elements(By.XPATH, ".//span[@title]")
                name = name_els[0].get_attribute("title") if name_els else "someone"
                chat_row.click()
                time.sleep(1.2)
                bubbles = driver.find_elements(
                    By.XPATH,
                    "//div[contains(@class,'message-in')]//span[contains(@class,'selectable-text')]",
                )
                last_text = bubbles[-1].text if bubbles else "(couldn't read the text)"
                # Remember (chat, text), not just the chat: the old code
                # ignored every LATER message from someone once they'd
                # messaged you once.
                if (name, last_text) in self._seen_whatsapp:
                    continue
                self._seen_whatsapp.add((name, last_text))
                return {"channel": "whatsapp", "from": name, "text": last_text}
            return None
        except Exception as exc:
            log.error("WhatsApp check failed: %s", exc)
            return None

    def send_whatsapp_reply(self, contact_name: str, text: str) -> bool:
        if not self.browser.available():
            return False
        driver = self.browser.ensure_driver()
        if driver is None:
            return False
        try:
            boxes = driver.find_elements(By.XPATH, "//footer//div[@role='textbox']")
            if not boxes:
                boxes = driver.find_elements(By.XPATH, _WA_TEXTBOX[0])
            if not boxes:
                return False
            box = boxes[0]
            box.click()
            box.send_keys(text)
            box.send_keys(Keys.ENTER)
            return True
        except Exception as exc:
            log.error("WhatsApp reply failed: %s", exc)
            return False

    # ---- send a fresh WhatsApp message on command ----
    @staticmethod
    def resolve_number(name: str) -> Optional[str]:
        """Digits-only phone number (with country code) for a typed/spoken
        name or number, or None if we must search the chat by name."""
        n = (name or "").strip()
        cc = str(CONFIG.get("default_country_code", "91"))
        if re.fullmatch(r"\+?[\d\s\-()]{10,}", n):
            digits = re.sub(r"\D", "", n)
            return cc + digits if len(digits) == 10 else digits
        contacts = {k.lower(): v for k, v in CONFIG.get("contacts", {}).items()}
        key = n.lower()
        if key not in contacts:
            close = difflib.get_close_matches(key, list(contacts), n=1, cutoff=0.8)
            key = close[0] if close else key
        value = contacts.get(key)
        if not value:
            return None
        digits = re.sub(r"\D", "", value)
        return cc + digits if len(digits) == 10 else digits

    def send_whatsapp_message(self, target: str, text: str) -> tuple[bool, str]:
        driver = self.browser.whatsapp_tab()
        if driver is None:
            return False, "I couldn't start the browser."

        number = self.resolve_number(target)
        if number:
            _safe_get(
                driver,
                f"https://web.whatsapp.com/send?phone={number}&text={urllib.parse.quote(text)}",
            )
            box = _wait_xpath(driver, _WA_TEXTBOX, 90)  # first time: scan the QR code
            if box is None:
                return False, "WhatsApp Web didn't load. Is it logged in? Scan the QR code once."
            time.sleep(1.2)
            btn = driver.find_elements(By.XPATH, _WA_SEND_BTN)
            if btn:
                btn[0].click()
            else:
                box.send_keys(Keys.ENTER)
            time.sleep(1.5)
            return True, ""

        # No number known: search the chat list by the name saved on your phone.
        if not _wait_xpath(driver, _WA_SEARCH, 1):
            _safe_get(driver, "https://web.whatsapp.com")
        search = _wait_xpath(driver, _WA_SEARCH, 90)
        if search is None:
            return False, "WhatsApp Web didn't load. Is it logged in? Scan the QR code once."
        search.click()
        search.send_keys(Keys.COMMAND if OS_NAME == "Darwin" else Keys.CONTROL, "a")
        search.send_keys(Keys.BACKSPACE)
        search.send_keys(target)
        time.sleep(1.8)
        search.send_keys(Keys.ENTER)
        time.sleep(1.2)

        box = _wait_xpath(driver, _WA_TEXTBOX, 15)
        if box is None:
            return False, f"I couldn't find a chat named {target}."
        try:
            header = driver.find_elements(By.XPATH, _WA_HEADER)
            title = (header[0].get_attribute("title") or "").lower() if header else ""
            if (
                title
                and target.lower() not in title
                and difflib.SequenceMatcher(None, target.lower(), title).ratio() < 0.6
            ):
                return False, f"The first match was {title}, not {target}, so I didn't send it."
        except Exception:
            pass
        box.click()
        box.send_keys(text)
        time.sleep(0.4)
        box.send_keys(Keys.ENTER)
        time.sleep(1.2)
        return True, ""


# --------------------------------------------------------------------------
# Skills / command handlers
# --------------------------------------------------------------------------
WEBSITES = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "chatgpt": "https://chat.openai.com",
    "claude": "https://claude.ai",
    "whatsapp": "https://web.whatsapp.com",
    "instagram": "https://www.instagram.com",
    "facebook": "https://www.facebook.com",
    "twitter": "https://twitter.com",
    "x": "https://x.com",
    "linkedin": "https://www.linkedin.com",
    "reddit": "https://www.reddit.com",
    "amazon": "https://www.amazon.in",
    "flipkart": "https://www.flipkart.com",
    "netflix": "https://www.netflix.com",
    "spotify": "https://open.spotify.com",
    "stack overflow": "https://stackoverflow.com",
    "stackoverflow": "https://stackoverflow.com",
    "wikipedia": "https://www.wikipedia.org",
    "drive": "https://drive.google.com",
    "maps": "https://maps.google.com",
    "chat gpt": "https://chat.openai.com",
}

# Local applications by OS. Value = command list passed to the shell/launcher.
APPS = {
    "Windows": {
        "chrome": "chrome",
        "notepad": "notepad",
        "calculator": "calc",
        "paint": "mspaint",
        "explorer": "explorer",
        "file manager": "explorer",
        "cmd": "cmd",
        "command prompt": "cmd",
        "task manager": "taskmgr",
        "control panel": "control",
        "word": "winword",
        "excel": "excel",
        "vs code": "code",
        "visual studio code": "code",
        "settings": "ms-settings:",
    },
    "Darwin": {
        "chrome": "Google Chrome",
        "safari": "Safari",
        "notes": "Notes",
        "calculator": "Calculator",
        "terminal": "Terminal",
        "finder": "Finder",
        "vs code": "Visual Studio Code",
        "visual studio code": "Visual Studio Code",
    },
    "Linux": {
        "chrome": "google-chrome",
        "firefox": "firefox",
        "terminal": "x-terminal-emulator",
        "files": "nautilus",
        "file manager": "nautilus",
        "calculator": "gnome-calculator",
        "vs code": "code",
        "visual studio code": "code",
    },
}

# Actual process/app names used to CLOSE a running app (as opposed to APPS,
# which holds the command used to LAUNCH it — these can differ, e.g. the
# Windows launcher command "chrome" vs. the running process "chrome.exe").
PROCESS_NAMES = {
    "Windows": {
        "chrome": "chrome.exe",
        "notepad": "notepad.exe",
        "calculator": "CalculatorApp.exe",
        "paint": "mspaint.exe",
        "explorer": "explorer.exe",
        "file manager": "explorer.exe",
        "cmd": "cmd.exe",
        "command prompt": "cmd.exe",
        "task manager": "Taskmgr.exe",
        "word": "WINWORD.EXE",
        "excel": "EXCEL.EXE",
        "vs code": "Code.exe",
        "visual studio code": "Code.exe",
    },
    "Darwin": {
        "chrome": "Google Chrome",
        "safari": "Safari",
        "notes": "Notes",
        "calculator": "Calculator",
        "terminal": "Terminal",
        "finder": "Finder",
        "vs code": "Visual Studio Code",
        "visual studio code": "Visual Studio Code",
    },
    "Linux": {
        "chrome": "chrome",
        "firefox": "firefox",
        "terminal": "x-terminal-emulator",
        "files": "nautilus",
        "file manager": "nautilus",
        "calculator": "gnome-calculator",
        "vs code": "code",
        "visual studio code": "code",
    },
}

# Extra names people use for apps -> the name shown in the Start menu / app list.
APP_ALIASES = {
    "cmd": "command prompt", "command line": "command prompt",
    "file manager": "file explorer", "files": "file explorer", "explorer": "file explorer",
    "vs code": "visual studio code", "vscode": "visual studio code", "code": "visual studio code",
    "calc": "calculator", "ms word": "word", "ms excel": "excel",
    "google chrome": "chrome",
}

_startapps_cache: dict = {}


def _windows_start_apps() -> dict:
    """Every app in the Start menu (classic + Microsoft Store): name -> AppID."""
    global _startapps_cache
    if _startapps_cache:
        return _startapps_cache
    apps: dict = {}
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-StartApps | ConvertTo-Json"],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(out.stdout or "[]")
        if isinstance(data, dict):
            data = [data]
        for item in data:
            name = (item.get("Name") or "").strip().lower()
            appid = (item.get("AppID") or "").strip()
            if name and appid:
                apps[name] = appid
    except Exception as exc:
        log.error("Get-StartApps failed: %s", exc)
    _startapps_cache = apps
    return apps


def _linux_desktop_apps() -> dict:
    apps: dict = {}
    dirs = [
        "/usr/share/applications",
        os.path.expanduser("~/.local/share/applications"),
        "/var/lib/flatpak/exports/share/applications",
        "/var/lib/snapd/desktop/applications",
    ]
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.endswith(".desktop"):
                continue
            try:
                with open(os.path.join(d, fn), encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.startswith("Name="):
                            apps[line[5:].strip().lower()] = fn[:-8]
                            break
            except Exception:
                continue
    return apps


def _best_match(target: str, names: list) -> Optional[str]:
    """Loose app-name matching: exact, whole word, substring, then spelling-close."""
    t = APP_ALIASES.get(target.lower().strip(), target.lower().strip())
    if not t or not names:
        return None
    if t in names:
        return t
    if len(t) >= 3:
        whole = [n for n in names if re.search(rf"\b{re.escape(t)}\b", n)]
        if whole:
            return min(whole, key=len)
        part = [n for n in names if t in n]
        if part:
            return min(part, key=len)
    close = difflib.get_close_matches(t, names, n=1, cutoff=0.75)
    return close[0] if close else None


SMALL_TALK = {
    r"\b(hello|hi|hey|hy)\b": [
        "Hello {title}! I'm right here.",
        "Hey {title}, good to hear you.",
        "Hi {title}! What are we doing today?",
    ],
    r"how are you|kaise ho|how r u": [
        "Running at full power, {title}. How about you?",
        "I'm great — no bugs today. How are you feeling?",
    ],
    r"what.*your name|who are you": [
        "I'm {name}, your personal assistant. Built to work for you, {title}.",
    ],
    r"thank(s| you)|shukriya": [
        "Anytime, {title}.",
        "That's what I'm here for.",
    ],
    r"i.?m (sad|tired|bored|stressed)": [
        "That happens, {title}. Want me to play some music on YouTube?",
        "Take a breath. I can open something relaxing if you want.",
    ],
    r"good (morning|afternoon|evening|night)": [
        "Good {0} to you too, {title}.",
    ],
    r"who made you|who created you": [
        "You did, {title}. Every line of me came from your machine.",
    ],
    r"tell me a joke|joke sunao": [
        "Why do programmers prefer dark mode? Because light attracts bugs.",
        "There are 10 kinds of people: those who understand binary, and those who don't.",
        "I would tell you a UDP joke, but you might not get it.",
    ],
}

FILLER_ACKS = [
    "On it, {title}.",
    "Right away.",
    "Done, {title}.",
    "Sure thing.",
]


@dataclass
class Rule:
    """One command rule: a regex and the function that handles it."""
    pattern: str
    handler: Callable[["Assistant", re.Match], None]
    regex: re.Pattern = field(init=False)

    def __post_init__(self) -> None:
        self.regex = re.compile(self.pattern, re.IGNORECASE)


# --------------------------------------------------------------------------
# The assistant
# --------------------------------------------------------------------------
class Assistant:
    def __init__(self, force_text: bool = False) -> None:
        self.speaker = Speaker()
        self.ears = Ears(force_text=force_text)
        self.running = True
        self.title = CONFIG["user_title"]
        self.name = CONFIG["assistant_name"]
        self.wake_word = (CONFIG.get("wake_word") or "").strip().lower()
        self.activation_phrase = (CONFIG.get("activation_phrase") or "").strip().lower()
        # Standby is for the background voice process; when you're typing,
        # start awake so commands work straight away.
        self.asleep = (
            bool(CONFIG.get("start_asleep", True))
            and bool(self.activation_phrase)
            and not self.ears.text_mode
        )
        self.rules = self._build_rules()

        # Shared, reused browser window + notification polling.
        self.browser = BrowserSession(CONFIG.get("browser_profile_dir", str(BROWSER_PROFILE_DIR)))
        self.notifications = NotificationCenter(self.browser, CONFIG)
        self.pending_reply: Optional[dict] = None  # {"stage": "confirm"|"compose", "channel": ..., "from": ..., ...}
        self._poll_thread: Optional[threading.Thread] = None

        # Protection against the same command running twice.
        self._recent_said: list = []       # (timestamp, cleaned text) of what WE spoke
        self._last_cmd = ""
        self._last_cmd_time = 0.0

    # ---------------- helpers ----------------
    def say(self, text: str) -> None:
        # replace() instead of str.format(): text with { or } in it (a
        # Wikipedia summary, a song name) made format() raise and the
        # assistant only said "Something went wrong".
        spoken = text.replace("{title}", self.title).replace("{name}", self.name)
        self.speaker.say(spoken)
        self._recent_said.append((time.time(), re.sub(r"[^\w\s]", "", spoken.lower()).strip()))
        self._recent_said = self._recent_said[-8:]

    def _is_echo(self, text: str) -> bool:
        """True if the mic just heard the assistant's own voice (e.g. it says
        "Playing X on YouTube" and then treats that as a new command)."""
        heard = re.sub(r"[^\w\s]", "", text.lower()).strip()
        if len(heard) < 4:
            return False
        now = time.time()
        for ts, said in self._recent_said:
            if not said or now - ts > 12:
                continue
            if (len(heard) >= 8 and heard in said) or difflib.SequenceMatcher(None, heard, said).ratio() >= 0.8:
                return True
        return False

    def _dispatch(self, text: str) -> None:
        """Run one command, ignoring an identical command repeated within 4 s."""
        now = time.time()
        if (not self.ears.text_mode and self.pending_reply is None
                and text == self._last_cmd and now - self._last_cmd_time < 4.0):
            log.info("Ignored duplicate command: %s", text)
            return
        if self.asleep:
            self._try_wake(text)
        else:
            self.handle(text)
        self._last_cmd, self._last_cmd_time = text, time.time()

    def ack(self) -> None:
        self.say(random.choice(FILLER_ACKS))

    def ask(self, question: str) -> str:
        self.say(question)
        for _ in range(2):
            reply = self.ears.listen()
            if reply:
                return reply
        return ""

    @staticmethod
    def open_url(url: str) -> None:
        webbrowser.open(url, new=2)

    def launch_app(self, key: str) -> bool:
        """Open a local app. Returns True ONLY if something was really found
        (the old version returned True for any name on Windows, so it said
        "Opening..." while nothing opened)."""
        key = (key or "").lower().strip()
        if not key:
            return False
        try:
            if OS_NAME == "Windows":
                # 1) any app in the Start menu (classic + Microsoft Store), fuzzy name match
                apps = _windows_start_apps()
                best = _best_match(key, list(apps))
                if best:
                    subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + apps[best]])
                    return True
                # 2) built-in table / PATH
                cmd = APPS.get("Windows", {}).get(key)
                if cmd:
                    if cmd.endswith(":"):
                        os.startfile(cmd)  # type: ignore[attr-defined]
                        return True
                    if shutil.which(cmd):
                        subprocess.Popen(cmd, shell=True)
                        return True
                exe = shutil.which(key)
                if exe:
                    subprocess.Popen([exe])
                    return True
                return False

            if OS_NAME == "Darwin":
                name = APPS.get("Darwin", {}).get(key) or APP_ALIASES.get(key, key)
                for candidate in (name, name.title()):
                    res = subprocess.run(["open", "-a", candidate], capture_output=True, text=True)
                    if res.returncode == 0:
                        return True
                return False

            # Linux
            name = APPS.get("Linux", {}).get(key) or key
            for cand in (name, name.replace(" ", "-")):
                if shutil.which(cand):
                    subprocess.Popen([cand])
                    return True
            desk = _linux_desktop_apps()
            best = _best_match(key, list(desk))
            if best:
                if shutil.which("gtk-launch"):
                    subprocess.Popen(["gtk-launch", desk[best]])
                    return True
                if shutil.which("gio"):
                    subprocess.Popen(["gio", "launch", desk[best] + ".desktop"])
                    return True
            return False
        except Exception as exc:
            log.error("Launch failed for %s: %s", key, exc)
            return False

    def _start_menu_search(self, target: str) -> bool:
        """Last resort on Windows: press Win, type the name, press Enter."""
        if pyautogui is None or OS_NAME != "Windows":
            return False
        try:
            pyautogui.press("win")
            time.sleep(0.7)
            pyautogui.typewrite(target, interval=0.03)
            time.sleep(0.9)
            pyautogui.press("enter")
            return True
        except Exception as exc:
            log.error("Start-menu search failed: %s", exc)
            return False

    def _close_named_app(self, target: str) -> bool:
        """Try hard to close a specific named app/process, using whichever
        mechanism is available: psutil first (cross-platform, most
        reliable), then an OS-native fallback (taskkill / osascript / pkill)
        so this still works even without psutil installed."""
        proc_name = PROCESS_NAMES.get(OS_NAME, {}).get(target)
        candidates = {c.lower() for c in (proc_name, target) if c}

        closed = False
        if psutil is not None:
            for proc in psutil.process_iter(["name"]):
                try:
                    name = (proc.info.get("name") or "").lower()
                except Exception:
                    continue
                if not name:
                    continue
                if any(cand in name or (len(name) >= 4 and name in cand) for cand in candidates):
                    try:
                        proc.terminate()
                        closed = True
                    except Exception as exc:
                        log.error("Could not terminate %s: %s", name, exc)
            if closed:
                return True

        try:
            if OS_NAME == "Windows":
                kill_target = proc_name or (target if target.lower().endswith(".exe") else target + ".exe")
                result = subprocess.run(
                    ["taskkill", "/IM", kill_target, "/F"], capture_output=True, text=True
                )
                return result.returncode == 0
            elif OS_NAME == "Darwin":
                result = subprocess.run(
                    ["osascript", "-e", f'quit app "{proc_name or target}"'],
                    capture_output=True, text=True,
                )
                return result.returncode == 0
            else:
                if shutil.which("pkill"):
                    result = subprocess.run(
                        ["pkill", "-f", proc_name or target], capture_output=True, text=True
                    )
                    return result.returncode == 0
        except Exception as exc:
            log.error("Close app failed for %s: %s", target, exc)
        return False

    def _close_active_window(self) -> bool:
        if pyautogui is None:
            self.say("I need pyautogui installed to close the active window, {title}.")
            return False
        try:
            if OS_NAME == "Darwin":
                pyautogui.hotkey("command", "w")
            else:
                pyautogui.hotkey("alt", "f4")
            return True
        except Exception as exc:
            log.error("Close active window failed: %s", exc)
            return False

    def _maybe_translate(self, text: str) -> str:
        """If Hindi/Hinglish support is on and the text looks non-English,
        translate it to English so the (English) command regexes still
        match. Cheap heuristic first so we don't call the translator on
        every plain-English sentence."""
        if GoogleTranslator is None:
            return text
        looks_ascii = all(ord(c) < 128 for c in text)
        common_hinglish = re.search(
            r"\b(hai|kya|kaise|karo|kar do|bhejo|bolo|nahi|haan|dikhao|khol|bajao|padho)\b",
            text,
        )
        if looks_ascii and not common_hinglish:
            return text
        try:
            translated = GoogleTranslator(source="auto", target="en").translate(text)
            if translated:
                log.info("Translated '%s' -> '%s'", text, translated)
                return translated.lower().strip()
        except Exception as exc:
            log.error("Translation failed: %s", exc)
        return text

    # ---------------- handlers ----------------
    def h_open(self, m: re.Match) -> None:
        raw = m.group("target").strip().lower()
        # "open whatsapp app" -> prefer the installed desktop app over the website
        want_app = bool(re.search(r"\b(app|application|software|program)\b", raw))

        target = re.sub(r"^(the|a|my)\s+", "", raw)
        target = re.sub(r"\s+(website|site|app|application|software|program|dot com)$", "", target).strip()

        if not target:
            target = self.ask("What should I open, {title}?").lower().strip()
            if not target:
                return

        web_key = next(
            (k for k in sorted(WEBSITES, key=len, reverse=True)
             if re.search(rf"\b{re.escape(k)}\b", target)),
            None,
        )

        # Websites first (unless the person said "app").
        if web_key and not want_app:
            self.say(f"Opening {web_key}, {{title}}.")
            # Route YouTube through the shared, reused browser window so
            # a later "search X" lands in THIS window, not a new one.
            if web_key == "youtube" and self.browser.available():
                if not self.browser.goto(WEBSITES[web_key]):
                    self.open_url(WEBSITES[web_key])
            else:
                self.open_url(WEBSITES[web_key])
            return

        # Local app (Start menu / Store / PATH). Only says "Opening" if found.
        if self.launch_app(target):
            self.say(f"Opening {target}, {{title}}.")
            return

        if web_key:
            self.say(f"I couldn't find the {web_key} app, opening the website instead.")
            self.open_url(WEBSITES[web_key])
            return

        if "." in target and " " not in target:
            self.say(f"Opening {target}.")
            self.open_url(f"https://{target}")
            return

        # "open X app" and nothing found: let Windows search find it.
        if want_app and self._start_menu_search(target):
            self.say(f"Searching Windows for {target}, {{title}}.")
            return

        guess = target.replace(" ", "")
        self.say(f"I couldn't find an app called {target}. Trying {guess} dot com.")
        self.open_url(f"https://{guess}.com")

    def h_close(self, m: re.Match) -> None:
        target = (m.group("target") or "").strip().lower()
        target = re.sub(r"^(the|a|my|this|that|current)\s+", "", target).strip()
        target = re.sub(r"\s+(window|app|application|program)s?$", "", target).strip()
        # Words like "window"/"this"/"it" on their own mean "the active
        # window", not a specific named app.
        if target in ("", "window", "this", "that", "it", "current"):
            target = ""

        if target:
            if self._close_named_app(target):
                self.say(f"Closed {target}, {{title}}.")
            else:
                self.say(
                    f"I couldn't find {target} running, {{title}}. "
                    "Closing the active window instead."
                )
                self._close_active_window()
        else:
            if self._close_active_window():
                self.say("Closed the active window, {title}.")
            else:
                self.say("I couldn't close that window, {title}.")

    def h_brightness(self, m: re.Match) -> None:
        word = (m.group("act") or "").strip().lower()
        increase_words = {"up", "increase", "brighter", "raise", "turn up", "brighten"}
        decrease_words = {"down", "decrease", "dimmer", "darker", "lower", "reduce", "turn down", "dim", "darken"}
        direction = "down" if word in decrease_words else "up"
        step = 10

        if sbc is not None:
            try:
                current = sbc.get_brightness(display=0)
                if isinstance(current, list):
                    current = current[0]
                new = min(100, current + step) if direction == "up" else max(0, current - step)
                sbc.set_brightness(new, display=0)
                self.say(f"Brightness set to {new} percent, {{title}}.")
                return
            except Exception as exc:
                log.error("screen_brightness_control failed: %s", exc)

        try:
            if OS_NAME == "Darwin":
                key_code = "144" if direction == "up" else "145"
                subprocess.run(
                    ["osascript", "-e", f'tell application "System Events" to key code {key_code}'],
                    capture_output=True, text=True,
                )
                self.ack()
                return
            elif OS_NAME == "Linux":
                if shutil.which("brightnessctl"):
                    pct = f"{step}%+" if direction == "up" else f"{step}%-"
                    subprocess.run(["brightnessctl", "set", pct], capture_output=True, text=True)
                    self.ack()
                    return
                if shutil.which("xbacklight"):
                    flag = "-inc" if direction == "up" else "-dec"
                    subprocess.run(["xbacklight", flag, str(step)], capture_output=True, text=True)
                    self.ack()
                    return
            elif OS_NAME == "Windows":
                sign = "+" if direction == "up" else "-"
                ps = (
                    "$b=(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightness).CurrentBrightness;"
                    f"$n=[Math]::Max(0,[Math]::Min(100,$b{sign}{step}));"
                    "(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,$n)"
                )
                subprocess.run(["powershell", "-Command", ps], capture_output=True, text=True)
                self.ack()
                return
        except Exception as exc:
            log.error("Brightness fallback failed: %s", exc)

        self.say(
            "I couldn't change the brightness, {title}. Installing "
            "screen-brightness-control (pip install screen-brightness-control) "
            "gives the most reliable control on this machine."
        )

    def h_youtube_play(self, m: re.Match) -> None:
        query = (m.group("q") or "").strip()
        query = re.sub(r"\s+on youtube$", "", query)
        query = re.sub(r"^(?:the\s+)?(?:song|video|music|track|gana|gaana)\s+", "", query).strip()
        if not query:
            query = self.ask("What should I play?")
        if not query:
            return
        self.say(f"Playing {query} on YouTube.")

        # 1) Best path: reuse the shared window, open the first real video and
        #    force playback (autoplay flag + JS play + ad skip).
        if self.browser.available() and self.browser.youtube_play_first_result(query):
            return

        # 2) No Selenium: scrape the first real video id so a plain
        #    webbrowser.open() still lands on a VIDEO (with autoplay), not a search.
        video_id = scrape_first_youtube_video_id(query)
        if video_id:
            self.open_url(f"https://www.youtube.com/watch?v={video_id}&autoplay=1")
            return

        # 3) pywhatkit as another attempt.
        if pywhatkit:
            try:
                pywhatkit.playonyt(query)
                return
            except Exception as exc:
                log.error("pywhatkit failed: %s", exc)

        # 4) Last resort: plain search (old behaviour).
        self.open_url(
            "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
        )

    def h_youtube_search(self, m: re.Match) -> None:
        q = m.group("q").strip() or self.ask("Search YouTube for what?")
        if not q:
            return
        self.say(f"Searching YouTube for {q}.")
        # Reuses the already-open YouTube window/tab instead of a new one.
        if self.browser.available() and self.browser.youtube_search(q):
            return
        self.open_url(
            "https://www.youtube.com/results?search_query=" + q.replace(" ", "+")
        )

    def h_google(self, m: re.Match) -> None:
        q = m.group("q").strip() or self.ask("What should I search?")
        if not q:
            return
        self.say(f"Searching Google for {q}.")
        self.open_url("https://www.google.com/search?q=" + q.replace(" ", "+"))

    def h_wiki(self, m: re.Match) -> None:
        q = m.group("q").strip() or self.ask("Which topic?")
        if not q:
            return
        if wikipedia is None:
            self.say("Wikipedia module isn't installed, opening the website instead.")
            self.open_url("https://en.wikipedia.org/wiki/" + q.replace(" ", "_"))
            return
        try:
            self.say("Looking that up.")
            summary = wikipedia.summary(q, sentences=2, auto_suggest=True)
            self.say(summary)
        except Exception as exc:
            log.error("Wikipedia error: %s", exc)
            self.say("I couldn't get a clean summary. Opening the page instead.")
            self.open_url("https://en.wikipedia.org/wiki/" + q.replace(" ", "_"))

    def h_time(self, _m: re.Match) -> None:
        self.say("It's " + dt.datetime.now().strftime("%I:%M %p").lstrip("0") + ", {title}.")

    def h_date(self, _m: re.Match) -> None:
        self.say("Today is " + dt.datetime.now().strftime("%A, %d %B %Y") + ".")

    def h_note(self, m: re.Match) -> None:
        text = m.group("q").strip() or self.ask("What should I write down?")
        if not text:
            return
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        with NOTES_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {text}\n")
        self.say("Noted, {title}.")

    def h_read_notes(self, _m: re.Match) -> None:
        if not NOTES_FILE.exists() or not NOTES_FILE.read_text(encoding="utf-8").strip():
            self.say("Your notes are empty, {title}.")
            return
        lines = NOTES_FILE.read_text(encoding="utf-8").strip().splitlines()[-5:]
        self.say("Here are your last notes.")
        for line in lines:
            self.say(line.split("] ", 1)[-1])

    def h_screenshot(self, _m: re.Match) -> None:
        if pyautogui is None:
            self.say("Install pyautogui first and I can take screenshots.")
            return
        path = APP_DIR / f"shot_{dt.datetime.now():%Y%m%d_%H%M%S}.png"
        try:
            pyautogui.screenshot().save(path)
            self.say("Screenshot saved, {title}.")
            print(f"   → {path}")
        except Exception as exc:
            log.error("Screenshot failed: %s", exc)
            self.say("I couldn't capture the screen.")

    def h_system_status(self, _m: re.Match) -> None:
        if psutil is None:
            self.say("Install psutil and I can read your system stats.")
            return
        cpu = psutil.cpu_percent(interval=0.6)
        ram = psutil.virtual_memory().percent
        msg = f"CPU is at {cpu:.0f} percent and memory at {ram:.0f} percent"
        battery = getattr(psutil, "sensors_battery", lambda: None)()
        if battery:
            plugged = "charging" if battery.power_plugged else "on battery"
            msg += f". Battery is {battery.percent:.0f} percent, {plugged}"
        self.say(msg + ", {title}.")

    def h_volume(self, m: re.Match) -> None:
        if pyautogui is None:
            self.say("I need pyautogui for volume control.")
            return
        action = m.group("act").lower()
        key = {"up": "volumeup", "increase": "volumeup", "down": "volumedown",
               "decrease": "volumedown", "mute": "volumemute",
               "unmute": "volumemute"}.get(action, "volumeup")
        presses = 1 if "mute" in key else 5
        for _ in range(presses):
            pyautogui.press(key)
        self.ack()

    def h_lock(self, _m: re.Match) -> None:
        self.say("Locking the screen, {title}.")
        time.sleep(1)
        try:
            if OS_NAME == "Windows":
                subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"])
            elif OS_NAME == "Darwin":
                subprocess.run(["pmset", "displaysleepnow"])
            else:
                subprocess.run(["loginctl", "lock-session"])
        except Exception as exc:
            log.error("Lock failed: %s", exc)
            self.say("I couldn't lock it.")

    def h_power(self, m: re.Match) -> None:
        action = "restart" if "restart" in m.group(0) or "reboot" in m.group(0) else "shut down"
        confirm = self.ask(f"Are you sure you want me to {action} the computer? Say yes to confirm.")
        if "yes" not in confirm and "confirm" not in confirm:
            self.say("Cancelled, {title}.")
            return
        self.say(f"{action.capitalize()}ing now. See you soon, {{title}}.")
        time.sleep(2)
        try:
            if OS_NAME == "Windows":
                subprocess.run(["shutdown", "/r" if action == "restart" else "/s", "/t", "5"])
            else:
                subprocess.run(["sudo", "shutdown", "-r" if action == "restart" else "-h", "now"])
        except Exception as exc:
            log.error("Power command failed: %s", exc)
            self.say("That command needs higher permissions.")

    def h_type(self, m: re.Match) -> None:
        if pyautogui is None:
            self.say("I need pyautogui to type for you.")
            return
        text = m.group("q").strip() or self.ask("What should I type?")
        if not text:
            return
        self.say("Typing it now.")
        time.sleep(0.6)
        pyautogui.typewrite(text, interval=0.02)

    def h_press_enter(self, _m: re.Match) -> None:
        if pyautogui is None:
            return
        pyautogui.press("enter")
        self.ack()

    def h_whatsapp(self, m: re.Match) -> None:
        """Send a WhatsApp message by command (typed or spoken), through
        WhatsApp Web in the shared Chrome window.

        Examples:
            message ravi saying hello
            whatsapp mom saying I will be late
            send a message to 9876543210 saying hi

        The target can be: a name saved in config.json "contacts", a phone
        number, or simply the contact's name as saved on your phone (found via
        WhatsApp Web search). First time only: scan the QR code.
        """
        name = re.sub(r"^to\s+", "", (m.group("name") or "").strip())
        msg = (m.group("msg") or "").strip()

        if not name:
            name = self.ask("Who should I message?")
            if not name:
                return
        if not msg:
            msg = self.ask(f"What should I say to {name}?")
            if not msg:
                return

        # In typed mode the command was lower-cased; restore the original casing.
        raw = getattr(self.ears, "last_raw", "") or ""
        idx = raw.lower().find(msg.lower())
        if idx >= 0:
            msg = raw[idx: idx + len(msg)]

        number = self.notifications.resolve_number(name)

        if not self.browser.available():
            if number and pywhatkit:
                try:
                    pywhatkit.sendwhatmsg_instantly(
                        "+" + number, msg, wait_time=20, tab_close=True, close_time=3
                    )
                    self.say(f"Sent to {name}.")
                except Exception as exc:
                    log.error("pywhatkit send failed: %s", exc)
                    self.say("I couldn't send that, {title}.")
            else:
                self.say("Install selenium (pip install selenium) so I can message people by name, {title}.")
            return

        self.say(f"Messaging {name} on WhatsApp, {{title}}. If it asks for a QR code, scan it once.")
        try:
            ok, why = self.notifications.send_whatsapp_message(name, msg)
        except Exception as exc:
            log.error("WhatsApp send crashed: %s", exc)
            ok, why = False, "Something went wrong in the browser."
        self.say(f"Sent to {name}, {{title}}." if ok else f"I couldn't send it. {why}")

    def h_add_contact(self, m: re.Match) -> None:
        """'save contact ravi as 9876543210' -> stored in config.json."""
        name = m.group("name").strip().lower()
        num = re.sub(r"[^\d+]", "", m.group("msg"))
        if not num.startswith("+"):
            digits = re.sub(r"\D", "", num)
            if len(digits) == 10:
                digits = str(CONFIG.get("default_country_code", "91")) + digits
            num = "+" + digits
        CONFIG.setdefault("contacts", {})[name] = num
        try:
            CONFIG_FILE.write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
            self.say(f"Saved {name}, {{title}}.")
        except Exception as exc:
            log.error("Saving contact failed: %s", exc)
            self.say("I couldn't save that contact.")

    def h_check_notification(self, _m: re.Match) -> None:
        """'What's the message' / 'any new notification' — checks WhatsApp
        (shared browser tab) then email (IMAP) on demand."""
        self.say("Checking, {title}.")
        result = self.notifications.check_whatsapp() or self.notifications.check_email()
        if not result:
            self.say("No new messages right now, {title}.")
            return
        self._announce_and_offer_reply(result)

    def _announce_and_offer_reply(self, result: dict) -> None:
        if result["channel"] == "whatsapp":
            self.say(f"WhatsApp message from {result['from']}: {result['text']}")
        else:
            self.say(f"Email from {result['from']}, subject: {result['subject']}.")
        self.pending_reply = {"stage": "confirm", **result}
        self.say("Can I reply, {title}?")

    def _handle_pending_reply(self, text: str) -> bool:
        """Returns True if `text` was consumed as part of the reply flow."""
        if self.pending_reply is None:
            return False
        stage = self.pending_reply.get("stage")
        lowered = text.lower()

        if stage == "confirm":
            if re.search(r"\b(yes|yeah|yep|sure|haan|ha)\b", lowered):
                self.pending_reply["stage"] = "compose"
                self.say("What should I say, {title}?")
                return True
            if re.search(r"\b(no|nah|nope|nahi|cancel)\b", lowered):
                self.pending_reply = None
                self.say("Okay, leaving it, {title}.")
                return True
            # Not a clear yes/no — drop the pending state and let the
            # normal command dispatch handle whatever they actually said,
            # instead of swallowing an unrelated command forever.
            self.pending_reply = None
            return False

        if stage == "compose":
            pending, self.pending_reply = self.pending_reply, None
            raw = (getattr(self.ears, "last_raw", "") or "").strip()
            if raw:
                text = raw  # send what was actually said/typed, with its capital letters
            if pending["channel"] == "whatsapp":
                sent = self.notifications.send_whatsapp_reply(pending["from"], text)
            else:
                sent = self.notifications.send_email_reply(
                    pending["from"], pending.get("subject", ""), text
                )
            self.say("Sent, {title}." if sent else "I couldn't send that, {title}.")
            return True

        return False

    def h_help(self, _m: re.Match) -> None:
        self.say(
            "I can open websites and apps, play and search YouTube, google things, "
            "read Wikipedia, tell the time, take notes, screenshots, check system status, "
            "control volume and screen brightness, close windows or apps, lock or shut "
            "down the machine, type for you, message people on WhatsApp, check your "
            "WhatsApp and email for new messages and reply to them if you ask 'what's "
            "the message', and understand Hindi. "
            f"Say 'go to sleep' to put me on standby — I'll keep running quietly and "
            f"you can wake me again by saying '{self.activation_phrase}'. "
            "Say 'exit', 'quit', or 'bye' to close me completely."
        )

    def h_exit(self, _m: re.Match) -> None:
        self.say(random.choice([
            "Going offline. Call me anytime, {title}.",
            "Shutting down. Take care, {title}.",
        ]))
        self.running = False
        self.browser.close()

    def h_sleep(self, _m: re.Match) -> None:
        """Go to standby instead of quitting the whole process."""
        if not self.activation_phrase:
            self.h_exit(_m)
            return
        self.asleep = True
        self.say(
            f"Going to sleep, {{title}}. Say '{self.activation_phrase}' to wake me."
        )

    def _try_wake(self, text: str) -> None:
        """Called instead of handle() while asleep — listens only for the
        activation phrase and ignores everything else."""
        if not self.activation_phrase or self.activation_phrase not in text:
            return
        self.asleep = False
        remainder = text.replace(self.activation_phrase, "", 1).strip(" ,.")
        self.say(random.choice([
            "Yes, {title}? I'm online.",
            "{name} online, {title}. Listening now.",
        ]))
        if remainder:
            self.handle(remainder)

    # ---------------- rule table ----------------
    def _build_rules(self) -> list[Rule]:
        return [
            # Notes / typing come FIRST and are anchored to the start, so the
            # text you dictate ("note close the window", "type play music")
            # is written down instead of being run as a command.
            Rule(r"^\s*(?:take |make |add )?(?:a )?note (?:that |this |down )?(?P<q>.*)", Assistant.h_note),
            Rule(r"^\s*(?:type|write)\s+(?P<q>.+)", Assistant.h_type),

            # WhatsApp send + save-contact come next so a message like
            # "message ravi saying bye" isn't mistaken for the "bye" = exit rule.
            Rule(
                r"^\s*(?:save|add)\s+(?:a\s+)?contact\s+(?P<name>.+?)\s+(?:as|number|is|:)\s*"
                r"(?P<msg>\+?[\d\s\-()]{8,})\s*$",
                Assistant.h_add_contact,
            ),
            Rule(
                r"^\s*(?:whatsapp|message)\s+(?P<name>.+?)(?:\s+(?:saying|that says|to say)\b|\s*:)\s*(?P<msg>.+)$",
                Assistant.h_whatsapp,
            ),

            # NOTE: exit / sleep only fire when that is the WHOLE command
            # ("bye", "okay goodbye", "quit", "go to sleep"). Otherwise
            # "quit chrome" or "play bye bye bye" / "play stand by me" would
            # shut the assistant down or put it to sleep.
            Rule(
                r"^\s*(?:" + re.escape(self.name.lower()) + r"\s+)?(?:ok(?:ay)?\s+|thanks?\s+|thank you\s+)?(?:good\s?bye|bye(?:\s+bye)?|exit|quit)"
                r"(?:\s+(?:now|for now|" + re.escape(self.name.lower()) + r"))?[\s.!]*$"
                r"|\b(?:shut ?down yourself|power off yourself|turn (?:yourself )?off)\b",
                Assistant.h_exit,
            ),
            Rule(
                r"^\s*(?:go offline|stop listening|sleep now|go to sleep|standby|stand by)\b",
                Assistant.h_sleep,
            ),
            Rule(r"\b(restart|reboot|shutdown|shut down) (the |my |your )?(pc|computer|system|laptop)\b", Assistant.h_power),
            Rule(r"\block (the )?(screen|pc|computer|system)\b", Assistant.h_lock),

            # Close a specific app ("close chrome", "quit notepad", "kill vs code")
            # or just the active window ("close this window", "close window").
            # Anchored to the start: "google how to kill a process" must NOT kill processes.
            Rule(r"^\s*(?:close|quit|kill)\s+(?P<target>.+)", Assistant.h_close),

            Rule(
                r"\bbrightness\s+(?P<act>up|down|increase|decrease|brighter|dimmer|darker)\b"
                r"|\b(?P<act2>increase|raise|turn up|decrease|lower|reduce|turn down)\s+(?:the\s+)?(?:screen\s+|display\s+)?brightness\b"
                r"|\b(?P<act3>brighten|dim|darken)\s+(?:the\s+)?(?:screen|display)\b",
                Assistant.h_brightness,
            ),

            Rule(r"\bopen\s+youtube\b.*?\b(?:and\s+)?search(?:\s+for)?\s+(?P<q>.+)", Assistant.h_youtube_search),
            Rule(r"\bopen\s+youtube\b.*?\b(?:and\s+)?play\s+(?P<q>.+)", Assistant.h_youtube_play),
            Rule(r"\bsearch (?:for )?(?P<q>.+?) on youtube\b|\byoutube search (?P<q2>.+)|\bsearch (?:for )?(?P<q3>.+?) (?:in|on) youtube\b", Assistant.h_youtube_search),
            # A command that STARTS with google/search is a web search, even if
            # it contains "play" ("search how to play guitar").
            Rule(r"^\s*(?:google for|google|search for|search|look up)\s+(?P<q>.+)", Assistant.h_google),
            Rule(r"\bplay (?P<q>.+?) on youtube\b|\bplay (?P<q2>.+)", Assistant.h_youtube_play),

            # Checking messages comes BEFORE the loose WhatsApp-send rule, so
            # "any new message from ravi" doesn't send "ravi" to a contact "from".
            Rule(
                r"\bwhat.?s the (message|notification|whatsapp|email|mail)\b"
                r"|\bany (new )?(message|notification|mail)s?\b"
                r"|\bcheck (my )?(whatsapp|email|mail|messages|notifications)\b",
                Assistant.h_check_notification,
            ),
            Rule(
                r"^\s*(?:whatsapp|message)\s+(?P<name>\w+)\s+(?:saying|that|:)?\s*(?P<msg>.+)"
                r"|^\s*send\s+(?:a\s+)?(?:whatsapp|text)(?:\s+message)?\s+to\s+(?P<name2>\w+)\s+(?:saying|that|:)?\s*(?P<msg2>.+)",
                Assistant.h_whatsapp,
            ),

            Rule(r"\b(what.?s the time|what is the time|what time is it|current time|tell me the time)\b", Assistant.h_time),
            Rule(r"\b(what.?s the date|what is the date|today.?s date|which day is it|tell me the date)\b", Assistant.h_date),
            Rule(r"\b(system status|cpu usage|memory usage|battery|how.?s my (pc|system|laptop))\b", Assistant.h_system_status),
            Rule(r"\b(take a |)screenshot\b|\bcapture (the )?screen\b", Assistant.h_screenshot),
            Rule(r"\b(read (my )?notes|show (my )?notes)\b", Assistant.h_read_notes),
            Rule(r"\b(?:take |make |add |)a? ?note (?:that |this |down )?(?P<q>.*)", Assistant.h_note),
            Rule(r"\b(?:google for|google|search for|search|look up)\s+(?P<q>.+)", Assistant.h_google),
            Rule(r"\bopen\s+(?P<target>.*)", Assistant.h_open),
            Rule(r"\bvolume (?P<act>up|down|increase|decrease|mute|unmute)\b|\b(?P<act2>mute|unmute)\b", Assistant.h_volume),
            Rule(r"\b(press enter|hit enter|enter key|go ahead and search)\b", Assistant.h_press_enter),
            Rule(r"\b(type|write)\s+(?P<q>.+)", Assistant.h_type),
            Rule(r"\b(help|what can you do|your commands)\b", Assistant.h_help),
            # "what is your name" / "who are you" are small talk, not Wikipedia.
            Rule(r"\b(?:wikipedia|who is|who was|what is|what are|tell me about)\s+(?!your\b|you\b|yourself\b)(?P<q>.+)", Assistant.h_wiki),
        ]

    # ---------------- dispatch ----------------
    def small_talk(self, text: str) -> bool:
        for pattern, replies in SMALL_TALK.items():
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                reply = random.choice(replies)
                if "{0}" in reply:
                    reply = reply.replace("{0}", m.group(1) if m.groups() else "day")
                self.say(reply)
                return True
        return False

    def handle(self, text: str) -> None:
        text = text.strip()
        if not text:
            return

        # Synonyms: "launch/start" -> open, "put on" -> play, "text ravi" -> message ...
        # (skipped while we're waiting for the body of a reply, so it isn't altered)
        if self.pending_reply is None:
            text = normalize_synonyms(text)

        composing = (self.pending_reply or {}).get("stage") == "compose"
        if CONFIG.get("hindi_support", True) and not composing:
            text = self._maybe_translate(text)
            if self.pending_reply is None:
                text = normalize_synonyms(text)  # also on the translated English

        # If we're mid-way through "can I reply, boss?" / "what should I
        # say?", route the text there first instead of normal commands.
        if self._handle_pending_reply(text):
            return

        if self.wake_word:
            if self.wake_word not in text:
                return
            text = text.replace(self.wake_word, "", 1).strip(" ,.")
            if not text:
                self.say(random.choice([
                    "Yes, {title}? I'm listening.",
                    "I'm here, {title}. What do you need?",
                ]))
                return

        for rule in self.rules:
            m = rule.regex.search(text)
            if m:
                groups = m.groupdict()
                merged = {}
                for base in ("q", "name", "msg", "act", "target"):
                    for suffix in ("", "2", "3"):
                        key = base + suffix
                        val = groups.get(key)
                        if val:
                            merged[base] = val
                if merged:
                    if "q" in merged and "target" not in merged:
                        merged["target"] = merged["q"]
                    rule.handler(self, _FakeMatch(merged))
                    return
                rule.handler(self, m)
                return

        if self.small_talk(text):
            return

        self.say(random.choice([
            "I didn't catch a command in that, {title}. Say 'help' to hear what I can do.",
            "Not sure how to handle that yet. Want me to google it?",
        ]))

    # ---------------- notification polling ----------------
    def _poll_notifications_loop(self) -> None:
        interval = max(10, int(CONFIG.get("notification_poll_seconds", 30)))
        while self.running:
            time.sleep(interval)
            if not self.running or self.asleep or self.pending_reply is not None:
                continue
            try:
                result = self.notifications.check_whatsapp() or self.notifications.check_email()
            except Exception as exc:
                log.error("Notification poll failed: %s", exc)
                continue
            if result:
                self._announce_and_offer_reply(result)

    def _maybe_start_polling(self) -> None:
        if not CONFIG.get("notification_polling_enabled", False):
            return
        if not (CONFIG.get("email", {}).get("enabled") or self.browser.available()):
            return
        self._poll_thread = threading.Thread(target=self._poll_notifications_loop, daemon=True)
        self._poll_thread.start()

    # ---------------- main loop ----------------
    def greet(self) -> None:
        hour = dt.datetime.now().hour
        part = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"
        if self.asleep:
            self.say(
                f"{self.name} is running in the background, {self.title}. "
                f"Say '{self.activation_phrase}' whenever you need me."
            )
        else:
            self.say(f"Hello {self.title}! Good {part}. {self.name} is online and listening.")

    def run(self) -> None:
        self.greet()
        self._maybe_start_polling()
        misses = 0
        while self.running:
            try:
                text = self.ears.listen()
                if not text:
                    misses += 1
                    if misses % 5 == 0 and not self.ears.text_mode and not self.asleep:
                        print("… still listening. Say 'help' or 'exit'.")
                    continue
                misses = 0
                if not self.ears.text_mode and self._is_echo(text):
                    log.info("Ignored own voice: %s", text)
                    continue
                self._dispatch(text)
            except KeyboardInterrupt:
                self.say("Stopping. Bye, {title}.")
                self.browser.close()
                break
            except Exception as exc:
                log.exception("Unhandled error")
                print(f"⚠️  Error: {exc}")
                self.say("Something went wrong, but I'm still here.")


class _FakeMatch:
    """Tiny stand-in so handlers can read named groups from alternate patterns."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def group(self, key):
        if key == 0:
            return " ".join(str(v) for v in self._data.values())
        return self._data.get(key, "")

    def groupdict(self) -> dict:
        return dict(self._data)


# --------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Personal desktop voice assistant")
    parser.add_argument("--text", action="store_true", help="type commands instead of speaking")
    parser.add_argument("--allow-multiple", action="store_true",
                        help="don't close other running copies (not recommended)")
    args = parser.parse_args()

    print("=" * 58)
    print(f"  {CONFIG['assistant_name']} — personal assistant  |  {OS_NAME}  |  pid {os.getpid()}")
    print(f"  config: {CONFIG_FILE}")
    print("=" * 58)

    if not args.allow_multiple and not acquire_single_instance():
        print("⚠️  Another copy of the assistant is already running and won't close.")
        print("    Close it first (Task Manager -> python.exe / pythonw.exe), then start again.")
        return

    if sr is None and not args.text:
        print("⚠️  SpeechRecognition not installed — running in text mode.")
    if webdriver is None:
        print("ℹ️  Selenium not installed — YouTube/WhatsApp will use one-shot browser tabs instead of a reused window.")
    if GoogleTranslator is None:
        print("ℹ️  deep-translator not installed — Hindi commands won't be auto-translated.")
    if sbc is None:
        print("ℹ️  screen-brightness-control not installed — brightness commands will use OS fallbacks.")

    global _ACTIVE
    _ACTIVE = Assistant(force_text=args.text)
    _ACTIVE.run()


if __name__ == "__main__":
    main()