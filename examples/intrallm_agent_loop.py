"""Minimal IntraLLM agent loop that gives the model sandbox tools.

Works with any OpenAI-compatible chat endpoint (vLLM, Ollama, TGI, an internal gateway...).

    pip install openai
    export SANDBOX_URL=http://sandbox.intra:8080 SANDBOX_TOKEN=isb_...   # role=agent token
    export LLM_BASE_URL=http://llm.intra/v1 LLM_MODEL=your-model LLM_API_KEY=...
    python examples/intrallm_agent_loop.py alice "用 python 算一下 2**100 并写入 result.txt"
"""

import os
import sys

from openai import OpenAI

from intrallm_sandbox.client import AgentToolkit, SandboxClient

SYSTEM = (
    "You are IntraLLM, an enterprise assistant. You can run code in an isolated Linux sandbox "
    "using the sandbox_* tools. Create one sandbox per task, reuse it, and destroy it when done."
)


def run(user: str, prompt: str) -> str:
    llm = OpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ.get("LLM_API_KEY", "none"))
    kit = AgentToolkit(SandboxClient(os.environ["SANDBOX_URL"], os.environ["SANDBOX_TOKEN"]), owner=user)
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    for _ in range(20):
        resp = llm.chat.completions.create(model=os.environ["LLM_MODEL"], messages=messages, tools=kit.tools)
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            return msg.content
        for call in msg.tool_calls:
            result = kit.call(call.function.name, call.function.arguments)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    return "stopped: too many tool steps"


if __name__ == "__main__":
    print(run(sys.argv[1], sys.argv[2]))
