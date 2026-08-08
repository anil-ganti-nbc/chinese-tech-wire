"""Scoring unit tests."""

from pipeline.score import score_article, detect_rumor, score_relevance


def test_high_relevance_gpu_leak():
    title = "英伟达 RTX 5090 工程样机跑分曝光，性能大幅提升"
    result = score_article(title, source="ithome")
    assert result["relevance_score"] >= 70
    assert result["priority_score"] >= 65
    assert result["rumor_flag"] is True
    names = [e.name for e in result["entities"]]
    assert any("Nvidia" in n or "RTX" in n for n in names)


def test_noise_shopping():
    title = "今日优惠券大全：京东手机配件打折导购"
    result = score_article(title, source="zol")
    assert result["relevance_score"] < 40
    assert result["priority_score"] <= 55


def test_rumor_detect():
    flag, conf = detect_rumor("消息人士称供应链曝光新芯片")
    assert flag is True
    assert conf > 0.3


from pipeline.entities import extract_entities, EntityType
from pipeline.upstream import detect_upstream


def test_entity_alias_nvidia():
    ents = extract_entities("英伟达 RTX 5090 工程样机曝光")
    names = {e.normalized for e in ents}
    assert "Nvidia" in names
    assert any(e.type == EntityType.COMPANY for e in ents)
    # RTX should appear as chip/product
    assert any("RTX" in (e.name or "") for e in ents)


def test_entity_alias_tsmc_cxmt():
    ents = extract_entities("台积电与长鑫存储合作推进国产DRAM")
    names = {e.normalized for e in ents}
    assert "TSMC" in names
    assert "CXMT" in names


def test_upstream_leak_zh():
    st, up, _ = detect_upstream("消息人士称供应链曝光新一代显卡规格")
    assert st == "LEAK"


def test_upstream_retail():
    st, up, _ = detect_upstream("RTX 5090 已在京东上架")
    assert st == "RETAIL_LISTING"
    assert up is not None


def test_upstream_official():
    st, _, _ = detect_upstream("AMD 正式发布 Ryzen 9000 系列处理器")
    assert st == "OFFICIAL"
