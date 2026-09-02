"""LLM 抽象层:OpenAI 兼容接口可插拔;mock 模式离线可用(测试/演示)。

设计约束:LLM 是"读文员"不是"决策者"——输出一律过 JSON Schema 校验,
confirmed 金额通道由 money_guard + 人工复核 + 发布校验三层把关,LLM 无权定稿。
"""
import hashlib
import json
import math
import re

import httpx

from app.config import settings


class LLMError(RuntimeError):
    pass


class BaseLLM:
    def complete_json(self, system: str, user: str, retries: int = 2) -> dict:
        raise NotImplementedError

    def embed(self, text: str) -> list[float]:
        raise NotImplementedError


class OpenAICompatLLM(BaseLLM):
    def __init__(self, base_url: str, api_key: str, model: str,
                 embed_base_url: str = "", embed_api_key: str = "", embed_model: str = "",
                 timeout: float = 120):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        # Embedding 独立配置;留空回退聊天接口/模型
        self.embed_base_url = (embed_base_url or base_url).rstrip("/")
        self.embed_api_key = embed_api_key or api_key
        self.embed_model = embed_model or model
        self._embed_dialect = None  # 首次成功后记住该接口的请求方言,避免每次都试

    def _chat(self, system: str, user: str, use_json_format: bool = True) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": int(getattr(settings, "llm_max_tokens", 0) or 8192),
        }
        # 部分接口(如 MiniMax abab)不支持 response_format,故做成可关闭并自动降级
        if use_json_format:
            body["response_format"] = {"type": "json_object"}
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body, timeout=self.timeout,
            )
        except httpx.TimeoutException as e:
            raise LLMError(f"接口超时(>{self.timeout}s):{type(e).__name__}")
        except httpx.HTTPError as e:
            raise LLMError(f"网络错误:{type(e).__name__}: {e}"[:160])
        if resp.status_code >= 400:
            raise LLMError(f"HTTP {resp.status_code}: {_api_err(resp)}")
        data = resp.json()
        content = _extract_chat_content(data)
        if content is None:
            # HTTP 200 但业务错误(MiniMax base_resp 等)或结构异常
            raise LLMError(_api_err(resp) or f"响应无 choices: {str(data)[:200]}")
        return content

    def complete_json(self, system: str, user: str, retries: int = 2) -> dict:
        last_err = None
        use_json = True
        format_fallback_used = False
        left = retries + 1
        while left > 0:
            try:
                u = user if last_err is None else f"{user}\n\n注意:只输出合法 JSON,不要多余文字。"
                raw = self._chat(system, u, use_json_format=use_json)
                out = _parse_json(raw)
                _trace_llm(system, user, model=self.model, raw=raw, parsed=out)
                return out
            except LLMError as e:
                last_err = str(e)
                # response_format 不被支持 → 关掉该参数再试(不消耗重试次数)
                if use_json and not format_fallback_used and _looks_like_format_unsupported(last_err):
                    use_json = False
                    format_fallback_used = True
                    continue
                # 超时:重试也会超时,直接放弃该篇,避免整批被慢篇卡住
                if "超时" in last_err or "timeout" in last_err.lower():
                    break
                left -= 1
            except json.JSONDecodeError as e:
                last_err = f"输出非 JSON: {e}"
                left -= 1
        _trace_llm(system, user, model=self.model, error=last_err)
        raise LLMError(f"LLM 调用失败: {last_err}")

    def embed(self, text: str) -> list[float]:
        """向量化。不同厂商 embedding 请求格式不同(OpenAI 用 input,MiniMax 用 texts+type),
        自动尝试多种方言,成功后记住,不针对某一家硬编码。"""
        text = text[:8000]
        url = f"{self.embed_base_url}/embeddings"
        headers = {"Authorization": f"Bearer {self.embed_api_key}"}
        dialects = [self._embed_dialect] if self._embed_dialect else _EMBED_DIALECTS
        last_err = None
        for build in dialects:
            payload = build(self.embed_model, text)
            try:
                resp = httpx.post(url, headers=headers, json=payload, timeout=self.timeout)
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                continue
            if resp.status_code >= 400:
                last_err = f"HTTP {resp.status_code}: {_api_err(resp)}"
                continue
            data = resp.json()
            vec = _extract_embedding(data)
            if vec is None:
                last_err = _api_err(resp) or f"格式不识别: {str(data)[:150]}"
                continue
            self._embed_dialect = build  # 记住成功方言
            return vec
        raise LLMError(f"向量接口调用失败: {last_err}")


