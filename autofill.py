"""Job application autofiller: fills forms on any site in your own Chrome. Never submits.

Answers come from dictionary.json (built from your resume). The AI (OpenRouter free models) only
writes long free-text answers ("Why do you want to join?", "Describe a project"). Anything the
dictionary can't answer is asked once in the terminal and saved back into dictionary.json.

Setup (once):
  1. cp .env.example .env and put your OpenRouter key in it (free key from openrouter.ai).
  2. cp dictionary.example.json dictionary.json, replace the placeholders with your details,
     and fill the "a": null entries you already know (or answer them when asked).
  3. Put your resume next to this file and set its filename as "resume" in config.json.
  4. First run opens a dedicated Chrome profile (~/.autofill-chrome); log in to job sites there once.

Run:
  python3 autofill.py                 # every link in links.txt not already submitted/skipped
  python3 autofill.py URL [URL ...]   # just these links
  python3 autofill.py --list-free     # free OpenRouter models available right now
"""
import argparse
import csv
import datetime
import difflib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import ai

HERE = Path(__file__).parent
JS = (HERE / "page.js").read_text()
CDP_PORT = 9222
CHROME_PROFILE = Path.home() / ".autofill-chrome"
TRACKER = HERE / "applications.csv"
DICTIONARY = HERE / "dictionary.json"
MAX_STEPS = 8          # Next-button clicks per application
JOB_TEXT_CHARS = 6000  # job description sent to the model
OPTION = "[role=option]:visible, [id*='-option-']:visible, [class*=suggestion]:visible, .dropdown-location:visible"

# Questions that need written prose: the only ones sent to the AI.
FREE_TEXT = re.compile(r"\bwhy\b|describe|tell us|explain|elaborate|cover letter|\bprojects?\b|motivat|about (yourself|you)\b"
                       r"|accomplish|anything else|additional (information|comments)|\bshare\b|what (makes|excites|interests|draws)")

# "Do you have experience with Docker?" style yes/no questions: answered from the resume's words.
SKILL_Q = re.compile(r"\b(experience|familiar|familiarity|proficient|proficiency|worked|knowledge|comfortable)\b(?: \w+){0,3}? (with|in|using|of|on)\b(.*)")
SKILL_STOP = set("""and or the a an any of such as etc like e g building build developing development working work production
environment environments system systems software application applications tool tools technology technologies framework frameworks
language languages professional hands on in with using project projects modern cloud based large scale writing code service services
platform platforms experience following these this other related tech stack you your have do""".split())

# Appended to system_prompt.md: the reply format the parser below depends on.
FORMAT_RULES = """
Reply with only a JSON object, no other text:
{"answers": [{"id": "<question id>", "value": "<your answer>"}]}
One entry per question id. Use "SKIP" as the value only if the question cannot be answered from the resume."""


# ---------- matching helpers ----------

def norm(s):
    return re.sub(r"[^a-z0-9+]+", " ", str(s).lower()).strip()


def pick(options, value, fuzzy=True):
    """Best option for value: exact, then whole-word prefix ("Yes" ~ "Yes, I am authorized"),
    then all words contained ("decline" ~ "I decline to self-identify"), then fuzzy."""
    v, names = norm(value), [norm(o) for o in options]
    if not v:
        return None
    if v in names:
        return options[names.index(v)]
    for o, n in zip(options, names):
        if n and ((n + " ").startswith(v + " ") or (v + " ").startswith(n + " ")):
            return o
    for o, n in zip(options, names):
        if set(v.split()) <= set(n.split()):
            return o
    close = difflib.get_close_matches(v, names, 1, 0.75) if fuzzy else []
    return options[names.index(close[0])] if close else None


def truthy(v):
    return v is True or str(v).strip().lower() in ("true", "yes", "y", "1", "checked")


# ---------- dictionary ----------

def lookup(f, entries):
    """Dictionary entries whose regex matches the field's question, specific to general."""
    text = norm(f["question"]) or norm(" ".join((f["name"], f["placeholder"], f["autocomplete"])))
    return [e for e in entries if re.search(e["q"], text)] if text else []


