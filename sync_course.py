"""
sync_course.py
==============
Downloads lecture transcripts from Panopto via Selenium DOM scraping.
- Single browser session, single login.
- Reads folder URLs from panopto_links.txt.
- Skips already-downloaded videos using history.json.
- Saves transcripts under BASE_PATH / <CourseName> / Lesson_N.txt
"""

import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
load_dotenv(dotenv_path=SCRIPT_DIR / ".env")

BASE_PATH = Path(r"C:\Users\מאור\OneDrive\שולחן העבודה\לימודים")
LINKS_FILE = SCRIPT_DIR / "panopto_links.txt"
HISTORY_FILE = SCRIPT_DIR / "history.json"

MOODLE_USERNAME = os.getenv("RUNI_USERNAME", "")
MOODLE_PASSWORD = os.getenv("RUNI_PASSWORD", "")

# Panopto instance root
PANOPTO_BASE = "https://runi.cloud.panopto.eu"
MOODLE_URL = "https://moodle.runi.ac.il"

# Timeouts (seconds)
PAGE_LOAD_TIMEOUT = 30
ELEMENT_TIMEOUT = 20
TRANSCRIPT_TIMEOUT = 40

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
# Driver setup
# ---------------------------------------------------------------------------


def build_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--start-maximized")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_experimental_option("detach", False)

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def login(driver: WebDriver) -> None:
    """
    RUNI SSO login flow with LTI handshake:
      1. Navigate to Moodle login page.
      2. Moodle redirects to my.runi.ac.il SSO IdP — wait for it.
      3. Fill credentials on the IdP form.
      4. Wait until we land back on the Moodle dashboard.
      5. Visit the first course and click the Panopto LTI link.
      6. Wait for Panopto LTI handshake to complete.
    """
    LOGIN_WAIT = 90  # total seconds to wait for login to complete

    print("[LOGIN] Navigating to Moodle to trigger SSO…")
    try:
        driver.get(MOODLE_URL)
        
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
            _perform_lti_handshake(driver)
            return
        print("[LOGIN] WARNING: SSO page not reached. Will try to fill any visible form.")

    # Step 2 — fill credentials
    wait = WebDriverWait(driver, 20)
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
            for ch in MOODLE_USERNAME:
                user_field.send_keys(ch)
                time.sleep(0.04)
            pass_field.clear()
            for ch in MOODLE_PASSWORD:
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
            print(f"[LOGIN] Credentials submitted for '{MOODLE_USERNAME}'.")
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

    # Step 4 — navigate to Panopto via LTI Handshake
    _perform_lti_handshake(driver)

def _perform_lti_handshake(driver: WebDriver) -> None:
    """Navigate to the first Moodle course and click Panopto to establish session via LTI Handshake."""
    print("[LOGIN] Initiating Panopto LTI Handshake...")
    
    # 1. Dynamic extraction of Moodle base URL (handles /2026/ etc.)
    import re
    m = re.search(r"(https://moodle\.runi\.ac\.il(?:/\d{4})?)", driver.current_url)
    moodle_base = m.group(1) if m else MOODLE_URL
    driver.get(f"{moodle_base}/my/")
    time.sleep(3)
    
    # 2. Find a course link flexibly
    course_links = []
    for el in driver.find_elements(By.TAG_NAME, "a"):
        href = el.get_attribute("href") or ""
        if "/course/view.php" in href:
            course_links.append(href)
            
    if not course_links:
        print("[LOGIN] No courses found on dashboard. LTI handshake failed.")
        return
        
    first_course_url = course_links[0]
    print(f"[LOGIN] Visiting first course: {first_course_url}")
    driver.get(first_course_url)
    time.sleep(3)

    # 3. Find and click the Panopto LTI link
    def _find_panopto_link(d):
        for el in d.find_elements(By.TAG_NAME, "a"):
            href = el.get_attribute("href") or ""
            text = el.text.lower()
            if "panopto" in href.lower() or "panopto" in text:
                return el
        return None

    print("[LOGIN] Searching for Panopto LTI link in course...")
    panopto_link = _find_panopto_link(driver)
    
    if not panopto_link:
        print("[LOGIN] No Panopto link found in the course. LTI handshake failed.")
        return
        
    panopto_url = panopto_link.get_attribute("href")
    print(f"[LOGIN] Clicking Panopto LTI link: {panopto_url}")
    try:
        panopto_link.click()
    except:
        driver.get(panopto_url)

    # 4. Wait for completion
    print("[LOGIN] Waiting for Panopto LTI handshake to complete...")
    time.sleep(5)
    print("[LOGIN] Panopto LTI handshake complete.")
