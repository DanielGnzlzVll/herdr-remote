"""Declarative model of the Telegram chat: what it should hold, what it holds, and the diff."""
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field

STATUS_GLYPH = {"blocked": "🔴", "working": "🟡", "done": "🟢", "idle": "🟢", "unknown": "⚪"}
STATUS_LABELS = {"blocked": "BLOCKED", "working": "WORKING", "done": "DONE", "idle": "IDLE", "unknown": "IDLE"}
TEXT_LIMIT = 4096
EXCERPT_CHARS = 600
PROMPT_CHARS = 400
CARD_MAX_AGE = 47 * 3600
SWEEP_BATCH = 100
WAITING_FOR_TEXT = "✍️ Waiting for text — reply to this card with your answer."
REPLY_TO_ANSWER = "Reply to this card to answer."


def default_log_dir() -> str:
    """Same location the relay uses for LOG_DIR, so the state file sits beside its own."""
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Logs/herdr-remote")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local"))
        return os.path.join(base, "herdr-remote", "logs")
    if os.path.isdir("/var/log") and os.access("/var/log", os.W_OK):
        return "/var/log/herdr-remote"
    return os.path.expanduser("~/.local/state/herdr-remote/log")


def card_key(host: str | None, pane_id: str) -> str:
    return f"{host or 'local'}|{pane_id}"


def agent_key(agent: dict) -> str:
    return card_key(agent.get("host"), agent["pane_id"])


# --- Last message extraction ---

def _is_tui_chrome(line: str) -> bool:
    """True for terminal-UI decoration that is not part of the agent's message."""
    s = line.strip()
    if not s:
        return False
    if set(s) <= set("─—-_"):
        return True
    if s[0] in "❯✻✔✗✢✽✶✷✳✴✦✧✺❋❂✼":
        return True
    if s.startswith("⏵⏵") or s.startswith("💬"):
        return True
    if "% of" in s and "tokens" in s and "|" in s:
        return True
    if s.startswith("💲") or "⏱" in s:
        return True
    low = s.lower()
    if "auto-compact" in low or "to run in background" in low or "esc to interrupt" in low:
        return True
    if "tokens)" in s and ("↓" in s or "↑" in s):
        return True
    return False


def extract_last_message(content: str) -> str:
    """Reduce a raw terminal snapshot to the agent's last message.

    Claude Code prefixes each agent turn with '●'; the last such block is kept and trailing TUI
    chrome is stripped. Without a marker (another harness) the de-chromed tail is returned.
    """
    lines = content.replace("\r", "").split("\n")
    markers = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("●")]
    block = lines[markers[-1]:] if markers else lines[:]
    while block and (not block[-1].strip() or _is_tui_chrome(block[-1])):
        block.pop()
    while block and not block[0].strip():
        block.pop(0)
    text = "\n".join(block).strip()
    if text.startswith("●"):
        text = text[1:].lstrip()
    return text


# --- Rendering ---

@dataclass(frozen=True)
class CardView:
    text: str
    buttons: tuple
    status: str
    prompt_id: str
    last_message: str

    @property
    def rendered_hash(self) -> str:
        payload = json.dumps([self.text, self.buttons], ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _card_body(status: str, blocked: dict | None, last_message: str) -> str:
    if status == "blocked":
        if not blocked:
            return ""
        parts = [(blocked.get("prompt") or "")[-PROMPT_CHARS:].strip()]
        if blocked.get("text_field"):
            if blocked.get("text_value"):
                parts.append(f"Typed so far: {blocked['text_value']}")
            parts.append(WAITING_FOR_TEXT)
        elif not blocked.get("options") and not blocked.get("multi_options"):
            parts.append(REPLY_TO_ANSWER)
        return "\n\n".join(p for p in parts if p)
    if status in ("done", "idle"):
        return last_message[-EXCERPT_CHARS:]
    return ""


def render_card(agent: dict, tab_label: str, blocked: dict | None, last_message: str, buttons: tuple) -> CardView:
    status = "blocked" if blocked else (agent.get("status") or "unknown")
    name = tab_label or agent.get("label") or agent.get("project") or agent["pane_id"]
    head = [f"{STATUS_GLYPH.get(status, '⚪')} {STATUS_LABELS.get(status, 'IDLE')} · {name}"]
    where = " · ".join(b for b in (
        f"📁 {agent['project']}" if agent.get("project") else "",
        agent.get("agent") or "",
    ) if b)
    host = agent.get("host") or "local"
    if host != "local":
        where = f"{where} @{host}" if where else f"@{host}"
    if where:
        head.append(where)
    if agent.get("title"):
        head.append(agent["title"])
    header = "\n".join(head)
    body = _card_body(status, blocked, last_message)
    room = TEXT_LIMIT - len(header) - 2
    if len(body) > room:
        body = body[-room:]
    text = f"{header}\n\n{body}" if body else header
    return CardView(
        text=text,
        buttons=buttons,
        status=status,
        prompt_id=(blocked or {}).get("prompt_id") or "",
        last_message=last_message if status in ("done", "idle") else "",
    )
