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
    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def _chat(self, system: str, user: str) -> str:
        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def complete_json(self, system: str, user: str, retries: int = 2) -> dict:
        last_err = None
        for attempt in range(retries + 1):
            try:
                raw = self._chat(system, user if attempt == 0 else f"{user}\n\n上次输出不是合法 JSON({last_err}),请修正。")
                return json.loads(_strip_fence(raw))
            except (json.JSONDecodeError, httpx.HTTPError) as e:  # noqa: PERF203
                last_err = str(e)
        raise LLMError(f"LLM JSON 输出失败: {last_err}")

    def embed(self, text: str) -> list[float]:
        resp = httpx.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": settings.llm_model, "input": text[:8000]},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


def _strip_fence(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    return raw


class MockLLM(BaseLLM):
    """离线确定性模拟:粗筛按安全关键词;抽取产出最小合法骨架。

    仅用于测试与离线演示,产出质量不代表真实 LLM;
    真实部署设 LLM_PROVIDER=openai_compat。
    """

    SEC_KEYWORDS = ["勒索", "数据泄露", "攻击", "泄露", "网络安全", "黑客", "瘫痪", "处罚", "内鬼"]

    def complete_json(self, system: str, user: str, retries: int = 2) -> dict:
        if "TASK=screen" in system:
            hit = sum(1 for k in self.SEC_KEYWORDS if k in user)
            score = min(0.95, 0.2 + hit * 0.18)
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
            _client = OpenAICompatLLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
        else:
            _client = MockLLM()
    return _client


def set_llm(client: BaseLLM):
    global _client
    _client = client
