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
        }
        # 部分接口(如 MiniMax abab)不支持 response_format,故做成可关闭并自动降级
        if use_json_format:
            body["response_format"] = {"type": "json_object"}
        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=body, timeout=self.timeout,
        )
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


def _parse_json(raw: str) -> dict:
    """鲁棒 JSON 解析:去 markdown fence → 直接解析 → 提取平衡括号子串再解析。"""
    cleaned = _strip_fence(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        sub = _extract_balanced(cleaned)
        if sub:
            return json.loads(sub)
        raise


class MockLLM(BaseLLM):
    """离线确定性模拟:粗筛按安全关键词;抽取产出最小合法骨架。

    仅用于测试与离线演示,产出质量不代表真实 LLM;
    真实部署设 LLM_PROVIDER=openai_compat。
    """

    SEC_KEYWORDS = ["勒索", "数据泄露", "信息泄露", "泄露", "攻击", "网络攻击", "网络安全",
                    "数据安全", "黑客", "入侵", "瘫痪", "宕机", "故障", "处罚", "罚款", "约谈",
                    "通报", "内鬼", "倒卖", "个人信息", "篡改", "DDoS", "木马", "漏洞", "暗网",
                    "拖库", "撞库", "钓鱼", "诈骗", "判决", "侵犯公民个人信息"]

    def complete_json(self, system: str, user: str, retries: int = 2) -> dict:
        out = self._mock_json(system, user)
        _trace_llm(system, user, model="mock", raw=None, parsed=out)
        return out

    def _mock_json(self, system: str, user: str) -> dict:
        if "TASK=screen" in system:
            hit = sum(1 for k in self.SEC_KEYWORDS if k in user)
            score = min(0.95, 0.25 + hit * 0.2)
            return {"is_candidate": score >= 0.55, "confidence": round(score, 2),
                    "reason": f"关键词命中 {hit} 个(mock)"}
        if "TASK=extract" in system:
            return self._mock_extract(user)
        if "TASK=relevance" in system:
            hit = sum(1 for k in self.SEC_KEYWORDS if k in user)
            return {"score": min(1.0, hit * 0.2), "reason": "mock"}
        if "TASK=list_template" in system:
            return {"item_selector": "a", "title_from": "text", "url_from": "href", "confidence": 0.5}
        return {}

    def _mock_extract(self, text: str) -> dict:
        org = None
        m = re.search(r"([一-鿿]{2,12}(?:医院|银行|集团|公司|大学|厂))", text)
        if m:
            org = m.group(1)
        ransom_amount = None
        rm = re.search(r"(?:勒索|要求支付|索要)[^。]{0,20}?(\d+(?:\.\d+)?)\s*万", text)
        if rm:
            ransom_amount = float(rm.group(1)) * 10000
        attack = []
        if "勒索" in text:
            attack.append("勒索软件")
        if "泄露" in text:
            attack.append("数据泄露(渠道未明)")
        if not attack:
            attack = ["其他"]
        consequences = []
        if "加密" in text or "勒索" in text:
            consequences.append("数据被加密或破坏")
        if "停" in text or "瘫痪" in text:
            consequences.append("业务中断")
        if "泄露" in text:
            consequences.append("数据泄露")
        if not consequences:
            consequences = ["未披露"]
        return {
            "title": (org or "未披露单位") + attack[0] + "事件",
            "occurred_date": {"date": "2026-07-01", "precision": "月"},
            "disclosed_date": "2026-07-15",
            "industry": {"level1": "医疗卫生" if org and "医院" in org else "其他"},
            "region": {"province": "未知"},
            "org_type": "医院" if org and "医院" in org else "其他",
            "org_name": org or "未披露",
            "org_size": "未知",
            "severity": {"level": "未定级", "auto_suggested": True},
            "affected_systems": ["未披露"],
            "attack_type": attack,
            "consequences": consequences,
            "entry_vector": [{"vector": "未知", "confidence": "推测"}],
            "root_cause": {"category": "未披露"},
            "security_controls": [{"control": "整体不明", "status": "不明"}],
            "downtime_hours": {"value": None, "status": "未披露"},
            "loss_L1": {"status": "未披露"},
            "loss_L2": {"status": "未披露"},
            "loss_L3": {"status": "未披露"},
            "loss_L4": {"status": "未披露"},
            "loss_L5": {"status": "未披露"},
            "loss_L6": {"severity": "无"},
            "ransom": {
                "applicable": bool(ransom_amount),
                "demanded_amount": ransom_amount,
                "demanded_currency": "CNY" if ransom_amount else None,
                "paid": "未披露" if ransom_amount else None,
            },
            "affected_users": {"status": "未披露"},
            "affected_records": {"status": "未披露"},
            "secondary_impact": ["未披露"],
            "disclosure_channel": ["媒体曝光"],
            "remediation_actions": [],
            "accountability": {"budget_approver": "未披露"},
            "sellable_mapping": [],
            "_source_spans": {"ransom": rm.group(0) if rm else None},
        }

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


def get_llm() -> BaseLLM:
    global _client
    if _client is None:
        if settings.llm_provider == "openai_compat" and settings.llm_base_url:
            _client = OpenAICompatLLM(
                settings.llm_base_url, settings.llm_api_key, settings.llm_model,
                settings.llm_embed_base_url, settings.llm_embed_api_key, settings.llm_embed_model,
            )
        else:
            _client = MockLLM()
    return _client


def set_llm(client: BaseLLM):
    global _client
    _client = client


def reset():
    """清空客户端缓存(配置变更后调用,下次 get_llm 用新配置重建)。"""
    global _client
    _client = None
