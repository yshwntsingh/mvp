
import re

BLOCKED_KEYWORDS = [
    "password", "secret", "token", "apikey",
    "salary", "resignation", "legal", "confidential"
]

ROLES = {
    "admin": ["public", "restricted", "secret"],
    "user": ["public", "restricted"],
    "guest": ["public"]
}

def has_blocked_keyword(text: str) -> bool:
    text = text.lower()
    return any(k in text for k in BLOCKED_KEYWORDS)

def redact(text: str) -> str:
    for k in BLOCKED_KEYWORDS:
        text = re.sub(k, "███", text, flags=re.IGNORECASE)
    return text

def role_allowed(role, sensitivity):
    return sensitivity in ROLES.get(role, [])
