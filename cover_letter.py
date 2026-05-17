#!/usr/bin/env python3
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import anthropic
import pdfplumber
from dotenv import load_dotenv

load_dotenv()

MODEL = "deepseek-v4-pro"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"

JOBS_DIR = Path("jobs")
OUTPUT_DIR = Path("output")
TEX_DIR = Path("tex")
MASTER_CV = Path("Resume/resume.tex")


def first_text(resp):
    for block in resp.content:
        if block.type == "text":
            return block.text.strip()
    raise ValueError(f"No text block in response. Block types: {[b.type for b in resp.content]}")


def compile_latex(tex_path, out_pdf):
    if not shutil.which("pdflatex"):
        print("pdflatex not found on PATH — skipping compilation.")
        return False
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            "pdflatex", "-interaction=nonstopmode",
            f"-output-directory={tmp}",
            str(tex_path.resolve()),
        ]
        r = subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")
        for _ in range(2):
            r = subprocess.run(cmd, capture_output=True, text=True)
        tmp_pdf = Path(tmp) / tex_path.with_suffix(".pdf").name
        if tmp_pdf.exists():
            shutil.copy(tmp_pdf, out_pdf)
            return True
        print("pdflatex failed. Last output:\n" + r.stdout[-3000:])
        return False


def extract_pdf_text(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    idx = text.find("Job Posting Information")
    return text[idx:] if idx != -1 else text[:4000]


def extract_job_data(client, text):
    import json
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": (
                "Extract from this WaterlooWorks posting as JSON (no markdown):\n"
                "job_id, job_title, company, work_term, duration, location, compensation, "
                "required_skills (array), cover_letter_required (bool), "
                "cover_letter_instructions (str, empty if none).\n\n"
                f"{text}"
            )
        }]
    )
    raw = first_text(resp)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(raw)


TEMPLATE_PATH = Path("cover_letter_template.tex")


def generate_cover_letter(client, job_data, cv_tex, base_name, note=""):
    instructions = job_data.get("cover_letter_instructions", "")
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": (
                "Write the body of a cover letter (2-3 paragraphs, plain text only, no LaTeX commands) "
                "for a University of Waterloo co-op student applying to the following role.\n\n"
                f"Job: {job_data['job_title']} at {job_data['company']}\n"
                f"Term: {job_data['work_term']}, {job_data['duration']}, {job_data['location']}\n"
                f"Required skills: {', '.join(job_data['required_skills'])}\n"
                + (f"Specific instructions: {instructions}\n" if instructions else "")
                + (f"Additional context to address: {note}\n" if note else "")
                + "\nCandidate CV:\n" + cv_tex[:3000] + "\n\n"
                "Return only the paragraphs, separated by blank lines. No salutation, no sign-off."
            )
        }]
    )
    body = first_text(resp).strip()

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    cl_tex = template.replace("<<COMPANY>>", job_data.get("company", ""))
    cl_tex = cl_tex.replace("<<BODY>>", body + "\n\n")

    cl_tex_path = TEX_DIR / f"{base_name}_CL.tex"
    cl_pdf_path = OUTPUT_DIR / f"{base_name}_CL.pdf"
    cl_tex_path.write_text(cl_tex, encoding="utf-8")
    if compile_latex(cl_tex_path, cl_pdf_path):
        print(f"Cover letter: {cl_pdf_path}")
    else:
        print(f"Cover letter LaTeX saved (compile failed): {cl_tex_path}")


def get_newest_job_pdf():
    matches = list(JOBS_DIR.glob("*.pdf"))
    if not matches:
        print("No PDF found in jobs/.")
        sys.exit(1)
    return sorted(matches, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def sanitize(name):
    return "".join(c if c.isalnum() else "_" for c in name).strip("_")


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    TEX_DIR.mkdir(exist_ok=True)

    client = anthropic.Anthropic(base_url=DEEPSEEK_BASE_URL)

    pdf_path = get_newest_job_pdf()
    print(f"Processing: {pdf_path.name}")
    text = extract_pdf_text(pdf_path)

    print("Extracting job data...")
    job_data = extract_job_data(client, text)
    print(f"  {job_data['job_title']} @ {job_data['company']}")

    cv_tex = MASTER_CV.read_text(encoding="utf-8")

    job_id = sanitize(job_data.get("job_id") or "unknown")
    company = sanitize((job_data.get("company") or "company").split()[0])
    base_name = f"{job_id}_{company}"

    note = (
        "I am currently pursuing a plan modification to Combinatorics and Optimization, "
        "Pure Math, and Statistics at the University of Waterloo. This modification is in "
        "progress and may not yet be reflected on my official transcript."
    )

    print("Generating cover letter...")
    generate_cover_letter(client, job_data, cv_tex, base_name, note=note)


if __name__ == "__main__":
    main()
