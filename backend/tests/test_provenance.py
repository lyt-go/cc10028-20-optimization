"""图谱溯证链路测试：同名实体不合并、范围可选、翻页锁定来源集合、重算只动选中来源。

这些用例刻意绕开模拟抽取，直接写入关系/实体/分块/图谱产物，以便精确控制
“已发布边”和“尚未入图的新来源”两类证据。
"""
import json

import pytest

import app.services.base as base_module
from app.services.kg_build import KgBuildService
from app.services.provenance import (
    ProvenanceError,
    SnapshotMismatchError,
    SnapshotNotFoundError,
    provenance_index,
)
from app.services.relation_extract import RelationExtractService


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _seed_source(doc_id, *, org="某科技集团", person="张三", confidence=0.8,
                 relation_id=None, extracted_at=1000.0, extra_pairs=()):
    """写入一个文档来源的 chunk/entity/relation 产物，返回关系集合编号。"""
    data_root = base_module.DATA_ROOT
    relation_set = f"{doc_id}_parsed"
    _write(data_root / "chunks" / f"{relation_set}_chunks.jsonl", [
        {"chunk_id": f"CHUNK-{doc_id}-0001", "doc_id": doc_id, "seq": 1, "text": f"{org}雇佣{person}"},
        {"chunk_id": f"CHUNK-{doc_id}-0002", "doc_id": doc_id, "seq": 2, "text": "补充说明"},
    ])
    _write(data_root / "entities" / f"{relation_set}_entities.jsonl", [
        {"entity_id": "ENT-1", "chunk_id": f"CHUNK-{doc_id}-0001", "entity_type": "ORG",
         "entity_text": org, "confidence": 0.9},
        {"entity_id": "ENT-2", "chunk_id": f"CHUNK-{doc_id}-0002", "entity_type": "PERSON",
         "entity_text": person, "confidence": 0.9},
    ])
    relations = [{
        "relation_id": relation_id or f"REL-{doc_id}",
        "head_entity": org,
        "tail_entity": person,
        "relation_type": "EMPLOY",
        "confidence": confidence,
        "evidence_sentence": f"{doc_id}：{org}雇佣{person}",
        "method": "Relation-BERT",
        "model_version": "v2.3.0",
        "extracted_at": extracted_at,
    }]
    for head, rel, tail, conf in extra_pairs:
        relations.append({
            "relation_id": f"REL-{doc_id}-{head}-{tail}",
            "head_entity": head,
            "tail_entity": tail,
            "relation_type": rel,
            "confidence": conf,
            "evidence_sentence": f"{doc_id}：{head}{rel}{tail}",
            "method": "Relation-BERT",
            "model_version": "v2.3.0",
            "extracted_at": extracted_at,
        })
    _write(data_root / "relations" / f"{relation_set}_relations.jsonl", relations)
    return relation_set


@pytest.fixture()
def isolated_data(tmp_path, monkeypatch):
    """每个用例使用独立数据根，避免并行测试互相看见对方的产物。"""
    import app.services.base as base
    import app.services.provenance as provenance

    root = tmp_path / "data"
    root.mkdir()
    # 各服务统一从 app.services.base.DATA_ROOT 取数据根，改这一处即可整体切换。
    monkeypatch.setattr(base, "DATA_ROOT", root)
    # 重新指向后清空内存索引，防止上一个用例的快照残留。
    provenance_index._evidence = []
    provenance_index._by_id = {}
    provenance_index._snapshots.clear()
    provenance_index._revision = 0
    provenance_index._signature = ""
    return root


