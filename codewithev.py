#!/usr/bin/env python3

import io
import json
import os
import re
import zipfile
import subprocess
import tempfile
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
    "powershell",
    "python",
    "bicep",
    "json",
    "groovy",
    "shell",
    "mixed"
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
        "iac_type": {"type": "string", "enum": list(IaCType.__args__)},
        "overview": {"type": "string"},
        "files": {"type": "object", "additionalProperties": {"type": "string"}},
        "post_steps": {"type": "array", "items": {"type": "string"}}
    },
    "required": ["iac_type", "overview", "files", "post_steps"]
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
        if st.sidebar.button("Logout", key="logout_btn"):
            st.session_state.is_admin = False
            st.rerun()
        return

    st.sidebar.subheader("Admin Login")
    user = st.sidebar.text_input("Username", key="admin_user")
    pwd = st.sidebar.text_input("Password", type="password", key="admin_pwd")

    if st.sidebar.button("Login", key="login_btn"):
        if (
            user.strip() == ADMIN_USER and
            pwd == ADMIN_PASSWORD
        ):
            st.session_state.is_admin = True
            st.rerun()
        else:
            st.sidebar.error("Unauthorized")

    st.stop()

# ============================================================
# Ollama Chat
# ============================================================

import urllib.request

def chat_with_system(system: str, user: str, model: str, ollama_url: str, temperature: float) -> str:
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "format": IAC_BUNDLE_SCHEMA,
        "options": {"temperature": temperature}
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
    except Exception as e:
        raise ValueError(f"Found JSON-like content but failed to parse: {e}")

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
# Streamlit UI
# ============================================================

st.set_page_config(page_title="Free IaC Agent", layout="wide")
st.title("IaC Agent — English → IaC Bundle")

require_admin()

with st.sidebar:
    st.header("Runtime Settings")
    ollama_url = st.text_input("Ollama URL", OLLAMA_URL_DEFAULT, key="ollama_url")
    model = st.text_input("Model", MODEL_DEFAULT, key="model")
    temperature = st.slider("Temperature", 0.0, 1.0, 0.2, key="temperature")

    st.header("Workflow")
    use_review = st.checkbox("Second-pass review", value=True, key="review_checkbox")
    run_tf = st.checkbox("Run Terraform checks", key="tf_checkbox")
    run_k8s = st.checkbox("Run kubectl dry-run", key="k8s_checkbox")

    st.subheader("Cloud Target")
    selected_cloud = st.selectbox("Select Cloud", options=["azure","aws","gcp"], key="cloud_select")

st.subheader("Select IaC Type")
selected_iac = st.selectbox("IaC Type", options=list(IaCType.__args__), key="iac_type_select")

st.subheader("Describe your request")
request = st.text_area("Request", height=150, key="request_text")

col1, col2 = st.columns(2)
generate_btn = col1.button("Generate", type="primary", use_container_width=True, key="generate_btn")
clear_btn = col2.button("Clear", use_container_width=True, key="clear_btn")

if clear_btn:
    st.session_state.clear()
    st.rerun()

# ============================================================
# Generate
# ============================================================

if generate_btn and request.strip():
    with st.spinner("Generating..."):
        raw = chat_with_system(SYSTEM_GENERATE, request, model, ollama_url, temperature)
        data = parse_json_strict(raw)
        validate_bundle(data)

        if use_review:
            with st.spinner("Reviewing..."):
                raw2 = chat_with_system(SYSTEM_REVIEW, json.dumps(data), model, ollama_url, 0.0)
                data = parse_json_strict(raw2)
                validate_bundle(data)

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
    left, right = st.columns([1,2])
    with left:
        selected_file = st.radio("Files", sorted(files.keys()), key="file_select")
        st.download_button(
            "Download ZIP",
            data=make_zip(files),
            file_name="iac_bundle.zip",
            mime="application/zip",
            use_container_width=True,
        )
    with right:
        lang = (
            "hcl" if selected_file.endswith(".tf") else
            "yaml" if selected_file.endswith((".yaml", ".yml")) else
            "bash" if selected_file.endswith(".sh") else
            "python" if selected_file.endswith(".py") else
            "powershell" if selected_file.endswith(".ps1") else
            "json" if selected_file.endswith(".json") else
            "groovy" if selected_file.endswith(".groovy") else
            "text"
        )
        st.code(files[selected_file], language=lang)

# ============================================================
# Deployment Section (Azure, AWS, GCP)
# ============================================================

if bundle:
    if selected_cloud=="azure":
        st.subheader("Azure Deployment")
        subscription = st.text_input("AZURE_SUBSCRIPTION_ID", key="azure_sub")
        resource_group = st.text_input("AZURE_RESOURCE_GROUP", key="azure_rg")
        deploy_btn = st.button("Deploy to Azure", key="azure_deploy_btn")
        if deploy_btn:
            st.info("Azure deployment placeholder (use az CLI with env vars)")
    elif selected_cloud=="aws":
        st.subheader("AWS Deployment")
        AWS_ACCESS_KEY_ID = st.text_input("AWS_ACCESS_KEY_ID", key="aws_access_key")
        AWS_SECRET_ACCESS_KEY = st.text_input("AWS_SECRET_ACCESS_KEY", type="password", key="aws_secret_key")
        AWS_DEFAULT_REGION = st.text_input("AWS_DEFAULT_REGION", value="us-east-1", key="aws_region")
        deploy_btn = st.button("Deploy to AWS", key="aws_deploy_btn")
        if deploy_btn:
            st.info("AWS deployment placeholder (Terraform apply with credentials)")
    elif selected_cloud=="gcp":
        st.subheader("GCP Deployment")
        gcp_key_file = st.file_uploader("Upload GCP Service Account JSON Key", type="json", key="gcp_key")
        deploy_btn = st.button("Deploy to GCP", key="gcp_deploy_btn")
        if deploy_btn:
            if gcp_key_file:
                st.info("GCP deployment placeholder (Terraform apply with GOOGLE_APPLICATION_CREDENTIALS)")
            else:
                st.error("GCP JSON key required")