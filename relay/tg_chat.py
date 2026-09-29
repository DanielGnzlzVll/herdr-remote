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
SWEEP_PASS_SPAN = 500
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
        prompt_id=(blocked or {}).get("alert_id") or (blocked or {}).get("prompt_id") or "",
        last_message=last_message if status in ("done", "idle") else "",
    )


# --- Known chat ---

@dataclass
class CardState:
    message_id: int
    sent_at: float
    status: str
    prompt_id: str
    rendered_hash: str
    last_message: str = ""
    absent_elapsed: float = 0.0


@dataclass
class Transient:
    message_id: int
    kind: str
    owner: str = ""
    deadline: float | None = None


@dataclass
class ChatState:
    cards: dict = field(default_factory=dict)
    transients: dict = field(default_factory=dict)
    high_water: int = 0
    swept_to: int = 0


def record_seen(state: ChatState, message_id: int) -> None:
    state.high_water = max(state.high_water, int(message_id))


def record_transient(state: ChatState, message_id: int, kind: str, owner: str = "",
                     deadline: float | None = None, now: float = 0.0, supersede: bool = False) -> None:
    """Add a non-card message to the known chat. supersede=True expires the owner's earlier ones."""
    if supersede:
        for existing in state.transients.values():
            if existing.kind == kind and existing.owner == owner and existing.message_id != message_id:
                existing.deadline = now
    state.transients[int(message_id)] = Transient(int(message_id), kind, owner, deadline)
    record_seen(state, message_id)


# --- Actions ---

@dataclass(frozen=True)
class AdvanceTimers:
    absent_elapsed: dict


@dataclass(frozen=True)
class Send:
    key: str
    view: CardView
    alert: bool


@dataclass(frozen=True)
class Edit:
    key: str
    view: CardView


@dataclass(frozen=True)
class Repost:
    key: str
    view: CardView
    alert: bool


@dataclass(frozen=True)
class Delete:
    ids: tuple
    card_keys: tuple = ()


@dataclass(frozen=True)
class Sweep:
    ids: tuple
    new_swept_to: int


def card_action(key: str, shown: CardState | None, view: CardView, now: float):
    if shown is None:
        return Send(key, view, alert=view.status == "blocked")
    if view.status == "blocked" and shown.status != "blocked":
        return Repost(key, view, alert=True)
    if (view.status == "blocked" and shown.prompt_id and view.prompt_id
            and shown.prompt_id != view.prompt_id):
        return Repost(key, view, alert=True)
    if view.status in ("done", "idle") and shown.status in ("working", "blocked"):
        return Repost(key, view, alert=True)
    if now - shown.sent_at >= CARD_MAX_AGE:
        return Repost(key, view, alert=False)
    if view.rendered_hash != shown.rendered_hash:
        return Edit(key, view)
    return None


def plan(state: ChatState, views: dict, now: float, relay_connected: bool,
         elapsed: float, close_grace: float) -> list:
    """One reconcile pass as data. Inputs are not mutated."""
    actions = []
    timers = {}
    gone = []
    for key, card in state.cards.items():
        if key in views:
            if card.absent_elapsed:
                timers[key] = 0.0
            continue
        absent = card.absent_elapsed + (elapsed if relay_connected else 0.0)
        if absent >= close_grace:
            gone.append(key)
        elif absent != card.absent_elapsed:
            timers[key] = absent
    if timers:
        actions.append(AdvanceTimers(timers))

    for key, view in views.items():
        action = card_action(key, state.cards.get(key), view, now)
        if action is not None:
            actions.append(action)

    deleting = {state.cards[key].message_id for key in gone}
    for transient in state.transients.values():
        expired = transient.deadline is not None and transient.deadline <= now
        orphaned = transient.kind == "linked" and (
            transient.owner in gone or (transient.owner not in state.cards and transient.owner not in views)
        )
        if expired or orphaned:
            deleting.add(transient.message_id)
    if deleting or gone:
        actions.append(Delete(tuple(sorted(deleting)), tuple(sorted(gone))))

    if state.high_water > state.swept_to:
        # Bounded so a first start's whole-history sweep cannot hold card updates back.
        end = min(state.high_water, state.swept_to + SWEEP_PASS_SPAN)
        desired = {card.message_id for card in state.cards.values()} | set(state.transients)
        ids = tuple(
            i for i in range(state.swept_to + 1, end + 1)
            if i not in desired and i not in deleting
        )
        actions.append(Sweep(ids, end))
    return actions


def apply(state: ChatState, action, now: float, message_id: int | None = None) -> None:
    """Commit one action's outcome to the known chat. Call only after Telegram accepted it."""
    if isinstance(action, AdvanceTimers):
        for key, value in action.absent_elapsed.items():
            if key in state.cards:
                state.cards[key].absent_elapsed = value
    elif isinstance(action, (Send, Repost)):
        old = state.cards.get(action.key)
        if isinstance(action, Repost) and old is not None:
            state.transients[old.message_id] = Transient(old.message_id, "ephemeral", "", now)
        view = action.view
        state.cards[action.key] = CardState(
            message_id=int(message_id), sent_at=now, status=view.status, prompt_id=view.prompt_id,
            rendered_hash=view.rendered_hash, last_message=view.last_message,
        )
        record_seen(state, message_id)
    elif isinstance(action, Edit):
        card = state.cards[action.key]
        view = action.view
        card.status, card.prompt_id = view.status, view.prompt_id
        card.rendered_hash, card.last_message = view.rendered_hash, view.last_message
    elif isinstance(action, Delete):
        for key in action.card_keys:
            state.cards.pop(key, None)
        for mid in action.ids:
            state.transients.pop(mid, None)
    elif isinstance(action, Sweep):
        state.swept_to = max(state.swept_to, action.new_swept_to)


def batches(ids, size: int = SWEEP_BATCH):
    ids = list(ids)
    return [ids[i:i + size] for i in range(0, len(ids), size)]


# --- Persistence ---

def state_to_json(state: ChatState) -> str:
    return json.dumps({
        "cards": {key: asdict(card) for key, card in state.cards.items()},
        "transients": [asdict(t) for t in state.transients.values()],
        "high_water": state.high_water,
        "swept_to": state.swept_to,
    })


def state_from_json(raw: str) -> ChatState:
    data = json.loads(raw)
    return ChatState(
        cards={key: CardState(**card) for key, card in data["cards"].items()},
        transients={int(t["message_id"]): Transient(**t) for t in data["transients"]},
        high_water=int(data["high_water"]),
        swept_to=int(data["swept_to"]),
    )


class ChatStore:
    def __init__(self, path: str):
        self.path = path

    def load(self) -> ChatState:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return state_from_json(fh.read())
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return ChatState()

    def save(self, state: ChatState) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".telegram_chat.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(state_to_json(state))
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
