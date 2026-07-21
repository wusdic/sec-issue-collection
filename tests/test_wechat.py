"""公众号:可信度重定级、黑名单、转载溯源。"""
from app.services import wechat


def test_account_credibility_reclassify():
    # 官方号 → S1(不是渠道默认 S4)
    assert wechat.account_credibility("国家网络安全通报中心", "S4") == "S1"
    # 专业媒体号 → S3
    assert wechat.account_credibility("FreeBuf", "S4") == "S3"
    # 未知号 → 回退渠道默认
    assert wechat.account_credibility("某不知名号", "S4") == "S4"
    # 前后空白/@ 归一
    assert wechat.account_credibility(" @安全内参 ", "S4") == "S3"


def test_blacklist():
    assert wechat.is_blacklisted("网络安全那些事儿营销号示例")
    assert not wechat.is_blacklisted("FreeBuf")


def test_detect_repost_by_source_line():
    text = "某医院遭勒索攻击系统瘫痪。本文转载自公众号安全内参,原文链接:https://mp.weixin.qq.com/s/abc123"
    r = wechat.detect_repost(text)
    assert r["is_repost"]
    assert r["original_account"] == "安全内参"
    assert r["original_wechat_url"].startswith("https://mp.weixin.qq.com/s/")


def test_detect_repost_source_prefix():
    text = "来源:安全客\n某企业发生数据泄露事件..."
    r = wechat.detect_repost(text)
    assert r["is_repost"] and r["original_account"] == "安全客"


def test_original_declaration_not_repost():
    text = "原创声明:本号原创,未经授权禁止转载。某银行系统遭攻击..."
    r = wechat.detect_repost(text)
    assert not r["is_repost"]


def test_plain_article_not_repost():
    text = "某市医院遭勒索软件攻击,系统瘫痪36小时,门诊停诊。"
    assert not wechat.detect_repost(text)["is_repost"]
