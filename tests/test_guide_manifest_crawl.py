"""guide_manifest_crawl 单元测试：全程 monkeypatch 打桩，不联网。"""
import os
import sys

# 爬虫脚本用扁平 import（from legal_crawl_common import ...），需把 scripts/crawl 加入 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts", "crawl")))

import guide_manifest_crawl as gmc


def test_normalize_title_strips_brackets_and_suffix():
    assert gmc.normalize_title("《中华全国律师协会律师办理合同审查业务操作指引》") == \
        "中华全国律师协会律师办理合同审查业务操作指引"
    # 去 (修订版)/（试行）尾注 + 空白，使同一指引不同版本归一为同一 key
    assert gmc.normalize_title("律师办理商业秘密法律业务操作指引（修订版）") == \
        gmc.normalize_title("律师办理商业秘密法律业务操作指引")
    assert gmc.normalize_title("  律师 办理 买卖合同 操作指引 ") == "律师办理买卖合同操作指引"


def test_validate_entry_rules():
    # 合法：title + 合法 source_type，source_url 允许为空（留待人工补源）
    assert gmc.validate_entry({"title": "X 指引", "source_type": "html", "source_url": "http://a"}) is None
    assert gmc.validate_entry({"title": "X 指引", "source_type": "pdf", "source_url": ""}) is None
    # 缺 title
    assert "title" in gmc.validate_entry({"title": "  ", "source_type": "html"})
    # source_type 非法
    assert "source_type" in gmc.validate_entry({"title": "X", "source_type": "doc"})


def test_extract_html_body_picks_main_block_and_strips_chrome():
    html = """
    <html><body>
      <nav>首页 业务进阶 登录</nav>
      <header>某某律协</header>
      <div class="main">
        <h1>律师办理合同审查业务操作指引</h1>
        <p>目录 第一章 总则 第1条 本指引所称合同审查……（正文很长）</p>
      </div>
      <footer>版权所有 京ICP备</footer>
    </body></html>
    """
    body = gmc.extract_html_body(html)
    assert "合同审查" in body
    assert "京ICP备" not in body and "业务进阶" not in body  # 导航/页脚被剥离


def test_passes_quality_gate():
    long_struct = "第一章 总则\n第1条 " + "审查要点。" * 600  # 远超 2000 字且含结构信号
    assert gmc.passes_quality(long_struct) is True
    assert gmc.passes_quality("太短没有结构") is False          # 长度不足
    assert gmc.passes_quality("无结构信号" * 600) is False        # 够长但无「目录/第N章/第N条/指引」


def _paths(tmp_path):
    return {"html": str(tmp_path / "raw_html"), "attachments": str(tmp_path / "attachments")}


def test_fetch_entry_body_html_first_success(tmp_path, monkeypatch):
    os.makedirs(tmp_path / "raw_html"); os.makedirs(tmp_path / "attachments")
    good = "第一章 总则 第1条 " + "审查要点。" * 600
    monkeypatch.setattr(gmc, "fetch_url", lambda url, cache_path=None, use_cache=True: f"<div>{good}</div>")
    entry = {"title": "X 指引", "source_type": "html", "source_url": "http://primary", "fallback_urls": []}
    body, url_used, atts, errors, net = gmc.fetch_entry_body(entry, _paths(tmp_path), use_cache=False)
    assert "审查要点" in body and url_used == "http://primary" and errors == []


def test_fetch_entry_body_falls_back_when_primary_too_short(tmp_path, monkeypatch):
    os.makedirs(tmp_path / "raw_html"); os.makedirs(tmp_path / "attachments")
    good = "第一章 总则 第1条 " + "审查要点。" * 600
    def fake_fetch(url, cache_path=None, use_cache=True):
        return "<div>太短</div>" if url == "http://primary" else f"<div>{good}</div>"
    monkeypatch.setattr(gmc, "fetch_url", fake_fetch)
    entry = {"title": "X", "source_type": "html", "source_url": "http://primary",
             "fallback_urls": ["http://backup"]}
    body, url_used, atts, errors, net = gmc.fetch_entry_body(entry, _paths(tmp_path), use_cache=False)
    assert url_used == "http://backup" and "审查要点" in body


def test_fetch_entry_body_pdf(tmp_path, monkeypatch):
    os.makedirs(tmp_path / "raw_html"); os.makedirs(tmp_path / "attachments")
    good = "第一章 总则 第1条 " + "审查要点。" * 600
    monkeypatch.setattr(gmc, "download_file", lambda url, dest, referer=None: str(tmp_path / "f.pdf"))
    monkeypatch.setattr(gmc, "extract_file_text", lambda path: good)
    entry = {"title": "X", "source_type": "pdf", "source_url": "http://a/x.pdf", "fallback_urls": []}
    body, url_used, atts, errors, net = gmc.fetch_entry_body(entry, _paths(tmp_path), use_cache=False)
    assert "审查要点" in body and atts and atts[0]["text_extracted"] is True


def test_fetch_entry_body_all_fail_records_errors(tmp_path, monkeypatch):
    os.makedirs(tmp_path / "raw_html"); os.makedirs(tmp_path / "attachments")
    def boom(url, cache_path=None, use_cache=True):
        raise RuntimeError("boom")
    monkeypatch.setattr(gmc, "fetch_url", boom)
    entry = {"title": "X", "source_type": "html", "source_url": "http://a", "fallback_urls": ["http://b"]}
    body, url_used, atts, errors, net = gmc.fetch_entry_body(entry, _paths(tmp_path), use_cache=False)
    assert body == "" and url_used is None and len(errors) == 2


