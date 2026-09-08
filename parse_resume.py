#!/usr/bin/env python3
"""
parse_resume.py — extract your work history from the resume PDF into
data/work_history.yaml, using local Ollama. Run once, then VERIFY/edit the result
(3B models miss dates). The Workday corrector fills each Work Experience block from it.

Usage:
    pip install pdfplumber pyyaml
    python parse_resume.py

Reads RESUME_PATH from .env. Writes data/work_history.yaml (most recent job first).
"""
import os, re, json, sys
import yaml
import apply  # for ollama() + .env load

HERE = os.path.dirname(os.path.abspath(__file__))
RESUME = os.environ.get("RESUME_PATH", "")

def extract_text(path):
    try:
        import pdfplumber
    except ImportError:
        sys.exit("Install pdfplumber first:  pip install pdfplumber")
    if not path or not os.path.exists(path):
        sys.exit(f"RESUME_PATH not found: {path!r}")
    parts = []
    with pdfplumber.open(path) as pdf:
        for pg in pdf.pages:
            parts.append(pg.extract_text() or "")
    return "\n".join(parts)

SYS = ("Extract ONLY the work experience from this resume as strict JSON: "
       'a list of jobs, most recent first, each: '
       '{"title","company","location","start","end","current","description"}. '
       'Dates are MM/YYYY (e.g. "06/2021"); end is "" if current, and set "current":true. '
       "Keep description to 1-2 sentences pulled from the resume, no invented facts. "
       "Output ONLY the JSON array, no prose, no code fence.")

def parse(text):
    if not apply.ollama_up():
        sys.exit("Ollama isn't reachable — start it, then rerun.")
    raw = apply.ollama(text[:8000], SYS, temperature=0)
    raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.I).strip()
    try:
        jobs = json.loads(raw)
    except Exception as e:
        sys.exit(f"Could not parse model output as JSON ({e}).\n--- output ---\n{raw[:1500]}")
    # normalize
    out = []
    for j in jobs if isinstance(jobs, list) else []:
        out.append({
            "title": (j.get("title") or "").strip(),
            "company": (j.get("company") or "").strip(),
            "location": (j.get("location") or "").strip(),
            "start": (j.get("start") or "").strip(),
            "end": (j.get("end") or "").strip(),
            "current": bool(j.get("current")) or not (j.get("end") or "").strip(),
            "description": " ".join((j.get("description") or "").split()),
        })
    return out

def main():
    text = extract_text(RESUME)
    jobs = parse(text)
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    p = os.path.join(HERE, "data", "work_history.yaml")
    header = ("# Auto-drafted from your resume by parse_resume.py. VERIFY every field —\n"
              "# especially dates (MM/YYYY). The Workday corrector fills each Work Experience\n"
              "# block from this list, in order (most recent first), overwriting Workday's parse.\n\n")
    with open(p, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump({"jobs": jobs}, f, sort_keys=False, allow_unicode=True, width=100)
    print(f"Wrote {len(jobs)} jobs to data/work_history.yaml — open it and verify the dates.")

if __name__ == "__main__":
    main()
