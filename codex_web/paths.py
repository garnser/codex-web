from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PROJECTS_FILE = DATA_DIR / "projects.json"
BOTS_CONNECTIONS_FILE = DATA_DIR / "bot_connections.json"
BOTS_BINDINGS_FILE = DATA_DIR / "bot_bindings.json"
BOTS_EVENTS_FILE = DATA_DIR / "bot_events.jsonl"
BOT_REPLY_TARGETS_FILE = DATA_DIR / "bot_reply_targets.json"
BOT_DELIVERY_TARGETS_FILE = DATA_DIR / "bot_delivery_targets.json"
BOT_DETAILS_FILE = DATA_DIR / "bot_details.json"
APPROVAL_MESSAGES_FILE = DATA_DIR / "approval_messages.json"
THREAD_INDEX_FILE = DATA_DIR / "thread_index.json"
THREAD_SETTINGS_FILE = DATA_DIR / "thread_settings.json"
ACTIVE_TURNS_FILE = DATA_DIR / "active_turns.json"
TURN_QUEUE_FILE = DATA_DIR / "queued_turns.json"
SLACK_THREAD_ICONS_FILE = DATA_DIR / "slack_thread_icons.json"
STATIC_DIR = ROOT / "static"

SLACK_RELAY_NOTICE = (
    "Slack relay rule: do not use Slack tools, Slack connectors, MCP Slack apps, or any direct Slack API calls in this "
    "turn. Write the Slack-facing update as a normal agent response instead; codex-web will relay it through the "
    "configured Slack bot with the correct thread name and icon impersonation. If a Slack handoff or channel update is "
    "needed, include that handoff text in your response rather than posting it yourself."
)
