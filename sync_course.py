"""
sync_course.py
==============
Downloads lecture transcripts from Panopto via Selenium DOM scraping.
- Single browser session, single login.
- Auto-discovers courses from the Moodle dashboard (no panopto_links.txt needed).
- Credentials stored securely via keyring; GUI dialog on first run.
- Skips already-downloaded videos using history.json.
- Saves transcripts under <save_folder> / <CourseName> / Lesson_N.txt
"""

import json
import random
import re
import sys
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path

import keyring
import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, InvalidSessionIdException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# PyInstaller-aware base directory: beside the .exe when frozen, beside the
# .py file when running from source.
if getattr(sys, 'frozen', False):
    SCRIPT_DIR = Path(sys.executable).parent
else:
    SCRIPT_DIR = Path(__file__).parent

HISTORY_FILE = SCRIPT_DIR / "history.json"

SERVICE_NAME = "IgniteTranscriptions"
LINEAR_API_URL = "https://api.linear.app/graphql"
_USERNAME: str = ""   # set once in main(), used by re-auth call-sites
_PASSWORD: str = ""
_SAVE_FOLDER: Path = Path.home() / "Transcriptions"

PANOPTO_BASE = "https://runi.cloud.panopto.eu"
MOODLE_URL = "https://moodle.runi.ac.il"

# Timeouts (seconds)
PAGE_LOAD_TIMEOUT = 60
ELEMENT_TIMEOUT = 20
TRANSCRIPT_TIMEOUT = 40
RENDERER_TIMEOUT_RETRIES = 3
RENDERER_RETRY_BASE_WAIT = 8  # seconds

# ---------------------------------------------------------------------------
# Credential GUI
# ---------------------------------------------------------------------------


def get_credentials() -> tuple[str, str, Path, str]:
    """
    Show a blocking Tkinter login window.
    Pre-fills from keyring if credentials were previously saved.
    Returns (username, password, save_folder, linear_api_key) on submit, or calls sys.exit(0) if closed.
    """
    saved_user       = keyring.get_password(SERVICE_NAME, "username")      or ""
    saved_pass       = keyring.get_password(SERVICE_NAME, "password")      or ""
    saved_folder     = keyring.get_password(SERVICE_NAME, "save_folder")   or str(Path.home() / "Transcriptions")
    saved_linear_key = keyring.get_password(SERVICE_NAME, "linear_api_key") or ""

    result: dict = {}

    root = tk.Tk()
    root.title("Ignite Transcriptions — Login")
    root.resizable(False, False)

    tk.Label(root, text="Moodle Username:").grid(row=0, column=0, padx=10, pady=8, sticky="e")
    user_var = tk.StringVar(value=saved_user)
    tk.Entry(root, textvariable=user_var, width=30).grid(row=0, column=1, columnspan=2, padx=10, pady=8)

    tk.Label(root, text="Moodle Password:").grid(row=1, column=0, padx=10, pady=8, sticky="e")
    pass_var = tk.StringVar(value=saved_pass)
    tk.Entry(root, textvariable=pass_var, show="*", width=30).grid(row=1, column=1, columnspan=2, padx=10, pady=8)

    tk.Label(root, text="Save folder:").grid(row=2, column=0, padx=10, pady=8, sticky="e")
    folder_var = tk.StringVar(value=saved_folder)
    tk.Entry(root, textvariable=folder_var, width=26).grid(row=2, column=1, padx=(10, 0), pady=8, sticky="ew")
    def browse():
        path = filedialog.askdirectory(title="Select folder to save transcripts")
        if path:
            folder_var.set(path)
    tk.Button(root, text="…", command=browse, width=3).grid(row=2, column=2, padx=(0, 10), pady=8)

    tk.Label(root, text="Linear API key:").grid(row=3, column=0, padx=10, pady=8, sticky="e")
    linear_key_var = tk.StringVar(value=saved_linear_key)
    tk.Entry(root, textvariable=linear_key_var, show="*", width=30).grid(row=3, column=1, columnspan=2, padx=10, pady=8)

    remember_var = tk.BooleanVar(value=True)
    tk.Checkbutton(root, text="Remember settings", variable=remember_var).grid(
        row=4, column=0, columnspan=3, pady=4
    )

    def on_submit():
        u, p, f = user_var.get().strip(), pass_var.get().strip(), folder_var.get().strip()
        lk = linear_key_var.get().strip()
        if not u or not p:
            messagebox.showerror("Error", "Username and password are required.")
            return
        if not f:
            messagebox.showerror("Error", "Please choose a save folder.")
            return
        if remember_var.get():
            keyring.set_password(SERVICE_NAME, "username",    u)
            keyring.set_password(SERVICE_NAME, "password",    p)
            keyring.set_password(SERVICE_NAME, "save_folder", f)
            if lk:
                keyring.set_password(SERVICE_NAME, "linear_api_key", lk)
            else:
                try:
                    keyring.delete_password(SERVICE_NAME, "linear_api_key")
                except Exception:
                    pass
        result["username"] = u
        result["password"] = p
        result["folder"]   = f
        result["linear_key"] = lk
        root.destroy()

    def on_close():
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    tk.Button(root, text="Start", command=on_submit, width=15).grid(
        row=5, column=0, columnspan=3, pady=12
    )
    root.bind("<Return>", lambda _: on_submit())

    if saved_user and saved_pass:
        status_text = f"Stored credentials for: {saved_user}"
    else:
        status_text = "No stored credentials found."
    tk.Label(root, text=status_text, fg="gray").grid(row=6, column=0, columnspan=3, pady=(0, 8))

    root.mainloop()

    if "username" not in result:
        sys.exit(0)
    return result["username"], result["password"], Path(result["folder"]), result.get("linear_key", "")


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------


