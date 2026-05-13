# Resume Tailor — Design Plan

## Goal
A CLI tool that reads a WaterlooWorks job PDF, extracts key information, matches it against a personal skill document, and produces a tailored resume (and cover letter if required) ready to upload.

---

## Project Structure

```
Resume Tailor/
├── CV.tex                          # Master resume — never modified
├── skills.csv                      # Personal skill document (hand-authored)
├── PLAN.md                         # This file
├── tailor.py                       # CLI entry point
├── jobs/                           # Set browser download path here; tool reads newest PDF
├── output/                         # Final PDFs: <JobID>_<Company>.pdf, <JobID>_<Company>_CL.pdf
└── tex/                            # Generated .tex files: <JobID>_<Company>.tex, <JobID>_<Company>_CL.tex
```

---

## Skill Document Format (`skills.csv`)

Four columns: `category`, `skill`, `evidence`, `level`

Levels: `proficient`, `intermediate`, `basic`, `familiar`, `advanced`

The `level` field controls LLM inclusion logic:
- `familiar` skills are only included if the job explicitly mentions them; framed as "familiarity with" or "exposure to"
- All other levels are eligible for selection based on relevance

The `evidence` field is a short phrase proving the skill — pulled from actual projects/experience. The LLM uses this to write honest, concrete narrative sentences.

---

## CLI Interface

```
python tailor.py
```

No arguments. The script:
1. Scans `jobs/` for the newest `.pdf` file
2. Extracts job data via LLM
3. Prints a summary table to terminal for review
4. Matches skills via LLM and prints matched skills for manual verification
5. Writes `.tex` files to `tex/`, compiles PDFs to `output/`
6. LaTeX aux files (`.log`, `.aux`, etc.) are compiled into a temp directory and discarded

---

## Pipeline

### Step 1 — Extract job data (LLM call #1)

Input: raw PDF text
Output: structured JSON

```json
{
  "job_id": "467014",
  "job_title": "Software Engineering Assistant [Startup]",
  "company": "Mechanize Inc",
  "work_term": "2026 - Fall",
  "duration": "8 month consecutive",
  "location": "San Francisco, CA (In-person)",
  "compensation": "$100/hr",
  "required_skills": ["Competency with AI coding tools", "Python"],
  "cover_letter_required": false,
  "cover_letter_instructions": ""
}
```

Fields to extract:
- **Work Term** + **Work Term Duration** → intern timing
- **Required Skills** section → skill list
- **Compensation and Benefits** → pay rate
- **Application Documents Required** + **Additional Application Information** → cover letter trigger

### Step 2 — Print summary to terminal

```
────────────────────────────────────────────
  Job:          Software Engineering Assistant [Startup]
  Company:      Mechanize Inc
  Pay:          $100/hr
  Term:         2026 Fall | 8 months | San Francisco (In-person)
  Skills req:   Competency with AI coding tools, Python
  Cover letter: Not required
────────────────────────────────────────────
Proceed? [Y/n]
```

User can abort here if the job isn't worth applying to.

### Step 3 — Match and group skills (LLM call #2)

Input: job JSON (title, summary, responsibilities, required skills) + full `skills.csv`
Output: 3–5 narrative bullet strings, grouped and ordered by relevance to the job

The LLM:
- Reasons semantically — "Competency with AI coding tools" maps to PyTorch/ONNX/Python rows even without keyword overlap
- Groups related skills into single bullets (e.g. Python + PyTorch + ONNX → one ML bullet for an AI role; Python + Algorithm Design → one quant bullet for a trading role)
- Prioritizes bullets by how central each skill cluster is to the job's focus
- Writes each bullet as a **narrative sentence** following the university HoQ format:
  - Experience/skill phrase + brief context (where it was developed)
  - E.g.: *"Demonstrated machine learning experience developing PyTorch models and ONNX inference pipelines through a browser extension project (SlopFilter)"*
  - Include interpersonal/soft skills (tutoring hours, mentorship) when relevant to the role
  - At most one or two bullets reference the specific project/context; rest stay concise

LLM prompt instruction: *"Group related skills into 3–5 bullets ordered by relevance to this job. Write each as a narrative sentence following Canadian co-op HoQ conventions: lead with experience or skill, optionally include one brief context reference."*

### Step 4 — Print matched skills for verification

```
Highlights of Qualifications to be added:
  1. Demonstrated machine learning experience developing PyTorch models and ONNX
     inference pipelines through browser extension project (SlopFilter).
  2. Proficient in Python, applied to algorithmic trading strategies and AI tooling.
  3. Competency with AI coding tools across multiple software engineering projects.
  4. Strong problem-solving and communication skills developed through 500+ hours
     of one-on-one and group contest math tutoring.

Looks good? [Y/n]
```

User can abort or continue.

### Step 5 — Generate tailored resume `.tex`

Start from `CV.tex`. Insert a `Highlights of Qualifications` section between the header block (`\end{center}`) and `\section{Education}`:

```latex
%-----------HIGHLIGHTS OF QUALIFICATIONS-----------
\section{Highlights of Qualifications}
 \begin{itemize}[leftmargin=0.15in, label={}]
    \small{\item{
     Demonstrated machine learning experience developing PyTorch models and ONNX inference pipelines through browser extension project (SlopFilter). \\
     Proficient in Python, applied to algorithmic trading strategies and AI tooling. \\
     Competency with AI coding tools across multiple software engineering projects. \\
     Strong problem-solving and communication skills developed through 500+ hours of one-on-one and group contest math tutoring. \\
    }}
 \end{itemize}
```

This mirrors the style of the existing `Technical Skills` section. Bullets are plain sentences — no bold skill label prefix, since the narrative format reads better without it.

### Step 6 — Generate cover letter `.tex` (conditional)

Only triggered if `cover_letter_required = true` or `cover_letter_instructions` is non-empty.

LLM call #3 (if needed): given job summary + your CV content + any specific instructions from the posting, generate a one-page cover letter in a matching LaTeX template.

### Step 7 — Compile to PDF

Run `pdflatex` with output directed to a temp directory. Move only the final `.pdf` to `output/`. Discard all aux files.

Output files:
- `tex/467014_Mechanize.tex`
- `output/467014_Mechanize.pdf`
- `tex/467014_Mechanize_CL.tex` (if cover letter)
- `output/467014_Mechanize_CL.pdf` (if cover letter)

---

## LLM Model

Use `claude-haiku-4-5-20251001` for both extraction and matching calls — fast, cheap (fractions of a cent per run), sufficient for structured extraction. Upgrade to `claude-sonnet-4-6` for cover letter generation only, since prose quality matters more there.

---

## Dependencies

- Python 3.x
- `anthropic` SDK (`pip install anthropic`)
- `pdfplumber` or `pymupdf` for PDF text extraction (`pip install pdfplumber`)
- `pdflatex` on system PATH (already confirmed by existing `.fls`/`.synctex.gz` files)

---

## Implementation Order

1. `skills.csv` — hand-author the skill document first; everything depends on it
2. PDF text extraction — verify the extracted text is clean on both sample files
3. LLM extraction call — parse to JSON, test on `Software Engineering Assistant [Startup]-467014.pdf`
4. Terminal summary + confirmation prompt
5. LLM matching call — verify it handles vague requirements correctly
6. Terminal skill verification prompt
7. LaTeX injection — insert Highlights section into CV copy
8. PDF compilation + file placement
9. Cover letter path (conditional)
