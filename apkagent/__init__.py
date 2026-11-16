"""apkagent - agentic APK protocol reverse-engineering library."""
from .llm import LLM, LLMError
from .parsing import parse_call
from .prompts import PromptStore
from .tools import ToolRegistry
from .workspace import Workspace, detect_package, layout_text, count_sources
from .android_tools import register_android_tools, register_shell_tool
from .agent import Agent, AgentResult
from .workflow import Workflow, Phase
from .jadx import decompile
from .server import start_llama_server

__all__ = [
    "LLM", "LLMError", "parse_call", "PromptStore", "ToolRegistry", "Workspace",
    "detect_package", "layout_text", "count_sources", "register_android_tools",
    "register_shell_tool", "Agent", "AgentResult", "Workflow", "Phase", "decompile",
    "start_llama_server", "build_agent",
]


def build_agent(apk, src, out, llm, *, lang="python", prompts_dir=None, ctx=8192,
                max_steps=30, max_obs=4000, shell=False, unsafe_shell=False, on_event=None):
    """One-call factory: wires workspace + tools + prompts + agent."""
    ws = Workspace(src, out)
    prompts = PromptStore(prompts_dir)
    reg = ToolRegistry()
    register_android_tools(reg, ws)
    if shell:
        register_shell_tool(reg, unsafe=unsafe_shell)
    variables = {
        "APK": str(apk), "LANG": lang, "PKG": detect_package(ws.src) or "(unknown)",
        "NFILES": str(count_sources(ws.src)), "LAYOUT": layout_text(ws.src),
    }
    agent = Agent(llm, reg, prompts, variables, ctx=ctx, max_steps=max_steps,
                  max_obs=max_obs, notes_path=ws.out / ".notes.json", on_event=on_event)
    return agent, prompts, variables
