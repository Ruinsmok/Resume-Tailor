#!/usr/bin/env python3
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import anthropic
import pdfplumber

JOBS_DIR = Path("jobs")
OUTPUT_DIR = Path("output")
TEX_DIR = Path("tex")
SKILLS_CSV = Path("skills.csv")
MASTER_CV = Path("CV.tex")

MODEL = "deepseek-chat"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def get_newest_pdf():
    JOBS_DIR.mkdir(exist_ok=True)
    pdfs = sorted(JOBS_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not pdfs:
        print("No PDFs found in jobs/. Download a job posting PDF there first.")
        sys.exit(1)
    return pdfs[0]


def extract_pdf_text(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def extract_job_data(client, text):
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                "Extract the following fields from this WaterlooWorks job posting as valid JSON.\n"
                "Fields: job_id (string), job_title (string), company (string), work_term (string), "
                "duration (string), location (string), compensation (string), "
                "required_skills (array of strings), cover_letter_required (boolean), "
                "cover_letter_instructions (string, empty string if none).\n"
                "Return only the JSON object, no markdown, no extra text.\n\n"
                f"Posting:\n{text}"
            )
        }]
    )
    raw = resp.content[0].text.strip()
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

    marker_start = re.search(
        r'%-+PROJECTS-+.*?\\resumeSubHeadingListStart',
        cv_tex, re.DOTALL
    )
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
        f"- [{row['level'].strip()}] {row['category']} / {row['skill']}: {row['evidence']}"
        for row in skills
    )
    job_block = (
        f"Title: {job_data['job_title']}\n"
        f"Company: {job_data['company']}\n"
        f"Required Skills: {', '.join(job_data['required_skills'])}\n"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1536,
        messages=[{
            "role": "user",
            "content": (
                "You are helping a University of Waterloo co-op student tailor their resume.\n\n"
                "Job:\n" + job_block + "\n"
                "Candidate skills ([level] category / skill: evidence):\n" + skills_lines + "\n\n"
                "Candidate projects:\n" + projects_context + "\n\n"
                "Return a single JSON object with two fields:\n"
                "1. \"bullets\": array of 3-5 narrative HoQ strings. Rules:\n"
                "   - Group related skills into single bullets ordered by relevance to the job.\n"
                "   - Write as narrative sentences (Canadian co-op HoQ style): lead with experience "
                "or skill phrase, optionally reference where developed (at most once or twice).\n"
                "   - [familiar] skills only if job explicitly requires them; frame as 'familiarity with'.\n"
                "   - Do not invent facts — use only the provided evidence.\n"
                "2. \"project_order\": array of all project names ordered from most to least relevant "
                "to this job. Include every project exactly once.\n"
                "Return only the JSON object, no markdown, no extra text.\n"
                'Example: {"bullets": ["Demonstrated ML experience..."], '
                '"project_order": ["SlopFilter", "IMC Prosperity", "Origami Pattern Designer", '
                '"Formalization of Elementary Number Theory", "Origametry"]}'
            )
        }]
    )
    raw = resp.content[0].text.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(raw)


def match_skills(client, job_data, skills):
    skills_lines = "\n".join(
        f"- [{row['level'].strip()}] {row['category']} / {row['skill']}: {row['evidence']}"
        for row in skills
    )
    job_block = (
        f"Title: {job_data['job_title']}\n"
        f"Company: {job_data['company']}\n"
        f"Required Skills: {', '.join(job_data['required_skills'])}\n"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                "You are helping a University of Waterloo co-op student write the "
                "Highlights of Qualifications section of their resume.\n\n"
                "Job:\n" + job_block + "\n"
                "Candidate skills ([level] category / skill: evidence):\n" + skills_lines + "\n\n"
                "Instructions:\n"
                "1. Select and group related skills into 3-5 bullets ordered by relevance to this job.\n"
                "2. Write each bullet as a narrative sentence following Canadian co-op HoQ conventions: "
                "lead with an experience or skill phrase, optionally reference where it was developed "
                "(at most once or twice across all bullets).\n"
                "3. Skills marked [familiar] are only included if the job explicitly requires them; "
                "frame them as 'familiarity with' or 'exposure to', never as 'experience in'.\n"
                "4. Do not invent facts — only use the provided evidence.\n"
                "5. Return a JSON array of strings, one string per bullet. No other text.\n"
                'Example: ["Demonstrated machine learning experience developing PyTorch models and '
                'ONNX inference pipelines through a browser extension project (SlopFilter).", '
                '"Strong problem-solving and communication skills developed through 500+ hours of '
                'contest math tutoring at AMC to Olympiad level."]'
            )
        }]
    )
    raw = resp.content[0].text.strip()
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
        max_tokens=2048,
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
    cl_tex = resp.content[0].text.strip()
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

    client = anthropic.Anthropic(api_key=os.environ["DEEPSEEK_API_KEY"], base_url=DEEPSEEK_BASE_URL)

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

    job_id = sanitize(job_data.get("job_id", "unknown"))
    company = sanitize(job_data.get("company", "company").split()[0])
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