def resolve(f, answer):
    """Turn a dictionary answer into a value for this field. None = can't answer, "" = leave empty on purpose.
    A list answer means acceptable alternatives, best first (a checkbox_group ticks every one that exists)."""
    if answer is None or answer == "":
        return answer
    if isinstance(answer, bool) and f["kind"] in ("text", "editable"):
        return ""  # a yes/no fact typed into a text box (newsletter email etc.): leave it
    if isinstance(answer, bool) and f["kind"] != "checkbox":
        answer = ["Yes", "I agree", "I consent", "Agree", "Accept", "I acknowledge"] if answer else ["No"]
    alts = answer if isinstance(answer, list) else [answer]
    kind, options = f["kind"], f["options"]
    if kind == "checkbox":
        return truthy(alts[0])
    if kind == "checkbox_group":
        chosen = [o for o in (pick(options, a, fuzzy=False) for a in alts) if o]
        return list(dict.fromkeys(chosen)) or None
    if kind == "combobox" and (f.get("typeable") or not options):
        # searchable dropdown: the peeked list may be partial, so type and let the site search
        hit = next((o for o in (pick(options, a, fuzzy=False) for a in alts) if o), None)
        return [hit] if hit else [str(a) for a in alts]
    if options:  # every alternative strictly first, so "Other" beats a fuzzy near-miss like "IIT Mandi"
        return next((o for fuzzy in (False, True) for o in (pick(options, a, fuzzy) for a in alts) if o), None)
    if f.get("input_type") == "number":
        return next((str(a) for a in alts if re.fullmatch(r"[\d.]+", str(a))), None)
    if kind == "date":
        return next((a for a in alts if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(a))), None)
    return str(alts[0])


def skill_answer(f, resume_words):
    """Yes if any technology named in the question is on the resume, else No. Leaves years-of-X questions alone."""
    m = SKILL_Q.search(norm(f["question"]))
    if not m or re.search(r"\d", f["question"]) or not any(norm(o).split()[:1] in (["yes"], ["no"]) for o in f["options"]):
        return None
    terms = [w for w in m.group(3).split() if len(w) > 1 and w not in SKILL_STOP]
    # ponytail: word match on resume text; "Java" vs "JavaScript" style near-misses answer No, review them
    return resolve(f, "Yes" if any(t in resume_words for t in terms) else "No") if terms else None


def save_answer(ctx, f, value):
    """Store your terminal answer as an exact-question entry, ahead of the general entries."""
    q = norm(f["question"])
    if q:
        ctx.dictionary.insert(0, {"q": "^" + re.escape(q) + "$", "a": value})
        DICTIONARY.write_text(json.dumps(ctx.dictionary, indent=2, ensure_ascii=False))


def file_for(f, ctx):
    q = norm(f["question"] + " " + f["name"])
    if "cover" not in q and re.search(r"resume|\bcv\b|attach|upload", q) and ctx.resume_path.exists():
        return str(ctx.resume_path)
    return None


# ---------- deciding values ----------

def ask_user(f):
    print(f"\n   ? {f['question'] or '(no label)'}" + ("  [required]" if f["required"] else ""))
    opts = f["options"]
    for i, o in enumerate(opts, 1):
        print(f"       {i}. {o}")
    hint = "y/n" if f["kind"] == "checkbox" else "numbers, comma separated" if f["kind"] == "checkbox_group" else "number or text" if opts else "answer"
    raw = input(f"     your {hint} (Enter = leave empty): ").strip()
    if not raw:
        return None
    if f["kind"] == "checkbox":
        return truthy(raw)
    if f["kind"] == "checkbox_group":
        return [opts[int(x) - 1] for x in raw.split(",") if x.strip().isdigit() and 0 < int(x) <= len(opts)]
    if opts and raw.isdigit() and 0 < int(raw) <= len(opts):
        return opts[int(raw) - 1]
    return raw