def load_history() -> dict:
    """Return the history dict {video_id: filepath}."""
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_history(history: dict) -> None:
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Linear reporting
# ---------------------------------------------------------------------------

def _linear_api_key() -> str | None:
    return keyring.get_password(SERVICE_NAME, "linear_api_key") or None


def _linear_post(api_key: str, query: str, variables: dict | None = None) -> dict:
    resp = requests.post(
        LINEAR_API_URL,
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _get_linear_team_id(api_key: str) -> str | None:
    try:
        data = _linear_post(api_key, "{ teams { nodes { id } } }")
        nodes = data["data"]["teams"]["nodes"]
        return nodes[0]["id"] if nodes else None
    except Exception as exc:
        print(f"[LINEAR] Could not fetch team ID: {exc}")
        return None


_CREATE_ISSUE = """
    mutation CreateIssue($input: IssueCreateInput!) {
        issueCreate(input: $input) { success issue { id url } }
    }
"""


def report_linear_success(courses_synced: int, new_transcripts: int) -> None:
    from datetime import datetime, timezone
    api_key = _linear_api_key()
    if not api_key:
        return
    team_id = _get_linear_team_id(api_key)
    if not team_id:
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        result = _linear_post(api_key, _CREATE_ISSUE, {"input": {
            "teamId": team_id,
            "title": f"Sync completed — {new_transcripts} new transcript(s) [{ts}]",
            "description": (
                f"**Sync summary**\n\n"
                f"- Courses processed: {courses_synced}\n"
                f"- New transcripts: {new_transcripts}\n"
                f"- Completed: {ts}\n"
            ),
            "priority": 0,
        }})
        print(f"[LINEAR] Success issue: {result['data']['issueCreate']['issue']['url']}")
    except Exception as exc:
        print(f"[LINEAR] Failed to report success: {exc}")


def report_linear_error(exc: BaseException) -> None:
    from datetime import datetime, timezone
    api_key = _linear_api_key()
    if not api_key:
        return
    team_id = _get_linear_team_id(api_key)
    if not team_id:
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tb_text = traceback.format_exc()
    try:
        _linear_post(api_key, _CREATE_ISSUE, {"input": {
            "teamId": team_id,
            "title": f"Sync error — {type(exc).__name__} [{ts}]",
            "description": (
                f"**Unhandled error during sync**\n\n"
                f"- Exception: `{type(exc).__name__}: {exc}`\n"
                f"- Timestamp: {ts}\n\n"
                f"**Traceback**\n```\n{tb_text}\n```"
            ),
            "priority": 2,
        }})
        print("[LINEAR] Error issue created.")
    except Exception as post_exc:
        print(f"[LINEAR] Failed to report error: {post_exc}")


# ---------------------------------------------------------------------------
# Driver setup
# ---------------------------------------------------------------------------


def build_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--window-size=1920,1080")
    if sys.platform != "win32":
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_experimental_option("detach", False)

    service = Service()  # Selenium Manager auto-detects Chrome and downloads matching ChromeDriver
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver


# ---------------------------------------------------------------------------
# Session recovery
# ---------------------------------------------------------------------------


def _ensure_driver_alive(driver: WebDriver) -> WebDriver:
    """Return driver if session is valid; rebuild and re-login otherwise."""
    try:
        driver.title
        return driver
    except Exception:
        print("[SESSION] Browser session lost — rebuilding…")
        try:
            driver.quit()
        except Exception:
            pass
        new_driver = build_driver()
        login(new_driver, _USERNAME, _PASSWORD)
        return new_driver


# ---------------------------------------------------------------------------
# Resilient navigation
# ---------------------------------------------------------------------------


def resilient_get(driver: WebDriver, url: str, retries: int = RENDERER_TIMEOUT_RETRIES) -> None:
    """
    Wraps driver.get() with retry logic for 'Timed out receiving message from renderer'.
    On renderer timeout: navigate to about:blank (resets renderer), wait with
    randomised back-off, then retry. All other exceptions re-raise immediately.
    Does NOT reduce any timeouts or skip any waits — bot detection must not be triggered.
    """
    for attempt in range(1, retries + 1):
        try:
            driver.get(url)
            return
        except WebDriverException as exc:
            msg = str(exc).lower()
            is_renderer_timeout = (
                "timed out receiving message from renderer" in msg
                or ("timeout" in msg and "renderer" in msg)
            )
            if not is_renderer_timeout or attempt == retries:
                raise
            wait_secs = RENDERER_RETRY_BASE_WAIT * attempt + random.uniform(2, 5)
            print(
                f"  [RESILIENT_GET] Renderer timeout on attempt {attempt}/{retries}. "
                f"Clearing renderer, waiting {wait_secs:.1f}s before retry…"
            )
            try:
                driver.get("about:blank")
            except Exception:
                pass
            time.sleep(wait_secs)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def login(driver: WebDriver, username: str, password: str) -> None:
    """
    RUNI SSO login flow:
      1. Navigate to Moodle login page.
      2. Moodle redirects to my.runi.ac.il SSO IdP — wait for it.
      3. Fill credentials on the IdP form.
      4. Wait until we land back on the Moodle dashboard.
    """
    LOGIN_WAIT = 90  # total seconds to wait for login to complete

    print("[LOGIN] Navigating to Moodle to trigger SSO…")
    try:
        resilient_get(driver, MOODLE_URL)
    except TimeoutException:
        pass

    # Step 1 — wait for SSO IdP (my.runi.ac.il)
    print("[LOGIN] Waiting for SSO redirect to my.runi.ac.il…")
    try:
        WebDriverWait(driver, LOGIN_WAIT).until(
            lambda d: "my.runi.ac.il" in d.current_url
        )
        print(f"[LOGIN] On IdP: {driver.current_url}")
    except TimeoutException:
        # Maybe already on Moodle dashboard (cookie still valid)
        if "moodle.runi.ac.il" in driver.current_url and "/login" not in driver.current_url:
            print("[LOGIN] Already authenticated — skipping credential step.")
            return
        print("[LOGIN] WARNING: SSO page not reached. Will try to fill any visible form.")

    # Step 2 — fill credentials
    try:
        # Look for username field with common selectors
        user_field = None
        for sel in ["input[name='username']", "#username", "input[type='text']"]:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els and els[0].is_displayed():
                user_field = els[0]
                break

        pass_field = None
        for sel in ["input[name='password']", "#password", "input[type='password']"]:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els and els[0].is_displayed():
                pass_field = els[0]
                break

        if user_field and pass_field:
            user_field.clear()
            for ch in username:
                user_field.send_keys(ch)
                time.sleep(0.04)
            pass_field.clear()
            for ch in password:
                pass_field.send_keys(ch)
                time.sleep(0.04)

            # Submit
            submit = None
            for sel in ["input[type='submit']", "button[type='submit']", "#loginbtn"]:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                if els and els[0].is_displayed():
                    submit = els[0]
                    break
            (submit or pass_field).click() if submit else pass_field.submit()
            print(f"[LOGIN] Credentials submitted for '{username}'.")
        else:
            print("[LOGIN] WARNING: Could not find login form — logging in manually. Waiting 90 s…")
            time.sleep(90)

    except Exception as e:
        print(f"[LOGIN] Login form error: {e}")

    # Step 3 — wait for Moodle dashboard
    print("[LOGIN] Waiting for Moodle dashboard…")
    try:
        WebDriverWait(driver, LOGIN_WAIT).until(
            lambda d: "moodle.runi.ac.il" in d.current_url and "/login" not in d.current_url
        )
        print(f"[LOGIN] Moodle login confirmed: {driver.current_url}")
    except TimeoutException:
        print("[LOGIN] WARNING: Did not reach Moodle dashboard — proceeding anyway.")


def is_logged_out(driver: WebDriver) -> bool:
    """Return True when the browser has been redirected away from Panopto for auth."""
    url = driver.current_url.lower()
    if "panopto.eu" in url:
        return False
    return True


# ---------------------------------------------------------------------------
# Course & Panopto folder discovery
# ---------------------------------------------------------------------------


def discover_panopto_folders(driver: WebDriver) -> list[str]:
    """
    Scrape the Moodle dashboard for all visible courses, click each course's
    Panopto LTI link, and return the resulting Panopto folder URLs.

    The first LTI click also establishes the Panopto session (replacing the
    old _perform_lti_handshake step).
    """
    m = re.search(r"(https://moodle\.runi\.ac\.il(?:/\d{4})?)", driver.current_url)
    moodle_base = m.group(1) if m else MOODLE_URL

    print("[DISCOVER] Navigating to Moodle dashboard…")
    resilient_get(driver, f"{moodle_base}/my/")
    time.sleep(3)

    # Log (read-only) the active dashboard filter so the user can diagnose
    # if fewer courses than expected are returned.
    try:
        filter_el = driver.find_element(By.CSS_SELECTOR, "button#groupingdropdown span.sr-only")
        print(f"[DISCOVER] Active dashboard filter: '{filter_el.text.strip()}'")
    except Exception:
        print("[DISCOVER] Could not read dashboard filter.")

    # Collect all course URLs via .course-link anchors inside .card.dashboard-card
    card_links = driver.find_elements(By.CSS_SELECTOR, ".card.dashboard-card a.course-link")
    course_urls = list(dict.fromkeys(
        el.get_attribute("href") for el in card_links if el.get_attribute("href")
    ))
    print(f"[DISCOVER] Found {len(course_urls)} course(s) on dashboard.")

    folder_urls: list[str] = []
    for idx, course_url in enumerate(course_urls, 1):
        print(f"[DISCOVER] [{idx}/{len(course_urls)}] {course_url}")
        try:
            resilient_get(driver, course_url)
            time.sleep(2)

            # Find <a class="aalink stretched-link"> whose span.instancename
            # contains "panopto" (case-insensitive).
            links = driver.find_elements(By.CSS_SELECTOR, "a.aalink.stretched-link")
            panopto_link = None
            for link in links:
                try:
                    span = link.find_element(By.CSS_SELECTOR, "span.instancename")
                    if "panopto" in span.text.lower():
                        panopto_link = link
                        break
                except Exception:
                    continue

            if panopto_link is None:
                print("  -> No Panopto link found -- skipping.")
                continue

            # Navigate to the LTI view page — Panopto loads inside an iframe
            resilient_get(driver, panopto_link.get_attribute("href"))

            # Wait for the iframe to appear, then switch into it and read the
            # "Open in Panopto" link href (= the full Panopto folder URL).
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.TAG_NAME, "iframe"))
                )
            except TimeoutException:
                print("  -> LTI page did not load an iframe -- skipping.")
                continue

            folder_url = None
            for iframe in driver.find_elements(By.TAG_NAME, "iframe"):
                try:
                    driver.switch_to.frame(iframe)
                    # Wait for the Panopto SPA to finish navigating to the
                    # course folder — video links only appear once it's loaded.
                    WebDriverWait(driver, 20).until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, "a[href*='Viewer.aspx']")
                        )
                    )
                    # The iframe URL hash now contains the folderID.
                    iframe_url = driver.execute_script("return window.location.href")
                    driver.switch_to.default_content()
                    if "folderID" in iframe_url:
                        folder_url = iframe_url
                        break
                    driver.switch_to.default_content()
                except TimeoutException:
                    driver.switch_to.default_content()
                except Exception:
                    driver.switch_to.default_content()

            if not folder_url:
                print("  -> Could not find folder URL inside iframe -- skipping.")
                continue

            print(f"  -> Panopto folder: {folder_url}")
            folder_urls.append(folder_url)

        except TimeoutException:
            print("  -> Timeout -- skipping.")
        except Exception as e:
            print(f"  -> Error: {e} -- skipping.")

    print(f"[DISCOVER] {len(folder_urls)} Panopto folder(s) discovered.")
    return folder_urls


