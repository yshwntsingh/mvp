import streamlit as st
import requests
import json
import re
import zipfile
import io

# =====================================================
# CONFIG
# =====================================================

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3"

# =====================================================
# JSON CLEANER
# =====================================================

def clean_llm_json(text):

    # remove markdown
    text = text.replace("```json", "").replace("```", "")

    # extract json block
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        text = match.group(0)

    # replace single quotes
    text = text.replace("'", '"')

    # quote keys
    text = re.sub(r'(\w+):', r'"\1":', text)

    return text


# =====================================================
# SAFE JSON PARSER
# =====================================================

def parse_json_safe(text):

    try:
        cleaned = clean_llm_json(text)
        return json.loads(cleaned)

    except Exception as e:

        st.error("⚠ JSON parsing failed")

        st.code(text)

        return {
            "project_name": "iac_project",
            "files": [
                {
                    "name": "error.txt",
                    "content": text
                }
            ]
        }


# =====================================================
# CALL OLLAMA
# =====================================================

def generate_iac(prompt):

    system_prompt = f"""
Return ONLY valid JSON.

Format:

{{
 "project_name": "iac_project",
 "files":[
   {{
     "name":"main.tf",
     "content":"terraform code"
   }}
 ]
}}

User Request:
{prompt}
"""

    payload = {
        "model": MODEL,
        "prompt": system_prompt,
        "stream": False
    }

    response = requests.post(OLLAMA_URL, json=payload)

    return response.json()["response"]


# =====================================================
# ZIP CREATOR
# =====================================================

def create_zip(bundle):

    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w") as z:

        for file in bundle["files"]:
            z.writestr(file["name"], file["content"])

    zip_buffer.seek(0)

    return zip_buffer


# =====================================================
# STREAMLIT UI
# =====================================================

st.title("⚡ AI Infrastructure Generator")

st.write("Generate Terraform / IaC using AI")

user_prompt = st.text_area(
    "Describe Infrastructure",
    placeholder="Example: Create AWS VPC with EC2 and security group"
)

if st.button("Generate IaC"):

    with st.spinner("Generating infrastructure code..."):

        raw_output = generate_iac(user_prompt)

        bundle = parse_json_safe(raw_output)

        st.success("Infrastructure generated!")

        for file in bundle["files"]:

            st.subheader(file["name"])
            st.code(file["content"], language="hcl")

        zip_file = create_zip(bundle)

        st.download_button(
            label="Download IaC ZIP",
            data=zip_file,
            file_name="iac_project.zip"
        )