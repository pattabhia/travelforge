from pathlib import Path
from dotenv import load_dotenv
import json
import logging
import logging.config
import os
import re
import uuid
import yaml
import streamlit as st

# ---------------------------------------------------------------------------
# Load configuration (works locally and on Streamlit Community Cloud)
# ---------------------------------------------------------------------------

# Load .env from the project root for local/dev
load_dotenv(Path(__file__).parent / ".env")

# If running on Streamlit Cloud and Secrets exist, copy them into env.
# This block is safe locally (it will no-op if no secrets.toml).
try:
    for k, v in st.secrets.items():
        # don't overwrite already-set OS env (like local AWS_PROFILE)
        os.environ.setdefault(k, str(v))
except Exception:
    pass  # no secrets available locally, which is fine

def cfg(key: str, default=None):
    return os.getenv(key, default)

AGENT_ID        = cfg("BEDROCK_AGENT_ID")
AGENT_ALIAS_ID  = cfg("BEDROCK_AGENT_ALIAS_ID", "TSTALIASID")
AWS_REGION      = cfg("AWS_REGION", "us-east-1")
AWS_PROFILE     = cfg("AWS_PROFILE")  # local-only; don't set in Streamlit Cloud
UI_TITLE        = cfg("BEDROCK_AGENT_TEST_UI_TITLE", "Welcome to Hotel Booking Agent")
UI_ICON         = cfg("BEDROCK_AGENT_TEST_UI_ICON")

# Fail fast if not configured
if not AGENT_ID:
    st.error("BEDROCK_AGENT_ID is not set (.env or Streamlit → Settings → Secrets).")
    st.stop()

# Import after env/secrets are in place so the client can pick them up
from src.clients import bedrock_agent_runtime  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
if os.path.exists("logging.yaml"):
    with open("logging.yaml", "r") as file:
        config = yaml.safe_load(file)
        logging.config.dictConfig(config)
else:
    level_name = cfg("LOG_LEVEL", "INFO")
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    logging.basicConfig(level=level)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Streamlit page config
# ---------------------------------------------------------------------------
st.set_page_config(page_title=UI_TITLE, page_icon=UI_ICON, layout="wide")
st.title(UI_TITLE)
st.caption(f"Agent: {AGENT_ID}  |  Alias: {AGENT_ALIAS_ID}  |  Region: {AWS_REGION}  |  Profile: {AWS_PROFILE or '(none)'}")

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
def init_session_state():
    st.session_state.session_id = str(uuid.uuid4())
    st.session_state.messages = []
    st.session_state.citations = []
    st.session_state.trace = {}

if len(st.session_state.items()) == 0:
    init_session_state()

with st.sidebar:
    if st.button("Reset Session"):
        init_session_state()

# ---------------------------------------------------------------------------
# Chat UI
# ---------------------------------------------------------------------------
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"], unsafe_allow_html=True)

if prompt := st.chat_input():
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.write(prompt)

    with st.chat_message("assistant"):
        with st.empty():
            with st.spinner("Invoking Bedrock Agent..."):
                response = bedrock_agent_runtime.invoke_agent(
                    AGENT_ID,
                    AGENT_ALIAS_ID,
                    st.session_state.session_id,
                    prompt
                )
            output_text = response["output_text"]

            # unwrap {"instruction": "...", "result": "..."} envelopes, if present
            try:
                output_json = json.loads(output_text, strict=False)
                if isinstance(output_json, dict) and "result" in output_json:
                    output_text = output_json["result"]
            except json.JSONDecodeError:
                pass

            # Citations
            if len(response["citations"]) > 0:
                citation_num = 1
                output_text = re.sub(r"%\[(\d+)\]%", r"<sup>[\1]</sup>", output_text)
                citation_locs = ""
                for citation in response["citations"]:
                    for retrieved_ref in citation.get("retrievedReferences", []):
                        s3 = retrieved_ref.get("location", {}).get("s3Location", {})
                        if "uri" in s3:
                            citation_locs += f"\n<br>[{citation_num}] {s3['uri']}"
                        citation_num += 1
                output_text += f"\n{citation_locs}"

            st.session_state.messages.append({"role": "assistant", "content": output_text})
            st.session_state.citations = response["citations"]
            st.session_state.trace = response["trace"]
            st.markdown(output_text, unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Trace sidebar
# ---------------------------------------------------------------------------
trace_types_map = {
    "Pre-Processing": ["preGuardrailTrace", "preProcessingTrace"],
    "Orchestration": ["orchestrationTrace"],
    "Post-Processing": ["postProcessingTrace", "postGuardrailTrace"],
}
trace_info_types_map = {
    "preProcessingTrace": ["modelInvocationInput", "modelInvocationOutput"],
    "orchestrationTrace": ["invocationInput", "modelInvocationInput", "modelInvocationOutput", "observation", "rationale"],
    "postProcessingTrace": ["modelInvocationInput", "modelInvocationOutput", "observation"],
}

with st.sidebar:
    st.title("Trace")
    step_num = 1
    for header, names in trace_types_map.items():
        st.subheader(header)
        has_trace = False
        for tname in names:
            if tname in st.session_state.trace:
                has_trace = True
                trace_steps = {}
                for trace in st.session_state.trace[tname]:
                    if tname in trace_info_types_map:
                        for info_type in trace_info_types_map[tname]:
                            if info_type in trace:
                                trace_id = trace[info_type]["traceId"]
                                trace_steps.setdefault(trace_id, []).append(trace)
                                break
                    else:
                        trace_id = trace.get("traceId")
                        if trace_id:
                            trace_steps.setdefault(trace_id, []).append({tname: trace})
                for trace_id, items in trace_steps.items():
                    with st.expander(f"Trace Step {step_num}", expanded=False):
                        for item in items:
                            st.code(json.dumps(item, indent=2), language="json", line_numbers=True, wrap_lines=True)
                    step_num += 1
        if not has_trace:
            st.text("None")

    st.subheader("Citations")
    if len(st.session_state.citations) > 0:
        citation_num = 1
        for citation in st.session_state.citations:
            for retrieved_ref in citation.get("retrievedReferences", []):
                with st.expander(f"Citation [{citation_num}]", expanded=False):
                    citation_str = json.dumps(
                        {"generatedResponsePart": citation.get("generatedResponsePart"), "retrievedReference": retrieved_ref},
                        indent=2,
                    )
                    st.code(citation_str, language="json", line_numbers=True, wrap_lines=True)
                citation_num += 1
    else:
        st.text("None")