def test_normalize_row_matches_guide_schema():
    body = "第一章 总则。试行一年。本指引于2022年12月20日通过。" + "内容。" * 100
    entry = {"title": "律师办理合同审查业务操作指引", "book": "指引①",
             "source_type": "html", "source_site": "binzhoulvxie.com"}
    row = gmc.normalize_row(entry, body, "http://primary", [])
    # guide schema 必备键齐全（缺一则 filter/markdown 会 KeyError）
    required = {"source_layer", "source_site", "association", "url", "list_url", "category",
                "is_priority_seed", "title_from_list", "publish_date_from_list", "title",
                "committee", "author", "source", "publish_date", "passed_date", "trial_period",
                "attachments", "body", "body_len", "html_sha256", "crawl_time"}
    assert required.issubset(row.keys())
    assert row["association"] == "全国律协" and row["from_guidebook"] is True
    assert row["book"] == "指引①" and row["is_priority_seed"] is False
    assert row["url"] == "http://primary" and row["body_len"] == len(body)
    assert row["trial_period"] == "试行一年" and row["passed_date"] == "2022年12月20日"


def test_dedupe_by_title_prefers_body_and_dedups_variants():
    rows = [
        {"title": "律师办理X业务操作指引（修订版）", "body": ""},        # 空正文，应被同名有正文者顶掉
        {"title": "《律师办理X业务操作指引》", "body": "有正文"},        # 与上同名（归一后）
        {"title": "律师办理Y业务操作指引", "body": "另一篇"},
    ]
    out = gmc.dedupe_by_title(rows)
    titles = {gmc.normalize_title(r["title"]) for r in out}
    assert titles == {"律师办理X业务操作指引", "律师办理Y业务操作指引"}
    x = [r for r in out if gmc.normalize_title(r["title"]) == "律师办理X业务操作指引"][0]
    assert x["body"] == "有正文"   # 有正文的版本胜出


import json


def test_run_manifest_end_to_end(tmp_path, monkeypatch):
    good = "第一章 总则 第1条 " + "审查要点。" * 600
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
        {"title": "律师办理合同审查业务操作指引", "book": "指引①", "source_type": "html",
         "source_url": "http://ok", "source_site": "a.cn"},
        {"title": "律师从事税法服务业务操作指引", "book": "指引③", "source_type": "html",
         "source_url": "", "source_site": "acla"},                                  # 空 url → unresolved
        {"title": "律师办理建设工程法律业务操作指引", "book": "指引④", "source_type": "html",
         "source_url": "http://broken", "source_site": "b.cn"},                     # 抓不到 → extract_failed
        {"title": "  ", "source_type": "html", "source_url": "http://x"},           # 缺 title → unresolved
    ]) + "\n", encoding="utf-8")

    def fake_fetch(entry, paths, use_cache):
        if entry.get("source_url") == "http://ok":
            return good, "http://ok", [], [], True
        return "", None, [], [{"title": entry.get("title"), "url": entry.get("source_url"), "error": "x"}], True
    monkeypatch.setattr(gmc, "fetch_entry_body", fake_fetch)
    monkeypatch.setattr(gmc, "polite_sleep", lambda: None)

    out_dir = str(tmp_path / "out")
    rows = gmc.run_manifest(str(manifest), out_dir, use_cache=False)

    # 落盘遵循新目录布局：jsonl 产物在 manifest/ 子目录，日志仍在 logs/
    all_rows = gmc.read_jsonl(os.path.join(out_dir, "manifest", "all_guides.jsonl"))
    assert len(all_rows) == 2                                   # 合同审查(有正文) + 建设工程(空正文保留)
    unresolved = gmc.read_jsonl(os.path.join(out_dir, "logs", "unresolved.jsonl"))
    assert len(unresolved) == 2                                # 空 url + 缺 title
    extract_failed = gmc.read_jsonl(os.path.join(out_dir, "logs", "extract_failed.jsonl"))
    assert len(extract_failed) == 1                            # 建设工程
    # filter 产物存在且「合同审查」入选合同相关
    related = gmc.read_jsonl(os.path.join(out_dir, "manifest", "contract_related_guides.jsonl"))
    assert any("合同审查" in (r.get("title") or "") for r in related)


def test_acla_guidebook_cli_invokes_run_manifest(monkeypatch):
    sys.argv = ["crawl_acla_guidebook.py", "--out", "/tmp/gb", "--crawl-only"]
    import crawl_acla_guidebook as cg
    captured = {}
    def fake_run(manifest_path, out_dir, *, use_cache, crawl_only, filter_only):
        captured.update(manifest_path=manifest_path, out_dir=out_dir,
                        use_cache=use_cache, crawl_only=crawl_only, filter_only=filter_only)
        return []
    monkeypatch.setattr(cg, "run_manifest", fake_run)
    cg.main()
    assert captured["manifest_path"] == "/tmp/gb/manifest.jsonl"   # 默认 <out>/manifest.jsonl
    assert captured["out_dir"] == "/tmp/gb" and captured["crawl_only"] is True
    assert captured["use_cache"] is True and captured["filter_only"] is False
