# batchAutoFiller

Fill job applications on any site from a list of links. It drives your own Chrome, fills every field it can from a dictionary of your details, uses a free AI model only for written answers ("Why do you want to join?"), and **never submits**: you review each form and click Submit yourself.

Tested on Greenhouse, Lever, Ashby, company career pages (e.g. Stripe) and the first steps of Workday.

## How it works

1. Opens each link in a dedicated Chrome profile (your logins persist there), clicking **Apply** / **Apply Manually** if needed.
2. Reads every field on the page: text boxes, dropdowns (native and custom), radio buttons, Yes/No toggles, checkboxes, file uploads, including inside iframes and shadow DOM.
3. Answers each field, in this order:
   - **`dictionary.json`**: regex rules matched against the question, e.g. `"e ?mail"` → your email. A list answer means "acceptable alternatives", so `["Bachelor's Degree", "B.Tech"]` fits whichever option the form offers.
   - **Skill questions** like "Do you have experience with Docker?" → Yes if the skill is in your resume, else No.
   - **AI (OpenRouter free models)** only for free-text questions. It gets your resume text (with email, phone and profile links removed), the job description and the questions. Your dictionary is never sent.
   - **You**: anything still unknown is asked once in the terminal and saved to `dictionary.json` for next time.
4. Clicks **Next / Continue** through multi-step forms and stops before the final submit.
5. Logs every link in `applications.csv`; submitted or skipped links are not opened again.

## Setup

Requires Python 3.10+, Google Chrome and `pdftotext` (`sudo apt install poppler-utils`).

```bash
pip install playwright
python3 -m playwright install chromium     # only needed for test_autofill.py
cp .env.example .env                        # add your free key from https://openrouter.ai
cp config.example.json config.json          # set "resume" to your resume file name
cp dictionary.example.json dictionary.json  # replace placeholders with your details
```

Put your resume PDF in this folder. In `dictionary.json`, entries with `"a": null` are asked in the terminal the first time they come up; fill them now to skip that.

## Usage

```bash
python3 autofill.py                 # every link in links.txt (one URL per line)
python3 autofill.py URL [URL ...]   # just these links
python3 autofill.py --list-free     # free OpenRouter models available right now
python3 test_autofill.py            # self-check on a local two-step form
```

For each link, the terminal shows what was filled and anything left empty. Then:

| Key | Meaning |
|---|---|
| Enter | you submitted it |
| `r` | fill again (e.g. you moved to the next page yourself) |
| `a` | needs attention, keep for later |
| `s` | skip |

If a page needs login, account creation or a CAPTCHA, it pauses until you finish and press Enter.

## Files

| File | Purpose |
|---|---|
| `autofill.py` | main script: browser, matching, filling, tracking |
| `page.js` | field extractor that runs inside the page |
| `ai.py` | OpenRouter client; tries the models in `config.json` in order |
| `system_prompt.md` | instructions for the AI's written answers; edit to change tone |
| `dictionary.example.json` | template for your answers |
| `links.txt` | job links to process |

`dictionary.json`, `config.json`, `.env`, your resume and `applications.csv` are git-ignored.

## Notes

- Free models come and go and get rate-limited; keep 3–4 from different providers in `config.json`. `--list-free` shows current ones.
- Location boxes take the site's top suggestion for your city; check them before submitting.
- Bot-check questions ("prove you're not a bot") are left for you on purpose.
- Automating LinkedIn Easy Apply breaks LinkedIn's terms; use company and job-board application pages.
