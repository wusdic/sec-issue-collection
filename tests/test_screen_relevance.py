"""粗筛阈值必须用『相关度』而非『判断置信度』。

历史 bug:模型判"不相关"且很有把握时 confidence=0.99,代码拿它当相关度比阈值,
0.99 ≥ 待定阈值 → 反而被塞进人工队列。实测一轮采集 69 篇里 63 篇(91%)中招,
"习近平会见哈萨克斯坦总统托卡耶夫"这类时政新闻因此出现在待人工列表里。
"""
import pytest

from app.services import llm
from app.services.extraction import screen_document


class _Fake(llm.BaseLLM):
    def __init__(self, out):
        self.out = out

    def complete_json(self, system, user, retries=2):
        return self.out

    def embed(self, text):
        return [0.0]


@pytest.fixture(autouse=True)
def _restore():
    yield
    llm.reset()


def _screen(out):
    llm.set_llm(_Fake(out))
    return screen_document({}, "标题", "正文")


def test_confident_irrelevant_gets_low_relevance():
    """判不相关 + 高把握 → 相关度必须低(此前会得 0.99 被留进人工队列)。"""
    v = _screen({"is_candidate": False, "confidence": 0.99, "reason": "外交会见新闻"})
    assert v["is_candidate"] is False
    assert v["confidence"] <= 0.05          # 相关度极低 → 会被直接丢弃
    assert v["judge_confidence"] == 0.99    # 判断把握度单独保留


def test_unsure_irrelevant_stays_mid_for_human_review():
    """判不相关但没把握 → 相关度居中,合理地进人工待定。"""
    v = _screen({"is_candidate": False, "confidence": 0.5, "reason": "拿不准"})
    assert 0.3 <= v["confidence"] <= 0.7


def test_relevant_keeps_high_relevance():
    v = _screen({"is_candidate": True, "confidence": 0.9, "reason": "某公司数据泄露"})
    assert v["is_candidate"] is True and v["confidence"] >= 0.85


def test_explicit_relevance_field_wins():
    """模型按新提示词输出 relevance 时以它为准。"""
    v = _screen({"is_candidate": False, "relevance": 0.02, "confidence": 0.99, "reason": "时政"})
    assert v["confidence"] == pytest.approx(0.02)


def test_relevance_clamped():
    v = _screen({"is_candidate": True, "relevance": 3.7, "confidence": 0.8})
    assert 0.0 <= v["confidence"] <= 1.0


def test_politics_news_is_discarded_not_queued():
    """端到端:时政新闻应被判为不相干丢弃,而不是进人工队列。"""
    from app.config import settings
    v = _screen({"is_candidate": False, "confidence": 0.99,
                 "reason": "这是外交会见新闻报道,与网络安全事件无关"})
    rel = v["confidence"]
    assert rel < settings.screen_manual_threshold, "应低于待定阈值 → 直接丢弃,不进人工队列"