# ---------------------------------------------------------------------------
# Folder scraping — session list
# ---------------------------------------------------------------------------


def extract_folder_id(url: str) -> str | None:
    """Pull the folderID UUID out of the URL."""
    match = re.search(r'folderID=%22([0-9a-f\-]+)%22', url, re.IGNORECASE)
    if match: return match.group(1)
    match = re.search(r'folderID="?([0-9a-f\-]+)"?', url, re.IGNORECASE)
    return match.group(1) if match else None


def get_course_name(driver: WebDriver) -> str:
    """Wait for the folder title to appear in the SPA before returning the name."""
    wait = WebDriverWait(driver, ELEMENT_TIMEOUT)
    selectors = [
        (By.CSS_SELECTOR, "#contentHeaderText"),
        (By.CSS_SELECTOR, "#contentHeader"),
        (By.CSS_SELECTOR, "#detail-title"),
        (By.CSS_SELECTOR, ".folder-name"),
        (By.CSS_SELECTOR, "h1.folder-name"),
        (By.TAG_NAME, "h1"),
    ]
    for by, sel in selectors:
        try:
            el = wait.until(EC.visibility_of_element_located((by, sel)))
            name = el.text.strip()
            if name:
                return sanitize_filename(name)
        except TimeoutException:
            continue
    return "Unknown_Course"


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


