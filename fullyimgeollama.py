#!/usr/bin/env python3

import io
import json
import os
import re
import zipfile
import base64
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
    "powershell",
    "python",
    "bicep",
    "json",
    "groovy",
    "shell",
]

Cloud = Literal["aws", "azure", "gcp"]

# ============================================================
# Ollama Defaults
# ============================================================

OLLAMA_URL_DEFAULT = "http://localhost:11434/api/chat"
MODEL_DEFAULT = "qwen2.5-coder:7b"
VISION_MODEL = "llava:latest"
CODE_MODEL = "qwen2.5-coder:7b"

# ============================================================
# JSON Schema
# ============================================================

IAC_BUNDLE_SCHEMA = {
    "required": ["iac_type", "overview", "files", "post_steps"],
}

# ============================================================
# System Prompts
# ============================================================

SYSTEM_GENERATE = """
You are an expert DevOps/IaC agent.

Return ONLY valid JSON following this structure:

{
  "iac_type": "...",
  "overview": "...",
  "files": { "filename": "content" },
  "post_steps": ["step1", "step2"]
}

No markdown.
No explanation.
"""

SYSTEM_REVIEW = """
You are a strict IaC reviewer.
Return ONLY corrected JSON.
No explanation.
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
    user = st.sidebar.text_input("Username", key="login_user")
    pwd = st.sidebar.text_input("Password", type="password", key="login_pwd")

    if st.sidebar.button("Login"):
        import hmac

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

def ollama_chat(model: str, messages: list, temperature: float = 0.2) -> str:
    import urllib.request
    import urllib.error

    payload = {
        "model": model,
        "stream": False,
        "messages": messages,
        "options": {"temperature": temperature},
    }

    req = urllib.request.Request(
        st.session_state.get("ollama_url", OLLAMA_URL_DEFAULT),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = resp.read().decode()
            return json.loads(body)["message"]["content"]

    except urllib.error.HTTPError as e:
        st.error(f"Ollama HTTP Error {e.code}")
        st.code(e.read().decode())
        raise

    except Exception as e:
        st.error(f"Ollama Connection Error: {str(e)}")
        raise


# ============================================================
# JSON Repair to Schema
# ============================================================

def repair_to_schema(raw_text: str) -> dict:
    repair_prompt = f"""
Fix this into STRICT valid JSON following this structure:

{{
  "iac_type": "...",
  "overview": "...",
  "files": {{ "filename": "content" }},
  "post_steps": ["step1"]
}}

Return ONLY valid JSON.

Broken content:
{raw_text}
"""

    repaired = ollama_chat(
        CODE_MODEL,
        [
            {"role": "system", "content": "You are a strict JSON repair engine."},
            {"role": "user", "content": repair_prompt},
        ],
        temperature=0.0,
    )

    return parse_json_strict(repaired)


# ============================================================
# Robust JSON Parser
# ============================================================

def parse_json_strict(text: str) -> dict:
    if not text or not text.strip():
        raise ValueError("Empty model response")

    text = text.strip()
    text = re.sub(r"```json", "", text)
    text = re.sub(r"```", "", text)

    try:
        return json.loads(text)
    except Exception:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass

    st.warning("Attempting automatic JSON repair...")
    return repair_to_schema(text)


# ============================================================
# Safe Validation (No Crash)
# ============================================================

def validate_bundle(data: dict) -> Tuple[bool, str]:
    for k in IAC_BUNDLE_SCHEMA["required"]:
        if k not in data:
            return False, f"Missing key: {k}"

    if not isinstance(data.get("files"), dict) or not data["files"]:
        return False, "files must be non-empty dict"

    if not isinstance(data.get("post_steps"), list):
        return False, "post_steps must be list"

    return True, ""


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
# UI
# ============================================================

st.set_page_config(page_title="Ollama IaC Agent", layout="wide")
st.title("Ollama IaC Agent — English/Diagram → IaC")

require_admin()

with st.sidebar:
    st.header("Runtime")
    st.text_input("Ollama URL", OLLAMA_URL_DEFAULT, key="ollama_url")
    temperature = st.slider("Temperature", 0.0, 1.0, 0.2)

st.header("Workflow")
use_review = st.checkbox("Second-pass review", value=True)

request = st.text_area("Describe your desired infra")
generate_btn = st.button("Generate from text")

uploaded_file = st.file_uploader("Upload architecture diagram", type=["png", "jpg", "jpeg"])
diagram_btn = st.button("Generate from diagram")

# ============================================================
# Generate from Text
# ============================================================

if generate_btn and request.strip():
    with st.spinner("Generating..."):
        raw = ollama_chat(
            MODEL_DEFAULT,
            [
                {"role": "system", "content": SYSTEM_GENERATE},
                {"role": "user", "content": request},
            ],
            temperature,
        )

        data = parse_json_strict(raw)
        valid, err = validate_bundle(data)

        if not valid:
            st.warning(f"Validation failed: {err}")
            data = repair_to_schema(raw)
            valid, err = validate_bundle(data)

            if not valid:
                st.error(f"Still invalid: {err}")
                st.stop()

        st.session_state["bundle"] = data
        st.success("IaC bundle generated!")


# ============================================================
# Generate from Diagram
# ============================================================

if uploaded_file and diagram_btn:
    image_b64 = base64.b64encode(uploaded_file.read()).decode()

    with st.spinner("Extracting architecture..."):
        raw_pass1 = ollama_chat(
            VISION_MODEL,
            [
                {"role": "system", "content": "Return ONLY valid JSON."},
                {"role": "user", "content": "Extract resources and connections.", "images": [image_b64]},
            ],
            temperature=0.0,
        )

        arch = parse_json_strict(raw_pass1)
        st.json(arch)

    with st.spinner("Generating IaC..."):
        raw_pass2 = ollama_chat(
            CODE_MODEL,
            [
                {"role": "system", "content": SYSTEM_GENERATE},
                {"role": "user", "content": json.dumps(arch)},
            ],
            temperature=0.0,
        )

        bundle = parse_json_strict(raw_pass2)
        valid, err = validate_bundle(bundle)

        if not valid:
            st.warning(f"Bundle invalid: {err}")
            bundle = repair_to_schema(raw_pass2)
            valid, err = validate_bundle(bundle)

            if not valid:
                st.error(f"Still invalid after repair: {err}")
                st.stop()

        st.session_state["bundle"] = bundle
        st.success("IaC bundle generated from diagram!")


# ============================================================
# Display Bundle
# ============================================================

bundle = st.session_state.get("bundle")

if bundle:
    st.subheader("Overview")
    st.write(bundle.get("overview", ""))

    st.subheader("Next Steps")
    for step in bundle["post_steps"]:
        st.code(step, language="bash")

    files = bundle["files"]
    selected = st.radio("Files", sorted(files.keys()))

    st.download_button(
        "Download ZIP",
        data=make_zip(files),
        file_name="iac_bundle.zip",
        mime="application/zip",
    )

    st.code(files[selected])


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