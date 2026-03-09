#!/usr/bin/env python3

import io
import json
import os
import re
import zipfile
import subprocess
import tempfile
import urllib.request
import hmac
from pathlib import Path
from typing import Dict, Literal, Tuple

import streamlit as st

# ============================================================
# Types
# ============================================================

IaCType = Literal[
    "terraform",
    "kubernetes",
    "bash",
    "github_actions",
    "mixed",
]

Cloud = Literal["aws", "azure", "gcp"]

# iac_type = st.sidebar.radio(
#     "IaC Type",
#     ["terraform", "kubernetes", "bash", "github_actions", "mixed"],
# )
# ============================================================
# Ollama Defaults
# ============================================================

OLLAMA_URL_DEFAULT = "http://localhost:11434/api/chat"
MODEL_DEFAULT = "qwen2.5-coder:7b"

# ============================================================
# JSON Schema (enforced by Ollama)
# ============================================================

IAC_BUNDLE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "iac_type": {
            "type": "string",
            "enum": [
                "terraform",
                "kubernetes",
                "bash",
                "github_actions",
                "mixed",
            ],
        },
        "overview": {"type": "string"},
        "files": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "post_steps": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["iac_type", "overview", "files", "post_steps"],
}

# ============================================================
# System Prompts
# ============================================================

SYSTEM_GENERATE = """
You are an expert DevOps/IaC agent.
Convert the user's plain-English request into a minimal, runnable IaC bundle.

Hard rules:
- Output MUST be valid JSON only.
- Follow provided JSON schema strictly.
- Prefer secure defaults.
- Never include destructive commands.
"""

SYSTEM_REVIEW = """
You are a strict IaC reviewer.
Fix security, correctness, usability issues.
Return ONLY corrected JSON.
"""

# ============================================================
# Admin Authentication
# ============================================================

ADMIN_USER = "admin"
ADMIN_PASSWORD = "Admin@123"


def require_admin() -> None:
    if "is_admin" not in st.session_state:
        st.session_state.is_admin = False

    if st.session_state.is_admin:
        st.sidebar.success("Logged in as admin")
        if st.sidebar.button("Logout"):
            st.session_state.is_admin = False
            st.rerun()
        return

    st.sidebar.subheader("Admin Login")
    user = st.sidebar.text_input("Username")
    pwd = st.sidebar.text_input("Password", type="password")

    if st.sidebar.button("Login"):
        if (
            hmac.compare_digest(user.strip(), ADMIN_USER)
            and hmac.compare_digest(pwd, ADMIN_PASSWORD)
        ):
            st.session_state.is_admin = True
            st.rerun()
        else:
            st.sidebar.error("Unauthorized")

    st.stop()


# ============================================================
# Ollama Chat
# ============================================================

def chat_with_system(
    system: str,
    user: str,
    model: str,
    ollama_url: str,
    temperature: float,
) -> str:

    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "format": IAC_BUNDLE_SCHEMA,
        "options": {"temperature": temperature},
    }

    req = urllib.request.Request(
        ollama_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=240) as resp:
        body = resp.read().decode()
        return json.loads(body)["message"]["content"]


# ============================================================
# JSON Parsing
# ============================================================

def parse_json_strict(text: str) -> dict:
    if not text.strip():
        raise ValueError("Empty model response")

    text = text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)

    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))

    raise ValueError("Model did not return valid JSON.")


# ============================================================
# Validation
# ============================================================

def validate_bundle(data: dict) -> None:
    for k in IAC_BUNDLE_SCHEMA["required"]:
        if k not in data:
            raise ValueError(f"Missing key: {k}")

    if not isinstance(data["files"], dict) or not data["files"]:
        raise ValueError("files must be non-empty dict")

    if not isinstance(data["post_steps"], list):
        raise ValueError("post_steps must be list")


# ============================================================
# ZIP Creation
# ============================================================

def make_zip(files: Dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in files.items():
            safe = path.strip().lstrip("/").replace("\\", "/")
            z.writestr(safe, content)
    return buf.getvalue()


# ============================================================
# Subprocess Utils
# ============================================================

def run_cmd(cmd, cwd: str) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        return p.returncode, out.strip()
    except FileNotFoundError:
        return 127, f"Command not found: {cmd[0]}"


# ============================================================
# Safe Checks
# ============================================================

def terraform_checks(files: Dict[str, str]) -> Dict[str, str]:
    results = {}
    with tempfile.TemporaryDirectory(prefix="iac_tf_") as td:
        base = Path(td)

        for path, content in files.items():
            if path.endswith((".tf", ".tfvars")):
                p = base / path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)

        for name, cmd in {
            "terraform fmt": ["terraform", "fmt", "-recursive"],
            "terraform init": ["terraform", "init", "-input=false"],
            "terraform validate": ["terraform", "validate"],
        }.items():
            code, out = run_cmd(cmd, str(base))
            results[name] = f"exit={code}\n{out}"

    return results


