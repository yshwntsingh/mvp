#!/usr/bin/env python3

import io
import json
import zipfile
import subprocess
import tempfile
import os
from pathlib import Path
from typing import Dict, Literal, Tuple
import streamlit as st
import urllib.request

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
TerraformLayout = Literal["single_file", "modules"]
KubernetesLayout = Literal["single_file", "multi_manifest"]

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
        if user.strip() == ADMIN_USER and pwd == ADMIN_PASSWORD:
            st.session_state.is_admin = True
            st.rerun()
        else:
            st.sidebar.error("Unauthorized")
    st.stop()

# ============================================================
# Ollama Chat
# ============================================================

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
        text = text.strip("```")
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
# ZIP Creation with folder structure
# ============================================================

def make_zip(files: Dict[str, str], iac_type: str, layout: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in files.items():
            safe = path.strip().replace("\\", "/").lstrip("/")
            if iac_type == "terraform" and layout == "modules":
                safe = f"modules/{safe}"
            elif iac_type == "kubernetes" and layout == "multi_manifest":
                safe = f"manifests/{safe}"
            z.writestr(safe, content)
    return buf.getvalue()

# ============================================================
# Subprocess Utils
# ============================================================

def run_cmd(cmd, cwd: str, env_vars=None) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env_vars)
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        return p.returncode, out.strip()
    except FileNotFoundError:
        return 127, f"Command not found: {cmd[0]}"

# ============================================================
# Deployment Helper
# ============================================================

def deploy_bundle(bundle: dict, iac_type: str, env_vars: dict):
    with tempfile.TemporaryDirectory(prefix="iac_deploy_") as td:
        base = Path(td)
        for path, content in bundle["files"].items():
            p = base / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)

        if iac_type == "terraform":
            outputs = {}
            for cmd_name, cmd in {
                "terraform init": ["terraform", "init", "-input=false"],
                "terraform plan": ["terraform", "plan"],
                "terraform apply": ["terraform", "apply", "-auto-approve"]
            }.items():
                code, out = run_cmd(cmd, cwd=str(base), env_vars=env_vars)
                outputs[cmd_name] = f"exit={code}\n{out}"

            st.subheader(f"{iac_type.capitalize()} Deployment Output")
            for name, out in outputs.items():
                with st.expander(name):
                    st.code(out)

        elif iac_type == "kubernetes":
            code, out = run_cmd(["kubectl", "apply", "-f", str(base)], cwd=str(base), env_vars=env_vars)
            st.subheader(f"{iac_type.capitalize()} Deployment Output")
            st.code(f"exit={code}\n{out}")

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

st.subheader("Select IaC Type")
selected_iac = st.selectbox("IaC Type", options=list(IaCType.__args__), key="iac_type_select")

# Layout selection
terraform_layout = None
kubernetes_layout = None
if selected_iac == "terraform":
    terraform_layout = st.selectbox("Terraform Layout", options=list(TerraformLayout.__args__),
                                    format_func=lambda x: "Single file" if x=="single_file" else "Modules",
                                    key="tf_layout")
if selected_iac == "kubernetes":
    kubernetes_layout = st.selectbox("Kubernetes Layout", options=list(KubernetesLayout.__args__),
                                     format_func=lambda x: "Single file" if x=="single_file" else "Multiple manifests",
                                     key="k8s_layout")

st.subheader("Describe your request")
request = st.text_area("Request", height=150, key="request_text")

generate_btn = st.button("Generate", key="generate_btn")
clear_btn = st.button("Clear", key="clear_btn")

if clear_btn:
    st.session_state.clear()
    st.rerun()

# ============================================================
# Generate Bundle
# ============================================================

if generate_btn and request.strip():
    forced_request = request
    if selected_iac == "terraform" and terraform_layout:
        forced_request += f"\nGenerate Terraform as: {terraform_layout}"
    if selected_iac == "kubernetes" and kubernetes_layout:
        forced_request += f"\nGenerate Kubernetes YAML as: {kubernetes_layout}"

    with st.spinner("Generating..."):
        raw = chat_with_system(SYSTEM_GENERATE, forced_request, model, ollama_url, temperature)
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
        layout_for_zip = terraform_layout or kubernetes_layout or ""
        st.download_button(
            "Download ZIP",
            data=make_zip(files, bundle["iac_type"], layout_for_zip),
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
# Multi-Cloud Deployment
# ============================================================

if bundle and selected_iac in ["terraform", "kubernetes"]:
    st.divider()
    st.subheader("Deploy to Cloud")

    cloud_provider = st.selectbox("Select Cloud Provider", ["Azure", "AWS", "GCP"], key="cloud_provider")
    env_vars = os.environ.copy()

    if cloud_provider == "Azure":
        with st.expander("Azure Credentials"):
            azure_client_id = st.text_input("AZURE_CLIENT_ID", key="azure_client_id")
            azure_tenant_id = st.text_input("AZURE_TENANT_ID", key="azure_tenant_id")
            azure_subscription_id = st.text_input("AZURE_SUBSCRIPTION_ID", key="azure_subscription_id")
            azure_client_secret = st.text_input("AZURE_CLIENT_SECRET", type="password", key="azure_client_secret")
        if st.button("Deploy to Azure", key="deploy_azure_btn"):
            if not all([azure_client_id, azure_tenant_id, azure_subscription_id, azure_client_secret]):
                st.error("Provide all Azure credentials!")
            else:
                env_vars.update({
                    "AZURE_CLIENT_ID": azure_client_id,
                    "AZURE_TENANT_ID": azure_tenant_id,
                    "AZURE_SUBSCRIPTION_ID": azure_subscription_id,
                    "AZURE_CLIENT_SECRET": azure_client_secret
                })
                st.info("Deploying to Azure...")
                deploy_bundle(bundle, selected_iac, env_vars)

    elif cloud_provider == "AWS":
        with st.expander("AWS Credentials"):
            aws_access_key = st.text_input("AWS_ACCESS_KEY_ID", key="aws_access_key")
            aws_secret_key = st.text_input("AWS_SECRET_ACCESS_KEY", type="password", key="aws_secret_key")
            aws_region = st.text_input("AWS_REGION", key="aws_region")
        if st.button("Deploy to AWS", key="deploy_aws_btn"):
            if not all([aws_access_key, aws_secret_key, aws_region]):
                st.error("Provide all AWS credentials!")
            else:
                env_vars.update({
                    "AWS_ACCESS_KEY_ID": aws_access_key,
                    "AWS_SECRET_ACCESS_KEY": aws_secret_key,
                    "AWS_REGION": aws_region
                })
                st.info("Deploying to AWS...")
                deploy_bundle(bundle, selected_iac, env_vars)

    elif cloud_provider == "GCP":
        with st.expander("GCP Credentials"):
            gcp_sa_path = st.text_input("Path to Service Account JSON", key="gcp_sa_path")
        if st.button("Deploy to GCP", key="deploy_gcp_btn"):
            if not gcp_sa_path or not Path(gcp_sa_path).exists():
                st.error("Provide valid GCP Service Account JSON path!")
            else:
                env_vars["GOOGLE_APPLICATION_CREDENTIALS"] = gcp_sa_path
                st.info("Deploying to GCP...")
                deploy_bundle(bundle, selected_iac, env_vars)