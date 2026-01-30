from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, List, NotRequired, TypedDict

from jinja2 import Template
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages


# ----------------------------
# Robust paths (local + container)
# ----------------------------
BASE_DIR = Path(__file__).resolve().parent          # .../app
PROMPTS_DIR = BASE_DIR / "brandpilot" / "prompts"   # .../app/brandpilot/prompts


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_prompt(filename: str) -> str:
    p = PROMPTS_DIR / filename
    if not p.exists():
        raise FileNotFoundError(f"Missing prompt file: {p} (cwd={Path.cwd()})")
    return _read_text(p)


def _render(template_text: str, data: dict) -> str:
    return Template(template_text).render(data=data)


def _safe_json_loads(s: str) -> dict:
    """
    Minimal JSON robustness:
    - strips surrounding code fences if the model wraps output
    - then parses JSON
    """
    s = (s or "").strip()

    # Remove ```json ... ``` or ``` ... ```
    if s.startswith("```"):
        parts = s.split("```")
        # typical: ["", "json\n{...}", ""]
        if len(parts) >= 2:
            s = parts[1].strip()
            if s.startswith("json"):
                s = s[4:].strip()

    return json.loads(s)


# ----------------------------
# Graph state
# ----------------------------
class State(TypedDict):
    # Incoming
    messages: Annotated[list, add_messages]
    image_urls: NotRequired[List[str]]   # optional
    brand_name: NotRequired[str]         # optional

    # Produced
    content_analysis_json: NotRequired[dict]
    brand_critique_json: NotRequired[dict]
    brand_voice_output: NotRequired[str]


# ----------------------------
# LLMs
# ----------------------------
# NOTE: For gpt-5.2-chat-* you must keep temperature at default (1).
MODEL = "gpt-5.2-chat-latest"

# Analyzer + Critic: force JSON object output
llm_json = ChatOpenAI(
    model=MODEL,
    temperature=1,
    response_format={"type": "json_object"},
)

# Voice: normal chat text output
llm_text = ChatOpenAI(
    model=MODEL,
    temperature=1,
)


def _latest_user_text(state: State) -> str:
    """
    Extract latest HumanMessage text.
    Supports both:
    - str content
    - list-of-parts content (OpenAI multimodal style)
    """
    for m in reversed(state["messages"]):
        if isinstance(m, HumanMessage):
            if isinstance(m.content, str):
                return m.content.strip()
            # list-of-parts
            try:
                texts: List[str] = []
                for part in m.content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        t = (part.get("text") or "").strip()
                        if t:
                            texts.append(t)
                return "\n".join(texts).strip()
            except Exception:
                return ""
    return ""


def content_analyzer(state: State):
    brand_name = state.get("brand_name") or "Antwerpen"
    image_urls = state.get("image_urls") or []

    brand_manual = load_prompt("brand_manual.md")
    content_analyzer_prompt = load_prompt("content_analyzer.md")

    system_text = _render(
        content_analyzer_prompt,
        data={"brandName": brand_name, "brandManual": brand_manual},
    )

    user_text = _latest_user_text(state) or "Analyseer deze upload."

    # Multimodal message: text + optional image URLs
    human_content = [{"type": "text", "text": user_text}]
    for u in image_urls:
        human_content.append({"type": "image_url", "image_url": {"url": u}})

    msg = llm_json.invoke(
        [
            SystemMessage(content=system_text),
            HumanMessage(content=human_content),
        ]
    )

    analysis = _safe_json_loads(msg.content)
    return {"content_analysis_json": analysis, "messages": [msg]}


def brand_critic(state: State):
    brand_name = state.get("brand_name") or "Antwerpen"

    brand_manual = load_prompt("brand_manual.md")
    brand_critic_prompt = load_prompt("brand_critic.md")

    system_text = _render(
        brand_critic_prompt,
        data={"brandName": brand_name, "brandManual": brand_manual},
    )

    analysis = state.get("content_analysis_json") or {}

    msg = llm_json.invoke(
        [
            SystemMessage(content=system_text),
            HumanMessage(content=json.dumps(analysis, ensure_ascii=False)),
        ]
    )

    critique = _safe_json_loads(msg.content)
    return {"brand_critique_json": critique, "messages": [msg]}


def brand_voice_reporter(state: State):
    brand_name = state.get("brand_name") or "Antwerpen"

    brand_manual = load_prompt("brand_manual.md")
    brand_voice_prompt = load_prompt("brand_voice.md")

    system_text = _render(
        brand_voice_prompt,
        data={"brandName": brand_name, "brandManual": brand_manual},
    )

    critique = state.get("brand_critique_json") or {}
    analysis = state.get("content_analysis_json") or {}

    # Feed the voice node the *useful* stuff (chat-style), not the whole dump.
    payload = {
        "brand_name": brand_name,
        "content_analysis": {
            "content": (analysis.get("content") or {}),
            "mood": (analysis.get("mood") or {}),
            "copy": (analysis.get("copy") or {}),
            "visual_assets": (analysis.get("visual_assets") or {}),
            "audience_signals": (analysis.get("audience_signals") or {}),
            "strategy_signals": (analysis.get("strategy_signals") or {}),
        },
        "brand_critique": {
            "submitted_asset_summary": critique.get("submitted_asset_summary", {}),
            "ship_decision": critique.get("ship_decision", {}),
            "findings": critique.get("findings", []),
            "risk_register": critique.get("risk_register", []),
            "unknowns_and_gaps": critique.get("unknowns_and_gaps", []),
            "brand_constitution": critique.get("brand_constitution", {}),
            "brand_lingo_constitution": critique.get("brand_lingo_constitution", {}),
        },
        # If you want the voice bot to react to the user's ask too:
        "user_message": _latest_user_text(state),
    }

    msg = llm_text.invoke(
        [
            SystemMessage(content=system_text),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        ]
    )

    return {"brand_voice_output": msg.content, "messages": [msg]}


# ----------------------------
# Build graph
# ----------------------------
builder = StateGraph(State)

builder.add_node("content_analyzer", content_analyzer)
builder.add_node("brand_critic", brand_critic)
builder.add_node("brand_voice_reporter", brand_voice_reporter)

builder.set_entry_point("content_analyzer")
builder.add_edge("content_analyzer", "brand_critic")
builder.add_edge("brand_critic", "brand_voice_reporter")
builder.add_edge("brand_voice_reporter", END)

agent = builder.compile()

