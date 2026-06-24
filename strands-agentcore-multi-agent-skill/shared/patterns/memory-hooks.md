# Memory Hooks (Strands × AgentCore Memory)

> Integrate with AgentCore Memory via the **Strands `HookProvider` interface**. It auto-saves every message and, at agent startup, prepends the last K turns to the system prompt. External code only needs to add a single hook instance.

## File location

```
agents/orchestrator-agent/memory/
├── __init__.py
└── short_term_memory.py    ← all the code in this pattern
```

## Full code

```python
"""
Short-term memory implementation for orchestrator agent.

Provides conversation history continuity using AgentCore Memory short-term
storage. Stores raw conversation turns; retrieves last K turns at agent init.
"""
import logging
import os
from typing import Any

from bedrock_agentcore.memory import MemoryClient
from botocore.exceptions import ClientError
from strands.hooks import (
    AgentInitializedEvent,
    HookProvider,
    HookRegistry,
    MessageAddedEvent,
)

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_DEFAULT_REGION")
SHORT_TERM_MEMORY_NAME = "OrchestratorShortTermMemory"
SHORT_TERM_MEMORY_EXPIRY_DAYS = 7
DEFAULT_CONVERSATION_TURNS = 10


class ShortTermMemoryHooks(HookProvider):
    """Strands hook provider that integrates AgentCore Memory short-term storage."""

    def __init__(
        self,
        memory_client: MemoryClient,
        memory_id: str,
        actor_id: str,
        session_id: str,
        logger,
        conversation_turns: int = DEFAULT_CONVERSATION_TURNS,
    ):
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.actor_id = actor_id
        self.session_id = session_id
        self.conversation_turns = conversation_turns
        self.logger = logger

    def on_agent_initialized(self, event: AgentInitializedEvent):
        """Load last K conversation turns and append to system prompt."""
        try:
            self.logger.info(f"Loading last {self.conversation_turns} turns for session {self.session_id}")
            recent_turns = self.memory_client.get_last_k_turns(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                k=self.conversation_turns,
            )
            if not recent_turns:
                self.logger.info("No conversation history yet")
                return

            messages = []
            for turn in recent_turns:
                for msg in turn:
                    role = msg["role"]
                    text = msg["content"]["text"]
                    messages.append(f"{role}: {text}")
            context = "\n".join(messages)

            event.agent.system_prompt += f"""

## CONVERSATION HISTORY (Last {self.conversation_turns} turns)
{context}

## CRITICAL: CONTEXT-AWARE RESPONSE PROTOCOL

**STEP 1: RESOLVE CONTEXTUAL REFERENCES (MANDATORY)**
Before processing ANY query, identify the SUBJECT from conversation history:
- If a previous response discussed a specific entity, assume follow-ups are about THAT entity
- Resolve pronouns: "the orders" → "[Previous Subject]'s orders"

**STEP 2: ENRICH QUERY WITH CONTEXT**
Transform vague follow-up queries into specific queries.

**STEP 3: PROCEED WITH TOOL CALLS**
Only after resolving context, route the ENRICHED query.
"""
            self.logger.info(f"✅ Loaded {len(recent_turns)} turns into system prompt")
        except Exception as e:
            self.logger.warning(f"❌ Error loading conversation history: {e}")
            # Don't fail agent init — degrade to no-memory mode

    def on_message_added(self, event: MessageAddedEvent):
        """Persist each new message to short-term memory."""
        try:
            messages = event.agent.messages
            if not messages:
                return
            last = messages[-1]
            role = last.get("role", "unknown")
            content = last.get("content", "")

            # ── Normalize content shape (Strands message can be list / dict / str)
            if isinstance(content, list) and content:
                if isinstance(content[0], dict) and "text" in content[0]:
                    text = content[0]["text"]
                else:
                    text = str(content[0])
            elif isinstance(content, dict) and "text" in content:
                text = content["text"]
            else:
                text = str(content)

            # ★ create_event signature: messages=[(text, ROLE_UPPERCASE), ...]
            self.memory_client.create_event(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                messages=[(text, role.upper())],
            )
            self.logger.info(f"✅ Stored {role} message ({len(text)} chars)")
        except Exception as e:
            self.logger.warning(f"❌ Error storing message: {e}")

    def register_hooks(self, registry: HookRegistry) -> None:
        """Register both hooks. Required by HookProvider interface."""
        registry.add_callback(AgentInitializedEvent, self.on_agent_initialized)
        registry.add_callback(MessageAddedEvent, self.on_message_added)
        self.logger.info("✅ Short-term memory hooks registered")


# ───────────────────────────────────────────────────────────────────────
# Memory creation helpers (idempotent)
# ───────────────────────────────────────────────────────────────────────

def create_short_term_memory(
    logger,
    region: str | None = None,
    memory_name: str | None = None,
    expiry_days: int | None = None,
) -> tuple[str, MemoryClient]:
    """
    Create or retrieve a short-term memory resource.

    ★ IMPORTANT: list_memories first, then create only if absent.
       AgentCore allows duplicate names — without this check, every restart
       creates a new Memory instance.
    """
    region = region or AWS_REGION
    if not region:
        raise ValueError("AWS region required")
    memory_name = memory_name or SHORT_TERM_MEMORY_NAME
    expiry_days = expiry_days or SHORT_TERM_MEMORY_EXPIRY_DAYS

    client = MemoryClient(region_name=region)
    try:
        memories = client.list_memories()
        for m in memories:
            if m.get("name") == memory_name:
                logger.info(f"✅ Reusing existing memory: {m['id']}")
                return m["id"], client
            # Fallback: id often contains the name
            if memory_name.lower() in m.get("id", "").lower():
                logger.info(f"✅ Reusing memory by id-match: {m['id']}")
                return m["id"], client

        logger.info(f"Creating new short-term memory: {memory_name}")
        memory = client.create_memory_and_wait(
            name=memory_name,
            strategies=[],                      # ← raw mode (no semantic / preference)
            description="Short-term conversation memory",
            event_expiry_days=expiry_days,
        )
        logger.info(f"✅ Created memory: {memory['id']}")
        return memory["id"], client
    except ClientError as e:
        logger.warning(f"❌ Memory service error: {e}")
        raise


def create_orchestrator_short_term_memory(
    logger, region: str | None = None, memory_name: str = "OrchestratorShortTermMemory"
) -> tuple[str, MemoryClient]:
    return create_short_term_memory(logger, region=region, memory_name=memory_name, expiry_days=7)


# ───────────────────────────────────────────────────────────────────────
# Read helpers (when external code queries directly)
# ───────────────────────────────────────────────────────────────────────

def get_conversation_history(
    logger, memory_client: MemoryClient, memory_id: str, actor_id: str, session_id: str, k: int = 5
) -> str:
    try:
        turns = memory_client.get_last_k_turns(memory_id=memory_id, actor_id=actor_id, session_id=session_id, k=k)
        return format_conversation_history(turns)
    except Exception as e:
        logger.warning(f"❌ Error retrieving history: {e}")
        return ""


def format_conversation_history(conversation_turns: list[dict[str, Any]]) -> str:
    if not conversation_turns:
        return "No conversation history available."
    lines = []
    for turn in conversation_turns:
        for msg in turn:
            lines.append(f"  {msg['role']}: {msg['content']['text']}")
        lines.append("")
    return "\n".join(lines)
```

