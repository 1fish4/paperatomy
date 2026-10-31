"""工作台服务端：上传校验、报告列表、路径安全。

这里的重点不是功能本身，而是三处不能出事的边界：
1. 上传只收真 PDF（扩展名 + %PDF 魔数双重校验）
2. 报告读取绝不允许目录穿越
3. 上传失败要给用户能读懂的原因，而不是 500
"""

import json

import pytest

from paperatomy.serve import MAX_UPLOAD_BYTES, Task, _State


@pytest.fixture
def state(tmp_path):
    return _State(tmp_path)


def _write_report(out_dir: "object", name: str, data: dict) -> None:
    (out_dir / name).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# 报告列表
# ---------------------------------------------------------------------------

def test_reports_empty(state):
    assert state.reports() == []


def test_reports_lists_newest_first(state):
    _write_report(state.out_dir, "a.json", {
        "paper_title": "旧报告", "template": {"id": "skim"},
        "pages": 10, "findings": [1, 2], "chain": {"nodes": [1]},
    })
    import time
    time.sleep(0.02)
    _write_report(state.out_dir, "b.json", {
        "paper_title": "新报告", "template": {"id": "survey"},
        "pages": 20, "findings": [1], "chain": {"nodes": []},
    })
    reports = state.reports()
    assert [r["title"] for r in reports] == ["新报告", "旧报告"]
    assert reports[0]["file"] == "b.json"
    assert reports[0]["blocks"] == 1 and reports[0]["chain"] == 0


def test_reports_skips_broken_json(state):
    (state.out_dir / "bad.json").write_text("{不是 JSON", encoding="utf-8")
    _write_report(state.out_dir, "good.json", {"paper_title": "正常"})
    assert [r["title"] for r in state.reports()] == ["正常"]


# ---------------------------------------------------------------------------
# 路径安全：报告读取是本服务唯一的文件入口
# ---------------------------------------------------------------------------

def test_report_path_normal(state):
    _write_report(state.out_dir, "ok.json", {"paper_title": "x"})
    assert state.report_path("ok.json") is not None


@pytest.mark.parametrize("bad", [
    "../secret.json",
    "a/b.json",
    "a\\b.json",
    "report.html",
    "",
    "/etc/passwd.json",
])
def test_report_path_rejects_traversal(state, bad):
    assert state.report_path(bad) is None


def test_report_path_rejects_missing_file(state):
    assert state.report_path("ghost.json") is None


# ---------------------------------------------------------------------------
# 上传校验
# ---------------------------------------------------------------------------

def test_save_upload_rejects_non_pdf_name(state):
    with pytest.raises(ValueError, match="PDF"):
        state.save_upload("论文.docx", b"%PDF-1.7 fake")


def test_save_upload_rejects_fake_pdf(state):
    with pytest.raises(ValueError, match="%PDF"):
        state.save_upload("fake.pdf", b"PK\x03\x04 this is a zip")


def test_save_upload_strips_path_components(state):
    p = state.save_upload("..\\..\\evil name?.pdf", b"%PDF-1.7\n...")
    # 路径分隔符被剥掉、危险字符被清洗，文件落在 papers/ 里
    assert p.parent == state.pdf_dir
    assert "evil" in p.name and "?" not in p.name and ".." not in p.name


def test_save_upload_dedupes_names(state):
    a = state.save_upload("paper.pdf", b"%PDF-1.7\none")
    b = state.save_upload("paper.pdf", b"%PDF-1.7\ntwo")
    assert a != b and b.stem == "paper-1"


def test_save_upload_size_limit(state):
    big = b"%PDF-1.7" + b"\0" * (MAX_UPLOAD_BYTES + 1)
    with pytest.raises(ValueError, match="120MB"):
        state.save_upload("huge.pdf", big)


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------

def test_task_public_shape():
    t = Task(id="abc", name="x.pdf", template="skim", lang="en")
    d = t.public()
    assert d["status"] == "queued" and d["template"] == "skim"
    assert "error" in d and "elapsed" in d


def test_start_task_rejects_concurrent(state, tmp_path):
    from paperatomy.serve import Task
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    t1 = Task(id="1", name="a.pdf")
    state.start_task(t1, pdf)          # running 被置位
    t2 = Task(id="2", name="b.pdf")
    state.start_task(t2, pdf)
    assert t2.status == "error"
    assert "已有任务" in t2.error


# ---------------------------------------------------------------------------
# 材料缓存
# ---------------------------------------------------------------------------

def test_material_falls_back_to_report_content(state):
    _write_report(state.out_dir, "r.json", {
        "paper_title": "某论文",
        "source": "",
        "findings": [{"block_id": "problem", "content": "开销大",
                      "evidence": [{"page": 3, "quote": "costly"}]}],
        "chain": {"thesis": "三层"},
    })
    title, mat = state.material_for("r.json")
    assert title == "某论文"
    assert "开销大" in mat and "三层" in mat


def test_material_caches_second_call(state):
    _write_report(state.out_dir, "r.json", {"paper_title": "t", "source": ""})
    state.material_for("r.json")
    # 第二次直接命中缓存；即便文件被删也不再读
    (state.out_dir / "r.json").unlink()
    title, _ = state.material_for("r.json")
    assert title == "t"