# ---- 诊断留痕(端到端分析用):记录每次 LLM 调用的提示词与原始返回 ----

def _task_of(system: str) -> str:
    for tag in ("screen", "extract", "relevance", "list_template"):
        if f"TASK={tag}" in (system or ""):
            return tag
    return "other"


def _trace_llm(system: str, user: str, model: str = "", raw: str | None = None,
               parsed=None, error: str | None = None):
    """把一次 LLM 调用记入诊断(有活跃诊断会话时才写);绝不影响主流程。"""
    try:
        from app.services import diagnostics
        if not diagnostics.active():
            return
        task = _task_of(system)
        summary = (f"LLM[{task}] {model or ''} " +
                   ("失败: " + str(error)[:120] if error else "ok"))
        diagnostics.record("llm", summary=summary, detail={
            "task": task, "model": model, "system": system, "user": user,
            "raw_response": raw, "parsed": parsed, "error": error,
        })
    except Exception:  # noqa: BLE001
        pass


# ---- 跨厂商兼容工具:响应解析与错误提取(不针对某一家特判) ----

def _api_err(resp) -> str:
    """从响应体提取错误信息(兼容 OpenAI error / MiniMax base_resp / 通用 message)。"""
    try:
        j = resp.json()
    except Exception:  # noqa: BLE001
        return (resp.text or "")[:300]
    for k in ("error", "base_resp", "message", "msg"):
        if k in j and j[k]:
            v = j[k]
            if isinstance(v, dict):
                # MiniMax base_resp: {status_code, status_msg}
                if v.get("status_code") in (0, None) and k == "base_resp":
                    return ""  # 业务成功
                return json.dumps(v, ensure_ascii=False)[:300]
            return str(v)[:300]
    return ""


def _extract_chat_content(data: dict):
    """兼容多种 chat 返回结构提取正文。"""
    try:
        ch = data.get("choices")
        if ch:
            msg = ch[0].get("message") or {}
            if msg.get("content"):
                return msg["content"]
            if ch[0].get("text"):  # 旧式 completion
                return ch[0]["text"]
    except (KeyError, IndexError, TypeError):
        pass
    return None


# Embedding 请求方言(不同厂商参数名不同,按序尝试):
#  OpenAI/Qwen 用 input;MiniMax 用 texts + type
_EMBED_DIALECTS = [
    lambda model, text: {"model": model, "input": text},
    lambda model, text: {"model": model, "input": [text]},
    lambda model, text: {"model": model, "texts": [text], "type": "db"},
]


def _extract_embedding(data: dict):
    """兼容多种 embedding 返回结构(OpenAI/Qwen data[].embedding、MiniMax vectors、顶层 embedding)。"""
    d = data.get("data")
    if isinstance(d, list) and d and isinstance(d[0], dict) and "embedding" in d[0]:
        return d[0]["embedding"]
    if isinstance(d, dict) and "embedding" in d:
        return d["embedding"]
    v = data.get("vectors")  # MiniMax
    if isinstance(v, list) and v and isinstance(v[0], list):
        return v[0]
    if isinstance(data.get("embedding"), list):
        return data["embedding"]
    out = data.get("output")  # 部分兼容层
    if isinstance(out, dict):
        embs = out.get("embeddings")
        if isinstance(embs, list) and embs and isinstance(embs[0], dict) and "embedding" in embs[0]:
            return embs[0]["embedding"]
    return None


def _looks_like_format_unsupported(err: str) -> bool:
    e = (err or "").lower()
    return any(x in e for x in (
        "response_format", "json_object", "not support", "unsupported",
        "invalid parameter", "unknown", "not allowed", "unexpected", "invalid_request",
    ))