def write_with_ai(todo, ctx):
    """One AI call for all free-text questions on the page. Returns {field id: text}."""
    request = {
        "resume": ctx.resume[:10000],
        "job": ctx.job,
        "questions": [{k: v for k, v in {"id": f["id"], "question": f["question"], "hint": f["placeholder"],
                                         "required": f["required"]}.items() if v not in (None, "")} for f in todo],
    }
    try:
        data, model = ai.chat(ctx.models, ctx.system, json.dumps(request, ensure_ascii=False))
    except Exception as e:
        print(f"   AI failed: {e}")
        return {}
    rows = data.get("answers", [data] if "id" in data else [])
    if isinstance(rows, dict):  # some models reply {"<id>": text}
        rows = [{"id": k, "value": v} for k, v in rows.items()]
    out = {str(a.get("id")): str(a.get("value")) for a in rows if isinstance(a, dict) and a.get("value") not in (None, "", "SKIP")}
    print(f"   AI ({model}) wrote {len(out)}/{len(todo)} answers")
    return out


def decide(fields, ctx):
    plan, writing, asking = {}, [], []
    for f in fields:
        if f["value"]:
            continue  # already filled (by the site, you, or a previous pass)
        if f["kind"] == "file":
            path = file_for(f, ctx)
            if path:
                plan[f["id"]] = path
            continue
        matches, value = lookup(f, ctx.dictionary), skill_answer(f, ctx.resume_words)
        if value is not None:
            matches = []
        for e in matches:  # a question can mention several topics: first entry with a usable value wins
            if e["a"] is None:
                break  # you said "ask me" for this kind of question
            value = resolve(f, e["a"])
            if value is not None:
                break
        if value == "":
            continue
        if value is not None:
            plan[f["id"]] = value
        elif f["kind"] in ("text", "editable") and (f.get("input_type") == "textarea" or FREE_TEXT.search(norm(f["question"]))):
            writing.append(f)
        elif matches or f["required"]:
            asking.append(f)  # unknown optional fields are left alone
    if writing:
        plan.update(write_with_ai(writing, ctx))
    for f in asking:
        value = ask_user(f)
        if value is not None:
            plan[f["id"]] = value
            save_answer(ctx, f, value)
    return plan


# ---------- page reading and filling ----------

def extract(page, peek=True):
    """All fields in all frames. Returns (fields, login_page)."""
    fields, login = [], False
    for i, frame in enumerate(page.frames):
        try:
            r = frame.evaluate(f"{JS}.extract('{i}')")
        except Exception:
            continue  # frame detached mid-read
        login |= r["password"]
        for f in r["fields"]:
            f["frame"] = frame
            fields.append(f)
    if peek:
        for f in fields:
            if f["kind"] == "combobox" and not f["options"] and not f["value"]:
                f["options"] = peek_options(f)
    return fields, login


def peek_options(f):
    """Open a custom dropdown just to read its options, then close it."""
    frame = f["frame"]
    try:
        frame.locator(f'[data-af="{f["id"]}"]').click(timeout=3000)
        frame.page.wait_for_timeout(600)
        texts = [t.strip() for t in frame.locator(OPTION).all_inner_texts() if t.strip()]
        frame.page.keyboard.press("Escape")
        return texts[:200]
    except Exception:
        return []


def click(loc):
    loc.evaluate("e => e.click()")  # JS click: works on visually-hidden radios/checkboxes too


