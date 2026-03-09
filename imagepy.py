#!/usr/bin/env python3

import io
import json
import os
import re
import zipfile
import subprocess
import tempfile
import urllib.request
import base64
import requests
import hmac
from pathlib import Path
from typing import Dict, Literal

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
    "powershell",
    "python",
    "bicep",
    "json",
    "groovy",
    "shell",
]

Cloud = Literal["aws", "azure", "gcp"]

# ============================================================
# Defaults
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
        "iac_type": {"type": "string"},
        "overview": {"type": "string"},
        "files": {"type": "object"},
        "post_steps": {"type": "array"},
    },
    "required": ["iac_type", "overview", "files", "post_steps"],
}

# ============================================================
# Prompts
# ============================================================

SYSTEM_GENERATE = """
You are an expert DevOps/IaC agent.
Return ONLY valid JSON matching schema.
Prefer secure defaults.
Never include destructive commands.
"""

SYSTEM_REVIEW = """
You are a strict IaC reviewer.
Fix security and correctness.
Return ONLY corrected JSON.
"""

# ============================================================
# Admin Auth
# ============================================================

ADMIN_USER = "admin"
ADMIN_PASSWORD = "Admin@123"

def require_admin():
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

def chat_with_system(system, user, model, ollama_url, temperature):
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
# Diagram → Infra JSON (GPT Vision)
# ============================================================

def extract_architecture_from_image(image_bytes):
    encoded = base64.b64encode(image_bytes).decode()

    system_prompt = """
You are a senior cloud architect.

Return STRICT JSON:
{
  "cloud_provider": "aws | azure | gcp | unknown",
  "region": "string or null",
  "resources": [],
  "relationships": []
}
"""

    payload = {
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Extract infrastructure."},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{encoded}"
                        },
                    },
                ],
            },
        ],
        "max_tokens": 2000,
    }

    headers = {
        "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY')}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=180,
    )

    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]

def convert_to_iac_prompt(data):
    return f"""
Generate production-ready infrastructure.

Cloud: {data.get("cloud_provider")}
Region: {data.get("region")}

Resources:
{json.dumps(data.get("resources", []), indent=2)}

Relationships:
{json.dumps(data.get("relationships", []), indent=2)}

Return structured JSON bundle.
"""

# ============================================================
# JSON Utils
# ============================================================

def parse_json_strict(text):
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*", "", text)
    text = re.sub(r"```$", "", text)
    return json.loads(text)

def validate_bundle(data):
    for k in IAC_BUNDLE_SCHEMA["required"]:
        if k not in data:
            raise ValueError(f"Missing key {k}")

# ============================================================
# ZIP
# ============================================================

def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in files.items():
            z.writestr(path, content)
    return buf.getvalue()

# ============================================================
# UI
# ============================================================

st.set_page_config(page_title="IaC Agent", layout="wide")

st.markdown("""
<style>
.main {background-color:#0f172a; color:white;}
.sidebar .sidebar-content {background-color:#111827;}
</style>
""", unsafe_allow_html=True)

st.title("IaC Agent — English or Diagram → IaC")

require_admin()

with st.sidebar:
    st.header("Runtime")
    ollama_url = st.text_input("Ollama URL", OLLAMA_URL_DEFAULT)
    model = st.text_input("Model", MODEL_DEFAULT)
    temperature = st.slider("Temperature", 0.0, 1.0, 0.2)

# ============================================================
# Manual Mode
# ============================================================

st.subheader("Manual Request")
request = st.text_area("Describe what you want")

generate_btn = st.button("Generate", type="primary")

# ============================================================
# Diagram Mode
# ============================================================

st.divider()
st.subheader("Or Upload Architecture Diagram")

uploaded_file = st.file_uploader(
    "Upload PNG/JPG diagram",
    type=["png", "jpg", "jpeg"],
)

generate_diagram = st.button("Generate from Diagram")

# ============================================================
# Execution
# ============================================================

if generate_btn and request:
    raw = chat_with_system(
        SYSTEM_GENERATE,
        request,
        model,
        ollama_url,
        temperature,
    )
    bundle = parse_json_strict(raw)
    validate_bundle(bundle)
    st.session_state["bundle"] = bundle

if generate_diagram and uploaded_file:
    if not os.environ.get("OPENAI_API_KEY"):
        st.error("Set OPENAI_API_KEY environment variable.")
        st.stop()

    image_bytes = uploaded_file.read()

    with st.spinner("Analyzing diagram..."):
        raw_extract = extract_architecture_from_image(image_bytes)

    structured = parse_json_strict(raw_extract)

    st.success(f"Detected Cloud: {structured.get('cloud_provider')}")
    st.json(structured)

    with st.spinner("Generating IaC..."):
        raw = chat_with_system(
            SYSTEM_GENERATE,
            convert_to_iac_prompt(structured),
            model,
            ollama_url,
            temperature,
        )

    bundle = parse_json_strict(raw)
    validate_bundle(bundle)
    st.session_state["bundle"] = bundle

# ============================================================
# Output
# ============================================================

bundle = st.session_state.get("bundle")

if bundle:
    st.divider()
    st.subheader("Overview")
    st.write(bundle.get("overview"))

    st.subheader("Files")
    files = bundle["files"]

    selected = st.radio("Select file", list(files.keys()))

    st.code(files[selected])

    st.download_button(
        "Download ZIP",
        data=make_zip(files),
        file_name="iac_bundle.zip",
        mime="application/zip",
    )

    st.subheader("Next Steps")
    for step in bundle["post_steps"]:
        st.code(step)