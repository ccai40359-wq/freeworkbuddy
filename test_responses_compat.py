import json
import unittest
from unittest.mock import patch

import converter
from desensitize import desensitize_body
from starlette.requests import Request
from webbuddy.routes import openai as openai_routes


def parse_events(chunks):
    events = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


class FakeResponse:
    status_code = 200

    def __init__(self, chunks):
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class FakeClient:
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, *args, **kwargs):
        return FakeResponse(self._chunks)


class FakeErrorResponse(FakeResponse):
    status_code = 403

    def __init__(self, body):
        self._body = body
        super().__init__([])

    async def aread(self):
        return self._body


class FakeErrorClient(FakeClient):
    def __init__(self, body):
        self._body = body

    def stream(self, *args, **kwargs):
        return FakeErrorResponse(self._body)


class RequestConversionTests(unittest.TestCase):
    def test_tencent_error_is_openai_compatible(self):
        raw = json.dumps({
            "code": 11140,
            "msg": "request illegal",
            "requestId": "request-123",
        }).encode()

        detail = converter._safe_err_raw(raw, 403)

        self.assertEqual(detail["error"]["message"], "request illegal")
        self.assertEqual(detail["error"]["code"], 11140)
        self.assertEqual(detail["error"]["status"], 403)
        self.assertEqual(detail["error"]["request_id"], "request-123")

        event = parse_events([converter._err_event(raw, 403).decode()])[0]
        self.assertEqual(event, detail)

    def test_removed_models_are_not_advertised(self):
        self.assertNotIn("kimi-k3", converter.DEFAULT_MODELS)
        self.assertNotIn("kimi-k2.7", converter.DEFAULT_MODELS)
        self.assertIn("kimi-k2.6", converter.DEFAULT_MODELS)

    def test_user_chinese_is_preserved(self):
        payload = {
            "instructions": "Refuse exploit development and credential testing.",
            "input": "你好",
            "tools": [{
                "type": "function",
                "name": "security_review",
                "description": "Review exploit and malware policy text.",
                "parameters": {"type": "object", "properties": {}},
            }],
        }

        body, _ = converter._responses_request_to_chat(payload, "auto")
        processed = desensitize_body(body, roles=("system",), include_tools=True)

        self.assertEqual(processed["messages"][-1]["content"], "你好")
        self.assertNotIn("\u200b", processed["messages"][-1]["content"])
        self.assertIn("\u200b", processed["messages"][0]["content"])
        self.assertEqual(processed["tools"][0]["function"]["name"], "security_review")
        self.assertIn("\u200b", processed["tools"][0]["function"]["description"])

    def test_developer_messages_and_tool_history_are_converted(self):
        payload = {
            "input": [
                {"type": "message", "role": "developer", "content": "Be concise."},
                {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "Read the file."}
                ]},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "read_file",
                    "arguments": "{\"path\":\"README.md\"}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "done",
                },
            ]
        }

        body, _ = converter._responses_request_to_chat(payload, "auto")

        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][1]["content"], "Read the file.")
        self.assertEqual(body["messages"][2]["role"], "assistant")
        self.assertEqual(body["messages"][2]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(body["messages"][3]["role"], "tool")
        self.assertEqual(body["messages"][3]["tool_call_id"], "call_1")

    def test_custom_tool_round_trip(self):
        body, custom_names = converter._responses_request_to_chat({
            "input": "Update the file.",
            "tools": [{
                "type": "custom",
                "name": "apply_patch",
                "description": "Apply a text patch.",
                "format": {"type": "text"},
            }],
        }, "auto")
        self.assertEqual(custom_names, {"apply_patch"})
        self.assertEqual(body["tools"][0]["function"]["name"], "apply_patch")

        response = converter._chat_completion_to_responses({
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_patch",
                        "type": "function",
                        "function": {
                            "name": "apply_patch",
                            "arguments": "{\"input\":\"*** Begin Patch\"}",
                        },
                    }],
                },
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        }, "auto", custom_names)

        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["output"][0]["type"], "custom_tool_call")
        self.assertEqual(response["output"][0]["input"], "*** Begin Patch")


