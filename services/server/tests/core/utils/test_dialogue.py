"""Tests for the Message/Dialogue classes used to build LLM context."""
import pytest

from core.utils.dialogue import Dialogue, Message


def test_message_assigns_default_uniq_id():
    m = Message(role="user", content="hi")
    assert m.uniq_id is not None
    assert len(m.uniq_id) > 0


def test_message_preserves_explicit_uniq_id():
    m = Message(role="user", content="hi", uniq_id="abc")
    assert m.uniq_id == "abc"


def test_message_default_is_not_temporary():
    m = Message(role="user", content="hi")
    assert m.is_temporary is False


def test_message_tool_call_id_default_is_none():
    m = Message(role="user", content="hi")
    assert m.tool_call_id is None


def test_dialogue_starts_empty():
    d = Dialogue()
    assert d.dialogue == []


def test_dialogue_put_appends_message():
    d = Dialogue()
    d.put(Message(role="user", content="hi"))
    d.put(Message(role="assistant", content="hello"))
    assert len(d.dialogue) == 2


def test_dialogue_get_llm_dialogue_includes_user_and_assistant():
    d = Dialogue()
    d.put(Message(role="user", content="hi"))
    d.put(Message(role="assistant", content="hello"))
    msgs = d.get_llm_dialogue()
    assert {"role": "user", "content": "hi"} in msgs
    assert {"role": "assistant", "content": "hello"} in msgs


def test_dialogue_update_system_message_replaces_existing():
    d = Dialogue()
    d.update_system_message("you are a helper")
    d.update_system_message("you are a coder")
    system_msgs = [m for m in d.dialogue if m.role == "system"]
    assert len(system_msgs) == 1
    assert system_msgs[0].content == "you are a coder"


def test_dialogue_update_system_message_creates_when_missing():
    d = Dialogue()
    d.update_system_message("you are a coder")
    assert len(d.dialogue) == 1
    assert d.dialogue[0].role == "system"


@pytest.mark.asyncio
async def test_get_llm_dialogue_with_memory_substitutes_into_system_message():
    d = Dialogue()
    d.update_system_message("Memory so far: <memory></memory>")
    msgs = d.get_llm_dialogue_with_memory(memory_str="user likes cats")
    system = next(m for m in msgs if m["role"] == "system")
    assert "user likes cats" in system["content"]
    assert "<memory></memory>" not in system["content"]


@pytest.mark.asyncio
async def test_get_llm_dialogue_separates_temporary_few_shot_from_actual_history():
    d = Dialogue()
    d.put(Message(role="user", content="real user msg", is_temporary=False))
    d.put(Message(role="user", content="fewshot example", is_temporary=True))
    msgs = d.get_llm_dialogue()
    contents = [m["content"] for m in msgs if m["role"] == "user"]
    assert "real user msg" in contents
    assert "fewshot example" in contents


def test_provider_tool_metadata_is_opt_in():
    signature = b"gemini-tool-signature"
    dialogue = Dialogue()
    dialogue.put(
        Message(
            role="assistant",
            tool_calls=[
                {
                    "id": "call-1",
                    "function": {"name": "bedtime_library", "arguments": "{}"},
                }
            ],
            provider_metadata={"thought_signatures": {"call-1": signature}},
        )
    )

    default_messages = dialogue.get_llm_dialogue()
    gemini_messages = dialogue.get_llm_dialogue_with_memory(
        include_provider_metadata=True
    )

    assert "provider_metadata" not in default_messages[0]
    assert gemini_messages[0]["provider_metadata"]["thought_signatures"]["call-1"] == signature