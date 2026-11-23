"""图谱溯证接口层测试：检索分页、快照校验、点选重算与未选文档隔离。"""
import json

import pytest

import app.services.base as base_module
from app.services.provenance import provenance_index

pytest.importorskip("httpx", reason="接口测试需要 httpx 提供 TestClient")
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _seed(doc_id, confidence=0.8, org="某科技集团", person="张三"):
    data_root = base_module.DATA_ROOT
    relation_set = f"{doc_id}_parsed"
    _write(data_root / "chunks" / f"{relation_set}_chunks.jsonl", [
        {"chunk_id": f"CHUNK-{doc_id}-0001", "doc_id": doc_id, "text": f"{org}雇佣{person}"},
    ])
    _write(data_root / "entities" / f"{relation_set}_entities.jsonl", [
        {"entity_id": "E1", "chunk_id": f"CHUNK-{doc_id}-0001", "entity_type": "ORG", "entity_text": org},
        {"entity_id": "E2", "chunk_id": f"CHUNK-{doc_id}-0001", "entity_type": "PERSON", "entity_text": person},
    ])
    _write(data_root / "relations" / f"{relation_set}_relations.jsonl", [{
        "relation_id": f"REL-{doc_id}",
        "head_entity": org,
        "tail_entity": person,
        "relation_type": "EMPLOY",
        "confidence": confidence,
        "evidence_sentence": f"{doc_id} 的证据句",
        "method": "Relation-BERT",
        "model_version": "v2.3.0",
        "extracted_at": 1000.0,
    }])


@pytest.fixture()
def client(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(base_module, "DATA_ROOT", root)
    # 提前放好演示种子标记，避免 TestClient 启动时往本用例的数据根里塞演示文档。
    marker = root / "checkpoints" / "provenance_demo.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    provenance_index._evidence = []
    provenance_index._by_id = {}
    provenance_index._snapshots.clear()
    provenance_index._revision = 0
    provenance_index._signature = ""
    with TestClient(app) as test_client:
        yield test_client


class TestSearchEndpoint:
    def test_search_and_page(self, client):
        for doc in ("DOC-A", "DOC-B", "DOC-C"):
            _seed(doc, confidence=0.7 + 0.03 * len(doc))
        first = client.get("/api/kg/provenance/search", params={
            "entity": "某科技集团", "scope": "all", "size": 2, "sort": "-confidence",
        })
        assert first.status_code == 200
        payload = first.json()
        assert payload["total"] == 3
        assert payload["source_set_size"] == 3
        assert len(payload["items"]) == 2

        second = client.get("/api/kg/provenance/search", params={
            "entity": "某科技集团", "scope": "all", "size": 2, "page": 2,
            "sort": "-confidence", "snapshot_id": payload["snapshot_id"],
        })
        assert second.status_code == 200
        assert second.json()["source_ids"] == payload["source_ids"]

    def test_scope_mismatch_conflict(self, client):
        _seed("DOC-A")
        first = client.get("/api/kg/provenance/search", params={"entity": "某科技集团"}).json()
        conflict = client.get("/api/kg/provenance/search", params={
            "entity": "某科技集团", "scope": "pending", "snapshot_id": first["snapshot_id"],
        })
        assert conflict.status_code == 409

    def test_unknown_snapshot_gone(self, client):
        response = client.get("/api/kg/provenance/search", params={
            "entity": "某科技集团", "snapshot_id": "PS-missing",
        })
        assert response.status_code == 410

    def test_invalid_scope_bad_request(self, client):
        response = client.get("/api/kg/provenance/search", params={
            "entity": "某科技集团", "scope": "sideways",
        })
        assert response.status_code == 400

    def test_evidence_detail(self, client):
        _seed("DOC-A")
        item = client.get("/api/kg/provenance/search", params={
            "entity": "某科技集团", "scope": "all",
        }).json()["items"][0]
        detail = client.get(f"/api/kg/provenance/evidence/{item['evidence_id']}")
        assert detail.status_code == 200
        body = detail.json()
        assert body["chunk_ids"] == item["chunk_ids"]
        assert body["provenance"]["chunk"]["primary"] == item["chunk_ids"][0]

    def test_snapshot_endpoint(self, client):
        _seed("DOC-A")
        first = client.get("/api/kg/provenance/search", params={"entity": "某科技集团"}).json()
        snapshot = client.get(f"/api/kg/provenance/snapshots/{first['snapshot_id']}")
        assert snapshot.status_code == 200
        assert snapshot.json()["source_set_size"] == first["source_set_size"]


class TestRecomputeEndpoint:
    def test_recompute_selected_only(self, client):
        _seed("DOC-A")
        _seed("DOC-B")
        _seed("DOC-C", confidence=0.72)
        search = client.get("/api/kg/provenance/search", params={
            "entity": "某科技集团", "scope": "all", "size": 50,
        }).json()
        target = next(item for item in search["items"] if item["doc_id"] == "DOC-C")
        before = {
            path.name: path.read_bytes()
            for path in (base_module.DATA_ROOT / "relations").glob("*.jsonl")
        }

        response = client.post("/api/kg/provenance/recompute", json={
            "snapshot_id": search["snapshot_id"],
            "evidence_ids": [target["evidence_id"]],
            "reason": "接口验证",
        })
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["documents"] == ["DOC-C"]
        assert body["records_written"] == 1

        after = {
            path.name: path.read_bytes()
            for path in (base_module.DATA_ROOT / "relations").glob("*.jsonl")
        }
        for name, content in before.items():
            assert after[name] == content, f"未选文档 {name} 不应被修改"
        new_files = set(after) - set(before)
        assert len(new_files) == 1

    def test_recompute_rejects_foreign_evidence(self, client):
        _seed("DOC-A")
        search = client.get("/api/kg/provenance/search", params={
            "entity": "某科技集团", "scope": "published",
        }).json()
        response = client.post("/api/kg/provenance/recompute", json={
            "snapshot_id": search["snapshot_id"],
            "evidence_ids": ["EV-R-not-in-snapshot"],
        })
        assert response.status_code == 409

    def test_recompute_document_guard(self, client):
        _seed("DOC-A")
        _seed("DOC-B")
        search = client.get("/api/kg/provenance/search", params={
            "entity": "某科技集团", "scope": "all", "size": 50,
        }).json()
        target = next(item for item in search["items"] if item["doc_id"] == "DOC-A")
        response = client.post("/api/kg/provenance/recompute", json={
            "snapshot_id": search["snapshot_id"],
            "evidence_ids": [target["evidence_id"]],
            "document_id": "DOC-B",
        })
        assert response.status_code == 409

    def test_recompute_requires_evidence(self, client):
        response = client.post("/api/kg/provenance/recompute", json={"reason": "空"})
        assert response.status_code == 400


class TestKgEntryProvenance:
    def test_entry_links_to_evidence(self, client):
        _seed("DOC-A")
        from app.services.kg_build import KgBuildService

        KgBuildService().build_from_relations("DOC-A_parsed")
        created = client.post("/api/kg_build", json={"values": {
            "三元组编号": "TRIPLE-DEMO",
            "头实体": "某科技集团",
            "谓词关系": "EMPLOY",
            "尾实体": "张三",
        }})
        assert created.status_code == 200
        entry_id = created.json()["entry"]["id"]

        response = client.get(f"/api/kg/{entry_id}/provenance")
        assert response.status_code == 200
        body = response.json()
        assert body["entry_id"] == entry_id
        assert body["total"] >= 1
        assert any(item["doc_id"] == "DOC-A" for item in body["items"])