## Usage (Orchestrator entry handler)

```python
from memory.short_term_memory import (
    ShortTermMemoryHooks,
    create_orchestrator_short_term_memory,
)

# 1) Create the Memory resource once, or reuse it
memory_id, memory_client = create_orchestrator_short_term_memory(logger, AWS_REGION)

# 2) Create a per-user/session hook instance on every invocation
hook = ShortTermMemoryHooks(
    memory_client=memory_client,
    memory_id=memory_id,
    actor_id=customer_id,            # ← user ID (Cognito sub or a business ID)
    session_id=session_id,           # ← conversation ID (issued by the UI or auto-generated)
    logger=logger,
    conversation_turns=20,
)

agent = Agent(
    model=bedrock_model,
    system_prompt=base_system_prompt,
    tools=tools,
    hooks=[hook],                    # ← done in one line
)

async for event in agent.stream_async(prompt):
    yield event
```

Messages are isolated per `actor_id` × `session_id`. The same `actor_id` + a different `session_id` = a different conversation room for the same user.

## Behavior flow

```
Agent init
   │
   ▼
on_agent_initialized()  ──→ get_last_k_turns(memory_id, actor_id, session_id, k=20)
   │                                │
   │                                └─→ prepend "## CONVERSATION HISTORY ..." to the system_prompt
   ▼
agent.stream_async(prompt)
   │
   ├──→ user message added       ──→ on_message_added() ──→ create_event(("...", "USER"))
   ├──→ tool calls / responses
   ├──→ assistant message added  ──→ on_message_added() ──→ create_event(("...", "ASSISTANT"))
   └──→ stream end
```

## Variant patterns

### 1. Long-term semantic recall

```python
# Add a strategy when creating the Memory
memory = client.create_memory_and_wait(
    name="OrchestratorLongTerm",
    strategies=[{"semantic": {}}],   # ← automatic embedding
    event_expiry_days=365,
)
```

