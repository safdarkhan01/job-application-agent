from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import zipfile
from xml.etree import ElementTree


MAX_RESUME_BYTES = 15 * 1024 * 1024
MAX_DOCUMENT_BYTES = 30 * 1024 * 1024
SKILLS = (
    "Python", "JavaScript", "TypeScript", "Java", "C++", "C#", "SQL", "Go", "Rust",
    "React", "Angular", "Vue", "Node.js", "Django", "Flask", "FastAPI", ".NET",
    "AWS", "Azure", "GCP", "Docker", "Kubernetes", "Terraform", "Linux", "Git",
    "PostgreSQL", "MySQL", "MongoDB", "Redis", "Excel", "Power BI", "Tableau",
    "Figma", "Salesforce", "SAP", "Pandas", "TensorFlow", "PyTorch",
    "Machine Learning", "Data Analysis", "Project Management", "Product Management",
    "Customer Service", "Financial Analysis", "Accounting", "Recruitment", "Marketing",
)
TITLES = (
    "Software Engineer", "Software Developer", "Frontend Developer", "Backend Developer",
    "Full Stack Developer", "Data Engineer", "Data Scientist", "Data Analyst",
    "Machine Learning Engineer", "DevOps Engineer", "Site Reliability Engineer",
    "Product Manager", "Project Manager", "Business Analyst", "UX Designer",
    "UI Designer", "Accountant", "Financial Analyst", "Sales Manager",
    "Marketing Manager", "Customer Success Manager", "Recruiter",
)


def contains_term(text: str, term: str) -> bool:
    return re.search(r"(?<![\w+#])" + re.escape(term) + r"(?![\w+#])", text, re.IGNORECASE) is not None


def extract_skills(text: str) -> list[str]:
    return [skill for skill in SKILLS if contains_term(text, skill)]


@dataclass
class ResumeImport:
    text: str
    titles: list[str]
    skills: list[str]
    experience: str
    email: str


def read_resume(path: Path) -> ResumeImport:
    if not path.is_file() or path.stat().st_size > MAX_RESUME_BYTES:
        raise ValueError("Choose a résumé file no larger than 15 MB.")
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Text résumés must be UTF-8. Export as UTF-8 text or DOCX.") from exc
        if "\x00" in text:
            raise ValueError("This file is binary, not a readable text résumé.")
    elif suffix == ".docx":
        try:
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo("word/document.xml")
                if info.file_size > MAX_DOCUMENT_BYTES:
                    raise ValueError("The DOCX document is too large to parse safely.")
                root = ElementTree.fromstring(archive.read(info))
                namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
                text = "\n".join("".join(node.itertext()) for node in root.iter(namespace + "p"))
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, RuntimeError) as exc:
            raise ValueError("This DOCX could not be read. Export an unencrypted DOCX or UTF-8 text.") from exc
    elif suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("PDF support is unavailable. Install the app dependencies or import DOCX/text.") from exc
        try:
            reader = PdfReader(path)
            if reader.is_encrypted:
                raise ValueError("Password-protected PDFs are unsupported. Export an unencrypted copy.")
            if len(reader.pages) > 100:
                raise ValueError("Résumés over 100 pages are not supported.")
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except ValueError:
            raise
        except Exception as exc:
            # pypdf exposes several parser exceptions; retain an explicit user-facing failure.
            raise ValueError("PDF parsing failed. Export an unencrypted PDF, DOCX, or UTF-8 text.") from exc
    else:
        raise ValueError("Unsupported résumé format. Choose PDF, DOCX, TXT, or Markdown; legacy DOC is unsupported.")
    text = text.strip()
    if len(text) < 30 or not any(char.isalpha() for char in text):
        raise ValueError("No usable résumé text was found. Scanned PDFs require OCR outside this app.")
    if len(text) > 250_000:
        raise ValueError("The extracted résumé text is too large.")
    titles = [title for title in TITLES if contains_term(text, title)]
    email = re.search(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", text)
    experience = "\n".join(line.strip() for line in text.splitlines()
                           if re.search(r"\b(?:19|20)\d{2}\b|\b\d+\+?\s+years?\b", line, re.I))
    return ResumeImport(text, titles, extract_skills(text), experience[:6000], email.group() if email else "")
