# Ignite Transcriptions — Panopto Transcript Downloader

Automatically downloads lecture transcripts from all your Panopto courses via Selenium.
Logs into Moodle, scans your dashboard, and saves every transcript it hasn't downloaded yet.

---

## For Students — Just Want to Use It?

### What you need before starting

- **Google Chrome** installed on your computer — [download here](https://www.google.com/chrome/)
- That's it. No Python, no setup.

### Step 1 — Download the program

Download **`Ignite-Transcriptions.exe`** from the shared Drive link and save it anywhere (Desktop is fine).

### Step 2 — Run it

Double-click `Ignite-Transcriptions.exe`.

> **Windows SmartScreen warning?** Click **"More info"** then **"Run anyway"**. This appears because the file isn't commercially signed.

### Step 3 — Enter your credentials

A small login window appears.

- **Moodle Username** — the username you use at `moodle.runi.ac.il`
- **Moodle Password** — your Moodle password
- Leave **"Remember credentials"** ticked

Click **Start** (or press Enter).

### Step 4 — Wait

A Chrome window opens and the program runs on its own:

1. Logs into Moodle
2. Scans your dashboard for all current courses
3. Enters each course and finds its Panopto folder
4. Downloads every transcript it hasn't already saved

When Chrome closes, everything is done.

### Step 5 — Find your transcripts

Transcripts are saved to your OneDrive desktop folder under `לימודים`:

```
OneDrive\שולחן העבודה\לימודים\
├── Course Name A\
│   ├── Lesson_1.txt
│   ├── Lesson_2.txt
│   └── ...
├── Course Name B\
│   └── Lesson_1.txt
└── ...
```

### Running again later

Just double-click the `.exe` again. Your credentials are pre-filled — press Enter to start.
The program skips anything already downloaded and only fetches new lectures.

### Troubleshooting

| Problem | Fix |
|---|---|
| SmartScreen blocks the file | Click "More info" → "Run anyway" |
| Chrome doesn't open | Install Google Chrome |
| Login keeps failing | Re-open the program, clear the fields, and re-enter your credentials |
| A course is missing from the results | Open Moodle in your browser and check that the course is visible on your dashboard |
| Want to re-download everything | Delete `history.json` (it appears next to the `.exe` after the first run) |

---

## For Developers — Running from Source

### Prerequisites

- Python 3.11+
- [Poetry](https://python-poetry.org/docs/#installation)
- Google Chrome

### Setup

```bash
# 1. Install dependencies
cd path/to/Ignite-Transcriptions
poetry install

# 2. Run
poetry run python sync_course.py
```

A login window will appear on first run. Credentials are stored in the Windows Credential Manager via `keyring` — no `.env` file needed.

### Building the exe

```bash
poetry add --group dev pyinstaller

pyinstaller --onefile --windowed \
  --hidden-import=keyring.backends.Windows \
  --hidden-import=keyring.backends._null \
  --name "Ignite-Transcriptions" \
  sync_course.py
```

Output: `dist\Ignite-Transcriptions.exe`

### Project structure

| File | Purpose |
|---|---|
| `sync_course.py` | Main script |
| `history.json` | Tracks downloaded videos (auto-created, gitignored) |
| `pyproject.toml` | Dependencies (Poetry) |