On retrieval:
```python
# When you want to find past conversations semantically similar to the user's query
records = memory_client.retrieve_memory_records(
    memory_id=memory_id,
    actor_id=actor_id,
    query=user_query,
    max_results=5,
)
```

→ In the hook's `on_agent_initialized`, prepend the retrieve results instead of the last K turns.

### 2. User preference learning

```python
strategies=[{"userPreference": {}}]
```

→ Memory automatically extracts/accumulates "preference" items. Query with `get_user_preferences(memory_id, actor_id)`.

Application: remember a first utterance like "reply in Korean" so subsequent sessions also respond in Korean.

### 3. Summarization-based compression

```python
strategies=[{"summarization": {}}]
```

→ Automatically summarizes conversations beyond a certain length. Reduces context-window burden.

### 4. Multi-hook chaining

You can register multiple hooks on one agent:

```python
agent = Agent(
    model=...,
    hooks=[
        ShortTermMemoryHooks(...),
        AuditLoggingHooks(...),       # send every message to a separate audit log
        GuardrailHooks(...),          # PII redaction
    ],
)
```

The `HookRegistry` runs callbacks for the same event in registration order.

## Unit tests (`tests/test_short_term_memory.py`)

```python
import logging
from unittest.mock import MagicMock
import pytest

from memory.short_term_memory import ShortTermMemoryHooks, create_short_term_memory


@pytest.fixture
def memory_client():
    return MagicMock()


def test_load_history_appends_to_system_prompt(memory_client):
    memory_client.get_last_k_turns.return_value = [
        [{"role": "USER", "content": {"text": "hi"}}, {"role": "ASSISTANT", "content": {"text": "hello"}}],
    ]
    hook = ShortTermMemoryHooks(memory_client, "mem-1", "actor", "session", logger=logging.getLogger(), conversation_turns=5)
    agent = MagicMock()
    agent.system_prompt = "BASE"
    event = MagicMock(agent=agent)

    hook.on_agent_initialized(event)
    assert "CONVERSATION HISTORY" in agent.system_prompt
    assert "USER: hi" in agent.system_prompt
    assert "ASSISTANT: hello" in agent.system_prompt


def test_message_added_persists(memory_client):
    hook = ShortTermMemoryHooks(memory_client, "mem-1", "actor", "session", logger=logging.getLogger())
    agent = MagicMock()
    agent.messages = [{"role": "user", "content": "Hello"}]
    event = MagicMock(agent=agent)

    hook.on_message_added(event)
    memory_client.create_event.assert_called_once()
    args = memory_client.create_event.call_args.kwargs
    assert args["actor_id"] == "actor"
    assert args["messages"] == [("Hello", "USER")]
```

## Pitfalls

| Pitfall | Avoidance |
|---|---|
| `create_event` rejects the dict form | Use the `messages=[(text, ROLE_UPPER)]` list-of-tuples form |
| Memory is recreated on every deploy | Call `list_memories()` and reuse if the same name exists |
| Multiple regions deployment | Memory is region-local — a separate Memory per region |
| Missing `system_prompt += ...` | Add a guard for when `event.agent.system_prompt` is None |
| A hook raises an exception and kills the agent | Wrap everything in try/except for graceful degradation |
| Token explosion in long conversations | Limit `conversation_turns` to 5–20 + consider a summarization strategy |
| **`actor_id` differs on every invocation (random UUID fallback)** | **`actor_id` must be a stable per-user ID (e.g., Cognito sub). Loud-warn on fallback via the entry handler's `resolve_customer_id()` helper — see `shared/reference/constraints.md` #25** |
| **`session_id` is timestamp-based (`YYYYMMDDHHMMSS`)** | **Allow only UUID — if another user calls in the same second, a session collision can cause a cross-user data leak** |

## actor_id / session_id stability — the key condition for Memory to work

This hook operates on the 3-dimensional key **`memory_id` × `actor_id` × `session_id`**.

| Dimension | Meaning | Recommended stable ID |
|---|---|---|
| `actor_id` | User (cross-session) | The Cognito ID Token `sub` claim — `f"cognito_{sub}"` |
| `session_id` | Conversation (changes whenever the user starts a new chat) | UUID — `crypto.randomUUID()` (frontend) / `uuid.uuid4()` (backend) |

**Test — whether history is visible when the same user calls twice**:

```python
# First call
hook = ShortTermMemoryHooks(client, memory_id, actor_id="cognito_abc-123", session_id="sess-1", ...)
# user: "My name is John Doe"

# Second call (same actor_id + session_id)
hook = ShortTermMemoryHooks(client, memory_id, actor_id="cognito_abc-123", session_id="sess-1", ...)
# user: "What is my name?"
# → the assistant should respond with "John Doe" to be correct.
#   If it does not, it is evidence that actor_id / session_id differs per invocation.
```
