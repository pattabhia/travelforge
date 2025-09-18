# src/clients/bedrock_agent.py
from __future__ import annotations
import os, json, logging, time
from typing import Dict, Any
import boto3
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)
_client = None
 
def _get_client():
    global _client
    if _client is None:
        region = os.getenv("AWS_REGION", "us-east-1")
        profile = os.getenv("AWS_PROFILE")  # set locally; NOT on Streamlit Cloud
        session = boto3.Session(profile_name=profile, region_name=region) if profile else boto3.Session(region_name=region)
        _client = session.client("bedrock-agent-runtime")
    return _client

def invoke_agent(agent_id: str, agent_alias_id: str, session_id: str, prompt: str) -> Dict[str, Any]:
    client = _get_client()
    # simple retry on throttling
    for attempt in range(4):
        try:
            resp = client.invoke_agent(
                agentId=agent_id,
                agentAliasId=agent_alias_id,
                sessionId=session_id,
                enableTrace=True,
                inputText=prompt,
            )
            break
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in {"ThrottlingException", "TooManyRequestsException"} and attempt < 3:
                time.sleep(0.8 * (2 ** attempt)); continue
            log.error("InvokeAgent failed: %s | %s", code, e)
            raise

    out, cites, trace = "", [], {}
    guard_seen = False
    for ev in resp.get("completion", []):
        if "chunk" in ev:
            ch = ev["chunk"]
            out += ch["bytes"].decode()
            if "attribution" in ch: cites += ch["attribution"].get("citations", [])
        if "trace" in ev:
            t = ev["trace"]["trace"]
            for name in ["guardrailTrace","preProcessingTrace","orchestrationTrace","postProcessingTrace"]:
                if name in t:
                    mapped = ("preGuardrailTrace" if name=="guardrailTrace" and not guard_seen
                              else "postGuardrailTrace" if name=="guardrailTrace" else name)
                    guard_seen = guard_seen or name=="guardrailTrace"
                    trace.setdefault(mapped, []).append(t[name])
    return {"output_text": out, "citations": cites, "trace": trace}