def scrape_session_urls(driver: WebDriver, folder_url: str) -> list[str]:
    folder_id = extract_folder_id(folder_url)
    if not folder_id:
        print(f"  [FOLDER] WARNING: Could not extract folderID from {folder_url}. Falling back to raw URL.")
        sorted_url = folder_url
    else:
        sorted_url = (
            f"{PANOPTO_BASE}/Panopto/Pages/Sessions/List.aspx"
            f'#folderID="{folder_id}"&sortColumn=1&sortAscending=true'
        )

    print("  [FOLDER] Hard-reloading to clear SPA state…")
    driver.get("about:blank")

    print(f"  [FOLDER] Loading (sorted): {sorted_url}")
    resilient_get(driver, sorted_url)

    if is_logged_out(driver):
        login(driver, _USERNAME, _PASSWORD)
        driver.get("about:blank")
        resilient_get(driver, sorted_url)

    wait = WebDriverWait(driver, ELEMENT_TIMEOUT)
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='Viewer.aspx']")))
    except TimeoutException:
        print("  [FOLDER] No session links found.")

    _scroll_to_bottom(driver)

    links = driver.find_elements(By.CSS_SELECTOR, "a[href*='Viewer.aspx']")
    urls = []
    seen_ids = set()
    for a in links:
        href = a.get_attribute("href")
        if href and "Viewer.aspx?id=" in href:
            vid_id = extract_video_id(href)
            if vid_id and vid_id not in seen_ids:
                urls.append(href)
                seen_ids.add(vid_id)
    urls.reverse()
    return urls