def kubectl_dry_run(files: Dict[str, str]) -> Dict[str, str]:
    results = {}
    with tempfile.TemporaryDirectory(prefix="iac_k8s_") as td:
        base = Path(td)
        has_yaml = False

        for path, content in files.items():
            if path.endswith((".yaml", ".yml")):
                has_yaml = True
                p = base / path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)

        if not has_yaml:
            return {"kubectl dry-run": "No YAML files found."}

        code, out = run_cmd(
            ["kubectl", "apply", "--dry-run=server", "-f", str(base)],
            str(base),
        )
        results["kubectl apply --dry-run=server"] = f"exit={code}\n{out}"

    return results


# ============================================================
# Streamlit UI
# ============================================================

st.set_page_config(page_title="Free IaC Agent", layout="wide")
st.title("IaC Agent — English → Terraform / YAML / Bash / CI")

# require_admin()

# with st.sidebar:
#     st.header("Runtime")
#     ollama_url = st.text_input("Ollama URL", OLLAMA_URL_DEFAULT)
#     model = st.text_input("Model", MODEL_DEFAULT)
#     temperature = st.slider("Temperature", 0.0, 1.0, 0.2)

#     st.header("Workflow")
#     use_review = st.checkbox("Second-pass review", value=True)
#     run_tf = st.checkbox("Run Terraform checks")
#     run_k8s = st.checkbox("Run kubectl dry-run")

# st.subheader("Describe what you want")
# request = st.text_area("Request", height=150)

require_admin()

with st.sidebar:
    st.header("Runtime")
    ollama_url = st.text_input("Ollama URL", OLLAMA_URL_DEFAULT)
    model = st.text_input("Model", MODEL_DEFAULT)
    temperature = st.slider("Temperature", 0.0, 1.0, 0.2)

    st.header("Workflow")
    use_review = st.checkbox("Second-pass review", value=True)
    run_tf = st.checkbox("Run Terraform checks")
    run_k8s = st.checkbox("Run kubectl dry-run")

    st.header("IaC Settings")  # 👈 Add this
    iac_type = st.selectbox(
        "IaC Type",
        ["terraform", "kubernetes", "bash", "github_actions", "mixed"],
    )

with st.sidebar:
    st.header("Runtime")
    ollama_url = st.text_input("Ollama URL", OLLAMA_URL_DEFAULT)
    model = st.text_input("Model", MODEL_DEFAULT)
    temperature = st.slider("Temperature", 0.0, 1.0, 0.2)

    st.header("Workflow")
    use_review = st.checkbox("Second-pass review", value=True)
    run_tf = st.checkbox("Run Terraform checks")
    run_k8s = st.checkbox("Run kubectl dry-run")

    st.header("IaC Settings")

    iac_type = st.selectbox(
        "IaC Type",
        ["terraform", "kubernetes", "bash", "github_actions", "mixed"],
    )

    cloud = st.selectbox(
        "Cloud Provider",
        ["aws", "azure", "gcp"],
    )
col1, col2 = st.columns(2)
generate_btn = col1.button("Generate", type="primary", use_container_width=True)
clear_btn = col2.button("Clear", use_container_width=True)

if clear_btn:
    st.session_state.clear()
    st.rerun()

# ============================================================
# Generate
# ============================================================

if generate_btn and request.strip():

    with st.spinner("Generating..."):
        raw = chat_with_system(
            SYSTEM_GENERATE,
            request,
            model,
            ollama_url,
            temperature,
        )

    data = parse_json_strict(raw)
    validate_bundle(data)

    if use_review:
        with st.spinner("Reviewing..."):
            raw2 = chat_with_system(
                SYSTEM_REVIEW,
                json.dumps(data),
                model,
                ollama_url,
                0.0,
            )
        data = parse_json_strict(raw2)
        validate_bundle(data)

    st.session_state["bundle"] = data

    checks = {}
    if run_tf:
        checks.update(terraform_checks(data["files"]))
    if run_k8s:
        checks.update(kubectl_dry_run(data["files"]))

    st.session_state["checks"] = checks

# ============================================================
# Output Rendering
# ============================================================

bundle = st.session_state.get("bundle")
checks = st.session_state.get("checks", {})

if bundle:
    st.divider()
    st.subheader("Overview")
    st.write(bundle["overview"])

    st.subheader("Next Steps")
    for step in bundle["post_steps"]:
        st.code(step, language="bash")

    files = bundle["files"]

    left, right = st.columns([1, 2])

    with left:
        selected = st.radio("Files", sorted(files.keys()))
        st.download_button(
            "Download ZIP",
            data=make_zip(files),
            file_name="iac_bundle.zip",
            mime="application/zip",
            use_container_width=True,
        )

    with right:
        lang = (
            "hcl"
            if selected.endswith(".tf")
            else "yaml"
            if selected.endswith((".yaml", ".yml"))
            else "markdown"
            if selected.endswith(".md")
            else "bash"
        )
        st.code(files[selected], language=lang)

    if checks:
        st.divider()
        st.subheader("Local Checks")
        for name, out in checks.items():
            with st.expander(name):
                st.code(out)