# apps/streamlit/app.py — Hotel Booking Agent (Streamlit + Bedrock Agents)
from __future__ import annotations

import os, re, json, uuid, logging, logging.config, sys
from pathlib import Path
import streamlit as st
import yaml

# -----------------------------------------------------------------------------
# 0) Locate project root reliably and make <root>/src importable
# -----------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent  # .../apps/streamlit

def _find_project_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "src").exists():
            return p
    # fallback: two levels up (apps/streamlit -> apps -> <root>)
    return start.parents[1]

ROOT = _find_project_root(HERE)
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

# -----------------------------------------------------------------------------
# 1) Load .env (before any getenv) and copy Secrets->env if present
# -----------------------------------------------------------------------------
def _load_env() -> Path | None:
    try:
        from dotenv import load_dotenv
    except Exception:
        return None
    for p in [HERE / ".env", HERE.parent / ".env", ROOT / ".env", Path.cwd() / ".env"]:
        if p.exists():
            load_dotenv(p, override=False)
            return p
    return None

_ENV_PATH = _load_env()

try:
    for k, v in st.secrets.items():
        os.environ.setdefault(k, str(v))
except Exception:
    pass

def cfg(k: str, d=None): return os.getenv(k, d)

AGENT_ID        = cfg("BEDROCK_AGENT_ID")
AGENT_ALIAS_ID  = cfg("BEDROCK_AGENT_ALIAS_ID", "TSTALIASID")
AWS_REGION      = cfg("AWS_REGION", "us-east-1")
AWS_PROFILE     = cfg("AWS_PROFILE")  # local only; don't set on Streamlit Cloud
UI_TITLE        = cfg("BEDROCK_AGENT_TEST_UI_TITLE", "Welcome to Hotel Booking Agent")
UI_ICON         = cfg("BEDROCK_AGENT_TEST_UI_ICON")
LOG_LEVEL_NAME  = cfg("LOG_LEVEL", "INFO")

# -----------------------------------------------------------------------------
# 2) Import your client from src/clients/
# -----------------------------------------------------------------------------
try:
    from clients.bedrock_agent_runtime import invoke_agent as _invoke_agent
except Exception as e:
    raise ImportError(
        "Could not import clients.bedrock_agent_runtime. "
        "Ensure the file exists at src/clients/bedrock_agent_runtime.py "
        "and that packages have __init__.py files (run: touch src/__init__.py src/clients/__init__.py)."
    ) from e

# -----------------------------------------------------------------------------
# 3) Logging
# -----------------------------------------------------------------------------
log_cfg = ROOT / "config" / "logging_config.yaml"
if log_cfg.exists():
    with open(log_cfg, "r") as f:
        logging.config.dictConfig(yaml.safe_load(f))
else:
    level = getattr(logging, str(LOG_LEVEL_NAME).upper(), logging.INFO)
    logging.basicConfig(level=level)
logger = logging.getLogger("app")

# -----------------------------------------------------------------------------
# 4) Streamlit UI
# -----------------------------------------------------------------------------
st.set_page_config(page_title=UI_TITLE, page_icon=UI_ICON, layout="wide")
st.title(UI_TITLE)

if not AGENT_ID:
    st.error("BEDROCK_AGENT_ID is not set (.env or Streamlit → Settings → Secrets).")
    st.stop()

st.caption(
    f"Agent: {AGENT_ID} | Alias: {AGENT_ALIAS_ID} | Region: {AWS_REGION} | "
    f"Profile: {AWS_PROFILE or '(none)'} | .env: {(_ENV_PATH and str(_ENV_PATH)) or '(none)'} | "
    f"ROOT: {ROOT} (src exists: {SRC.exists()})"
)

def init_state():
    st.session_state.session_id = str(uuid.uuid4())
    st.session_state.messages = []
    st.session_state.citations = []
    st.session_state.trace = {}

if not st.session_state.get("session_id"):
    init_state()

with st.sidebar:
    if st.button("Reset Session"):
        init_session = init_state()

# History
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"], unsafe_allow_html=True)

# Chat
if prompt := st.chat_input():
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"): st.write(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Invoking Bedrock Agent..."):
            resp = _invoke_agent(AGENT_ID, AGENT_ALIAS_ID, st.session_state.session_id, prompt)

        out = resp.get("output_text", "")
        try:
            obj = json.loads(out, strict=False)
            if isinstance(obj, dict) and "result" in obj:
                out = obj["result"]
        except json.JSONDecodeError:
            pass

        cites = resp.get("citations") or []
        if cites:
            out = re.sub(r"%\[(\d+)\]%", r"<sup>[\1]</sup>", out)
            lines, n = [], 1
            for c in cites:
                for ref in c.get("retrievedReferences", []):
                    s3 = (ref.get("location") or {}).get("s3Location") or {}
                    if "uri" in s3:
                        lines.append(f"[{n}] {s3['uri']}")
                    n += 1
            if lines:
                out += "\n<br>" + "<br>".join(lines)

        st.session_state.messages.append({"role": "assistant", "content": out})
        st.session_state.citations = cites
        st.session_state.trace = resp.get("trace") or {}
        st.markdown(out, unsafe_allow_html=True)

# Trace sidebar
trace_groups = {
    "Pre-Processing": ["preGuardrailTrace", "preProcessingTrace"],
    "Orchestration": ["orchestrationTrace"],
    "Post-Processing": ["postProcessingTrace", "postGuardrailTrace"],
}
trace_info = {
    "preProcessingTrace": ["modelInvocationInput", "modelInvocationOutput"],
    "orchestrationTrace": ["invocationInput", "modelInvocationInput", "modelInvocationOutput", "observation", "rationale"],
    "postProcessingTrace": ["modelInvocationInput", "modelInvocationOutput", "observation"],
}
with st.sidebar:
    st.subheader("Trace")
    step = 1
    for header, kinds in trace_groups.items():
        st.caption(header)
        found = False
        for kind in kinds:
            if kind in st.session_state.trace:
                found = True
                steps = {}
                for t in st.session_state.trace[kind]:
                    if kind in trace_info:
                        for info in trace_info[kind]:
                            if info in t:
                                tid = t[info]["traceId"]; steps.setdefault(tid, []).append(t); break
                    else:
                        tid = t.get("traceId")
                        if tid: steps.setdefault(tid, []).append({kind: t})
                for tid, items in steps.items():
                    with st.expander(f"Trace Step {step}", expanded=False):
                        for it in items:
                            st.code(json.dumps(it, indent=2), language="json", line_numbers=True, wrap_lines=True)
                    step += 1
        if not found:
            st.text("None")

    st.subheader("Citations")
    if st.session_state.citations:
        n = 1
        for c in st.session_state.citations:
            for ref in c.get("retrievedReferences", []):
                with st.expander(f"Citation [{n}]", expanded=False):
                    st.code(json.dumps(
                        {"generatedResponsePart": c.get("generatedResponsePart"), "retrievedReference": ref}, indent=2
                    ), language="json", line_numbers=True, wrap_lines=True)
                n += 1
    else:
        st.text("None")