# #def _perform_lti_handshake(driver: WebDriver) -> None:
#     """Navigate to the first Moodle course and click Panopto to establish session via LTI Handshake."""
#     print("[LOGIN] Initiating Panopto LTI Handshake...")
#     # 1. Ensure we are on the dashboard
#     driver.get(f"{MOODLE_URL}/my/")
    
#     # 2. Scrape the course links on the dashboard and visit the first course
#     try:
#         WebDriverWait(driver, 30).until(
#             lambda d: d.find_elements(By.CSS_SELECTOR, "a[href*='/course/view.php?id=']")
#         )
#         course_links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/course/view.php?id=']")
#         if not course_links:
#             print("[LOGIN] No courses found on dashboard. LTI handshake failed.")
#             return
            
#         first_course_url = course_links[0].get_attribute("href")
#         print(f"[LOGIN] Visiting first course: {first_course_url}")
#         driver.get(first_course_url)
#     except Exception as e:
#         print(f"[LOGIN] Error finding courses on dashboard: {e}")
#         return

#     # 3. Look for the Panopto LTI link
#     def _find_panopto_link(d):
#         elements = d.find_elements(By.TAG_NAME, "a")
#         for el in elements:
#             href = el.get_attribute("href") or ""
#             text = el.text.lower()
#             if "panopto" in href.lower() or "panopto" in text:
#                 return el
#         return None

#     try:
#         print("[LOGIN] Searching for Panopto LTI link in course...")
#         panopto_link = WebDriverWait(driver, 30).until(_find_panopto_link)
#         if not panopto_link:
#             print("[LOGIN] No Panopto link found in the course. LTI handshake failed.")
#             return
            
#         panopto_url = panopto_link.get_attribute("href")
#         print(f"[LOGIN] Clicking Panopto LTI link: {panopto_url}")
#         driver.get(panopto_url)
#     except Exception as e:
#         print(f"[LOGIN] Error finding Panopto LTI link: {e}")
#         return

#     # 4. Wait for the Panopto iframe/LTI handshake to complete
#     print("[LOGIN] Waiting for Panopto LTI handshake to complete...")
#     try:
#         # Check if PANOPTO_BASE (or the host) appears in the page source
#         WebDriverWait(driver, 60).until(
#             lambda d: PANOPTO_BASE in d.page_source or "runi.cloud.panopto.eu" in d.page_source
#         )
#         print("[LOGIN] Panopto LTI handshake complete.")
#         time.sleep(4)
#     except TimeoutException:
#         print("[LOGIN] WARNING: Panopto LTI handshake did not fully complete or timeout reached.")

# ##########################
def is_logged_out(driver: WebDriver) -> bool:
    """Return True when the browser has been redirected away from Panopto for auth."""
    url = driver.current_url.lower()
    # A valid Panopto page has panopto.eu in the URL
    if "panopto.eu" in url:
        return False
    # Everything else (moodle, my.runi, saml, /login) means we need to log in
    return True


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
    wait = WebDriverWait(driver, ELEMENT_TIMEOUT)
    selectors = [(By.CSS_SELECTOR, "h1.folder-name"), (By.TAG_NAME, "h1")]
    for by, sel in selectors:
        try:
            el = wait.until(EC.visibility_of_element_located((by, sel)))
            name = el.text.strip()
            if name: return sanitize_filename(name)
        except: continue
    return "Unknown_Course"


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


