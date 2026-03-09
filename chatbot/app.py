import streamlit as st
import json
import os
import hashlib
from pathlib import Path

from langchain_community.llms import Ollama
from langchain_community.document_loaders import PyPDFLoader
from langchain_community.vectorstores import Chroma
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.embeddings import OllamaEmbeddings
from langchain.chains import RetrievalQA

from security import *
from agents import *
from audit import *

# ===============================
# CONFIG
# ===============================
st.set_page_config("Enterprise Secure RAG")
st.title("🔐 Enterprise Secure RAG (Qwen)")

# ===============================
# USERS (hashed passwords)
# ===============================
USERS = {
    "admin": hashlib.sha256("admin123".encode()).hexdigest(),
    "user1": hashlib.sha256("user123".encode()).hexdigest(),
}

# ===============================
# SESSION INIT
# ===============================
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

# ===============================
# LOGIN SYSTEM
# ===============================
if not st.session_state.authenticated:
    st.subheader("🔐 Login")

    username = st.text_input("Username")
    password = st.text_input("Password", type="password")

    if st.button("Login"):
        hashed = hashlib.sha256(password.encode()).hexdigest()

        if username in USERS and USERS[username] == hashed:
            st.session_state.authenticated = True
            st.session_state.user = username
            st.success("Login successful")
            st.rerun()
        else:
            st.error("Invalid credentials")

    st.stop()

# ===============================
# LOGOUT BUTTON
# ===============================
col1, col2 = st.columns([8, 1])
with col2:
    if st.button("Logout"):
        st.session_state.clear()
        st.rerun()

# ===============================
# ASSIGN ROLE
# ===============================
user = st.session_state.user
role = "admin" if user == "admin" else "user"

st.success(f"Logged in as: {user} ({role})")

# ===============================
# LLM + EMBEDDINGS
# ===============================
# llm = Ollama(model="qwen2.5:7b")
# embeddings = OllamaEmbeddings(model="qwen2.5:7b")

llm = Ollama(model="qwen2.5:3b")
embeddings = OllamaEmbeddings(model="nomic-embed-text")

# ===============================
# UPLOAD SECTION (ADMIN ONLY)
# ===============================
st.subheader("📄 Upload Document (Admin Only)")

if role == "admin":
    uploaded_file = st.file_uploader("Upload PDF", type=["pdf"])
    sensitivity = st.selectbox(
        "Sensitivity Level",
        ["public", "restricted", "secret"]
    )

    if uploaded_file and st.button("Upload & Index"):
        upload_path = Path("data/uploads") / uploaded_file.name
        upload_path.parent.mkdir(parents=True, exist_ok=True)

        with open(upload_path, "wb") as f:
            f.write(uploaded_file.getbuffer())

        policy_path = "data/policy.json"
        policy = {}

        if os.path.exists(policy_path):
            with open(policy_path) as f:
                policy = json.load(f)

        policy[uploaded_file.name] = sensitivity

        with open(policy_path, "w") as f:
            json.dump(policy, f, indent=2)

        loader = PyPDFLoader(str(upload_path))
        docs = loader.load()

        chunks = RecursiveCharacterTextSplitter(
            chunk_size=800,
            chunk_overlap=100
        ).split_documents(docs)

        vectordb = Chroma(
            persist_directory="vectordb",
            embedding_function=embeddings
        )

        vectordb.add_documents(chunks)
        vectordb.persist()

        st.success("Uploaded, labeled, and indexed successfully")

# ===============================
# LOAD VECTOR DB
# ===============================
vectordb = Chroma(
    persist_directory="vectordb",
    embedding_function=embeddings
)

retriever = vectordb.as_retriever(search_kwargs={"k": 3})

# ===============================
# CHAT SECTION
# ===============================
query = st.chat_input("Ask securely...")

if query:

    # INPUT BLOCK
    if has_blocked_keyword(query):
        log_event(user, role, query, "BLOCKED_INPUT")
        st.error("Blocked keyword detected")
        st.stop()

    docs = retriever.get_relevant_documents(query)

    policy = {}
    if os.path.exists("data/policy.json"):
        with open("data/policy.json") as f:
            policy = json.load(f)

    # RBAC CHECK
    for d in docs:
        source = os.path.basename(d.metadata.get("source", ""))
        sensitivity = policy.get(source, "public")

        if not role_allowed(role, sensitivity):
            log_event(user, role, query, "RBAC_DENY")
            st.error("Access denied by policy")
            st.stop()

    # LLM RESPONSE
    qa = RetrievalQA.from_chain_type(llm=llm, retriever=retriever)
    answer = qa.run(query)

    safe_answer = redact(answer)

    if not verifier_agent(safe_answer):
        log_event(user, role, query, "VERIFIER_FAIL")
        st.error("Response blocked")
        st.stop()

    log_event(user, role, query, "SUCCESS")
    st.write(safe_answer)