def fill(f, value):
    frame, kind = f["frame"], f["kind"]
    loc = frame.locator(f'[data-af="{f["id"]}"]').first
    if kind == "text" and re.search(r"\blocation\b|\bcity\b", norm(f["question"])):
        try:
            return fill_combobox(frame, loc, {**f, "typeable": True}, str(value))  # location fields often suggest places
        except Exception:
            pass  # no suggestion list: plain text is fine
    if kind in ("text", "date"):
        loc.fill(str(value))
        loc.evaluate("e => { e.dispatchEvent(new Event('change', {bubbles: true})); e.blur(); }")
        if not loc.input_value():
            raise ValueError("value did not stick")  # sites may reformat it (location, phone), only empty is a failure
    elif kind == "editable":
        loc.click()
        frame.page.keyboard.insert_text(str(value))
    elif kind == "select":
        choice = pick(f["options"], value)
        if choice is None:
            raise ValueError(f"no option like {value!r}")
        loc.select_option(label=choice)
    elif kind in ("radio", "checkbox_group"):
        for v in value if isinstance(value, list) else [value]:
            choice = pick(f["options"], v)
            if choice is None:
                raise ValueError(f"no option like {v!r}")
            click(frame.locator(f'[data-af="{f["id"]}.{f["options"].index(choice)}"]'))
    elif kind == "checkbox":
        if truthy(value) != loc.evaluate("e => e.checked ?? (e.getAttribute('aria-checked') || e.getAttribute('aria-pressed')) === 'true'"):
            click(loc)
    elif kind == "combobox":
        for v in value if isinstance(value, list) else [value]:
            try:
                return fill_combobox(frame, loc, f, str(v))
            except ValueError as e:
                error = e
        raise error
    elif kind == "file":
        loc.set_input_files(value)


def fill_combobox(frame, loc, f, value):
    loc.click(timeout=3000)
    place = re.search(r"\blocation\b|\bcity\b|located", norm(f["question"]))
    if f.get("typeable"):
        loc.fill("")
        query = value.split(",")[0] if place else value[:40]  # place search finds "Barh", not "Barh, Bihar, India"
        loc.press_sequentially(query, delay=30)  # real keystrokes so search-as-you-type dropdowns react
    options = frame.locator(OPTION)
    try:
        options.first.wait_for(timeout=3000)  # search-as-you-type lists load async
    except Exception:
        pass
    texts = [t.strip() for t in options.all_inner_texts()]
    choice = pick(texts, value, fuzzy=False)
    if choice is None and texts and place:
        choice = texts[0]  # ponytail: place search takes the top suggestion; check location fields before submitting
    if choice is None:
        frame.page.keyboard.press("Escape")
        raise ValueError(f"no option like {value!r}")
    options.nth(texts.index(choice)).click()


def find_button(page, kind):
    for frame in page.frames:
        try:
            text = frame.evaluate(f"{JS}.findButton('{kind}')")
        except Exception:
            continue
        if text:
            return frame.locator("[data-af-btn]").first, text
    return None, None


def settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(800)


def fill_page(page, ctx):
    """Fill one form page. Returns (fields, problems)."""
    fields, _ = extract(page)
    plan = decide(fields, ctx)
    problems, filled = [], 0
    for f in fields:
        if f["id"] in plan:
            try:
                fill(f, plan[f["id"]])
                filled += 1
            except Exception as e:
                problems.append(f"{f['question'][:70]!r}: {str(e).splitlines()[0]}")
        elif f["required"] and not f["value"]:
            problems.append(f"{f['question'][:70]!r}: left empty")
    print(f"   filled {filled} of {len(fields)} fields")
    return fields, problems


def fill_form(page, ctx):
    """Fill this page, click Next through multi-step forms, stop before submit. Returns problems."""
    problems = []
    for _ in range(MAX_STEPS):
        fields, step_problems = fill_page(page, ctx)
        problems += step_problems
        button, text = find_button(page, "next")
        if not button:
            break
        if step_problems:
            print(f"   not clicking '{text}': this step has problems")
            break
        before = [f["question"] for f in fields]
        print(f"   clicking '{text}'")
        button.click()
        settle(page)
        if [f["question"] for f in extract(page, peek=False)[0]] == before:
            problems.append(f"'{text}' did not advance (site validation?)")
            break
    return problems


# ---------- browser, tracker, main loop ----------

