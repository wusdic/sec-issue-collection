"""跨厂商 LLM 兼容:JSON 解析鲁棒、多种返回结构、response_format 降级。"""
from app.services import llm
from app.services.llm import (
    OpenAICompatLLM, _extract_balanced, _extract_chat_content, _extract_embedding,
    _looks_like_format_unsupported, _parse_json,
)


def test_parse_json_plain():
    assert _parse_json('{"ok": true}') == {"ok": True}


def test_parse_json_with_fence():
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_with_surrounding_text():
    # 模型输出前后带解释文字(不严格纯 JSON)
    raw = '好的,结果如下:\n{"title": "某医院遭勒索", "n": 2}\n以上。'
    assert _parse_json(raw) == {"title": "某医院遭勒索", "n": 2}


def test_extract_balanced_nested():
    raw = 'x {"a": {"b": [1,2]}, "c": "}"} y'
    assert _parse_json(raw) == {"a": {"b": [1, 2]}, "c": "}"}


def test_chat_content_openai_shape():
    assert _extract_chat_content({"choices": [{"message": {"content": "hi"}}]}) == "hi"


def test_chat_content_missing_returns_none():
    # MiniMax HTTP200 业务错误:无 choices
    assert _extract_chat_content({"base_resp": {"status_code": 1004, "status_msg": "auth failed"}}) is None


def test_embedding_openai_shape():
    assert _extract_embedding({"data": [{"embedding": [0.1, 0.2]}]}) == [0.1, 0.2]


def test_embedding_minimax_vectors_shape():
    # MiniMax 返回 vectors 而非 data[].embedding
    assert _extract_embedding({"vectors": [[0.3, 0.4, 0.5]]}) == [0.3, 0.4, 0.5]


def test_embedding_toplevel_shape():
    assert _extract_embedding({"embedding": [1.0, 2.0]}) == [1.0, 2.0]


def test_embedding_unknown_returns_none():
    assert _extract_embedding({"weird": 1}) is None


def test_format_unsupported_detection():
    assert _looks_like_format_unsupported("HTTP 400: invalid parameter response_format")
    assert _looks_like_format_unsupported("unknown field json_object")
    assert not _looks_like_format_unsupported("HTTP 401: auth failed")


def test_embed_dialect_fallback(monkeypatch):
    """embedding 请求方言自动降级:OpenAI input 报错 → 切 MiniMax texts 成功,并记住方言。"""
    import json as _json
    calls = []

    class R:
        def __init__(self, code, body):
            self.status_code = code
            self._b = body
            self.text = _json.dumps(body)
        def json(self):
            return self._b

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        if "input" in json:  # OpenAI 方言 → MiniMax 报缺 texts
            return R(200, {"vectors": None, "base_resp": {"status_code": 2013, "status_msg": "missing texts"}})
        if "texts" in json:  # MiniMax 方言 → 成功
            return R(200, {"vectors": [[0.1, 0.2]], "base_resp": {"status_code": 0}})
        return R(400, {"error": "bad"})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    c = OpenAICompatLLM("http://x/v1", "k", "m", embed_model="embo-01")
    assert c.embed("文本") == [0.1, 0.2]
    calls.clear()
    c.embed("再来")  # 已记住方言,直接用 texts
    assert "texts" in calls[0] and len(calls) == 1


def test_response_format_fallback(monkeypatch):
    """response_format 报错时自动关闭并重试成功(不消耗 retries)。"""
    c = OpenAICompatLLM("http://x/v1", "k", "m")
    calls = []

    def fake_chat(system, user, use_json_format=True):
        calls.append(use_json_format)
        if use_json_format:
            raise llm.LLMError("HTTP 400: response_format not supported")
        return '{"ok": true}'

    monkeypatch.setattr(c, "_chat", fake_chat)
    out = c.complete_json("sys", "user", retries=0)  # retries=0 也应触发降级
    assert out == {"ok": True}
    assert calls == [True, False]  # 先带 format 失败,再不带 format 成功