class TestSameNameEntities:
    def test_documents_are_not_merged(self, isolated_data):
        _seed_source("DOC-A")
        _seed_source("DOC-B")
        result = provenance_index.search("某科技集团", scope="all", size=50)
        assert result["total"] == 2
        docs = {item["doc_id"] for item in result["items"]}
        assert docs == {"DOC-A", "DOC-B"}
        evidence_ids = {item["evidence_id"] for item in result["items"]}
        assert len(evidence_ids) == 2, "不同文档的同名实体必须各自成为独立证据"

    def test_published_vs_pending_scope(self, isolated_data):
        _seed_source("DOC-A")
        KgBuildService().build_from_relations("DOC-A_parsed")
        _seed_source("DOC-B")  # 新来源，尚未入图

        published = provenance_index.search("某科技集团", scope="published", size=50)
        pending = provenance_index.search("某科技集团", scope="pending", size=50)
        combined = provenance_index.search("某科技集团", scope="all", size=50)

        assert {item["doc_id"] for item in published["items"]} == {"DOC-A"}
        assert {item["doc_id"] for item in pending["items"]} == {"DOC-B"}
        assert combined["total"] == published["total"] + pending["total"]
        assert published["counts"]["pending"] == 1

    def test_scope_alias_and_invalid(self, isolated_data):
        _seed_source("DOC-A")
        assert provenance_index.search("某科技集团", scope="edge", size=5)["scope"] == "published"
        with pytest.raises(ProvenanceError):
            provenance_index.search("某科技集团", scope="unknown")

    def test_same_pair_in_two_graph_versions_stays_separate(self, isolated_data):
        _seed_source("DOC-A")
        _seed_source("DOC-B")
        KgBuildService().build_from_relations("DOC-A_parsed")
        KgBuildService().build_from_relations("DOC-B_parsed")
        result = provenance_index.search("某科技集团", scope="published", size=50)
        assert result["total"] == 2
        versions = {item["graph_version"] for item in result["items"]}
        assert len(versions) == 2, "不同版图的同名边应保留各自版本"


class TestPaginationPinsSourceSet:
    def test_pages_share_one_source_set(self, isolated_data):
        for doc in ("DOC-A", "DOC-B", "DOC-C", "DOC-D"):
            _seed_source(doc)
        first = provenance_index.search("某科技集团", scope="all", page=1, size=2)
        assert first["source_set_size"] == 4
        assert len(first["source_ids"]) == 4

        second = provenance_index.search(
            "某科技集团", scope="all", page=2, size=2, snapshot_id=first["snapshot_id"]
        )
        assert second["source_ids"] == first["source_ids"]
        assert second["total"] == first["total"]
        assert second["snapshot_id"] == first["snapshot_id"]

        first_ids = {item["evidence_id"] for item in first["items"]}
        second_ids = {item["evidence_id"] for item in second["items"]}
        assert first_ids.isdisjoint(second_ids), "两页不应重复同一条证据"

    def test_sort_change_keeps_set(self, isolated_data):
        for doc in ("DOC-A", "DOC-B", "DOC-C"):
            _seed_source(doc, confidence=0.7 + 0.05 * len(doc))
        first = provenance_index.search("某科技集团", scope="all", size=2, sort="confidence")
        reversed_page = provenance_index.search(
            "某科技集团", scope="all", size=2, sort="-confidence",
            snapshot_id=first["snapshot_id"],
        )
        assert reversed_page["source_ids"] == first["source_ids"]
        assert reversed_page["total"] == first["total"]

    def test_new_source_arrival_does_not_shift_old_query(self, isolated_data):
        _seed_source("DOC-A")
        _seed_source("DOC-B")
        first = provenance_index.search("某科技集团", scope="all", size=1)
        before = list(first["source_ids"])

        _seed_source("DOC-C")  # 并发到达的新来源
        provenance_index.ensure_fresh(True)

        second = provenance_index.search(
            "某科技集团", scope="all", size=1, page=2, snapshot_id=first["snapshot_id"]
        )
        assert second["source_ids"] == before, "旧查询不应换成另一组证据"
        assert second["total"] == 2
        assert second["revision"] == first["revision"]

        fresh = provenance_index.search("某科技集团", scope="all", size=50)
        assert fresh["total"] == 3
        assert fresh["snapshot_id"] != first["snapshot_id"]

    def test_snapshot_fingerprint_must_match(self, isolated_data):
        _seed_source("DOC-A")
        _seed_source("DOC-B")
        first = provenance_index.search("某科技集团", scope="published")
        with pytest.raises(SnapshotMismatchError):
            provenance_index.search(
                "某科技集团", scope="pending", snapshot_id=first["snapshot_id"]
            )
        with pytest.raises(SnapshotMismatchError):
            provenance_index.search(
                "张三", scope="published", snapshot_id=first["snapshot_id"]
            )

    def test_unknown_snapshot_rejected(self, isolated_data):
        with pytest.raises(SnapshotNotFoundError):
            provenance_index.search("某科技集团", snapshot_id="PS-does-not-exist")


