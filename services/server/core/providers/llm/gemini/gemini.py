import json
import os
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List

import requests
from google import genai
from google.genai import types

from core.providers.llm.base import LLMProviderBase
from core.utils.util import check_model_key
from config.logger import setup_logging
from requests import RequestException

log = setup_logging()
TAG = __name__


def test_proxy(proxy_url: str, test_url: str) -> bool:
    try:
        resp = requests.get(test_url, proxies={"http": proxy_url, "https": proxy_url})
        return 200 <= resp.status_code < 400
    except RequestException:
        return False


def setup_proxy_env(http_proxy: str | None, https_proxy: str | None):
    """
    分别测试 HTTP 和 HTTPS 代理是否可用，并设置环境变量。
    如果 HTTPS 代理不可用但 HTTP 可用，会将 HTTPS_PROXY 也指向 HTTP。
    """
    test_http_url = "http://www.google.com"
    test_https_url = "https://www.google.com"

    ok_http = ok_https = False

    if http_proxy:
        ok_http = test_proxy(http_proxy, test_http_url)
        if ok_http:
            os.environ["HTTP_PROXY"] = http_proxy
            log.bind(tag=TAG).info(f"配置提供的Gemini HTTPS代理连通成功: {http_proxy}")
        else:
            log.bind(tag=TAG).warning(f"配置提供的Gemini HTTP代理不可用: {http_proxy}")

    if https_proxy:
        ok_https = test_proxy(https_proxy, test_https_url)
        if ok_https:
            os.environ["HTTPS_PROXY"] = https_proxy
            log.bind(tag=TAG).info(f"配置提供的Gemini HTTPS代理连通成功: {https_proxy}")
        else:
            log.bind(tag=TAG).warning(
                f"配置提供的Gemini HTTPS代理不可用: {https_proxy}"
            )

    # 如果https_proxy不可用，但http_proxy可用且能走通https，则复用http_proxy作为https_proxy
    if ok_http and not ok_https:
        if test_proxy(http_proxy, test_https_url):
            os.environ["HTTPS_PROXY"] = http_proxy
            ok_https = True
            log.bind(tag=TAG).info(f"复用HTTP代理作为HTTPS代理: {http_proxy}")

    if not ok_http and not ok_https:
        log.bind(tag=TAG).error(
            f"Gemini 代理设置失败: HTTP 和 HTTPS 代理都不可用，请检查配置"
        )
        raise RuntimeError("HTTP 和 HTTPS 代理都不可用，请检查配置")


