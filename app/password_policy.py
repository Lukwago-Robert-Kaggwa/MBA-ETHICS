import re

MIN_LENGTH = 10

_UPPER_RE = re.compile(r"[A-Z]")
_LOWER_RE = re.compile(r"[a-z]")
_DIGIT_RE = re.compile(r"[0-9]")
_SYMBOL_RE = re.compile(r"[^A-Za-z0-9]")


def validate_password_strength(password, email=None):
    """Returns None if the password meets policy, otherwise an error message."""
    password = password or ""
    if len(password) < MIN_LENGTH:
        return f"Password must be at least {MIN_LENGTH} characters long."

    classes_present = sum(
        1 for pattern in (_UPPER_RE, _LOWER_RE, _DIGIT_RE, _SYMBOL_RE) if pattern.search(password)
    )
    if classes_present < 3:
        return (
            "Password must include at least 3 of the following: "
            "uppercase letters, lowercase letters, numbers, symbols."
        )

    if email:
        local_part = (email.split("@", 1)[0] or "").lower()
        if len(local_part) >= 4 and local_part in password.lower():
            return "Password must not contain your email address."

    return None
