"""知识图谱服务：实体关系三元组的图谱入图、版本管理与来源分块追溯。

从关系抽取产物读取三元组并构建知识图谱，将三元组持久化为图谱快照文件。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService
from app.services.provenance import doc_of_relation_set
from app.store import store


class KgBuildService(BaseModuleService):
    MODULE = "kg_build"
    ENTITY = "图谱三元组"
    OPERATION_TERM = "建图"
    LIST_FIELDS = ["三元组编号", "头实体", "谓词关系", "尾实体", "置信度", "来源分块", "图谱版本", "入图时间"]
    REQUIRED_FIELDS = ["三元组编号", "头实体", "谓词关系"]
    STATUS_ORDER = ["草稿", "待建图", "已建图", "已废弃"]
    ACTION_RULES = {"启动建图": "待建图", "确认建图": "已建图", "标记废弃": "已废弃"}
    NEGATIVE_ACTIONS = ["标记废弃"]

    GRAPH_VERSION = "v1.0"

    def derive_graph_version(self, relation_set_id: str) -> str:
        """图谱版本按来源集合稳定派生，不同版本的同名实体对仍可区分。"""
        digest = self._compute_checksum(relation_set_id)
        return f"{self.GRAPH_VERSION}-{digest[:6]}"

    def build_from_relations(self, relation_set_id: str, graph_version: str | None = None) -> dict[str, Any]:
        """从关系抽取产物构建知识图谱三元组，将三元组写入图谱快照文件。

        重算产物只包含被点选重算的实体对，所以建图时把原始关系和全部重算版本合并，
        同一实体对以最新重算结果为准，其余关系对保持原样。
        """
        relations = self._load_relation_history(relation_set_id)
# 数据操作：入湖三元组数据构建知识图谱节点与边数据
        if not relations:
            return {"error": f"关系抽取产物 {relation_set_id} 不存在或为空，请先执行关系抽取"}

        version = graph_version or self.derive_graph_version(relation_set_id)
        triples = []
        for rel in relations:
            if rel.get("confidence", 0) < 0.6:
                continue
            triple_id = f"TRIPLE-{rel['relation_id']}"
            doc_id = str(rel.get("doc_id") or "").strip() or doc_of_relation_set(relation_set_id)
            chunk_ids = [str(cid) for cid in (rel.get("chunk_ids") or []) if str(cid).strip()]
            triples.append({
                "triple_id": triple_id,
                "head": rel["head_entity"],
                "relation": rel["relation_type"],
                "tail": rel["tail_entity"],
                "confidence": rel["confidence"],
                "source": relation_set_id,
                "doc_id": doc_id,
                "chunk_ids": chunk_ids,
                "relation_id": rel.get("relation_id", ""),
                "model": rel.get("method") or rel.get("model") or "",
                "model_version": rel.get("model_version", ""),
                "extraction_version": rel.get("extraction_version", ""),
                "graph_version": version,
                "ingested_at": time.time(),
            })

        # 去重：相同头-关系-尾的保留置信度最高的
        triples = self._deduplicate_triples(triples)

        output_path = self._data_dir("graph") / f"{relation_set_id}_graph.jsonl"
        written = self._replace_jsonl(output_path, triples)

        # 生成图谱边和节点的统计
        nodes = set()
        edges = []
        for t in triples:
            nodes.add(t["head"])
# 数据产物：图谱三元组数据集写入graph目录持久化文件
            nodes.add(t["tail"])
            edges.append(f"{t['head']}→[ {t['relation']} ]→{t['tail']}")

        self._write_checkpoint(f"graph_{relation_set_id}", {
            "relation_set_id": relation_set_id,
            "version": version,
            "nodes": len(nodes),
            "edges": len(edges),
            "min_confidence": min((t["confidence"] for t in triples), default=0),
            "avg_confidence": sum(t["confidence"] for t in triples) / max(len(triples), 1),
        })

        return {
            "relation_set_id": relation_set_id,
            "graph_version": version,
            "triples": len(triples),
            "nodes": len(nodes),
            "edges": len(edges),
            "records_written": written,
# 数据导出：三元组导出文件写入导出目录供下游下载
            "output": str(output_path),
        }

    def _load_relation_history(self, relation_set_id: str) -> list[dict[str, Any]]:
        """合并原始关系与全部重算版本，每个实体对保留最新一条。"""
        from app.services.relation_extract import _record_recency, _relation_pair

        directory = self._data_dir("relations")
        paths: list[Path] = []
        canonical = directory / f"{relation_set_id}_relations.jsonl"
        if canonical.exists():
            paths.append(canonical)
        paths.extend(sorted(directory.glob(f"{relation_set_id}_relations_v*.jsonl")))
        paths.extend(sorted(directory.glob(f"{relation_set_id}_relations_recompute_*.jsonl")))

        latest: dict[tuple[str, str, str], dict[str, Any]] = {}
        for path in paths:
            for record in self._read_jsonl(path):
                pair = _relation_pair(record)
                if pair is None:
                    continue
                current = latest.get(pair)
                if current is None or _record_recency(record) >= _record_recency(current):
                    latest[pair] = record
        return sorted(latest.values(), key=lambda rec: str(rec.get("relation_id") or ""))

    def _deduplicate_triples(self, triples: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按头实体、关系、尾实体去重，保留置信度最高的三元组。"""
        seen = {}
        for t in triples:
            key = (t["head"], t["relation"], t["tail"])
            if key not in seen or t["confidence"] > seen[key]["confidence"]:
                seen[key] = t
        return sorted(seen.values(), key=lambda x: x["triple_id"])

    def query_graph(self, entity: str) -> dict[str, Any]:
        """从图谱中查询与指定实体相关的三元组，附带来源与版本信息。"""
        graph_files = sorted(self._data_dir("graph").glob("*.jsonl"))
        results = []
        for gf in graph_files:
            triples = self._read_jsonl(gf)
            for t in triples:
                if entity in (t.get("head", ""), t.get("tail", "")):
                    results.append(t)
        return {"entity": entity, "related_triples": len(results), "triples": results}

    def search_provenance(
        self,
        entity: str,
        *,
        scope: str = "all",
        doc_id: str | None = None,
        relation: str | None = None,
        page: int = 1,
        size: int = 20,
        sort: str | None = None,
        snapshot_id: str | None = None,
        match: str = "exact",
    ) -> dict[str, Any]:
        """图谱溯证检索：一次查询锁定证据集合，翻页与排序不改变来源集合。"""
        # 延迟导入，避免 kg_build <-> provenance 的循环引用。
        from app.services.provenance import provenance_index

        return provenance_index.search(
            entity,
            scope=scope,
            doc_id=doc_id,
            relation=relation,
            page=page,
            size=size,
            sort=sort,
            snapshot_id=snapshot_id,
            match=match,
        )

    def get_provenance_evidence(self, evidence_id: str) -> dict[str, Any]:
        from app.services.provenance import ProvenanceError, provenance_index

        try:
            return provenance_index.get_evidence(evidence_id)
        except ProvenanceError as exc:
            return {"error": str(exc)}

    def provenance_stats(self) -> dict[str, Any]:
        from app.services.provenance import provenance_index

        return provenance_index.stats(refresh=False)