class LLMProvider(LLMProviderBase):
    def __init__(self, cfg: Dict[str, Any]):
        self.model_name = cfg.get("model_name", "gemini-2.0-flash")
        self.api_key = cfg["api_key"]
        http_proxy = cfg.get("http_proxy")
        https_proxy = cfg.get("https_proxy")

        model_key_msg = check_model_key("LLM", self.api_key)
        if model_key_msg:
            log.bind(tag=TAG).error(model_key_msg)

        if http_proxy or https_proxy:
            log.bind(tag=TAG).info(
                f"检测到Gemini代理配置，开始测试代理连通性和设置代理环境..."
            )
            setup_proxy_env(http_proxy, https_proxy)
            log.bind(tag=TAG).info(
                f"Gemini 代理设置成功 - HTTP: {http_proxy}, HTTPS: {https_proxy}"
            )
        # google-genai 的 HTTP timeout 單位是毫秒；設定仍以秒為單位。
        self.timeout = cfg.get("timeout", 120)
        self.client = genai.Client(
            api_key=self.api_key,
            http_options=types.HttpOptions(timeout=int(self.timeout * 1000)),
        )

        self.gen_cfg = {
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 40,
            "max_output_tokens": 2048,
        }

    ALLOWED_SCHEMA_FIELDS = {
        'type', 'format', 'description', 'nullable', 'enum',
        'items', 'max_items', 'min_items', 'properties', 'required'
    }

    @staticmethod
    def _clean_schema(d):
        if not isinstance(d, dict):
            return d
        cleaned = {}
        for k, v in d.items():
            if k == 'properties' and isinstance(v, dict):
                cleaned['properties'] = {
                    prop_name: LLMProvider._clean_schema(prop_schema)
                    for prop_name, prop_schema in v.items()
                    if isinstance(prop_schema, dict)
                }
            elif k in LLMProvider.ALLOWED_SCHEMA_FIELDS:
                if k == 'items':
                    cleaned['items'] = LLMProvider._clean_schema(v)
                else:
                    cleaned[k] = v
        if 'required' in cleaned and isinstance(cleaned['required'], list):
            props = cleaned.get('properties', {})
            cleaned['required'] = [r for r in cleaned['required'] if r in props]
            if not cleaned['required']:
                cleaned.pop('required', None)
        return cleaned

    @staticmethod
    def _build_tools(funcs: List[Dict[str, Any]] | None):
        if not funcs:
            return None
        return [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name=f["function"]["name"],
                        description=f["function"]["description"],
                        parameters=LLMProvider._clean_schema(f["function"]["parameters"]),
                    )
                    for f in funcs
                ]
            )
        ]

    # Gemini 3 的工具呼叫需要逐次回送模型產生的 thought signature。
    supports_provider_metadata = True

    def response(self, session_id, dialogue, **kwargs):
        yield from self._generate(dialogue, None)

    def response_with_functions(self, session_id, dialogue, functions=None):
        yield from self._generate(dialogue, self._build_tools(functions))

    @staticmethod
    def _build_contents(dialogue):
        tool_names = {}
        for message in dialogue:
            for tool_call in message.get("tool_calls") or []:
                function = tool_call.get("function") or {}
                if tool_call.get("id") and function.get("name"):
                    tool_names[tool_call["id"]] = function["name"]

        contents = []
        system_instructions = []
        for message in dialogue:
            role = message["role"]
            if role == "system":
                if message.get("content"):
                    system_instructions.append(str(message["content"]))
                continue

            if role == "assistant" and "tool_calls" in message:
                provider_metadata = message.get("provider_metadata") or {}
                signatures = provider_metadata.get("thought_signatures") or {}
                parts = []
                for tool_call in message["tool_calls"]:
                    tool_id = tool_call.get("id")
                    function = tool_call["function"]
                    raw_arguments = function.get("arguments") or "{}"
                    arguments = (
                        json.loads(raw_arguments)
                        if isinstance(raw_arguments, str)
                        else raw_arguments
                    )
                    if not isinstance(arguments, dict):
                        raise ValueError(
                            "Gemini function-call arguments must be a JSON object"
                        )
                    function_call = types.FunctionCall(
                        name=function["name"],
                        args=arguments,
                        id=tool_id,
                    )
                    parts.append(
                        types.Part(
                            function_call=function_call,
                            thought_signature=signatures.get(tool_id),
                        )
                    )
                contents.append(types.Content(role="model", parts=parts))
                continue

            if role == "tool":
                tool_id = message.get("tool_call_id")
                tool_name = tool_names.get(tool_id)
                if not tool_name:
                    raise ValueError(
                        f"Gemini tool response has no matching function call: {tool_id}"
                    )
                raw_response = message.get("content", "")
                try:
                    response = (
                        json.loads(raw_response)
                        if isinstance(raw_response, str)
                        else raw_response
                    )
                except json.JSONDecodeError:
                    response = raw_response
                if not isinstance(response, dict):
                    response = {"output": response}
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_function_response(
                                name=tool_name,
                                response=response,
                            )
                        ],
                    )
                )
                continue

            contents.append(
                types.Content(
                    role="model" if role == "assistant" else "user",
                    parts=[types.Part.from_text(text=str(message.get("content", "")))],
                )
            )

        system_instruction = "\n\n".join(system_instructions) or None
        return contents, system_instruction

    def _generate(self, dialogue, tools):
        contents, system_instruction = self._build_contents(dialogue)
        tool_config = (
            types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.AUTO,
                )
            )
            if tools is not None
            else None
        )
        generation_config = types.GenerateContentConfig(
            **self.gen_cfg,
            system_instruction=system_instruction,
            tools=tools,
            tool_config=tool_config,
        )
        stream = self.client.models.generate_content_stream(
            model=self.model_name,
            contents=contents,
            config=generation_config,
        )

        tool_calls_by_id = {}
        try:
            for chunk in stream:
                candidates = getattr(chunk, "candidates", None) or []
                if not candidates:
                    continue
                candidate = candidates[0]
                candidate_content = getattr(candidate, "content", None)
                if candidate_content is None:
                    continue

                for part in candidate_content.parts:
                    function_call = getattr(part, "function_call", None)
                    if function_call is not None:
                        tool_id = getattr(function_call, "id", None)
                        previous = tool_calls_by_id.get(tool_id) if tool_id else None
                        tool_id = tool_id or (
                            previous.id if previous is not None else uuid.uuid4().hex
                        )
                        tool_calls_by_id[tool_id] = SimpleNamespace(
                            id=tool_id,
                            index=(
                                previous.index
                                if previous is not None
                                else len(tool_calls_by_id)
                            ),
                            type="function",
                            function=SimpleNamespace(
                                name=function_call.name,
                                arguments=json.dumps(
                                    dict(function_call.args or {}),
                                    ensure_ascii=False,
                                ),
                            ),
                            thought_signature=getattr(part, "thought_signature", None),
                        )
                    elif getattr(part, "text", None):
                        yield (
                            part.text
                            if tools is None
                            else (part.text, None)
                        )

            if tool_calls_by_id:
                yield None, sorted(
                    tool_calls_by_id.values(),
                    key=lambda tool_call: tool_call.index,
                )
        finally:
            close_stream = getattr(stream, "close", None)
            if close_stream is not None:
                close_stream()
