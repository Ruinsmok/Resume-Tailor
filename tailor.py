#!/usr/bin/env python3
import csv
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import anthropic
import pdfplumber
from dotenv import load_dotenv

load_dotenv()

JOBS_DIR = Path("jobs")


def first_text(resp):
    for block in resp.content:
        if block.type == "text":
            return block.text.strip()
    raise ValueError(f"No text block in response. Block types: {[b.type for b in resp.content]}")

OUTPUT_DIR = Path("output")
TEX_DIR = Path("tex")
SKILLS_CSV = Path("skills.csv")
MASTER_CV = Path("Resume/resume.tex")

#do not change current model name or URL
MODEL = "deepseek-v4-pro"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"


def get_newest_pdf():
    JOBS_DIR.mkdir(exist_ok=True)
    pdfs = sorted(JOBS_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not pdfs:
        print("No PDFs found in jobs/. Download a job posting PDF there first.")
        sys.exit(1)
    return pdfs[0]


def extract_pdf_text(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    idx = text.find("Job Posting Information")
    return text[idx:] if idx != -1 else text[:4000]


def extract_job_data(client, text):
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


def load_skills():
    with open(SKILLS_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def strip_latex(text):
    text = re.sub(r'\\textbf\{([^}]+)\}', r'\1', text)
    text = re.sub(r'\\emph\{([^}]+)\}', r'\1', text)
    text = re.sub(r'\\href\{[^}]+\}\{([^}]+)\}', r'\1', text)
    text = re.sub(r'\$[^$]+\$', '', text)
    text = re.sub(r'\\[a-zA-Z]+\*?\{([^}]*)\}', r'\1', text)
    text = re.sub(r'\\[a-zA-Z]+', '', text)
    text = re.sub(r'[{}]', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def parse_projects(cv_tex):
    section = re.search(
        r'%-+PROJECTS-+.*?\\resumeSubHeadingListStart(.*?)\\resumeSubHeadingListEnd',
        cv_tex, re.DOTALL
    )
    if not section:
        return []
    content = section.group(1)
    chunks = re.split(r'(?=\\resumeProjectHeading)', content)
    projects = []
    for chunk in chunks:
        if '\\resumeProjectHeading' not in chunk:
            continue
        name_match = re.search(r'\\textbf\{([^}]+)\}', chunk)
        name = name_match.group(1) if name_match else "Unknown"
        projects.append((name, chunk))
    return projects


def build_projects_context(projects):
    lines = []
    for name, block in projects:
        tech_match = re.search(r'\\emph\{([^}]+)\}', block)
        tech = strip_latex(tech_match.group(1)) if tech_match else ""
        bullets = re.findall(r'\\resumeItem\{((?:[^{}]|\{[^{}]*\})*)\}', block)
        lines.append(f"Project: {name}")
        if tech:
            lines.append(f"  Tech: {tech}")
        for b in bullets:
            lines.append(f"  - {strip_latex(b)}")
    return "\n".join(lines)


def reorder_projects(cv_tex, project_order, parsed_projects):
    project_dict = {name.lower(): (name, block) for name, block in parsed_projects}
    reordered = []
    seen = set()
    for name in project_order:
        key = name.lower()
        if key in project_dict:
            reordered.append(project_dict[key][1])
            seen.add(key)
    for name, block in parsed_projects:
        if name.lower() not in seen:
            reordered.append(block)

    marker_end = re.search(
        r'(%-+PROJECTS-+.*?\\resumeSubHeadingListStart)(.*?)(\\resumeSubHeadingListEnd)',
        cv_tex, re.DOTALL
    )
    if not marker_end:
        return cv_tex
    before = cv_tex[:marker_end.start(2)]
    after = cv_tex[marker_end.end(2):]
    return before + "\n" + "".join(reordered) + "    " + after


def match_and_rank(client, job_data, skills, projects_context):
    skills_lines = "\n".join(
        f"{row['skill']}|{row['level'].strip()}: {row['evidence']}"
        for row in skills
    )
    job_block = (
        f"Title: {job_data['job_title']} @ {job_data['company']}\n"
        f"Skills required: {', '.join(job_data['required_skills'])}\n"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        messages=[{
            "role": "user",
            "content": (
                "Tailor a UWaterloo co-op resume. Return JSON only, no markdown.\n\n"
                "JOB:\n" + job_block + "\n"
                "SKILLS (skill|level: evidence):\n" + skills_lines + "\n\n"
                "PROJECTS:\n" + projects_context + "\n\n"
                "Return {\"bullets\": [...], \"project_order\": [...]} where:\n"
                "- bullets: 3-5 narrative HoQ sentences, group related skills, order by relevance. "
                "[familiar] skills only if job requires them, framed as 'familiarity with'. Facts only.\n"
                "- project_order: all project names most→least relevant, each exactly once."
            )
        }]
    )
    raw = first_text(resp)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(raw)


def print_summary(job_data):
    cl = "Not required"
    if job_data.get("cover_letter_required"):
        cl = "Required"
    if job_data.get("cover_letter_instructions"):
        snippet = job_data["cover_letter_instructions"][:70]
        cl = f"Required — {snippet}{'...' if len(job_data['cover_letter_instructions']) > 70 else ''}"
    print("\n" + "─" * 62)
    print(f"  Job:     {job_data['job_title']}")
    print(f"  Company: {job_data['company']}")
    print(f"  Pay:     {job_data['compensation']}")
    print(f"  Term:    {job_data['work_term']} | {job_data['duration']} | {job_data['location']}")
    print(f"  Skills:  {', '.join(job_data['required_skills'])}")
    print(f"  CL:      {cl}")
    print("─" * 62 + "\n")


def confirm(prompt="Proceed? [Y/n] "):
    return input(prompt).strip().lower() in ("", "y", "yes")


def build_hq_latex(bullets):
    escaped = [b.replace("&", r"\&").replace("%", r"\%").replace("$", r"\$") for b in bullets]
    items = "\n".join(f"    \\resumeItem{{{b}}}" for b in escaped)
    return (
        "%-----------HIGHLIGHTS OF QUALIFICATIONS-----------\n"
        "\\section{Highlights of Qualifications}\n"
        "  \\resumeSubHeadingListStart\n"
        + items + "\n"
        "  \\resumeSubHeadingListEnd\n"
    )


def inject_hq_section(cv_tex, bullets):
    marker = "%-----------EDUCATION-----------"
    if marker not in cv_tex:
        print(f"Warning: could not find injection marker '{marker}' in CV.tex.")
        print("LaTeX written without HoQ section — edit manually.")
        return cv_tex
    hq_block = build_hq_latex(bullets) + "\n"
    return cv_tex.replace(marker, hq_block + marker, 1)


def sanitize(name):
    return "".join(c if c.isalnum() else "_" for c in name).strip("_")


def compile_latex(tex_path, out_pdf):
    if not shutil.which("pdflatex"):
        print("pdflatex not found on PATH — skipping compilation. Open the .tex file manually.")
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


def generate_cover_letter(client, job_data, cv_tex, base_name):
    instructions = job_data.get("cover_letter_instructions", "")
    resp = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        messages=[{
            "role": "user",
            "content": (
                "Write a one-page cover letter in LaTeX for a University of Waterloo co-op student "
                "applying to the following role.\n\n"
                f"Job: {job_data['job_title']} at {job_data['company']}\n"
                f"Term: {job_data['work_term']}, {job_data['duration']}, {job_data['location']}\n"
                f"Required skills: {', '.join(job_data['required_skills'])}\n"
                + (f"Specific instructions: {instructions}\n" if instructions else "")
                + "\nCandidate CV (for context on background and projects):\n"
                + cv_tex[:3000] + "\n\n"
                "Return a complete compilable LaTeX document using \\documentclass[letterpaper,11pt]{letter}. "
                "Address it to 'Hiring Manager'. Sign off as Ruiyang (Ryan) Ye. "
                "Return only the LaTeX source, no markdown fences."
            )
        }]
    )
    cl_tex = first_text(resp)
    cl_tex = cl_tex.removeprefix("```latex").removeprefix("```").removesuffix("```").strip()
    cl_tex_path = TEX_DIR / f"{base_name}_CL.tex"
    cl_pdf_path = OUTPUT_DIR / f"{base_name}_CL.pdf"
    cl_tex_path.write_text(cl_tex, encoding="utf-8")
    if compile_latex(cl_tex_path, cl_pdf_path):
        print(f"Cover letter: {cl_pdf_path}")
    else:
        print(f"Cover letter LaTeX saved (compile failed): {cl_tex_path}")


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    TEX_DIR.mkdir(exist_ok=True)

    client = anthropic.Anthropic(base_url=DEEPSEEK_BASE_URL)

    pdf_path = get_newest_pdf()
    print(f"Processing: {pdf_path.name}")
    text = extract_pdf_text(pdf_path)

    print("Extracting job data...")
    job_data = extract_job_data(client, text)
    print_summary(job_data)

    if not confirm():
        sys.exit(0)

    cv_tex = MASTER_CV.read_text(encoding="utf-8")
    parsed_projects = parse_projects(cv_tex)
    projects_context = build_projects_context(parsed_projects)

    skills = load_skills()
    print("Matching skills and ranking projects...")
    result = match_and_rank(client, job_data, skills, projects_context)
    bullets = result.get("bullets", [])
    project_order = result.get("project_order", [])

    print("\nHighlights of Qualifications to be added:")
    for i, b in enumerate(bullets, 1):
        print(f"  {i}. {b}")

    print("\nProject order (most to least relevant):")
    for i, p in enumerate(project_order, 1):
        print(f"  {i}. {p}")
    print()

    if not confirm("Looks good? [Y/n] "):
        sys.exit(0)

    job_id = sanitize(job_data.get("job_id") or "unknown")
    company = sanitize((job_data.get("company") or "company").split()[0])
    base_name = f"{job_id}_{company}"

    tailored = inject_hq_section(cv_tex, bullets)
    tailored = reorder_projects(tailored, project_order, parsed_projects)

    tex_out = TEX_DIR / f"{base_name}.tex"
    pdf_out = OUTPUT_DIR / f"{base_name}.pdf"
    tex_out.write_text(tailored, encoding="utf-8")

    print(f"LaTeX written: {tex_out}")

    print(f"Compiling...")
    if compile_latex(tex_out, pdf_out):
        print(f"Resume ready: {pdf_out}")
    else:
        print(f"LaTeX saved to {tex_out} — compile manually.")

    if job_data.get("cover_letter_required") or job_data.get("cover_letter_instructions"):
        print("\nGenerating cover letter...")
        generate_cover_letter(client, job_data, cv_tex, base_name)


if __name__ == "__main__":
    main()
