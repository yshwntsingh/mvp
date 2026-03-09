#!/usr/bin/env python3

import io
import json
import os
import re
import zipfile
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Dict, Literal, Tuple
import streamlit as st
import hmac
import hashlib

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
                "powershell",
                "python",
                "bicep",
                "json",
                "groovy",
                "shell",
            ],
        },
        "overview": {"type": "string"},
        "files": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "post_steps": {"type": "array", "items": {"type": "string"}},
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
            st.experimental_rerun()
        return

    st.sidebar.subheader("Admin Login")
    user = st.sidebar.text_input("Username", key="login_user")
    pwd = st.sidebar.text_input("Password", type="password", key="login_pwd")

    if st.sidebar.button("Login"):
        if (
            hmac.compare_digest(user.strip(), ADMIN_USER)
            and hmac.compare_digest(pwd, ADMIN_PASSWORD)
        ):
            st.session_state.is_admin = True
            st.experimental_rerun()
        else:
            st.sidebar.error("Unauthorized")

    st.stop()


# ============================================================
# Ollama Chat
# ============================================================


def chat_with_system(
    system: str, user: str, model: str, ollama_url: str, temperature: float
) -> str:
    payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
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
    except Exception as e:
        # Try to extract JSON object manually
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception as e2:
                raise ValueError(f"Found JSON-like content but failed to parse: {e2}") from e
        raise ValueError("Model did not return valid JSON.") from e


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
st.markdown(
    """
<style>
    .main {
        background-color: #1f2937;  /* dark slate */
        color: white;
    }
    .sidebar .sidebar-content {
        background-color: #111827;  /* darker sidebar */
    }
    /* Scrollbars, buttons etc. can be styled further */
</style>
""",
    unsafe_allow_html=True,
)

st.title("IaC Agent — English → Terraform / YAML / Bash / CI")

require_admin()

with st.sidebar:
    st.header("Runtime")
    ollama_url = st.text_input("Ollama URL", OLLAMA_URL_DEFAULT, key="ollama_url")
    model = st.text_input("Model", MODEL_DEFAULT, key="model")
    temperature = st.slider("Temperature", 0.0, 1.0, 0.2)

    st.header("Cloud Credentials")

    # AWS Panel
    with st.expander("AWS Deployment", expanded=True):
        st.image(
            "https://cdn.iconscout.com/icon/free/png-256/aws-1869025-1583149.png",
            width=100,
        )
        aws_access_key = st.text_input("AWS_ACCESS_KEY_ID", key="aws_access_key")
        aws_secret_key = st.text_input(
            "AWS_SECRET_ACCESS_KEY", type="password", key="aws_secret_key"
        )
        aws_region = st.text_input("AWS_DEFAULT_REGION", "us-east-1", key="aws_region")

    # Azure Panel
    with st.expander("Azure Deployment", expanded=False):
        st.image(
            "https://upload.wikimedia.org/wikipedia/commons/f/fa/Microsoft_Azure_Logo.svg",
            width=100,
        )
        azure_client_id = st.text_input("AZURE_CLIENT_ID", key="azure_client_id")
        azure_tenant_id = st.text_input("AZURE_TENANT_ID", key="azure_tenant_id")
        azure_subscription_id = st.text_input(
            "AZURE_SUBSCRIPTION_ID", key="azure_subscription_id"
        )
        azure_client_secret = st.text_input(
            "AZURE_CLIENT_SECRET", type="password", key="azure_client_secret"
        )

    # GCP Panel
    with st.expander("GCP Deployment", expanded=False):
        st.image(
            "https://upload.wikimedia.org/wikipedia/commons/5/5a/Google_Cloud_logo.svg",
            width=100,
        )
        gcp_sa_path = st.text_input("GCP Service Account JSON Path", key="gcp_sa_path")

st.header("Workflow")
use_review = st.checkbox("Second-pass review", value=True)
run_tf = st.checkbox("Run Terraform checks")
run_k8s = st.checkbox("Run kubectl dry-run")

st.subheader("Describe what you want")
request = st.text_area("Request", height=150)

col1, col2 = st.columns(2)
generate_btn = col1.button("Generate", type="primary", use_container_width=True)
clear_btn = col2.button("Clear", use_container_width=True)

if clear_btn:
    st.session_state.clear()
    st.experimental_rerun()

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
            raw2 = chat_with_system(
                SYSTEM_REVIEW, json.dumps(data), model, ollama_url, 0.0
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
# Terraform Layout Option
# ============================================================

terraform_layout = None
bundle = st.session_state.get("bundle")
checks = st.session_state.get("checks", {})

if bundle and bundle["iac_type"] == "terraform":
    st.subheader("Terraform Layout")
    terraform_layout = st.selectbox(
        "Choose Terraform layout",
        ["Single File", "Modules"],
        key="terraform_layout",
    )

# ============================================================
# Output Rendering
# ============================================================

if bundle:
    st.divider()
    st.subheader("Overview")
    st.write(bundle.get("overview", "Overview not returned"))

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

# ============================================================
# Cloud Deployment
# ============================================================

st.subheader("Deploy to Cloud")
cloud_provider = st.selectbox("Select Cloud Provider", ["Azure", "AWS", "GCP"], key="cloud_provider")
deploy_btn = st.button("Deploy", key="deploy_btn")

def deploy_bundle(bundle, selected_iac, env_vars, terraform_layout=None):
    # Customize deployment commands here depending on layout if needed
    msg = f"Deploying {selected_iac}"
    if selected_iac == "terraform" and terraform_layout:
        msg += f" using layout: {terraform_layout}"
    st.success(msg)
    st.json(env_vars)

if deploy_btn and bundle:
    env_vars = os.environ.copy()
    selected_iac = bundle["iac_type"]

    if cloud_provider == "Azure":
        if not all([azure_client_id, azure_tenant_id, azure_subscription_id, azure_client_secret]):
            st.error("Provide all Azure credentials!")
        else:
            env_vars.update(
                {
                    "AZURE_CLIENT_ID": azure_client_id,
                    "AZURE_TENANT_ID": azure_tenant_id,
                    "AZURE_SUBSCRIPTION_ID": azure_subscription_id,
                    "AZURE_CLIENT_SECRET": azure_client_secret,
                }
            )
            deploy_bundle(bundle, selected_iac, env_vars, terraform_layout)

    elif cloud_provider == "AWS":
        if not all([aws_access_key, aws_secret_key, aws_region]):
            st.error("Provide all AWS credentials!")
        else:
            env_vars.update(
                {
                    "AWS_ACCESS_KEY_ID": aws_access_key,
                    "AWS_SECRET_ACCESS_KEY": aws_secret_key,
                    "AWS_DEFAULT_REGION": aws_region,
                }
            )
            deploy_bundle(bundle, selected_iac, env_vars, terraform_layout)

    elif cloud_provider == "GCP":
        if not gcp_sa_path or not Path(gcp_sa_path).exists():
            st.error("Provide valid GCP Service Account JSON path!")
        else:
            env_vars["GOOGLE_APPLICATION_CREDENTIALS"] = gcp_sa_path
            deploy_bundle(bundle, selected_iac, env_vars, terraform_layout)