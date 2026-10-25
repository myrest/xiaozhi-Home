import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.genai import types

from core.providers.llm.gemini.gemini import LLMProvider
from core.utils.dialogue import Dialogue, Message


class _ClosableStream:
    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._chunks)

    def close(self):
        self.closed = True


def _chunk(*parts):
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=list(parts)))]
    )


class TestGeminiToolCall(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()

    def _provider(self):
        with patch(
            "core.providers.llm.gemini.gemini.genai.Client",
            return_value=self.client,
        ) as client_factory:
            provider = LLMProvider(
                {"api_key": "unit-test-key", "model_name": "gemini-test"}
            )
        client_factory.assert_called_once_with(
            api_key="unit-test-key",
            http_options=types.HttpOptions(timeout=120000),
        )
        return provider

    def test_function_response_replays_signature_and_correct_tool_result(self):
        signature = b"model-generated-signature"
        function_call = types.FunctionCall(
            id="call-1",
            name="bedtime_library",
            args={"subdirectories_only": True},
        )
        call_stream = _ClosableStream(
            [_chunk(SimpleNamespace(function_call=function_call, thought_signature=signature, text=None))]
        )
        response_stream = _ClosableStream(
            [_chunk(SimpleNamespace(function_call=None, thought_signature=None, text="找到三個系列。"))]
        )
        self.client.models.generate_content_stream.side_effect = [
            call_stream,
            response_stream,
        ]
        provider = self._provider()
        functions = [
            {
                "type": "function",
                "function": {
                    "name": "bedtime_library",
                    "description": "列出故事系列",
                    "parameters": {
                        "type": "object",
                        "properties": {"subdirectories_only": {"type": "boolean"}},
                    },
                },
            }
        ]

        first_turn = list(
            provider.response_with_functions(
                "session-1",
                [
                    {"role": "system", "content": "依工具結果回答。"},
                    {"role": "user", "content": "睡前故事有哪些？"},
                ],
                functions,
            )
        )
        self.assertEqual(len(first_turn), 1)
        tool_call = first_turn[0][1][0]
        self.assertEqual(tool_call.id, "call-1")
        self.assertEqual(tool_call.thought_signature, signature)
        self.assertTrue(call_stream.closed)

        result = {"series": [{"name": "米小圈系列", "total": 4290}]}
        dialogue = Dialogue()
        dialogue.update_system_message("依工具結果回答。")
        dialogue.put(Message(role="user", content="睡前故事有哪些？"))
        dialogue.put(
            Message(
                role="assistant",
                tool_calls=[
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments,
                        },
                    }
                ],
                provider_metadata={
                    "thought_signatures": {tool_call.id: tool_call.thought_signature}
                },
            )
        )
        dialogue.put(
            Message(
                role="tool",
                tool_call_id=tool_call.id,
                content=json.dumps(result, ensure_ascii=False),
            )
        )
        serialized_dialogue = dialogue.get_llm_dialogue_with_memory(
            include_provider_metadata=True
        )

        second_turn = list(
            provider.response_with_functions(
                "session-1", serialized_dialogue, functions
            )
        )
        self.assertEqual(second_turn, [("找到三個系列。", None)])
        self.assertTrue(response_stream.closed)

        request = self.client.models.generate_content_stream.call_args_list[1].kwargs
        self.assertEqual(request["config"].system_instruction, "依工具結果回答。")
        function_calling_config = request["config"].tool_config.function_calling_config
        self.assertNotIn(
            "streamFunctionCallArguments",
            function_calling_config.model_dump(by_alias=True, exclude_none=True),
        )
        model_call, function_result = request["contents"][1:]
        self.assertEqual(model_call.role, "model")
        self.assertEqual(model_call.parts[0].thought_signature, signature)
        self.assertEqual(
            model_call.parts[0].function_call.args,
            {"subdirectories_only": True},
        )
        self.assertEqual(function_result.role, "user")
        self.assertEqual(
            function_result.parts[0].function_response.name,
            "bedtime_library",
        )
        self.assertEqual(
            function_result.parts[0].function_response.response,
            result,
        )



    def test_interrupted_stream_closes_without_yielding_from_finally(self):
        stream = _ClosableStream(
            [_chunk(SimpleNamespace(function_call=None, thought_signature=None, text="partial"))]
        )
        self.client.models.generate_content_stream.return_value = stream
        provider = self._provider()

        response = provider.response("session-1", [{"role": "user", "content": "hi"}])
        self.assertEqual(next(response), "partial")
        response.close()

        self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
