"""图谱溯证索引：把图谱边、关系来源、分块与抽取版本汇总成可稳定检索的证据集合。

同一段实体文本可能来自不同文档、不同图谱版本或不同抽取批次。图谱本身只记录
``头实体-关系-尾实体``，直接按实体扫描多份 graph/*.jsonl 会把同名实体合并到
一起，也无法区分“已发布边”与“尚未入图的新来源”。

本模块解决三件事：

1. **证据不合并**：每条证据都带 ``doc_id`` / ``relation_set_id`` / ``chunk_ids``，
   不同文档的同名实体分别成为独立证据。
2. **范围可选**：``published`` 只查已入图的边（当前边来历），``pending`` 只查
   尚未进入已发布图的关系来源（需要补抽/重算的新证据），``all`` 两者都查。
3. **查询快照稳定**：一次检索会固定一份 ``source_ids`` 证据集合，翻页、改排序、
   甚至并发有新来源到达并重建索引，都不会把这次查询换成另一组证据。

检索始终读取本索引，不直接扫描目录，索引与检索入口共用同一份数据。
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

from app.services import base as base_module

SCOPE_ALIASES = {
    "published": "published",
    "graph": "published",
    "edge": "published",
    "current": "published",
    "pending": "pending",
    "candidate": "pending",
    "unpublished": "pending",
    "relation": "pending",
    "source": "pending",
    "all": "all",
    "*": "all",
}

MATCH_ALIASES = {"exact": "exact", "equals": "exact", "contains": "contains"}

SORT_FIELDS = ("confidence", "observed_at", "doc_id", "chunk_id", "evidence_id", "graph_version")


class ProvenanceError(Exception):
    """溯证检索/快照校验失败，路由层据此转换成 4xx 响应。"""


class SnapshotNotFoundError(ProvenanceError):
    """引用了一个不存在或已过期的检索快照。"""


class SnapshotMismatchError(ProvenanceError):
    """快照与本次查询条件不一致，拒绝让查询在两组证据之间切换。"""


def _text_key(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _unique(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _strip_jsonl(name: str) -> str:
    return name[:-6] if name.endswith(".jsonl") else name


def _artifact_set(name: str, marker: str) -> str | None:
    """从 ``{set}{marker}[...].jsonl`` 文件名里取出集合编号。"""
    stem = _strip_jsonl(name)
    if marker not in stem:
        return None
    return stem.split(marker)[0]


def _relation_set_from_filename(name: str) -> str:
    """关系产物文件名 -> 关系集合编号；重算版本文件也归属原集合。"""
    stem = _strip_jsonl(name)
    for marker in ("_relations_recompute_", "_relations_v", "_relations"):
        if marker in stem:
            return stem.split(marker)[0]
    return stem


def is_recompute_file(name: str) -> bool:
    return "_relations_recompute_" in name


def _recompute_rank(name: str) -> str:
    """从 ``..._relations_recompute_{batch}.jsonl`` 里取出批次号，用于判定版本新旧。"""
    stem = _strip_jsonl(name)
    if "_relations_recompute_" not in stem:
        return ""
    return stem.split("_relations_recompute_", 1)[1]


def _graph_set_from_filename(name: str) -> str | None:
    return _artifact_set(name, "_graph")


def doc_of_relation_set(relation_set_id: str) -> str:
    """``DOC-1_parsed`` -> ``DOC-1``；其它集合沿用自身作为文档标识。"""
    suffix = "_parsed"
    return relation_set_id[:-len(suffix)] if relation_set_id.endswith(suffix) else relation_set_id


class ProvenanceIndex:
    """内存溯证索引 + 检索快照登记表。

    索引在第一次检索时自动构建；之后按数据目录签名按需刷新。快照保存的是证据
    记录的深拷贝，因此并发重建索引不会影响已经在翻页的查询。
    """

    MAX_SNAPSHOTS = 64

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._evidence: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._snapshots: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._revision = 0
        self._signature = ""
        self._file_count = 0
        self._built_at = 0.0
        self._counts = {"published": 0, "pending": 0, "documents": 0, "entities": 0}

    # ------------------------------------------------------------------ 索引构建

    def _signature_now(self) -> tuple[str, int]:
        parts: list[str] = []
        count = 0
        for sub in ("graph", "relations", "entities", "chunks"):
            directory = base_module.DATA_ROOT / sub
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.jsonl")):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                count += 1
                parts.append(f"{sub}/{path.name}:{stat.st_size}:{stat.st_mtime_ns}")
        digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
        return digest, count

    @staticmethod
    def _safe_read_jsonl(path: Path) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not path.exists():
            return records
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        records.append(value)
        except OSError:
            return records
        return records

    def _load_entity_maps(self) -> dict[str, dict[str, Any]]:
        """entity_set_id -> 实体文本到分块/实体记录的反查表。"""
        maps: dict[str, dict[str, Any]] = {}
        directory = base_module.DATA_ROOT / "entities"
        if not directory.exists():
            return maps
        for path in sorted(directory.glob("*.jsonl")):
            set_id = _artifact_set(path.name, "_entities")
            if not set_id:
                continue
            by_text: dict[str, list[dict[str, Any]]] = {}
            for record in self._safe_read_jsonl(path):
                text = _text_key(record.get("entity_text"))
                if not text:
                    continue
                by_text.setdefault(text, []).append(record)
            maps.setdefault(set_id, {"by_text": {}})["by_text"].update(by_text)
        return maps

    @staticmethod
    def _chunks_for_texts(entity_map: dict[str, Any] | None, texts: Iterable[str]) -> tuple[list[str], list[str]]:
        if not entity_map:
            return [], []
        by_text = entity_map.get("by_text", {})
        chunk_ids: list[str] = []
        entity_ids: list[str] = []
        for text in texts:
            for record in by_text.get(_text_key(text), []):
                chunk_ids.append(record.get("chunk_id"))
                entity_ids.append(record.get("entity_id"))
        return _unique(chunk_ids), _unique(entity_ids)

    def _load_graph_evidence(
        self,
        relation_lookup: dict[tuple[str, str, str, str], dict[str, Any]],
        entity_maps: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], set[tuple[str, str, str, str]]]:
        evidence: list[dict[str, Any]] = []
        published_pairs: set[tuple[str, str, str, str]] = set()
        directory = base_module.DATA_ROOT / "graph"
        if not directory.exists():
            return evidence, published_pairs

        deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for path in sorted(directory.glob("*.jsonl")):
            file_set = _graph_set_from_filename(path.name)
            for triple in self._safe_read_jsonl(path):
                head = str(triple.get("head") or "").strip()
                tail = str(triple.get("tail") or "").strip()
                relation = str(triple.get("relation") or triple.get("predicate") or "").strip()
                if not head or not tail or not relation:
                    continue
                source = str(triple.get("source") or triple.get("relation_set_id") or file_set or "").strip()
                triple_id = str(triple.get("triple_id") or "").strip()
                if not triple_id:
                    triple_id = "TRIPLE-" + hashlib.sha1(
                        f"{source}|{head}|{relation}|{tail}".encode("utf-8")
                    ).hexdigest()[:12]
                graph_version = str(triple.get("graph_version") or triple.get("version") or "unknown")
                doc_id = str(triple.get("doc_id") or "").strip()
                entity_set_id = str(triple.get("entity_set_id") or source or "").strip()
                match = relation_lookup.get((source, _text_key(head), relation, _text_key(tail)))
                if not doc_id:
                    doc_id = str((match or {}).get("doc_id") or "").strip() or doc_of_relation_set(source)
                if not entity_set_id:
                    entity_set_id = str((match or {}).get("entity_set_id") or source)

                chunk_ids = _unique(triple.get("chunk_ids") or [])
                entity_ids: list[str] = []
                if not chunk_ids:
                    chunk_ids, entity_ids = self._chunks_for_texts(entity_maps.get(entity_set_id), (head, tail))
                else:
                    _, entity_ids = self._chunks_for_texts(entity_maps.get(entity_set_id), (head, tail))

                extraction_version = str(triple.get("extraction_version") or "").strip()
                model = triple.get("model") or (match or {}).get("model") or ""
                model_version = triple.get("model_version") or (match or {}).get("model_version") or ""
                if not extraction_version:
                    extraction_version = (match or {}).get("extraction_version") or ""
                if not extraction_version:
                    extraction_version = f"{model}@{model_version}" if model or model_version else "unknown"

                evidence_id = "EV-G-" + hashlib.sha1(
                    f"{doc_id}|{source}|{triple_id}|{graph_version}".encode("utf-8")
                ).hexdigest()[:12]
                record = {
                    "evidence_id": evidence_id,
                    "source_kind": "graph_edge",
                    "scope": "published",
                    "published": True,
                    "in_published_graph": True,
                    "head": head,
                    "relation": relation,
                    "tail": tail,
                    "pair_key": f"{head}|{relation}|{tail}",
                    "confidence": _safe_float(triple.get("confidence")),
                    "doc_id": doc_id,
                    "entity_set_id": entity_set_id,
                    "relation_set_id": source,
                    "chunk_ids": chunk_ids,
                    "chunk_id": chunk_ids[0] if chunk_ids else "",
                    "entity_ids": entity_ids,
                    "relation_id": str(triple.get("relation_id") or (match or {}).get("relation_id") or ""),
                    "model": model,
                    "model_version": model_version,
                    "extraction_version": extraction_version,
                    "graph_version": graph_version,
                    "triple_id": triple_id,
                    "edge_id": triple_id,
                    "evidence_sentence": (match or {}).get("evidence_sentence") or "",
                    "graph_file": f"graph/{path.name}",
                    "relation_file": (match or {}).get("relation_file") or "",
                    "observed_at": _safe_float(triple.get("ingested_at")),
                    "recompute_batch": "",
                    "versions": [extraction_version],
                    "version_details": [{
                        "version": extraction_version,
                        "extraction_version": extraction_version,
                        "confidence": _safe_float(triple.get("confidence")),
                        "relation_file": (match or {}).get("relation_file") or f"graph/{path.name}",
                        "graph_version": graph_version,
                    }],
                    "provenance": {
                        "edge": {"triple_id": triple_id, "graph_version": graph_version, "graph_file": f"graph/{path.name}"},
                        "chunk": {"chunk_ids": chunk_ids, "primary": chunk_ids[0] if chunk_ids else ""},
                        "extraction": {
                            "relation_id": str(triple.get("relation_id") or (match or {}).get("relation_id") or ""),
                            "extraction_version": extraction_version,
                            "model": model,
                            "model_version": model_version,
                            "relation_file": (match or {}).get("relation_file") or "",
                        },
                    },
                }
                published_pairs.add((doc_id, _text_key(head), relation, _text_key(tail)))
                key = (source, triple_id, graph_version)
                previous = deduped.get(key)
                if previous is None or record["observed_at"] >= previous["observed_at"]:
                    deduped[key] = record

        evidence.extend(deduped.values())
        return evidence, published_pairs

    def _load_relation_evidence(
        self,
        published_pairs: set[tuple[str, str, str, str]],
        entity_maps: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[tuple[str, str, str, str], dict[str, Any]]]:
        """关系来源按“文档+实体对”汇总，同一来源的多个抽取版本保留在 versions 里。

        每条证据只代表一个文档里的一个实体对，不会把不同文档的同名实体合并；
        同一来源被重算多次时，新旧版本都挂在 ``versions`` 下可同时回查，但只有
        最新版本参与当前证据集合，避免同一实体对在检索结果里重复刷屏。
        """
        evidence: list[dict[str, Any]] = []
        lookup: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        directory = base_module.DATA_ROOT / "relations"
        if not directory.exists():
            return evidence, lookup

        # (doc, relation_set, pair) -> 版本列表
        grouped: dict[tuple[str, str, tuple[str, str, str]], list[dict[str, Any]]] = {}
        for path in sorted(directory.glob("*.jsonl")):
            relation_set = _relation_set_from_filename(path.name)
            for record in self._safe_read_jsonl(path):
                head = str(record.get("head_entity") or record.get("head") or "").strip()
                tail = str(record.get("tail_entity") or record.get("tail") or "").strip()
                relation = str(record.get("relation_type") or record.get("relation") or "").strip()
                if not head or not tail or not relation:
                    continue
                entity_set_id = str(record.get("entity_set_id") or relation_set).strip()
                doc_id = str(record.get("doc_id") or "").strip() or doc_of_relation_set(entity_set_id)
                model = str(record.get("method") or record.get("model") or "")
                model_version = str(record.get("model_version") or "")
                extraction_version = str(record.get("extraction_version") or "").strip()
                if not extraction_version:
                    extraction_version = f"{model}@{model_version}" if model or model_version else "unknown"
                recompute_batch = str(record.get("recompute_batch") or "").strip()
                if not recompute_batch and is_recompute_file(path.name):
                    recompute_batch = _recompute_rank(path.name)
                version_label = extraction_version
                if recompute_batch:
                    version_label = f"{extraction_version}#rc-{recompute_batch}"

                version = {
                    "version": version_label,
                    "extraction_version": extraction_version,
                    "relation_id": str(record.get("relation_id") or "").strip(),
                    "confidence": _safe_float(record.get("confidence")),
                    "evidence_sentence": record.get("evidence_sentence") or "",
                    "model": model,
                    "model_version": model_version,
                    "chunk_ids": _unique(record.get("chunk_ids") or []),
                    "entity_ids": _unique(record.get("entity_ids") or []),
                    "relation_file": f"relations/{path.name}",
                    "recompute_batch": recompute_batch,
                    "recomputed_at": _safe_float(record.get("recomputed_at")),
                    "observed_at": _safe_float(
                        record.get("recomputed_at") or record.get("extracted_at")
                    ),
                    "order": (1 if recompute_batch else 0, _safe_float(record.get("recomputed_at") or record.get("extracted_at")), recompute_batch, path.name),
                }
                key = (doc_id, relation_set, (head, relation, tail))
                grouped.setdefault(key, []).append(version)

        for (doc_id, relation_set, (head, relation, tail)), versions in sorted(grouped.items()):
            # 关系产物是追加写入的，重复抽取会产生同版本重复行；按版本号+关系编号+文件去重，
            # 保证 versions 列表里每个历史版本只出现一次。
            deduped_versions: dict[tuple[str, str, str], dict[str, Any]] = {}
            for version in versions:
                key = (version["version"], version["relation_id"], version["relation_file"])
                current = deduped_versions.get(key)
                if current is None or version["order"] >= current["order"]:
                    deduped_versions[key] = version
            versions = sorted(deduped_versions.values(), key=lambda item: item["order"])
            latest = versions[-1]
            pair_key = (doc_id, _text_key(head), relation, _text_key(tail))
            in_graph = pair_key in published_pairs
            chunk_ids = latest["chunk_ids"]
            entity_ids = latest["entity_ids"]
            if not chunk_ids:
                chunk_ids, mapped_entity_ids = self._chunks_for_texts(
                    entity_maps.get(relation_set), (head, tail)
                )
                entity_ids = entity_ids or mapped_entity_ids

            # evidence_id 只由文档+来源集合+实体对决定，重算不会换一个编号。
            evidence_id = "EV-R-" + hashlib.sha1(
                f"{doc_id}|{relation_set}|{head}|{relation}|{tail}".encode("utf-8")
            ).hexdigest()[:12]
            version_labels = [item["version"] for item in reversed(versions)]
            item = {
                "evidence_id": evidence_id,
                "source_kind": "relation_source",
                "scope": "pending" if not in_graph else "published_source",
                "published": in_graph,
                "in_published_graph": in_graph,
                "head": head,
                "relation": relation,
                "tail": tail,
                "pair_key": f"{head}|{relation}|{tail}",
                "confidence": latest["confidence"],
                "doc_id": doc_id,
                "entity_set_id": relation_set,
                "relation_set_id": relation_set,
                "chunk_ids": chunk_ids,
                "chunk_id": chunk_ids[0] if chunk_ids else "",
                "entity_ids": entity_ids,
                "relation_id": latest["relation_id"],
                "model": latest["model"],
                "model_version": latest["model_version"],
                "extraction_version": latest["extraction_version"],
                "graph_version": "",
                "triple_id": "",
                "edge_id": "",
                "evidence_sentence": latest["evidence_sentence"],
                "graph_file": "",
                "relation_file": latest["relation_file"],
                "observed_at": latest["observed_at"],
                "recompute_batch": latest["recompute_batch"],
                "versions": version_labels,
                "version_details": versions,
                "provenance": {
                    "edge": {"triple_id": "", "graph_version": "", "graph_file": ""},
                    "chunk": {"chunk_ids": chunk_ids, "primary": chunk_ids[0] if chunk_ids else ""},
                    "extraction": {
                        "relation_id": latest["relation_id"],
                        "extraction_version": latest["extraction_version"],
                        "model": latest["model"],
                        "model_version": latest["model_version"],
                        "relation_file": latest["relation_file"],
                        "recompute_batch": latest["recompute_batch"],
                        "versions": version_labels,
                    },
                },
            }
            evidence.append(item)
            lookup[pair_key] = item

        return evidence, lookup

    def rebuild(self) -> dict[str, Any]:
        """重新读取图谱/关系产物，替换索引；已登记的快照保持原样。"""
        with self._lock:
            signature, file_count = self._signature_now()
            entity_maps = self._load_entity_maps()
            relation_evidence, _ = self._load_relation_evidence(set(), entity_maps)
            # 先建立关系侧索引以补齐图谱边的抽取版本/分块信息，再计算已发布实体对。
            lookup_for_graph: dict[tuple[str, str, str, str], dict[str, Any]] = {}
            for item in relation_evidence:
                key = (item["relation_set_id"], _text_key(item["head"]), item["relation"], _text_key(item["tail"]))
                lookup_for_graph[key] = item

            graph_evidence, published_pairs = self._load_graph_evidence(lookup_for_graph, entity_maps)
            # 关系侧证据只有在对应实体对尚未入图时才作为“待补抽来源”出现；
            # 一旦入图，边本身已经承载了来历，不再重复列一条来源证据。
            pending = []
            for item in relation_evidence:
                pair_key = (item["doc_id"], _text_key(item["head"]), item["relation"], _text_key(item["tail"]))
                if pair_key in published_pairs:
                    continue
                item["in_published_graph"] = False
                item["published"] = False
                item["scope"] = "pending"
                pending.append(item)

            evidence = sorted(graph_evidence + pending, key=lambda item: item["evidence_id"])
            self._evidence = evidence
            self._by_id = {item["evidence_id"]: item for item in evidence}
            self._revision += 1
            self._signature = signature
            self._file_count = file_count
            self._built_at = time.time()
            self._counts = {
                "published": sum(1 for item in evidence if item["scope"] == "published"),
                "pending": sum(1 for item in evidence if item["scope"] == "pending"),
                "documents": len({item["doc_id"] for item in evidence}),
                "entities": len({_text_key(item["head"]) for item in evidence} | {_text_key(item["tail"]) for item in evidence}),
            }
            return self.stats_locked()

    def ensure_fresh(self, refresh: bool | None = None) -> None:
        with self._lock:
            if refresh is False and self._evidence:
                return
            signature, _ = self._signature_now()
            if refresh is True or not self._evidence or signature != self._signature:
                self.rebuild()

    def stats_locked(self) -> dict[str, Any]:
        return {
            "revision": self._revision,
            "built_at": self._built_at,
            "signature": self._signature,
            "files": self._file_count,
            "evidence_total": len(self._evidence),
            **self._counts,
            "snapshots": len(self._snapshots),
        }

    def stats(self, refresh: bool | None = None) -> dict[str, Any]:
        if refresh is not False:
            self.ensure_fresh(refresh)
        with self._lock:
            return self.stats_locked()

    # ------------------------------------------------------------------ 检索

    @staticmethod
    def _normalize_scope(scope: str | None) -> str:
        if not scope:
            return "all"
        canonical = SCOPE_ALIASES.get(str(scope).strip().casefold())
        if canonical is None:
            raise ProvenanceError(f"未知检索范围：{scope}，可选 published/pending/all")
        return canonical

    @staticmethod
    def _normalize_match(match: str | None) -> str:
        canonical = MATCH_ALIASES.get(str(match or "exact").strip().casefold())
        if canonical is None:
            raise ProvenanceError(f"未知匹配方式：{match}，可选 exact/contains")
        return canonical

    @staticmethod
    def _entity_matches(item: dict[str, Any], target: str, mode: str) -> bool:
        heads = (_text_key(item.get("head")), _text_key(item.get("tail")))
        if mode == "contains":
            return any(target in value for value in heads)
        return target in heads

    def _filter_evidence(
        self,
        entity: str,
        scope: str,
        doc_id: str | None,
        relation: str | None,
        match: str,
    ) -> list[dict[str, Any]]:
        target = _text_key(entity)
        if not target:
            raise ProvenanceError("实体文本不能为空")
        relation_key = _text_key(relation) if relation else ""
        doc_key = str(doc_id).strip() if doc_id else ""
        rows = []
        for item in self._evidence:
            if scope != "all" and item["scope"] != scope:
                continue
            if doc_key and item["doc_id"] != doc_key:
                continue
            if relation_key and _text_key(item["relation"]) != relation_key:
                continue
            if not self._entity_matches(item, target, match):
                continue
            rows.append(item)
        return rows

    @staticmethod
    def _sort_rows(rows: list[dict[str, Any]], sort: str | None) -> list[dict[str, Any]]:
        """排序只影响当前页内的顺序，不改变快照里的来源集合。

        先按 evidence_id 排好稳定的次序，再做一次稳定排序去调整主键方向，
        这样同分记录的顺序在翻页前后一致。
        """
        raw = (sort or "-confidence").strip()
        descending = raw.startswith("-")
        field = raw.lstrip("+-")
        if field not in SORT_FIELDS:
            # 兼容 "confidence_desc" / "doc_id_asc" 写法
            for suffix_desc in ("_desc", " desc"):
                if field.endswith(suffix_desc):
                    field, descending = field[: -len(suffix_desc)], True
                    break
            for suffix_asc in ("_asc", " asc"):
                if field.endswith(suffix_asc):
                    field, descending = field[: -len(suffix_asc)], False
                    break
        if field not in SORT_FIELDS:
            raise ProvenanceError(f"未知排序字段：{raw}，可选 {'/'.join(SORT_FIELDS)}")

        ordered = sorted(rows, key=lambda item: item["evidence_id"])
        ordered.sort(
            key=lambda item: _safe_float(item.get(field))
            if field in ("confidence", "observed_at")
            else str(item.get(field) or ""),
            reverse=descending,
        )
        return ordered

    def _register_snapshot(self, fingerprint: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
        token = f"PS-{self._revision}-{uuid.uuid4().hex[:12]}"
        id_set = {item["evidence_id"] for item in rows}
        snapshot = {
            "snapshot_id": token,
            "revision": self._revision,
            "created_at": time.time(),
            "fingerprint": dict(fingerprint),
            "records": copy.deepcopy(rows),
            "ids": [item["evidence_id"] for item in rows],
            "id_set": id_set,
            # 统计也一并锁定：各范围的条数在同一快照里保持一致，翻页不会漂移。
            "counts": self._counts_locked(fingerprint),
            "documents": sorted({item["doc_id"] for item in rows if item["doc_id"]}),
        }
        self._snapshots[token] = snapshot
        while len(self._snapshots) > self.MAX_SNAPSHOTS:
            self._snapshots.popitem(last=False)
        return snapshot

    def _get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        snapshot = self._snapshots.get(snapshot_id)
        if snapshot is None:
            raise SnapshotNotFoundError(f"检索快照 {snapshot_id} 不存在或已过期，请重新检索")
        return snapshot

    def search(
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
        refresh: bool | None = None,
    ) -> dict[str, Any]:
        """按实体检索溯证证据；翻页时携带 snapshot_id 可锁定来源集合。"""
        scope_key = self._normalize_scope(scope)
        match_key = self._normalize_match(match)
        page = max(int(page or 1), 1)
        size = max(min(int(size or 20), 200), 1)
        fingerprint = {
            "entity": _text_key(entity),
            "scope": scope_key,
            "doc_id": str(doc_id).strip() if doc_id else "",
            "relation": _text_key(relation) if relation else "",
            "match": match_key,
        }

        with self._lock:
            if snapshot_id:
                snapshot = self._get_snapshot(snapshot_id)
                if snapshot["fingerprint"] != fingerprint:
                    raise SnapshotMismatchError(
                        "检索条件与快照不一致，已拒绝在两组证据之间切换；请重新发起检索"
                    )
            else:
                self.ensure_fresh(refresh)
                rows = self._filter_evidence(
                    entity, scope_key, doc_id, relation, match_key
                )
                rows = sorted(rows, key=lambda item: item["evidence_id"])
                snapshot = self._register_snapshot(fingerprint, rows)

            rows = snapshot["records"]
            revision = snapshot["revision"]
            pinned_ids = list(snapshot["ids"])
            counts = dict(snapshot["counts"])
            documents = list(snapshot["documents"])

            ordered = self._sort_rows(rows, sort)
            total = len(ordered)
            start = (page - 1) * size
            items = copy.deepcopy(ordered[start:start + size])
            return {
                "entity": entity,
                "scope": scope_key,
                "doc_id": doc_id or "",
                "relation": relation or "",
                "match": match_key,
                "sort": sort or "-confidence",
                "page": page,
                "size": size,
                "total": total,
                "items": items,
                "source_ids": pinned_ids,
                "source_set_size": len(pinned_ids),
                "documents": documents,
                "counts": counts,
                "snapshot_id": snapshot["snapshot_id"],
                "revision": revision,
            }

    def _counts_of_rows(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "published": sum(1 for item in rows if item["scope"] == "published"),
            "pending": sum(1 for item in rows if item["scope"] == "pending"),
            "all": len(rows),
            "documents": len({item["doc_id"] for item in rows if item["doc_id"]}),
        }

    def _counts_locked(self, fingerprint: dict[str, Any]) -> dict[str, int]:
        entity = fingerprint["entity"]
        match = fingerprint["match"]
        doc_id = fingerprint["doc_id"] or None
        relation = fingerprint["relation"] or None
        published = self._filter_evidence(entity, "published", doc_id, relation, match)
        pending = self._filter_evidence(entity, "pending", doc_id, relation, match)
        return self._counts_of_rows(published + pending)

    def get_evidence(self, evidence_id: str) -> dict[str, Any]:
        with self._lock:
            self.ensure_fresh(None)
            item = self._by_id.get(evidence_id)
            if item is None:
                raise ProvenanceError(f"证据 {evidence_id} 不存在")
            return copy.deepcopy(item)

    def snapshot(self, snapshot_id: str) -> dict[str, Any]:
        with self._lock:
            snapshot = self._get_snapshot(snapshot_id)
            return {
                "snapshot_id": snapshot["snapshot_id"],
                "revision": snapshot["revision"],
                "created_at": snapshot["created_at"],
                "fingerprint": copy.deepcopy(snapshot["fingerprint"]),
                "source_ids": list(snapshot["ids"]),
                "source_set_size": len(snapshot["ids"]),
                "counts": dict(snapshot["counts"]),
                "documents": list(snapshot["documents"]),
            }

    def snapshot_records(
        self,
        snapshot_id: str,
        evidence_ids: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """取出快照中的证据深拷贝；用于重算时锁定“这一次查询的那组证据”。"""
        with self._lock:
            snapshot = self._get_snapshot(snapshot_id)
            if evidence_ids is None:
                return copy.deepcopy(snapshot["records"])
            selected: list[dict[str, Any]] = []
            requested = list(evidence_ids)
            foreign = [eid for eid in requested if eid not in snapshot["id_set"]]
            if foreign:
                raise SnapshotMismatchError(
                    "所选证据不属于该检索快照，已拒绝执行重算：" + ", ".join(foreign[:5])
                )
            by_id = {item["evidence_id"]: item for item in snapshot["records"]}
            for eid in requested:
                selected.append(copy.deepcopy(by_id[eid]))
            return selected

    def resolve_evidence(self, evidence_ids: Iterable[str]) -> list[dict[str, Any]]:
        with self._lock:
            self.ensure_fresh(None)
            result = []
            missing = []
            for eid in evidence_ids:
                item = self._by_id.get(eid)
                if item is None:
                    missing.append(eid)
                else:
                    result.append(copy.deepcopy(item))
            if missing:
                raise ProvenanceError("证据不存在：" + ", ".join(missing[:5]))
            return result


provenance_index = ProvenanceIndex()