def _strip_fence(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    return raw.strip()


def _extract_balanced(raw: str):
    """从文本中提取第一个平衡的 {...} 或 [...](容忍模型输出前后解释文字)。"""
    start = None
    for i, ch in enumerate(raw):
        if ch in "{[":
            start = i
            open_ch, close_ch = ch, ("}" if ch == "{" else "]")
            break
    if start is None:
        return None
    depth, in_str, esc = 0, False, False
    for j in range(start, len(raw)):
        c = raw[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return raw[start:j + 1]
    return None


def _strip_think(s: str) -> str:
    """去掉推理模型的思维链(<think>…</think>),避免把思维里的示例 JSON 当成答案。"""
    if "</think>" in s:
        s = s.rsplit("</think>", 1)[1]          # 取最后一个 </think> 之后的正文
    return re.sub(r"(?is)<think>.*?</think>", " ", s)


def _iter_balanced(raw: str):
    """扫描出所有顶层平衡的 {...} / [...] 子串。"""
    i, n = 0, len(raw)
    while i < n:
        if raw[i] in "{[":
            open_ch = raw[i]
            close_ch = "}" if open_ch == "{" else "]"
            depth, in_str, esc, j, done = 0, False, False, i, False
            while j < n:
                c = raw[j]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                else:
                    if c == '"':
                        in_str = True
                    elif c == open_ch:
                        depth += 1
                    elif c == close_ch:
                        depth -= 1
                        if depth == 0:
                            yield raw[i:j + 1]
                            i, done = j + 1, True
                            break
                j += 1
            if not done:
                i += 1
        else:
            i += 1


def _best_json_object(raw: str) -> dict | None:
    """从文本里挑出最像答案的 JSON 对象:所有能解析的平衡块中取最大的 dict(list 则取其中 dict)。"""
    best = None
    for blk in _iter_balanced(raw):
        try:
            v = json.loads(blk)
        except json.JSONDecodeError:
            continue
        if isinstance(v, list):
            v = next((x for x in v if isinstance(x, dict)), None)
        if isinstance(v, dict) and (best is None or len(blk) > best[0]):
            best = (len(blk), v)
    return best[1] if best else None


def _parse_json(raw: str) -> dict:
    """鲁棒 JSON 解析:去 fence/思维链 → 直接解析(list 取其中 dict)→ 取最大平衡对象。

    兼容推理模型(MiniMax-M3 等)先输出 <think> 再给 JSON、以及把记录包成数组的情况。
    """
    cleaned = _strip_think(_strip_fence(raw))
    try:
        v = json.loads(cleaned)
        if isinstance(v, list):
            v = next((x for x in v if isinstance(x, dict)), None)
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass
    for src in (cleaned, raw):          # 先在去思维链后的正文找,再退回原文兜底
        obj = _best_json_object(src)
        if obj is not None:
            return obj
    raise json.JSONDecodeError("未找到 JSON 对象", cleaned[:120], 0)


_ENVELOPE = {"event_id", "status", "review", "confidence_overall", "completeness_score", "change_log", "sources"}
_UNINFORMATIVE_PREF = ("未披露", "未知", "不明", "其他", "无", "未定级")
_NEED_RE = re.compile(r"NEED_ID=(\S+)")
_SCHEMA_RE = re.compile(r"SCHEMA_FILE=(\S+)")


def _resolve_node(node: dict, root: dict) -> dict:
    """展开 $ref / allOf / anyOf / oneOf,得到一个可直接生成骨架的节点。"""
    seen = 0
    while isinstance(node, dict) and seen < 8:
        seen += 1
        if "$ref" in node:
            ref = str(node["$ref"])
            cur = root
            for part in ref.lstrip("#/").split("/"):
                cur = (cur or {}).get(part) if isinstance(cur, dict) else None
            node = cur or {}
            continue
        if "allOf" in node and node["allOf"]:
            merged: dict = {}
            for sub in node["allOf"]:
                sub = _resolve_node(sub, root)
                for k, v in sub.items():
                    if k == "properties":
                        merged.setdefault("properties", {}).update(v)
                    elif k == "required":
                        merged["required"] = list(dict.fromkeys(list(merged.get("required") or []) + list(v)))
                    else:
                        merged.setdefault(k, v)
            node = {**{k: v for k, v in node.items() if k != "allOf"}, **merged}
            continue
        for key in ("anyOf", "oneOf"):
            if key in node and node[key]:
                cands = [c for c in node[key] if _resolve_node(c, root).get("type") != "null"]
                node = _resolve_node(cands[0] if cands else node[key][0], root)
                break
        else:
            break
    return node if isinstance(node, dict) else {}


def _skeleton(node: dict, root: dict, depth: int = 0):
    """按 Schema 生成最小合法值:required 子键全填;枚举优先取『未披露/未知』类;数组给空。"""
    node = _resolve_node(node, root)
    if "enum" in node:
        enum = [e for e in node["enum"] if e is not None]
        for pref in _UNINFORMATIVE_PREF:
            if pref in enum:
                return pref
        return enum[0] if enum else None
    t = node.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "null")
    if t == "object" or (t is None and node.get("properties")):
        out = {}
        props = node.get("properties") or {}
        for r in node.get("required") or []:
            if depth == 0 and r in _ENVELOPE:
                continue
            out[r] = _skeleton(props.get(r, {}), root, depth + 1)
        return out
    if t == "array":
        # minItems≥1 的数组给一个"未披露"型条目,否则 strict 校验过不去
        if int(node.get("minItems") or 0) >= 1 and depth < 6:
            item = _skeleton(node.get("items") or {"type": "string"}, root, depth + 1)
            return [item] if item not in (None, {}) else ["未披露"]
        return []
    if t == "string":
        if node.get("format") == "date":
            from datetime import date as _d
            return _d.today().isoformat()
        if node.get("format") == "date-time":
            from datetime import datetime as _dt
            return _dt.utcnow().isoformat(timespec="seconds")
        return "未披露"
    if t in ("number", "integer"):
        return None if isinstance(node.get("type"), list) else 0
    if t == "boolean":
        return False
    return None


def _set_path(obj: dict, path: str, value, append: bool = False):
    parts = [x for x in str(path).split(".") if x]
    cur = obj
    for k in parts[:-1]:
        if not isinstance(cur.get(k), dict):
            cur[k] = {}
        cur = cur[k]
    last = parts[-1]
    if append:
        lst = cur.get(last)
        if not isinstance(lst, list):
            lst = []
        if value not in lst:
            lst.append(value)
        cur[last] = lst
    else:
        cur[last] = value


def _get_path(obj, path: str):
    cur = obj
    for k in [x for x in str(path).split(".") if x]:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _transform(v, how):
    if how in (None, "", "str"):
        return v
    try:
        if how == "wan":
            return float(v) * 10000
        if how == "float":
            return float(v)
        if how == "int":
            return int(float(v))
    except (TypeError, ValueError):
        return v
    return v


class MockLLM(BaseLLM):
    """离线确定性模拟(通用平台):粗筛按画像 mock.screen_keywords 的命中数打分;抽取按记录 Schema
    生成最小合法骨架,再套画像 mock.extract_rules(正则 / 包含词 → 字段)。用哪个画像由提示词里的
    NEED_ID / SCHEMA_FILE 标记决定(prompts.py 注入),没有标记就用默认需求。

    仅用于测试与离线演示,产出质量不代表真实 LLM;真实部署设 LLM_PROVIDER=openai_compat。
    """

    def _ctx_from(self, system: str):
        from app.services import need_ctx
        m = _NEED_RE.search(system or "")
        return need_ctx.get(None, m.group(1) if m else need_ctx.default_need_id())

    def _keywords(self, ctx) -> list[str]:
        kws = [str(k) for k in (ctx.mock.get("screen_keywords") or []) if str(k)]
        if kws:
            return kws
        # 画像没给 mock 关键词:退回找源词表里的事件词/主体词,再不行就空(视为一律相关)
        r = ctx.query_recipes
        for k in ("event_terms", "subject_terms"):
            kws += [str(x) for x in (r.get(k) or []) if str(x)]
        return kws

    def complete_json(self, system: str, user: str, retries: int = 2) -> dict:
        out = self._mock_json(system, user)
        _trace_llm(system, user, model="mock", raw=None, parsed=out)
        return out

    def _mock_json(self, system: str, user: str) -> dict:
        if "TASK=screen" in system:
            ctx = self._ctx_from(system)
            kws = self._keywords(ctx)
            if not kws:
                return {"is_candidate": True, "relevance": 0.6, "confidence": 0.6,
                        "reason": "画像无 mock 关键词,默认相关(mock)"}
            hit = sum(1 for k in kws if k in user)
            score = min(0.95, 0.25 + hit * 0.2)
            return {"is_candidate": score >= 0.55, "confidence": round(score, 2),
                    "relevance": round(score, 2), "reason": f"关键词命中 {hit} 个(mock)"}
        if "TASK=extract" in system:
            ctx = self._ctx_from(system)
            m = _SCHEMA_RE.search(system or "")
            return self._mock_extract(user, ctx=ctx, schema_path=m.group(1) if m else None)
        if "TASK=relevance" in system:
            ctx = self._ctx_from(system)
            hit = sum(1 for k in self._keywords(ctx) if k in user)
            return {"score": min(1.0, hit * 0.2), "reason": "mock"}
        if "TASK=list_template" in system:
            return {"item_selector": "a", "title_from": "text", "url_from": "href", "confidence": 0.5}
        if "terms" in system and "JSON" in system:      # 挖词提示词(query_evolution.harvest)
            return {"terms": []}
        return {}

    def _mock_extract(self, text: str, ctx=None, schema_path: str | None = None) -> dict:
        from app.services import need_ctx
        c = ctx or need_ctx.get(None, need_ctx.default_need_id())
        try:
            from app.services.extraction import load_record_schema
            schema = load_record_schema(schema_path or c.schema_file)
        except Exception:  # noqa: BLE001 没有 Schema 也要能产出(测试/演示)
            schema = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}
        out = _skeleton(schema, schema) or {}
        m = re.search(r"标题[::]\s*([^\n]+)", text or "")
        title = (m.group(1).strip() if m else (text or "").strip().split("\n")[0][:60]).strip()
        out["title"] = title or "未披露"
        rt = c.record_types
        if "record_type" in (schema.get("properties") or {}) or rt.get("values"):
            out["record_type"] = rt.get("default") or ((rt.get("values") or ["单一记录"])[0])
        spans: dict[str, str] = {}
        for rule in c.mock.get("extract_rules") or []:
            path = str(rule.get("path") or "")
            if not path:
                continue
            value = None
            hit = False
            if rule.get("regex"):
                try:
                    mm = re.search(str(rule["regex"]), text or "")
                except re.error:
                    mm = None
                if mm:
                    hit = True
                    value = mm.group(1) if mm.groups() else mm.group(0)
                    spans.setdefault(path.split(".")[0], mm.group(0))
            elif rule.get("when_contains"):
                needles = rule["when_contains"] if isinstance(rule["when_contains"], list) else [rule["when_contains"]]
                hit = any(str(n) in (text or "") for n in needles)
            elif rule.get("when_path_contains"):
                wp = rule["when_path_contains"] or {}
                cur = _get_path(out, str(wp.get("path") or ""))
                hit = bool(cur) and str(wp.get("needle") or "") in str(cur)
            else:
                hit = True
            if not hit:
                continue
            if "value" in rule:
                value = rule["value"]
            if "append" in rule:
                _set_path(out, path, rule["append"], append=True)
                continue
            if value is None:
                continue
            _set_path(out, path, _transform(value, rule.get("transform")))
        # 抽到了赎金/金额之类的『要求/声称』值时,标记 applicable(若 Schema 有该布尔位)
        for k, v in list(out.items()):
            if isinstance(v, dict) and "applicable" in v and any(
                    vv not in (None, "", [], {}, False, 0) for kk, vv in v.items() if kk != "applicable"):
                v["applicable"] = True
        if spans:
            out["_source_spans"] = spans
        return out

    def embed(self, text: str) -> list[float]:
        """确定性伪向量:字符 n-gram 哈希桶,可用于相似度(非语义,仅测试)。"""
        dim = 256
        v = [0.0] * dim
        t = re.sub(r"\s+", "", text or "")
        for i in range(len(t) - 1):
            g = t[i : i + 2]
            h = int(hashlib.md5(g.encode()).hexdigest()[:8], 16)
            v[h % dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


_client: BaseLLM | None = None
_screen_client: BaseLLM | None = None


def _build(model: str) -> BaseLLM:
    if settings.llm_provider == "openai_compat" and settings.llm_base_url:
        return OpenAICompatLLM(
            settings.llm_base_url, settings.llm_api_key, model,
            settings.llm_embed_base_url, settings.llm_embed_api_key, settings.llm_embed_model,
            timeout=float(getattr(settings, "llm_timeout", 90) or 90),
        )
    return MockLLM()


def get_llm() -> BaseLLM:
    """抽取/通用客户端(用 llm_model)。"""
    global _client
    if _client is None:
        _client = _build(settings.llm_model)
    return _client


def get_screen_llm() -> BaseLLM:
    """粗筛客户端:配了 llm_screen_model 就用小模型(省钱提速),否则回退抽取模型。"""
    global _screen_client
    if _screen_client is None:
        model = (settings.llm_screen_model or "").strip() or settings.llm_model
        _screen_client = _build(model) if model != settings.llm_model else get_llm()
    return _screen_client


def set_llm(client: BaseLLM):
    """注入客户端(测试/离线用)。同时覆盖粗筛客户端,否则粗筛仍会走真实接口。"""
    global _client, _screen_client
    _client = client
    _screen_client = client


def reset():
    """清空客户端缓存(配置变更后调用,下次 get_llm/get_screen_llm 用新配置重建)。"""
    global _client, _screen_client
    _client = None
    _screen_client = None
