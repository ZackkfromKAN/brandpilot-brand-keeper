from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, List, Optional, TypedDict, NotRequired

from jinja2 import Template
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages


# --- Load internal brand truth (manual) ---
BRAND_MANUAL = Path("src/brandpilot/prompts/brand_manual.md").read_text(encoding="utf-8")

# --- Load prompts (your strict prompting) ---
CONTENT_ANALYZER_PROMPT = Path("src/brandpilot/prompts/content_analyzer.md").read_text(encoding="utf-8")
BRAND_CRITIC_PROMPT = Path("src/brandpilot/prompts/brand_critic.md").read_text(encoding="utf-8")
BRAND_VOICE_PROMPT = Path("src/brandpilot/prompts/brand_voice.md").read_text(encoding="utf-8")


class State(TypedDict):
    # Required: what user submits
    messages: Annotated[list, add_messages]
    image_urls: NotRequired[List[str]]   # can be [] or omitted
    brand_name: NotRequired[str]         # optional; default "Antwerpen"
    brand_voice_output: NotRequired[str]
 

    # Produced by nodes
    content_analysis_json: NotRequired[dict]
    brand_critique_json: NotRequired[dict]


# ✅ Run all agents on GPT-5.2 Chat (supports text + image input)
# Use the alias so you stay on the current GPT-5.2 chat snapshot.
llm = ChatOpenAI(
    model="gpt-5.2-chat-latest",
    # JSON mode: forces valid JSON object output when supported by model
    # If you ever see invalid JSON, we can add a repair step, but usually this works well.
    response_format={"type": "json_object"},
)
llm_text = ChatOpenAI(model="gpt-5.2-chat-latest")


def _render(template_text: str, data: dict) -> str:
    return Template(template_text).render(data=data)


def content_analyzer(state: State):
    brand_name = state.get("brand_name") or "Antwerpen"
    image_urls = state.get("image_urls") or []

    system_text = _render(
        CONTENT_ANALYZER_PROMPT,
        data={"brandName": brand_name, "brandManual": BRAND_MANUAL},
    )

    # Combine: user text + optional images (vision)
    # Studio/Lovable will pass image URLs. Later we can support base64 data URLs too.
    user_text = ""
    for m in reversed(state["messages"]):
        if isinstance(m, HumanMessage):
            user_text = m.content
            break

    human_content = [{"type": "text", "text": user_text}]
    for u in image_urls:
        human_content.append({"type": "image_url", "image_url": {"url": u}})

    msg = llm.invoke([
        SystemMessage(content=system_text),
        HumanMessage(content=human_content),
    ])

    analysis = json.loads(msg.content)
    return {"content_analysis_json": analysis, "messages": [msg]}


def brand_critic(state: State):
    brand_name = state.get("brand_name") or "Antwerpen"

    system_text = _render(
        BRAND_CRITIC_PROMPT,
        data={"brandName": brand_name, "brandManual": BRAND_MANUAL},
    )

    analysis = state.get("content_analysis_json") or {}
    msg = llm.invoke([
        SystemMessage(content=system_text),
        HumanMessage(content=json.dumps(analysis, ensure_ascii=False)),
    ])

    critique = json.loads(msg.content)
    return {"brand_critique_json": critique, "messages": [msg]}

def brand_voice_reporter(state: State):
    brand_name = state.get("brand_name") or "Antwerpen"

    system_text = _render(
        BRAND_VOICE_PROMPT,
        data={"brandName": brand_name, "brandManual": BRAND_MANUAL},
    )

    payload = {
        "brand_critique": state.get("brand_critique_json") or {},
        "content_analysis": state.get("content_analysis_json") or {},
    }

    msg = llm_text.invoke([
        SystemMessage(content=system_text),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
    ])

    return {"brand_voice_output": msg.content, "messages": [msg]}

builder = StateGraph(State)
builder.add_node("content_analyzer", content_analyzer)
builder.add_node("brand_critic", brand_critic)
builder.add_node("brand_voice_reporter", brand_voice_reporter)

builder.set_entry_point("content_analyzer")
builder.add_edge("content_analyzer", "brand_critic")
builder.add_edge("brand_critic", "brand_voice_reporter")
builder.add_edge("brand_voice_reporter", END)

agent = builder.compile()

