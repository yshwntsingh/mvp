
#!/usr/bin/env python3

import io
import json
import re
import zipfile
import base64
import hmac
from typing import Dict

import requests
import streamlit as st

# =====================================================
# CONFIG
# =====================================================

OLLAMA_URL = "http://localhost:11434/api/chat"

CODE_MODEL = "qwen2.5-coder:7b"
VISION_MODEL = "llava"

ADMIN_USER = "admin"
ADMIN_PASSWORD = "Admin@123"

# =====================================================
# ADMIN LOGIN
# =====================================================

def require_admin():

    if "is_admin" not in st.session_state:
        st.session_state.is_admin = False

    if st.session_state.is_admin:
        st.sidebar.success("Admin logged in")

        if st.sidebar.button("Logout"):
            st.session_state.is_admin = False
            st.rerun()

        return

    st.sidebar.subheader("Admin Login")

    user = st.sidebar.text_input("Username")
    pwd = st.sidebar.text_input("Password", type="password")

    if st.sidebar.button("Login"):

        if (
            hmac.compare_digest(user, ADMIN_USER)
            and hmac.compare_digest(pwd, ADMIN_PASSWORD)
        ):
            st.session_state.is_admin = True
            st.rerun()

        else:
            st.sidebar.error("Unauthorized")

    st.stop()

# =====================================================
# OLLAMA CHAT
# =====================================================

def ollama_chat(model, messages, images=None, temperature=0.2):

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }

    if images:
        payload["images"] = images

    r = requests.post(
        OLLAMA_URL,
        json=payload,
        timeout=600,
    )

    r.raise_for_status()

    data = r.json()

    return data["message"]["content"]

# =====================================================
# JSON PARSER
# =====================================================

def parse_json_strict(text):

    text = text.strip()

    text = re.sub(r"```json", "", text)
    text = re.sub(r"```", "", text)

    try:
        return json.loads(text)

    except:

        match = re.search(r"\{.*\}", text, re.DOTALL)

        if match:
            return json.loads(match.group(0))

    raise ValueError("Model returned invalid JSON")

# =====================================================
# ZIP CREATOR
# =====================================================

def make_zip(files: Dict[str, str]):

    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:

        for path, content in files.items():

            safe = path.strip().lstrip("/").replace("\\", "/")

            z.writestr(safe, content)

    return buf.getvalue()

# =====================================================
# STREAMLIT UI
# =====================================================

st.set_page_config(layout="wide")

st.title("DevOps AI Platform")

require_admin()

# =====================================================
# SIDEBAR SETTINGS
# =====================================================

st.sidebar.header("AI Settings")

temperature = st.sidebar.slider(
    "Temperature",
    0.0,
    1.0,
    0.2
)

selected_cloud = st.sidebar.selectbox(
    "Cloud",
    ["aws","azure","gcp"]
)

# =====================================================
# DEVOPS COPILOT CHAT
# =====================================================

st.header("DevOps Copilot")

if "chat" not in st.session_state:
    st.session_state.chat = []

user_msg = st.chat_input("Ask DevOps question")

if user_msg:

    st.session_state.chat.append(("user", user_msg))

    response = ollama_chat(

        CODE_MODEL,

        [
            {"role":"system","content":"You are an expert DevOps engineer"},
            {"role":"user","content":user_msg}
        ],

        temperature=temperature

    )

    st.session_state.chat.append(("ai", response))

for role,msg in st.session_state.chat:

    if role=="user":
        st.chat_message("user").write(msg)
    else:
        st.chat_message("assistant").write(msg)

# =====================================================
# TEXT → IAC
# =====================================================

st.divider()

st.header("Generate Infrastructure from Text")

request = st.text_area("Describe infrastructure")

if st.button("Generate IaC"):

    with st.spinner("Generating infrastructure..."):

        prompt = f"""
Return STRICT JSON:

{{
"overview":"",
"files":{{"filename":"content"}},
"post_steps":[]
}}

Generate {selected_cloud} infrastructure for:

{request}
"""

        raw = ollama_chat(

            CODE_MODEL,

            [
                {"role":"system","content":"Expert DevOps engineer"},
                {"role":"user","content":prompt}
            ]

        )

        bundle = parse_json_strict(raw)

        st.session_state.bundle = bundle

# =====================================================
# DIAGRAM → IAC
# =====================================================

st.divider()

st.header("Generate Infrastructure from Diagram")

uploaded = st.file_uploader(
    "Upload architecture diagram",
    type=["png","jpg","jpeg"]
)

if uploaded and st.button("Analyze Diagram"):

    image_b64 = base64.b64encode(uploaded.read()).decode()

    with st.spinner("Analyzing architecture..."):

        raw_arch = ollama_chat(

            VISION_MODEL,

            [
                {"role":"system","content":"Analyze cloud architecture diagrams"},
                {"role":"user","content":"Extract resources and connections and return JSON"}
            ],

            images=[image_b64],
            temperature=0

        )

        arch = parse_json_strict(raw_arch)

        st.json(arch)

    with st.spinner("Generating IaC..."):

        raw_iac = ollama_chat(

            CODE_MODEL,

            [
                {"role":"system","content":"Generate infrastructure code"},
                {"role":"user","content":json.dumps(arch)}
            ]

        )

        bundle = parse_json_strict(raw_iac)

        st.session_state.bundle = bundle

# =====================================================
# SHOW GENERATED FILES
# =====================================================

bundle = st.session_state.get("bundle")

if bundle:

    st.divider()

    st.header("Generated Infrastructure")

    st.subheader("Overview")

    st.write(bundle.get("overview",""))

    st.subheader("Next Steps")

    for step in bundle["post_steps"]:
        st.code(step)

    files = bundle["files"]

    left,right = st.columns([1,2])

    with left:

        selected_file = st.radio(
            "Files",
            list(files.keys())
        )

        st.download_button(

            "Download ZIP",

            data=make_zip(files),

            file_name="iac_bundle.zip",

            mime="application/zip"

        )

    with right:

        st.code(files[selected_file])
