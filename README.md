# LinkedIn Easy Apply Autonomous Agent 🤖💼

An intelligent, autonomous LinkedIn Easy Apply agent built with **Playwright**, anti-detection stealth, and **Ollama/Groq AI** for answering screening questions.

It automatically finds matching job postings for target roles, screens them against your suitability rules (filtering out senior roles and blacklisted agencies), navigates multi-step Easy Apply modal forms, uploads your resume, and accurately completes all questions.

---

## Key Features

- **Anti-Bot & Persistent Profile**: Uses Playwright with a persistent Chromium context (`.browser_profile`). You log in manually once; all future runs reuse your authenticated session without triggering 2FA, OTP, or CAPTCHA challenges.
- **Stealth Evasion**: Patches `navigator.webdriver`, utilizes realistic user-agents, and mimics human typing and scrolling behaviors with randomized jitter.
- **Intelligent Screening Resolution**:
  - **Rule-based & Regex matcher**: Instantly answers questions regarding work authorization, visa sponsorship, years of experience per skill, notice period, and CTC.
  - **AI Fallback (Ollama / Groq)**: For novel or open-ended company screening questions, it uses local Ollama (`llama3.2`) or Groq to generate truthful, concise answers matching candidate profile details.
- **Dry-Run Safety Mode (Enabled by Default)**: Fills out every form step completely up to the final "Submit" button, saves a verification screenshot in `results/dry_runs/`, and safely discards the application without submitting. You can inspect the results before enabling live submissions!
- **SQLite Application Tracker**: Tracks processed job IDs, application statuses, screening logs, and enforces daily application limits to protect your LinkedIn account from shadowbans.

---

## Directory Structure

```text
linkedin_easy_applier/
├── .browser_profile/       # Stored LinkedIn session cookies & tokens (gitignored)
├── results/
│   └── dry_runs/           # Verification screenshots taken during dry runs
├── applications.db         # SQLite database tracking application history
├── config.yaml             # Candidate profile, skills experience, & search criteria
├── resume.pdf              # PDF resume automatically attached to applications
├── requirements.txt        # Python package dependencies
├── models.py               # Pydantic data schemas
├── browser.py              # Playwright browser manager with stealth & persistence
├── filter.py               # Job suitability & senior role exclusion engine
├── form_filler.py          # Multi-step Easy Apply modal form filler
├── llm_helper.py           # Ollama / Groq screening question resolver
├── search.py               # LinkedIn search coordinator & job card iterator
└── main.py                 # CLI interface
```

---

## Quick Start Guide

### 1. Install Dependencies

From the `AI Job Searcher/V1` directory (using your Python virtual environment):

```powershell
# Activate venv if not already active
.\venv\Scripts\Activate.ps1

# Navigate to the project directory
cd linkedin_easy_applier

# Install requirements
pip install -r requirements.txt

# Install Playwright browser binaries
playwright install chromium
```

### 2. Configure Your Profile & Resume

1. **Resume**: Replace `resume.pdf` with your real PDF resume in the `linkedin_easy_applier/` folder.
2. **Config (`config.yaml`)**: Open `config.yaml` and verify:
   - Your contact details (phone, email, city).
   - Skill experience mapping (`python: 2`, `genai: 2`, `langgraph: 1.5`, etc.).
   - Screening answers (notice period, current CTC, expected CTC, work authorization).

### 3. One-Time LinkedIn Login

Run the login command to authenticate once:

```powershell
python main.py login
```

- A visible Chromium browser window will open to `linkedin.com/login`.
- Log in manually with your LinkedIn credentials (and complete any 2FA or verification prompt).
- Return to your terminal and press **[ENTER]**.
- Your authenticated session will be saved in `.browser_profile/` and will automatically persist for all future runs!

### 4. Run in Dry-Run Mode (Test First!)

Test the agent on your target role without submitting anything:

```powershell
# Run with default settings (Dry Run enabled by default)
python main.py run

# Or override role and location via CLI:
python main.py run --role "AI Engineer" --location "Bengaluru" --limit 5
```

The browser will open, search for matching Easy Apply jobs, inspect each job, fill out all form inputs, and capture a screenshot at the final submit step in `results/dry_runs/` before discarding the modal safely.

### 5. Run Live Application Submission

Once you are satisfied with the dry-run results, run live submissions:

```powershell
python main.py run --live --limit 10
```

### 6. View Application Stats & History

Check how many applications have been processed, dry-run passed, or skipped:

```powershell
python main.py stats
```

---

## CLI Reference

| Command | Description | Example |
| :--- | :--- | :--- |
| `python main.py login` | Open browser to log in and save session | `python main.py login` |
| `python main.py run` | Run search & apply (Dry-Run by default) | `python main.py run` |
| `python main.py run --live` | Submit applications for real | `python main.py run --live --limit 15` |
| `python main.py run --role "..."` | Target a specific job title | `python main.py run --role "Python Developer"` |
| `python main.py run --location "..."` | Target a specific city/country | `python main.py run --location "Remote"` |
| `python main.py run --headless` | Run browser in background | `python main.py run --headless` |
| `python main.py stats` | View SQLite database statistics | `python main.py stats` |
| `python main.py test-ai` | Test Ollama/Groq question answering | `python main.py test-ai --question "Notice period?"` |

---

## LinkedIn Safety Guidelines & Best Practices

1. **Batching & Hourly Limits**: Running **~10 applications per hour/session** is realistic and mirrors manual human application speeds. The default is set to `max_applications_per_run: 15`.
2. **Platform Daily Ceiling**: LinkedIn enforces an internal platform limit (typically **30–50 Easy Applies per 24 hours** on free accounts). Running 2 to 3 sessions of 10 applications per day is an optimal, safe balance.
3. **Action Delays**: The agent includes human-like pacing intervals (2–5s between inputs and 12–15s between jobs).
4. **Session Persistence**: Always rely on `.browser_profile` rather than repeatedly entering login credentials.