def _scroll_to_bottom(driver: WebDriver) -> None:
    last_height = driver.execute_script("return document.body.scrollHeight")
    for _ in range(5):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height: break
        last_height = new_height


# ---------------------------------------------------------------------------
# Video ID extraction
# ---------------------------------------------------------------------------


def extract_video_id(url: str) -> str | None:
    match = re.search(r'Viewer\.aspx\?id=([0-9a-f\-]+)', url, re.IGNORECASE)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Transcript DOM scraping
# ---------------------------------------------------------------------------


def open_transcript_panel(driver: WebDriver) -> bool:
    """Find and click the Captions/Transcript tab in the side panel."""
    wait = WebDriverWait(driver, ELEMENT_TIMEOUT)
    try:
        tabs = wait.until(EC.presence_of_all_elements_located(
            (By.CSS_SELECTOR, "#eventTabControl .event-tab-header")
        ))
        for tab in tabs:
            tab_text = tab.text.lower()
            if "caption" in tab_text or "תמלול" in tab_text:
                print("    [TRANSCRIPT] Switching to Captions tab...")
                driver.execute_script("arguments[0].click();", tab)
                time.sleep(2)
                return True
        return False
    except Exception as e:
        print(f"    [WARN] Could not find or click Captions tab: {e}")
        return False


