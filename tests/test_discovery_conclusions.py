import json
from importlib import import_module
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from paperatomy import conclusions, discovery, serve
from paperatomy.llm import LLMConfig


def _work(number, *, refs=(), related=()):
    return {"id": f"https://openalex.org/W{number}", "display_name": f"Paper {number}",
            "referenced_works": [f"https://openalex.org/W{ref}" for ref in refs],
            "related_works": [f"https://openalex.org/W{ref}" for ref in related],
            "abstract_inverted_index": {"Research": [0], "paper": [1]},
            "publication_year": 2024}


def test_one_hop_uses_indexed_edges_and_shared_reference_counts(tmp_path):
    works = {1: _work(1, refs=(2, 3), related=(2, 4, 5, 6)), 2: _work(2),
             3: _work(3), 4: _work(4, refs=(2, 3)), 5: _work(5, refs=(9,)),
             6: _work(6, refs=(1, 2))}

    class API:
        def _fetch(self, url):
            parsed = urlsplit(url)
            query = parse_qs(parsed.query)
            if parsed.path.endswith("/W1"):
                return works[1]
            if query["filter"] == ["cites:W1"]:
                return {"results": [works[6]]}
            return {"results": [works[number] for number in (2, 3, 4, 5, 6)]}

    result = discovery.expand_work("W1", API())
    assert [row["id"] for row in result["references"]] == ["W2", "W3"]
    assert [row["id"] for row in result["cited_by"]] == ["W6"]
    assert [(row["id"], row["shared"]) for row in result["similar"]] == [("W4", 2)]
    assert result["seed"]["abstract"] == "Research paper"
    state = serve._State(tmp_path)
    state.reading.save(result["similar"][0], "调度")
    state.reading.save(result["similar"][0], "调度")
    (state.out_dir / "paper4.json").write_text(json.dumps({"paper_title": "Paper 4"}), encoding="utf-8")
    listing = state.query_reports(col="调度")
    assert listing["collections"] == [{"name": "调度", "count": 0}]
    assert [item["id"] for item in listing["reading"]] == ["W4"]
    assert listing["reading"][0]["matching_report"] == "paper4.json"
    assert not state.query_reports(col="其他")["reading"]


def test_conclusion_cells_only_show_existing_quotes_and_conditions_gate_relations(monkeypatch):
    def report(title):
        return {"paper_title": title, "findings": [{"block_id": "result", "evidence": [
            {"page": 7, "quote": title + " original evidence.", "verified": True},
            {"page": 9, "quote": "Paraphrase.", "verified": False},
        ]}]}

    def cell(text, ref="E1"):
        return {"text": text, "refs": [ref]}

    def answer(paper, setup, ref="E1"):
        return {"paper": paper, "conditions": cell(setup), "metric": cell("carbon g"),
                "baseline": cell("ASAP"), "claim": cell("Lower emissions", ref)}

    def chat(messages, cfg):
        documents = json.loads(messages[-1]["content"])["papers"]
        assert all(list(item["evidence"]) == ["E1"] for item in documents)
        return json.dumps({"papers": [answer(1, "Dataset A"), answer(2, "Dataset B"),
                                     answer(3, "Dataset A", "E99")],
                           "relations": [{"a": 1, "b": 2, "relation": "different",
                                          "reason": "Unreliable model assertion."}]})

    monkeypatch.setattr(conclusions, "chat", chat)
    items = [(key + ".json", key, report(key)) for key in ("A", "B", "C")]
    value = conclusions.compare_conclusions("What happens?", items, LLMConfig())
    assert value["papers"][0]["cells"]["claim"]["evidence"][0]["quote"] == "A original evidence."
    assert not value["papers"][2]["cells"]["claim"]["text"]
    assert [item["status"] for item in value["relations"]] == ["口径待对齐", "证据不足", "证据不足"]


def test_numbered_direct_citation_requires_bibliographic_and_in_text_match(tmp_path, monkeypatch):
    refs = SimpleNamespace(text="[2] A reference to Paper B.\n[3] An unrelated reference.",
                           is_reference=True, start_page=4, end_page=4)
    source = SimpleNamespace(sections=[refs], paragraphs=[
        SimpleNamespace(page=2, text="Our evaluation uses a different workload from [2]."),
        SimpleNamespace(page=4, text="[2] A reference to Paper B."),
    ])
    monkeypatch.setattr(import_module("paperatomy.parse"), "parse", lambda path: source)
    monkeypatch.setattr("paperatomy.refnet.extract_title",
                        lambda entry: "Paper B" if "Paper B" in entry else "")
    items = [("a.json", "a", {"paper_title": "Paper A"}),
             ("b.json", None, {"paper_title": "Paper B"})]
    assert conclusions.direct_citations(items, tmp_path) == [{
        "from": "a.json", "to": "b.json", "pdf": "a", "page": 2,
        "context": "Our evaluation uses a different workload from [2].",
    }]
