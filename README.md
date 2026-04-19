# Ignite Transcriptions — Panopto Transcript Downloader

Automatically downloads lecture transcripts (captions) from Panopto via Selenium DOM scraping.
Uses a **single browser session** with a single Moodle/SSO login. Skips already-downloaded videos.

## Prerequisites

- Python 3.11+
- [Poetry](https://python-poetry.org/docs/#installation)
- Google Chrome installed

---

## Setup

### 1. Clone / open the project

```bash
cd path/to/Ignite-Transcriptions
```

### 2. Configure credentials

Create a `.env` file (copy from `.env.example` – **never commit this file**):

```env
RUNI_USERNAME=your.username
RUNI_PASSWORD=YourPassword
```

### 3. Install dependencies

```bash
poetry install
```

This creates an isolated `.venv/` and installs:
- `selenium` — browser automation
- `webdriver-manager` — auto-downloads the matching ChromeDriver
- `python-dotenv` — loads `.env`

### 4. Add folder URLs

Edit `panopto_links.txt` — one Panopto folder URL per line, blank lines are ignored:

```
https://runi.cloud.panopto.eu/Panopto/Pages/Sessions/List.aspx?...#folderID=%22...%22
https://runi.cloud.panopto.eu/Panopto/Pages/Sessions/List.aspx?...#folderID=%22...%22
```

> **Note:** The script does **not** auto-discover your enrolled courses. You must manually add each course's Panopto folder URL here. To find the URL, open the course's Panopto folder in your browser and copy the full URL from the address bar.

---

## Running

```bash
poetry run python sync_course.py
```

### What happens

1. Chrome opens and logs in to Panopto via Moodle SSO (**once**).
2. For each folder URL in `panopto_links.txt`:
   - The course name is extracted from the page header.
   - All video session URLs are scraped.
   - Each new video's transcript panel is opened and its text is extracted.
   - The transcript is saved to:
     ```
     <output_dir>\<CourseName>\Lesson_N.txt
     ```
     (`output_dir` is configured near the top of `sync_course.py`)
3. `history.json` is updated after each video so the script can resume safely.

---

## Output structure

```
<output_dir>\
├── Course Name A\
│   ├── Lesson_1.txt
│   ├── Lesson_2.txt
│   └── ...
├── Course Name B\
│   ├── Lesson_1.txt
│   └── ...
└── ...
```

---

## Resuming / Re-running

Re-run the same command at any time. Videos already listed in `history.json` are skipped automatically.

To force a re-download, delete the relevant entry from `history.json` or delete the file entirely.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Login redirects in a loop | Check `.env` credentials |
| No transcript found for videos | The captions tab selector may need updating — inspect the page and update `open_transcript_panel()` in `sync_course.py` |
| ChromeDriver version mismatch | `webdriver-manager` handles this automatically; ensure Chrome is up to date |
| Hebrew path errors | All paths use `pathlib.Path` with `utf-8` encoding — should work on Windows 10/11 |

---

## Security

- `.env`, `history.json`, and saved cookies are excluded from git via `.gitignore`.
- Transcripts are saved outside the repository directory and are also gitignored.