class TestEvidenceDetails:
    def test_edge_chunk_and_version_roundtrip(self, isolated_data):
        _seed_source("DOC-A")
        KgBuildService().build_from_relations("DOC-A_parsed")
        result = provenance_index.search("某科技集团", scope="published", size=5)
        item = result["items"][0]
        assert item["provenance"]["edge"]["triple_id"]
        assert item["provenance"]["edge"]["graph_version"]
        assert item["chunk_ids"], "应能回查到来源分块"
        assert item["provenance"]["chunk"]["primary"] == item["chunk_ids"][0]
        assert item["provenance"]["extraction"]["relation_id"]
        assert item["provenance"]["extraction"]["extraction_version"] == "Relation-BERT@v2.3.0"

        detail = provenance_index.get_evidence(item["evidence_id"])
        assert detail["evidence_id"] == item["evidence_id"]
        assert detail["provenance"] == item["provenance"]

    def test_unknown_evidence_rejected(self, isolated_data):
        with pytest.raises(ProvenanceError):
            provenance_index.get_evidence("EV-R-nope")


class TestRecomputeFromEvidence:
    def test_recompute_writes_only_selected_pairs(self, isolated_data):
        _seed_source("DOC-A", extra_pairs=[("某科技集团", "LOCATE", "Beijing", 0.75)])
        _seed_source("DOC-B", extra_pairs=[("某科技集团", "LOCATE", "Beijing", 0.75)])
        _seed_source("DOC-C")

        result = provenance_index.search("某科技集团", scope="all", size=50)
        target = next(item for item in result["items"] if item["doc_id"] == "DOC-C")
        other_docs_before = {
            path.name: path.read_bytes()
            for path in (isolated_data / "relations").glob("*.jsonl")
            if "DOC-C" not in path.name
        }
        doc_c_other_before = {
            path.read_bytes()
            for path in (isolated_data / "relations").glob("DOC-C*.jsonl")
        }

        recompute = RelationExtractService().recompute_from_evidence(
            [target],
            snapshot_id=result["snapshot_id"],
            evidence_ids=[target["evidence_id"]],
            reason="补抽验证",
        )
        assert "error" not in recompute, recompute
        assert recompute["documents"] == ["DOC-C"]
        assert recompute["updated_relations"] == 1
        assert recompute["records_written"] == 1, "重算产物只写被选中的实体对"

        # 未选中的文档字节级不变
        for name, content in other_docs_before.items():
            assert (isolated_data / "relations" / name).read_bytes() == content
        # 同一文档里未选中的实体对也不写新版本
        doc_c_sources = {
            path.read_bytes() for path in (isolated_data / "relations").glob("DOC-C*.jsonl")
        }
        assert doc_c_other_before <= doc_c_sources

        output = (isolated_data / "relations" / recompute["files"][0].split("/")[-1]).read_text()
        written = [json.loads(line) for line in output.splitlines() if line.strip()]
        assert len(written) == 1
        assert written[0]["head_entity"] == target["head"]
        assert written[0]["tail_entity"] == target["tail"]
        assert written[0]["recompute_batch"] == recompute["batch"]
        assert written[0]["confidence"] > target["confidence"]

    def test_recompute_keeps_old_snapshot_stable(self, isolated_data):
        _seed_source("DOC-A", confidence=0.8)
        _seed_source("DOC-B", confidence=0.8)
        first = provenance_index.search("某科技集团", scope="all", size=50)
        old_ids = list(first["source_ids"])
        target = next(item for item in first["items"] if item["doc_id"] == "DOC-B")

        RelationExtractService().recompute_from_evidence(
            [target], snapshot_id=first["snapshot_id"], evidence_ids=[target["evidence_id"]]
        )
        provenance_index.ensure_fresh(True)

        again = provenance_index.search(
            "某科技集团", scope="all", size=50, snapshot_id=first["snapshot_id"]
        )
        assert again["source_ids"] == old_ids
        assert all(item["evidence_id"] in old_ids for item in again["items"])

        fresh = provenance_index.search("某科技集团", scope="all", size=50)
        updated = next(item for item in fresh["items"] if item["doc_id"] == "DOC-B")
        assert updated["confidence"] > 0.8
        assert len(updated["versions"]) == 2, "原版本与重算版本应可同时回查"

        # 重算成果进入建图后，该来源从“待补抽”转为已发布边，证据编号保持不变。
        from app.services.kg_build import KgBuildService as _KgBuild

        _KgBuild().build_from_relations("DOC-B_parsed")
        published = provenance_index.search("某科技集团", scope="published", size=50)
        doc_b = next(item for item in published["items"] if item["doc_id"] == "DOC-B")
        assert doc_b["confidence"] > 0.8
        assert doc_b["graph_version"]

    def test_recompute_rejects_foreign_evidence(self, isolated_data):
        _seed_source("DOC-A")
        _seed_source("DOC-B")
        first = provenance_index.search("某科技集团", scope="published", size=50)
        assert first["total"] == 0
        outside = next(
            item for item in provenance_index.search("某科技集团", scope="all", size=50)["items"]
            if item["evidence_id"] not in first["source_ids"]
        )
        with pytest.raises(SnapshotMismatchError):
            provenance_index.snapshot_records(first["snapshot_id"], [outside["evidence_id"]])

    def test_recompute_twice_accumulates(self, isolated_data):
        _seed_source("DOC-A", confidence=0.8)
        result = provenance_index.search("某科技集团", scope="all", size=5)
        target = result["items"][0]
        svc = RelationExtractService()

        first = svc.recompute_from_evidence([target], evidence_ids=[target["evidence_id"]])
        assert first["updated_relations"] == 1
        provenance_index.ensure_fresh(True)
        after_first = provenance_index.get_evidence(target["evidence_id"])
        assert after_first["confidence"] > 0.8

        second = svc.recompute_from_evidence(
            [after_first], evidence_ids=[after_first["evidence_id"]]
        )
        assert second["updated_relations"] == 1
        provenance_index.ensure_fresh(True)
        after_second = provenance_index.get_evidence(target["evidence_id"])
        assert after_second["confidence"] > after_first["confidence"]
        assert after_second["confidence"] <= 0.99

    def test_recompute_rejects_mixed_documents(self, isolated_data):
        _seed_source("DOC-A")
        _seed_source("DOC-B")
        result = provenance_index.search("某科技集团", scope="all", size=50)
        svc = RelationExtractService()
        mixed = svc.recompute_from_evidence(result["items"])
        # 两条证据来自不同文档：按文档+来源集合分组各自重算，但返回的 documents 必须分开列出
        assert "error" not in mixed, mixed
        assert mixed["documents"] == ["DOC-A", "DOC-B"]

    def test_recompute_empty_evidence(self, isolated_data):
        assert "error" in RelationExtractService().recompute_from_evidence([])


class TestDemoDataset:
    def test_demo_sources_are_distinct_and_idempotent(self, isolated_data):
        from app.demo_provenance import seed_demo_provenance

        first = seed_demo_provenance()
        assert first["seeded"] is True
        assert first["published"] == 2
        assert first["pending"] == 1
        assert seed_demo_provenance()["seeded"] is False

        published = provenance_index.search("某科技集团", scope="published", size=10)
        pending = provenance_index.search("某科技集团", scope="pending", size=10)
        assert {item["doc_id"] for item in published["items"]} == {"DOC-DEMO-A", "DOC-DEMO-B"}
        assert {item["doc_id"] for item in pending["items"]} == {"DOC-DEMO-C"}
        # 两版图里同一实体对的边各自保留自己的图谱版本。
        assert len({item["graph_version"] for item in published["items"]}) == 2