def connect(p):
    url = f"http://localhost:{CDP_PORT}"
    try:
        return p.chromium.connect_over_cdp(url)
    except Exception:
        pass
    subprocess.Popen(["google-chrome", f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={CHROME_PROFILE}",
                      "--no-first-run", "--no-default-browser-check"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(40):
        time.sleep(0.5)
        try:
            return p.chromium.connect_over_cdp(url)
        except Exception:
            pass
    sys.exit(f"Could not start or reach Chrome on port {CDP_PORT}")


def tracked():
    if not TRACKER.exists():
        return set()
    with TRACKER.open() as fh:
        return {r["url"] for r in csv.DictReader(fh) if r["status"] in ("submitted", "skipped")}


def track(url, title, status, notes):
    new = not TRACKER.exists()
    with TRACKER.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["time", "url", "title", "status", "notes"])
        w.writerow([datetime.datetime.now().isoformat(timespec="minutes"), url, title, status, notes])


def run_link(browser_ctx, url, ctx):
    page = browser_ctx.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    settle(page)
    title = page.title()
    ctx.job = {"url": url, "title": title, "description": page.inner_text("body")[:JOB_TEXT_CHARS]}
    applies, asked = 0, False
    while True:
        page = browser_ctx.pages[-1]  # Apply often opens a new tab
        page.bring_to_front()
        fields, login = extract(page, peek=False)
        is_form = len(fields) >= (1 if asked else 3)  # 1-2 stray fields = job page footer/search, not the form
        if not is_form and not login and applies < 2:  # Workday: Apply, then Apply Manually
            applies += 1
            button, text = find_button(page, "apply")
            if button:
                print(f"   clicking '{text}'")
                button.click()
                settle(page)
                continue
        if login or not is_form:
            what = "Log in / create the account" if login else "Open the application form"
            if input(f"   {what} in Chrome, then press Enter (s = skip): ").strip().lower() == "s":
                return title, "skipped", ""
            asked = True
            continue
        problems = fill_form(page, ctx)
        for p in problems:
            print("   ! " + p)
        cmd = input("   Check the form and submit it in Chrome yourself.\n"
                    "   Then: Enter = submitted | r = fill again (e.g. next page) | a = needs attention | s = skip: ").strip().lower()
        if cmd != "r":
            status = {"s": "skipped", "a": "needs-attention"}.get(cmd, "submitted")
            return title, status, "; ".join(problems)


def load_context():
    if not DICTIONARY.exists():
        sys.exit("dictionary.json missing: cp dictionary.example.json dictionary.json and fill in your details")
    config = json.loads((HERE / "config.json").read_text())
    resume_path = HERE / config["resume"]
    resume = ""
    if resume_path.exists():
        resume = subprocess.run(["pdftotext", "-layout", str(resume_path), "-"], capture_output=True, text=True).stdout
    else:
        print(f"warning: resume not found at {resume_path}; uploads and AI answers disabled")
    return SimpleNamespace(
        dictionary=json.loads(DICTIONARY.read_text()), resume=resume, resume_words=set(norm(resume).split()),
        resume_path=resume_path, job={},
        models=config["models"], system=(HERE / "system_prompt.md").read_text() + "\n" + FORMAT_RULES,
    )


def main():
    ap = argparse.ArgumentParser(description="Fill job application forms; you review and submit.")
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--list-free", action="store_true", help="list free OpenRouter models")
    args = ap.parse_args()
    if args.list_free:
        return ai.list_free()

    links = args.urls or [l.strip() for l in (HERE / "links.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
    done = tracked()
    links = [l for l in links if l not in done]
    if not links:
        return print("Nothing to do: every link is already submitted or skipped in applications.csv")
    ctx = load_context()

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser_ctx = connect(p).contexts[0]
        for i, url in enumerate(links, 1):
            print(f"\n[{i}/{len(links)}] {url}")
            try:
                title, status, notes = run_link(browser_ctx, url, ctx)
            except KeyboardInterrupt:
                print("\nstopped")
                break
            except Exception as e:
                title, status, notes = "", "error", str(e).splitlines()[0]
                print("   error:", notes)
            track(url, title, status, notes)
            print(f"   -> {status}")


if __name__ == "__main__":
    main()