def scrape_transcript(driver: WebDriver) -> str | None:
    wait = WebDriverWait(driver, TRANSCRIPT_TIMEOUT)
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".event-text span")))
        elements = driver.find_elements(By.CSS_SELECTOR, ".event-text span")
        lines = [el.text.strip() for el in elements if el.text.strip()]
        return "\n".join(lines) if lines else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# File saving
# ---------------------------------------------------------------------------


def get_next_lesson_path(course_dir: Path) -> Path:
    course_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while (course_dir / f"Lesson_{n}.txt").exists(): n += 1
    return course_dir / f"Lesson_{n}.txt"


def save_transcript(transcript: str, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(transcript)
    print(f"    [SAVE] Saved -> {path}")


# ---------------------------------------------------------------------------
# Per-video processing
# ---------------------------------------------------------------------------


def process_video(driver: WebDriver, video_url: str, course_dir: Path, history: dict) -> bool:
    video_id = extract_video_id(video_url)
    if not video_id or video_id in history: return False

    resilient_get(driver, video_url)
    if is_logged_out(driver):
        login(driver, _USERNAME, _PASSWORD)
        resilient_get(driver, video_url)

    time.sleep(3)
    open_transcript_panel(driver)
    transcript = scrape_transcript(driver)

    if transcript:
        save_transcript(transcript, get_next_lesson_path(course_dir))
        history[video_id] = {"status": "done"}
        save_history(history)
        return True
    return False


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def main() -> None:
    global _USERNAME, _PASSWORD, _SAVE_FOLDER
    _USERNAME, _PASSWORD, _SAVE_FOLDER, _ = get_credentials()  # _ = linear key (stored to keyring)

    history = load_history()
    driver = build_driver()
    courses_synced = 0
    new_transcripts = 0
    try:
        login(driver, _USERNAME, _PASSWORD)
        folder_urls = discover_panopto_folders(driver)

        if not folder_urls:
            print("[MAIN] No Panopto folders found — nothing to do.")
            report_linear_success(courses_synced=0, new_transcripts=0)
            return

        print(f"[MAIN] Processing {len(folder_urls)} folder(s).")
        for folder_url in folder_urls:
            print(f"\n{'='*60}")
            driver = _ensure_driver_alive(driver)
            try:
                session_urls = scrape_session_urls(driver, folder_url)
            except InvalidSessionIdException:
                driver = _ensure_driver_alive(driver)
                session_urls = scrape_session_urls(driver, folder_url)
            course_name = get_course_name(driver)
            print(f"[FOLDER] Course: '{course_name}' — {len(session_urls)} session(s)")
            course_dir = _SAVE_FOLDER / course_name
            for url in session_urls:
                driver = _ensure_driver_alive(driver)
                try:
                    if process_video(driver, url, course_dir, history):
                        new_transcripts += 1
                except InvalidSessionIdException:
                    driver = _ensure_driver_alive(driver)
                    if process_video(driver, url, course_dir, history):
                        new_transcripts += 1
            courses_synced += 1

        report_linear_success(courses_synced=courses_synced, new_transcripts=new_transcripts)

    except Exception as exc:
        report_linear_error(exc)
        raise
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
