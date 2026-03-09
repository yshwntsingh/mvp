#!/usr/bin/env python3

import io
import json
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

# ============================================================
# Ollama Defaults
# ============================================================

OLLAMA_URL_DEFAULT = "http://localhost:11434/api/chat"
MODEL_DEFAULT = "qwen2.5-coder:7b"

# ============================================================
# JSON Schema
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
                "powershell",
                "python",
                "bicep",
                "json",
                "groovy",
                "shell",
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
# Prompts
# ============================================================

SYSTEM_GENERATE = """
You are an expert DevOps/IaC agent.
Convert the user's plain-English request into a minimal, runnable IaC bundle.

Hard rules:
- Output MUST be valid JSON only.
- Follow provided JSON schema strictly.
- Use the specified cloud provider.
- Use the specified IaC type.
- If Terraform layout is provided, strictly follow it.
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


def require_admin():
    if "is_admin" not in st.session_state:
        st.session_state.is_admin = False

    if st.session_state.is_admin:
        st.sidebar.success("Logged in as admin")
        if st.sidebar.button("Logout", key="logout_btn"):
            st.session_state.is_admin = False
            st.rerun()
        return

    st.sidebar.subheader("Admin Login")

    user = st.sidebar.text_input("Username", key="admin_user")
    pwd = st.sidebar.text_input("Password", type="password", key="admin_pwd")

    if st.sidebar.button("Login", key="login_btn"):
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

def chat_with_system(system, user, model, url, temperature):
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
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=240) as resp:
        body = resp.read().decode()
        return json.loads(body)["message"]["content"]


# ============================================================
# Helpers
# ============================================================

def parse_json_strict(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return json.loads(text)


def make_zip(files: Dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in files.items():
            z.writestr(path.strip().lstrip("/"), content)
    return buf.getvalue()


# ============================================================
# Streamlit UI
# ============================================================

st.set_page_config(page_title="IaC Agent", layout="wide")
st.title("IaC Agent — English → Terraform / YAML / Bash / CI")

require_admin()

with st.sidebar:
    st.header("Runtime")

    ollama_url = st.text_input(
        "Ollama URL",
        OLLAMA_URL_DEFAULT,
        key="ollama_url",
    )

    model = st.text_input(
        "Model",
        MODEL_DEFAULT,
        key="model",
    )

    temperature = st.slider(
        "Temperature",
        0.0,
        1.0,
        0.2,
        key="temperature",
    )

    st.header("Workflow")
    use_review = st.checkbox(
        "Second-pass review",
        value=True,
        key="use_review",
    )

    st.header("IaC Settings")

    iac_type = st.selectbox(
        "IaC Type",
        ["terraform", "kubernetes", "bash", "github_actions", "mixed"],
        key="iac_type",
    )

    cloud = st.selectbox(
        "Cloud Provider",
        ["aws", "azure", "gcp"],
        key="cloud",
    )

    # Terraform Layout only visible if Terraform selected
    if iac_type == "terraform":
        st.header("Terraform Settings")
        terraform_layout = st.selectbox(
            "Terraform Layout",
            ["single_file", "modules"],
            key="terraform_layout",
        )
    else:
        terraform_layout = None

st.subheader("Describe what you want")
request = st.text_area(
    "Request",
    height=150,
    key="request_text",
)

generate_btn = st.button("Generate", key="generate_btn")

# ============================================================
# Generate
# ============================================================

if generate_btn and request.strip():

    layout_instruction = ""
    if iac_type == "terraform" and terraform_layout:
        layout_instruction = f"\nThe Terraform layout MUST be: {terraform_layout}."

    forced_request = f"""
User request:
{request}

The IaC type MUST be: {iac_type}
The cloud provider MUST be: {cloud}
{layout_instruction}
"""

    with st.spinner("Generating..."):
        raw = chat_with_system(
            SYSTEM_GENERATE,
            forced_request,
            model,
            ollama_url,
            temperature,
        )

    data = parse_json_strict(raw)

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

    st.session_state["bundle"] = data

# ============================================================
# Output Rendering
# ============================================================

bundle = st.session_state.get("bundle")

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
        selected = st.radio(
            "Files",
            sorted(files.keys()),
            key="file_select",
        )

        st.download_button(
            "Download ZIP",
            data=make_zip(files),
            file_name="iac_bundle.zip",
            mime="application/zip",
            key="download_zip",
        )

    with right:
        # Simple language detection
        if selected.endswith(".tf"):
            lang = "hcl"
        elif selected.endswith((".yaml", ".yml")):
            lang = "yaml"
        elif selected.endswith(".md"):
            lang = "markdown"
        else:
            lang = "bash"

        st.code(files[selected], language=lang)