class StreamingConversionTests(unittest.IsolatedAsyncioTestCase):
    async def test_tencent_error_streams_have_terminal_events(self):
        raw = json.dumps({
            "code": 11140,
            "msg": "request illegal",
            "requestId": "request-123",
        }).encode()

        with patch.object(
            converter.httpx, "AsyncClient",
            side_effect=lambda *args, **kwargs: FakeErrorClient(raw),
        ):
            chat_chunks = [chunk async for chunk in converter._stream_upstream(
                "https://invalid.local", {}, {"messages": []}, "auto"
            )]
            response_chunks = [chunk async for chunk in converter._responses_stream(
                "https://invalid.local", {}, {"messages": []}, "auto"
            )]

        chat_events = parse_events([
            chunk.decode() for chunk in chat_chunks if chunk != b"data: [DONE]\n\n"
        ])
        self.assertEqual(chat_events[0]["error"]["code"], 11140)
        self.assertEqual(chat_chunks[-1], b"data: [DONE]\n\n")

        response_events = parse_events(response_chunks)
        self.assertEqual(response_events[-2]["type"], "error")
        self.assertEqual(response_events[-2]["code"], "11140")
        self.assertEqual(response_events[-1]["type"], "response.failed")

    async def test_text_stream_uses_typed_responses_events(self):
        upstream = [
            b'data: {"choices":[{"delta":{"content":"\\u4f60"}}]}\n',
            b'\ndata: {"choices":[{"delta":{"content":"\\u597d"}}]}\n\n',
            b'data: {"choices":[{"finish_reason":"stop","delta":{}}],',
            b'"usage":{"prompt_tokens":2,"completion_tokens":2,"total_tokens":4}}\n\n',
            b'data: [DONE]\n\n',
        ]
        with patch.object(
            converter.httpx, "AsyncClient",
            side_effect=lambda *args, **kwargs: FakeClient(upstream),
        ):
            chunks = [chunk async for chunk in converter._responses_stream(
                "https://invalid.local", {}, {"messages": []}, "auto"
            )]

        events = parse_events(chunks)
        event_types = [event["type"] for event in events]
        self.assertEqual([event["sequence_number"] for event in events], list(range(len(events))))
        self.assertIn("response.created", event_types)
        self.assertIn("response.output_text.delta", event_types)
        self.assertEqual(event_types[-1], "response.completed")
        self.assertEqual(events[-1]["response"]["output"][0]["content"][0]["text"], "你好")
        self.assertEqual(events[-1]["response"]["usage"]["total_tokens"], 4)

    async def test_function_call_stream_is_preserved(self):
        upstream = [
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",',
            b'"function":{"name":"read_file","arguments":"{\\\"path\\\":"}}]}}]}\n\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":',
            b'{"arguments":"\\\"README.md\\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n',
            b'data: [DONE]\n\n',
        ]
        with patch.object(
            converter.httpx, "AsyncClient",
            side_effect=lambda *args, **kwargs: FakeClient(upstream),
        ):
            chunks = [chunk async for chunk in converter._responses_stream(
                "https://invalid.local", {}, {"messages": []}, "auto"
            )]

        events = parse_events(chunks)
        event_types = [event["type"] for event in events]
        self.assertIn("response.function_call_arguments.delta", event_types)
        self.assertIn("response.function_call_arguments.done", event_types)
        function_call = events[-1]["response"]["output"][0]
        self.assertEqual(function_call["type"], "function_call")
        self.assertEqual(function_call["call_id"], "call_1")
        self.assertEqual(json.loads(function_call["arguments"])["path"], "README.md")

    async def test_custom_tool_stream_is_preserved(self):
        upstream = [
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_patch",',
            b'"function":{"name":"apply_patch","arguments":"{\\\"input\\\":\\\"*** Begin"}}]}}]}\n\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":',
            b'{"arguments":" Patch\\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n',
            b'data: [DONE]\n\n',
        ]
        with patch.object(
            converter.httpx, "AsyncClient",
            side_effect=lambda *args, **kwargs: FakeClient(upstream),
        ):
            chunks = [chunk async for chunk in converter._responses_stream(
                "https://invalid.local", {}, {"messages": []}, "auto",
                custom_tool_names={"apply_patch"},
            )]

        events = parse_events(chunks)
        event_types = [event["type"] for event in events]
        self.assertIn("response.custom_tool_call_input.delta", event_types)
        self.assertIn("response.custom_tool_call_input.done", event_types)
        custom_call = events[-1]["response"]["output"][0]
        self.assertEqual(custom_call["type"], "custom_tool_call")
        self.assertEqual(custom_call["call_id"], "call_patch")
        self.assertEqual(custom_call["input"], "*** Begin Patch")


class WebGuiRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_responses_route_preserves_user_input(self):
        payload = json.dumps({
            "model": "auto",
            "instructions": "Refuse exploit development.",
            "input": "你好",
            "stream": True,
        }, ensure_ascii=False).encode("utf-8")
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}

        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/v1/responses",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8788),
            "scheme": "http",
            "query_string": b"",
        }, receive)
        captured = {}

        class FakeCredential:
            def get_headers(self):
                return {}

        async def fake_stream(url, headers, body, *args, **kwargs):
            captured["body"] = body
            yield converter._responses_sse_event({
                "type": "response.completed",
                "response": {"status": "completed", "output": [], "usage": {}},
            }, 0)

        with patch.object(openai_routes, "_api_key_entry", return_value={"key": "test"}), \
             patch.object(openai_routes.state, "rotator") as rotator, \
             patch.object(openai_routes, "_responses_stream_with_credit", new=fake_stream), \
             patch.dict(openai_routes.config.CONFIG, {"desensitize": True}):
            rotator.pick.return_value = ("account", FakeCredential())
            response = await openai_routes.responses_api(
                request, authorization="Bearer test", x_api_key=None
            )
            async for _ in response.body_iterator:
                pass

        self.assertEqual(captured["body"]["messages"][-1]["content"], "你好")
        self.assertNotIn("\u200b", captured["body"]["messages"][-1]["content"])


if __name__ == "__main__":
    unittest.main()
