import boto3
import json
from typing import List, Dict
import streamlit as st

### streamlit code
st.title("Code Llama")
st.write("This is a demo of the Code Llama model.")

role = st.selectbox("Select a role", ["Developer", "Tech Documentation", "Reviewer", "Unit Test Creator"])
question = st.text_area("Enter your question", key="question")

### sagemaker code
client = boto3.client("sagemaker-runtime")
endpoint13b_name = 'jumpstart-dft-llama-codellama-13b-i-20240210-203037'

def query_endpoint(payload):
    client = boto3.client('runtime.sagemaker')
    response = client.invoke_endpoint(
        EndpointName=endpoint13b_name,
        ContentType='application/json',
        Body=json.dumps(payload).encode('utf-8'),
    )
    response = response["Body"].read().decode("utf8")
    response = json.loads(response)
    return response

def format_instructions(instructions: List[Dict[str, str]]) -> List[str]:
    """Format instructions for Code Llama.
    
    The model only supports 'system', 'user' and 'assistant' roles, starting with 'system', then 'user' and 
    alternating (u/a/u/a/u...). The last message must be from 'user'.
    """
    prompt: List[str] = []

    if instructions[0]["role"] == "system":
        content = "".join(["<<SYS>>\n", instructions[0]["content"], "\n<</SYS>>\n\n", instructions[1]["content"]])
        instructions = [{"role": instructions[1]["role"], "content": content}] + instructions[2:]

    for user, answer in zip(instructions[::2], instructions[1::2]):
        prompt.extend(["<s>", "[INST] ", (user["content"]).strip(), " [/INST] ", (answer["content"]).strip(), "</s>"])

    prompt.extend(["<s>", "[INST] ", (instructions[-1]["content"]).strip(), " [/INST] "])

    return "".join(prompt)

if role == "Developer":
    messages = [
        {
                "role": "system",
                "content": "You are an expert programmer that helps to write Python code based on the user request, with concise explanations. Don't be too verbose.",
        },
        {
                "role": "user",
                "content": question ,
        }
    ]
elif role == "Tech Documentation":
    messages = [
        {
                "role": "system",
                "content": "You are a programmer that helps to write technical documentation based on the user request, with concise explanations. Don't be too verbose.",
        },
        {
                "role": "user",
                "content": question ,
        }
    ]
elif role == "Reviewer":
    messages = [
        {
                "role": "system",
                "content": "You are a programmer that helps to write code reviews based on the user request, with concise explanations. Don't be too verbose.",
        },
        {
                "role": "user",
                "content": question ,
        }
    ]
elif role == "Unit Test Creator":
    messages = [
        {
                "role": "system",
                "content": "You are a programmer that helps to write unit tests based on the user request, with concise explanations. Don't be too verbose.",
        },
        {
                "role": "user",
                "content": question ,
        }
    ]

if question:
    prompt = format_instructions(messages)
    payload = {"inputs": prompt, "parameters": {"max_new_tokens": 256, "temperature": 0.2, "top_p": 0.9}}
    response = query_endpoint(payload)


    st.code(response[0]['generated_text'])