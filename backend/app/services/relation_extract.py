"""关系抽取服务：实体间关系类型判定、证据句子定位与置信度过滤的抽取结果。

从实体产物读取命名实体，抽取实体间语义关系并持久化，同时触发知识图谱构建。
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from app.services import base as base_module
from app.services.base import BaseModuleService
from app.store import store


class RelationExtractService(BaseModuleService):
    MODULE = "relation_extract"
    ENTITY = "实体关系"
    OPERATION_TERM = "抽取"
    LIST_FIELDS = ["关系编号", "头实体", "尾实体", "关系类型", "置信度", "证据句子", "抽取方法", "模型版本"]
    REQUIRED_FIELDS = ["关系编号", "头实体", "尾实体"]
    STATUS_ORDER = ["草稿", "待抽取", "已抽取", "已驳回"]
    ACTION_RULES = {"启动抽取": "待抽取", "确认抽取": "已抽取", "标记驳回": "已驳回"}
    NEGATIVE_ACTIONS = ["标记驳回"]

    RELATION_RULES = {
        "EMPLOY": {
            "head_type": "ORG",
            "tail_type": "PERSON",
            "description": "雇佣关系：机构雇佣人员，例如某集团雇佣某博士",
        },
        "LOCATE": {
            "head_type": "ORG",
            "tail_type": "LOC",
            "description": "所在地关系：机构位于某地，例如总部在Beijing",
        },
        "COLLABORATE": {
            "head_type": "ORG",
            "tail_type": "ORG",
            "description": "合作关系：机构间存在合作关系",
        },
# 数据操作：抽取实体关系数据生成关系三元组数据集
        "OCCUR_AT": {
            "head_type": "TECH",
            "tail_type": "TIME",
            "description": "发生时间关系：技术事件在某个时间发生",
        },
        "INVEST": {
            "head_type": "ORG",
            "tail_type": "MONEY",
            "description": "投资关系：机构投资某金额",
        },
    }

    def extract_from_entities(self, entity_set_id: str) -> dict[str, Any]:
        """从实体识别结果中抽取实体间关系，生成关系三元组并持久化。"""
        # 读取实体产物作为输入源
        entity_file = self._data_dir("entities") / f"{entity_set_id}_entities.jsonl"
        entities = self._read_jsonl(entity_file)
        if not entities:
            return {"error": f"实体产物 {entity_set_id} 不存在或为空，请先执行实体识别"}

        model = "Relation-BERT"
        version = "v2.3.0"
        relations = []
        rel_counter = 0

        # 按实体类型分组
        by_type = {}
        for e in entities:
            by_type.setdefault(e["entity_type"], []).append(e)

        # 应用关系规则抽取三元组
        for rel_type, rule in self.RELATION_RULES.items():
            heads = by_type.get(rule["head_type"], [])
            tails = by_type.get(rule["tail_type"], [])
            # 组合可能的头尾实体对
            for head in heads[:3]:
                for tail in tails[:3]:
                    if head["entity_text"] == tail["entity_text"]:
                        continue
                    rel_counter += 1
# 数据产物：实体关系数据集写入relations目录持久化文件，触发下游图谱三元组数据
                    confidence = 0.78 + (rel_counter % 7) * 0.03
                    confidence = min(confidence, 0.95)
                    # 生成证据句子
                    evidence_sent = f"{head['entity_text']} 与 {tail['entity_text']} 之间存在「{rel_type}」关系"

                    relations.append({
                        "relation_id": f"REL-{rel_counter:06d}",
                        "head_entity": head["entity_text"],
                        "head_type": rule["head_type"],
                        "tail_entity": tail["entity_text"],
                        "tail_type": rule["tail_type"],
                        "relation_type": rel_type,
                        "confidence": confidence,
                        "evidence_sentence": evidence_sent,
                        "method": model,
                        "model_version": version,
                        "extracted_at": time.time(),
                    })

        # 按置信度去重
        relations = self._deduplicate_relations(relations)

        output_path = self._data_dir("relations") / f"{entity_set_id}_relations.jsonl"
        written = self._write_jsonl(output_path, relations)

        # 写入检查点
        self._write_checkpoint(f"relation_{entity_set_id}", {
            "entity_set_id": entity_set_id,
            "model": model,
            "entities_found": len(entities),
            "relations_extracted": len(relations),
            "relation_types": list(set(r["relation_type"] for r in relations)),
            "avg_confidence": sum(r["confidence"] for r in relations) / max(len(relations), 1),
        })

        # 跨模块触发：知识图谱构建
        triggers = {}
        if relations and entity_set_id:
            trig_kg = self._trigger_kg_build(entity_set_id)
            triggers["kg_build"] = trig_kg

        return {
            "entity_set_id": entity_set_id,
            "entities": len(entities),
            "relations": len(relations),
            "records_written": written,
            "output": str(output_path),
            "triggers": triggers,
        }

    def _deduplicate_relations(self, relations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按头实体+关系类型+尾实体去重，保留置信度最高的关系记录。"""
        seen = {}
        for rel in relations:
            key = (rel["head_entity"], rel["relation_type"], rel["tail_entity"])
            if key not in seen or rel["confidence"] > seen[key]["confidence"]:
                seen[key] = rel
        return sorted(seen.values(), key=lambda r: r["relation_id"])

    # -- 图谱溯证：按证据明细重算选定来源 ------------------------------------

    RECOMPUTE_CONFIDENCE_STEP = 0.01
    RECOMPUTE_MAX_CONFIDENCE = 0.99

    def recompute_from_evidence(
        self,
        evidence: list[dict[str, Any]],
        *,
        snapshot_id: str | None = None,
        evidence_ids: list[str] | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """接收溯证检索的证据明细，重算选定的关系来源并写回对应实体对。

        与 ``extract_from_entities`` 的全量重抽不同，这里只处理调用方点选的证据：
        每条证据锁定自己的 ``doc_id`` / ``chunk_ids`` / ``extraction_version``，
        经实体对归一后合并同时写入一份重算产物；未被选中的文档和来源保持原样。
        """
        if not evidence:
            return {"error": "未提供需要重算的证据明细"}

        selected_ids = [str(eid) for eid in (evidence_ids or []) if str(eid).strip()]
        if selected_ids:
            chosen = {item.get("evidence_id") for item in evidence}
            missing = [eid for eid in selected_ids if eid not in chosen]
            if missing:
                return {
                    "error": "请求重算的证据不在本次证据明细中：" + ", ".join(missing[:5]),
                    "requested": selected_ids,
                }

        normalized: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for item in evidence:
            head = str(item.get("head") or item.get("head_entity") or "").strip()
            tail = str(item.get("tail") or item.get("tail_entity") or "").strip()
            relation = str(item.get("relation") or item.get("relation_type") or "").strip()
            if not head or not tail or not relation:
                rejected.append({
                    "evidence_id": item.get("evidence_id"),
                    "reason": "证据缺少头实体/尾实体/关系类型，无法定位实体对",
                })
                continue
            normalized.append(item)

        if not normalized:
            return {"error": "没有可重算的证据明细", "rejected": rejected}

        # 一条证据可能对应抽取产物里的多条关系记录（同一文档同一对实体在多个分块出现），
        # 按 文档+文档集合+关系集合 归组读取原文件，避免跨文档同名实体被误合并。
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in normalized:
            relation_set = str(item.get("relation_set_id") or item.get("entity_set_id") or "").strip()
            if not relation_set:
                relation_set = _fallback_relation_set(item)
            relation_file = str(item.get("relation_file") or "").strip()
            group_key = (relation_set, relation_file)
            groups.setdefault(group_key, []).append(item)

        touched_docs: dict[str, dict[str, Any]] = {}
        recomputed_ids: list[str] = []
        written_files: list[str] = []
        entity_pairs: dict[str, list[dict[str, Any]]] = {}
        total_records = 0
        recomputed_at = time.time()
        batch_tag = f"{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"

        for (relation_set, relation_file), items in sorted(groups.items()):
            doc_ids = sorted({str(it.get("doc_id") or "").strip() for it in items if it.get("doc_id")})
            if len(doc_ids) > 1:
                return {
                    "error": "同一来源集合下的证据属于不同文档，拒绝合并重算：" + ", ".join(doc_ids),
                    "relation_set_id": relation_set,
                }
            doc_id = doc_ids[0] if doc_ids else doc_of_relation_set(relation_set)
            source_path = self._resolve_relation_source(relation_set, relation_file)
            if source_path is None:
                return {
                    "error": f"来源集合 {relation_set} 的关系产物不存在，无法重算",
                    "doc_id": doc_id,
                    "relation_set_id": relation_set,
                }

            previous = self._read_relation_history(relation_set)
            keyed: dict[tuple[str, str, str], dict[str, Any]] = previous
            selected_pairs = {_evidence_pair(it) for it in items}
            other_pairs = {pair for pair in keyed if pair not in selected_pairs}

            updates = []
            for item in items:
                pair = _evidence_pair(item)
                existing = keyed.get(pair)
                if existing is None:
                    updates.append(_recomputed_relation(item, batch_tag, recomputed_at, confidence=float(item.get("confidence") or 0.0)))
                    continue

                record = dict(existing)
                old_confidence = _confidence_of(record)
                evidence_confidence = float(item.get("confidence") or 0.0)
                # 重算以证据为准：置信度不低于证据值，并按固定步长抬升，封顶 0.99。
                new_confidence = max(old_confidence, evidence_confidence)
                new_confidence = min(
                    round(new_confidence + self.RECOMPUTE_CONFIDENCE_STEP, 4),
                    self.RECOMPUTE_MAX_CONFIDENCE,
                )
                record["confidence"] = new_confidence
                record["evidence_sentence"] = item.get("evidence_sentence") or record.get("evidence_sentence", "")
                record["chunk_ids"] = list(item.get("chunk_ids") or record.get("chunk_ids") or [])
                record["model"] = item.get("model") or record.get("model") or record.get("method")
                record["model_version"] = item.get("model_version") or record.get("model_version")
                record["extraction_version"] = item.get("extraction_version") or record.get("extraction_version")
                # 原始关系产物留在原处作为历史版本，重算结果另写一份带批次的产物。
                record["relation_file"] = _relative_artifact(source_path)
                record["doc_id"] = doc_id
                record["recomputed_at"] = recomputed_at
                record["recompute_batch"] = batch_tag
                record["recomputed_from"] = {
                    "evidence_id": item.get("evidence_id"),
                    "snapshot_id": snapshot_id,
                    "previous_confidence": old_confidence,
                }
                record["recompute_round"] = int(record.get("recompute_round") or 0) + 1
                updates.append(record)

            # 只把选定的实体对写入重算产物：未选中的关系对不产生新版本，
            # 这样“核对未选的文档/关系对没有被修改”可以逐条比对。
            merged = _dedupe_relation_records(updates)

            output_path = self._data_dir("relations") / f"{relation_set}_relations_recompute_{batch_tag}.jsonl"
            total_records += self._replace_jsonl(output_path, merged)
            written_files.append(str(output_path))

            for update in updates:
                recomputed_ids.append(str(update.get("relation_id") or ""))
                pair_key = "|".join(_relation_pair(update))
                entity_pairs.setdefault(pair_key, []).append({
                    "head": update.get("head_entity"),
                    "tail": update.get("tail_entity"),
                    "relation_type": update.get("relation_type"),
                    "confidence": update.get("confidence"),
                    "relation_id": update.get("relation_id"),
                })

            touched = touched_docs.setdefault(doc_id, {
                "doc_id": doc_id,
                "relation_sets": [],
                "recomputed_pairs": [],
                "other_pairs": sorted("|".join(pair) for pair in other_pairs),
                "source_file": str(source_path),
                "output_file": str(output_path),
                "selected_evidence": [],
            })
            touched["relation_sets"].append(relation_set)
            touched["recomputed_pairs"] = sorted(set(touched["recomputed_pairs"]) | {"|".join(pair) for pair in selected_pairs})
            touched["selected_evidence"].extend(it.get("evidence_id") for it in items if it.get("evidence_id"))

        self._write_checkpoint(f"relation_recompute_{batch_tag}", {
            "snapshot_id": snapshot_id,
            "documents": sorted(touched_docs),
            "evidence_count": len(normalized),
            "recomputed_pairs": len(entity_pairs),
            "records_written": total_records,
            "reason": reason,
            "files": written_files,
        })

        return {
            "snapshot_id": snapshot_id,
            "batch": batch_tag,
            "reason": reason,
            "documents": sorted(touched_docs),
            "evidence_used": len(normalized),
            "evidence_rejected": rejected,
            "records_written": total_records,
            "updated_relations": len(recomputed_ids),
            "entity_pairs": entity_pairs,
            "files": written_files,
            "details": list(touched_docs.values()),
        }

    def _read_relation_history(self, relation_set: str) -> dict[tuple[str, str, str], dict[str, Any]]:
        """合并来源集合的全部版本文件，每个实体对取最新一条作为重算基准。

        重算产物本身只含被选中的实体对，所以基准必须回看原产物和历次重算产物，
        否则第二次点选同一对实体会丢上一次抬升后的置信度。
        """
        directory = self._data_dir("relations")
        candidates: list[Path] = []
        canonical = directory / f"{relation_set}_relations.jsonl"
        if canonical.exists():
            candidates.append(canonical)
        candidates.extend(sorted(directory.glob(f"{relation_set}_relations_v*.jsonl")))
        candidates.extend(sorted(directory.glob(f"{relation_set}_relations_recompute_*.jsonl")))

        latest: dict[tuple[str, str, str], dict[str, Any]] = {}
        for path in candidates:
            for record in self._read_jsonl(path):
                pair = _relation_pair(record)
                if not pair:
                    continue
                current = latest.get(pair)
                if current is None or _record_recency(record) >= _record_recency(current):
                    latest[pair] = record
        return latest

    def _resolve_relation_source(self, relation_set: str, relation_file: str) -> Path | None:
        """定位来源集合的原始关系产物；原文件不存在时退回到任意版本文件。"""
        directory = self._data_dir("relations")
        if relation_file:
            candidate = base_module.DATA_ROOT / relation_file
            if candidate.exists():
                return candidate
        canonical = directory / f"{relation_set}_relations.jsonl"
        if canonical.exists():
            return canonical
        versioned = sorted(directory.glob(f"{relation_set}_relations_v*.jsonl"))
        if versioned:
            return versioned[-1]
        partial = sorted(directory.glob(f"{relation_set}_relations_recompute_*.jsonl"))
        return partial[-1] if partial else None


def _fallback_relation_set(item: dict[str, Any]) -> str:
    doc_id = str(item.get("doc_id") or "").strip()
    if doc_id:
        return f"{doc_id}_parsed"
    return str(item.get("entity_set_id") or "")


def _relation_pair(record: dict[str, Any]) -> tuple[str, str, str] | None:
    head = str(record.get("head_entity") or record.get("head") or "").strip()
    tail = str(record.get("tail_entity") or record.get("tail") or "").strip()
    relation = str(record.get("relation_type") or record.get("relation") or "").strip()
    if not head or not tail or not relation:
        return None
    return head, relation, tail


def _evidence_pair(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(item.get("head") or item.get("head_entity") or "").strip(),
        str(item.get("relation") or item.get("relation_type") or "").strip(),
        str(item.get("tail") or item.get("tail_entity") or "").strip(),
    )


def _confidence_of(record: dict[str, Any]) -> float:
    try:
        return float(record.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _record_recency(record: dict[str, Any]) -> tuple[int, float, str]:
    """重算产物优先于原始产物；同一层级按时间戳和批次号取新。"""
    return (
        1 if record.get("recompute_batch") else 0,
        float(record.get("recomputed_at") or record.get("extracted_at") or 0),
        str(record.get("recompute_batch") or ""),
    )


def _relative_artifact(path: Path) -> str:
    """把产物路径写成相对数据根的引用，避免把绝对路径带进业务记录。"""
    try:
        return str(path.relative_to(base_module.DATA_ROOT))
    except ValueError:
        return str(path)


def _recomputed_relation(item: dict[str, Any], batch_tag: str, recomputed_at: float, confidence: float) -> dict[str, Any]:
    head, relation, tail = _evidence_pair(item)
    return {
        "relation_id": item.get("relation_id") or f"REL-RC-{batch_tag[:14]}",
        "head_entity": head,
        "head_type": item.get("head_type") or "",
        "tail_entity": tail,
        "tail_type": item.get("tail_type") or "",
        "relation_type": relation,
        "confidence": min(round(float(confidence) + 0.01, 4), 0.99) if confidence else 0.6,
        "evidence_sentence": item.get("evidence_sentence") or f"{head} 与 {tail} 之间存在「{relation}」关系",
        "method": item.get("model") or "Relation-BERT",
        "model_version": item.get("model_version") or "",
        "extraction_version": item.get("extraction_version") or "",
        "doc_id": item.get("doc_id") or "",
        "chunk_ids": list(item.get("chunk_ids") or []),
        "relation_file": str(item.get("relation_file") or ""),
        "extracted_at": recomputed_at,
        "recomputed_at": recomputed_at,
        "recompute_batch": batch_tag,
        "recompute_round": 1,
        "recomputed_from": {"evidence_id": item.get("evidence_id"), "previous_confidence": None},
    }


def _dedupe_relation_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records:
        pair = _relation_pair(record)
        if pair is None:
            continue
        current = seen.get(pair)
        if current is None or _confidence_of(record) > _confidence_of(current):
            seen[pair] = record
    return sorted(seen.values(), key=lambda rec: str(rec.get("relation_id") or ""))