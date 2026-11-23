"""演示用图谱溯证产物：让新启动的服务里立刻能看到“同名实体 + 未入图新来源”。

只写数据产物目录（graph/relations/entities/chunks），不碰内存里的示例行；
以标记文件保证幂等，重复启动不会叠加记录。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services import base as base_module

DEMO_MARKER = "provenance_demo.json"

# 同一实体的三份来源：A、B 已入图（两版图都有边），C 是新到、尚未入图的关系来源。
DEMO_SOURCES = [
    {
        "doc_id": "DOC-DEMO-A",
        "entity_text": "某科技集团",
        "person": "张三",
        "relation_type": "EMPLOY",
        "confidence": 0.86,
        "evidence": "某科技集团在2024年第三季度与首席科学家张三博士签订聘任协议。",
        "published": True,
    },
    {
        "doc_id": "DOC-DEMO-B",
        "entity_text": "某科技集团",
        "person": "张三",
        "relation_type": "EMPLOY",
        "confidence": 0.81,
        "evidence": "集团人力资源部确认，张三博士担任首席科学家职务。",
        "published": True,
    },
    {
        "doc_id": "DOC-DEMO-C",
        "entity_text": "某科技集团",
        "person": "张三",
        "relation_type": "EMPLOY",
        "confidence": 0.74,
        "evidence": "新接入的补充材料显示，张三博士继续任职于某科技集团。",
        "published": False,
    },
]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def seed_demo_provenance(force: bool = False) -> dict[str, Any]:
    """写入演示产物并返回摘要；已写过则直接跳过。"""
    data_root = base_module.DATA_ROOT
    marker = data_root / "checkpoints" / DEMO_MARKER
    if marker.exists() and not force:
        return {"seeded": False, "reason": "演示产物已存在"}

    for index, source in enumerate(DEMO_SOURCES):
        doc_id = source["doc_id"]
        relation_set = f"{doc_id}_parsed"
        chunk_ids = [f"CHUNK-{doc_id}-0001", f"CHUNK-{doc_id}-0002"]
        _write_jsonl(data_root / "chunks" / f"{relation_set}_chunks.jsonl", [
            {"chunk_id": chunk_ids[0], "doc_id": doc_id, "seq": 1, "page": 1,
             "length": len(source["evidence"]), "text": source["evidence"]},
            {"chunk_id": chunk_ids[1], "doc_id": doc_id, "seq": 2, "page": 2,
             "length": 32, "text": "相关背景信息见附件。"},
        ])
        _write_jsonl(data_root / "entities" / f"{relation_set}_entities.jsonl", [
            {"entity_id": f"ENT-{doc_id}-ORG", "chunk_id": chunk_ids[0], "entity_type": "ORG",
             "entity_text": source["entity_text"], "start_offset": 0, "end_offset": 5,
             "confidence": 0.93, "model": "NER-BiLSTM-CRF", "model_version": "v3.1.0"},
            {"entity_id": f"ENT-{doc_id}-PERSON", "chunk_id": chunk_ids[0], "entity_type": "PERSON",
             "entity_text": source["person"], "start_offset": 9, "end_offset": 11,
             "confidence": 0.91, "model": "NER-BiLSTM-CRF", "model_version": "v3.1.0"},
        ])
        relation = {
            "relation_id": f"REL-{doc_id}-01",
            "head_entity": source["entity_text"],
            "head_type": "ORG",
            "tail_entity": source["person"],
            "tail_type": "PERSON",
            "relation_type": source["relation_type"],
            "confidence": source["confidence"],
            "evidence_sentence": source["evidence"],
            "method": "Relation-BERT",
            "model_version": "v2.3.0",
            "extraction_version": "Relation-BERT@v2.3.0",
            "doc_id": doc_id,
            "chunk_ids": chunk_ids,
            "extracted_at": 1_750_000_000 + index,
        }
        _write_jsonl(data_root / "relations" / f"{relation_set}_relations.jsonl", [relation])

    # A、B 入图形成两个图谱版本；C 故意不入图，作为“未入图的新来源”。
    from app.services.kg_build import KgBuildService

    built = []
    for source in DEMO_SOURCES:
        if not source["published"]:
            continue
        result = KgBuildService().build_from_relations(f"{source['doc_id']}_parsed")
        built.append(result.get("graph_version") or result.get("error"))

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({
            "documents": [source["doc_id"] for source in DEMO_SOURCES],
            "published": [source["doc_id"] for source in DEMO_SOURCES if source["published"]],
            "pending": [source["doc_id"] for source in DEMO_SOURCES if not source["published"]],
            "graph_versions": built,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "seeded": True,
        "documents": len(DEMO_SOURCES),
        "published": len(built),
        "pending": sum(1 for s in DEMO_SOURCES if not s["published"]),
    }
