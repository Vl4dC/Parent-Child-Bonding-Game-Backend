"""Nothing secret is committed: tracked files hold placeholders, synthetic demo fixtures or nothing."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = ("tests/", "learning-service/static/")
SKIP_FILES = {"uv.lock", "docs/openapi.json"}
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".sql",
    ".toml",
    ".yml",
    ".yaml",
    ".json",
    ".txt",
    ".example",
    ".cfg",
    ".ini",
    ".js",
    ".html",
    ".css",
    "",
}

PATTERNS = {
    "Supabase secret key": re.compile(r"sb_secret_[A-Za-z0-9_-]{10,}"),
    "JSON web token": re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{5,}"),
    "Anthropic key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
    "OpenRouter key": re.compile(r"sk-or-[A-Za-z0-9_-]{20,}"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
}
PLACEHOLDER_PASSWORDS = {
    "replace_me",
    "postgres",
    "password",
    "example",
    "changeme",
    "your-password",
    "secret",
}
DSN = re.compile(r"postgres(?:ql)?://[^:/\s@]+:([^@\s/]+)@")


def tracked_files():
    git = shutil.which("git")
    if not git or not (ROOT / ".git").exists():
        pytest.skip("git metadata is not available")
    out = subprocess.run(
        [git, "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True
    ).stdout
    return [name for name in out.decode().split("\0") if name]


def scannable(name):
    return (
        name not in SKIP_FILES
        and not name.startswith(SKIP_DIRS)
        and Path(name).suffix in TEXT_SUFFIXES
    )


def test_no_credential_shaped_strings_are_tracked():
    findings = []
    for name in tracked_files():
        if not scannable(name) or not (ROOT / name).is_file():
            continue
        text = (ROOT / name).read_text(encoding="utf-8", errors="ignore")
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{name}: {label}")
        for match in DSN.finditer(text):
            if match.group(1).lower() not in PLACEHOLDER_PASSWORDS:
                findings.append(f"{name}: database URL with a real-looking password")
    assert findings == []


def test_no_environment_or_key_files_are_tracked():
    risky = []
    for name in tracked_files():
        base = Path(name).name
        if base == ".env" or (
            base.startswith(".env.") and base not in {".env.example", ".env.demo.example"}
        ):
            risky.append(name)
        if base.endswith((".pem", ".key", ".p12", ".pfx", ".keystore")) or base in {
            "id_rsa",
            "id_ed25519",
            "credentials.json",
            "service-account.json",
        }:
            risky.append(name)
    assert risky == []


def test_the_ignore_file_protects_local_secrets():
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in ignored and ".env.*" in ignored and ".venv/" in ignored
    assert "!.env.example" in ignored and "!.env.demo.example" in ignored


def test_the_only_published_passwords_are_the_two_synthetic_demo_logins():
    allowed = {"DemoParent123!", "DemoChild123!"}
    docs = "\n".join((ROOT / n).read_text(encoding="utf-8") for n in ("README.md", "API.md"))
    published = set(re.findall(r'"password":\s*"([^"]+)"', docs)) | set(
        re.findall(r"`([A-Za-z]+Demo[A-Za-z0-9!]+|Demo[A-Za-z0-9!]+)`", docs)
    )
    assert {p for p in published if "Demo" in p} <= allowed
    # Every other example password in the docs is an obvious placeholder, never a reusable value.
    for password in published - allowed:
        assert re.search(r"(?i)example|replace|different|placeholder|your", password), password