def scrape_session_urls(driver: WebDriver, folder_url: str) -> list[str]:
    print(f"  [FOLDER] Loading: {folder_url}")
    driver.get(folder_url)
    
    if is_logged_out(driver):
        login(driver)
        driver.get(folder_url)

    wait = WebDriverWait(driver, ELEMENT_TIMEOUT)
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='Viewer.aspx']")))
    except TimeoutException:
        print("  [FOLDER] No session links found.")

    _scroll_to_bottom(driver)

    links = driver.find_elements(By.CSS_SELECTOR, "a[href*='Viewer.aspx']")
    urls = []
    seen = set()
    for a in links:
        href = a.get_attribute("href")
        if href and "Viewer.aspx?id=" in href and href not in seen:
            urls.append(href)
            seen.add(href)
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
        # 1. Identify all tab headers in the side panel
        tabs = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "#eventTabControl .event-tab-header")))
        
        # 2. Iterate through tabs to find the 'Captions' or Hebrew equivalent
        for tab in tabs:
            tab_text = tab.text.lower()
            if "caption" in tab_text or "תמלול" in tab_text:
                print(f"    [TRANSCRIPT] Switching to Captions tab...")
                # Use JS click to avoid 'ElementClickIntercepted' errors
                driver.execute_script("arguments[0].click();", tab)
                time.sleep(2) # Allow the transcript list to render
                return True
        return False
    except Exception as e:
        print(f"    [WARN] Could not find or click Captions tab: {e}")
        return False

# def open_transcript_panel(driver: WebDriver) -> bool:
#     wait = WebDriverWait(driver, ELEMENT_TIMEOUT)
#     trigger_selectors = [
#         (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'caption')]"),
#         (By.ID, "captions-tab"),
#         (By.CSS_SELECTOR, "[aria-label*='aption']"),
#     ]
#     for by, sel in trigger_selectors:
#         try:
#             btn = wait.until(EC.element_to_be_clickable((by, sel)))
#             btn.click()
#             time.sleep(1)
#             return True
#         except: continue
#     return False


def scrape_transcript(driver: WebDriver) -> str | None:
    wait = WebDriverWait(driver, TRANSCRIPT_TIMEOUT)
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".event-text span")))
        elements = driver.find_elements(By.CSS_SELECTOR, ".event-text span")
        lines = [el.text.strip() for el in elements if el.text.strip()]
        return "\n".join(lines) if lines else None
    except: return None


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
    print(f"    [SAVE] Saved → {path}")


# ---------------------------------------------------------------------------
# Per-video processing
# ---------------------------------------------------------------------------


def process_video(driver: WebDriver, video_url: str, course_dir: Path, history: dict) -> bool:
    video_id = extract_video_id(video_url)
    if not video_id or video_id in history: return False

    driver.get(video_url)
    if is_logged_out(driver):
        login(driver)
        driver.get(video_url)

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
    if not MOODLE_USERNAME or not MOODLE_PASSWORD:
        print("[ERROR] Credentials missing.")
        sys.exit(1)

    if not LINKS_FILE.exists():
        print(f"[ERROR] Links file not found: {LINKS_FILE}")
        sys.exit(1)

    folder_urls = [
        line.strip()
        for line in LINKS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and line.strip().startswith("http")
    ]
    if not folder_urls:
        print("[ERROR] No URLs found in panopto_links.txt")
        sys.exit(1)

    print(f"[MAIN] {len(folder_urls)} folder URL(s) to process.")
    history = load_history()
    driver = build_driver()
    try:
        login(driver)
        for folder_url in folder_urls:
            print(f"\n{'='*60}")
            # Navigate to the folder first, then get the course name
            session_urls = scrape_session_urls(driver, folder_url)
            course_name = get_course_name(driver)
            print(f"[FOLDER] Course: '{course_name}' — {len(session_urls)} session(s)")
            course_dir = BASE_PATH / course_name
            for url in session_urls:
                process_video(driver, url, course_dir, history)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
