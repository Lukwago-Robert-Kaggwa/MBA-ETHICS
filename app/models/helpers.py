import re

UJ_STUDENT_EMAIL_RE = re.compile(r"^(?P<number>\d+)@student\.uj\.ac\.za$", re.IGNORECASE)


def normalize_email(email):
    return (email or "").strip().lower()


def student_email_for(student_number):
    clean_number = re.sub(r"\D", "", student_number or "")
    return f"{clean_number}@student.uj.ac.za"


def is_uj_student_email(email):
    return bool(UJ_STUDENT_EMAIL_RE.match(normalize_email(email)))
