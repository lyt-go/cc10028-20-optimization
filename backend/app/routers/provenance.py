"""图谱溯证路由：实体检索、翻页锁定证据快照、证据明细回查与来源重算。

与通用资源路由分开，是因为溯证检索的“范围/快照/重算”不是普通列表分页：
查询必须固定一份来源集合，重算必须只动点选的那条证据。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.kg_build import KgBuildService
from app.services.provenance import (
    ProvenanceError,
    SnapshotMismatchError,
    SnapshotNotFoundError,
    provenance_index,
)
from app.services.relation_extract import RelationExtractService

router = APIRouter(prefix="/api/kg", tags=["图谱溯证"])


class RecomputePayload(BaseModel):
    document_id: str | None = None
    snapshot_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    reason: str = ""


def _error(status_code: int, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)


@router.get("/provenance/search")
def search_provenance(
    entity: str = Query(..., min_length=1),
    scope: str = Query(default="all"),
    doc_id: str | None = Query(default=None),
    relation: str | None = Query(default=None),
    match: str = Query(default="exact"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    sort: str | None = Query(default=None),
    snapshot_id: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        result = provenance_index.search(
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
    except SnapshotNotFoundError as exc:
        raise _error(410, str(exc)) from exc
    except SnapshotMismatchError as exc:
        raise _error(409, str(exc)) from exc
    except ProvenanceError as exc:
        raise _error(400, str(exc)) from exc
    # 检索结果的 items 与 source_ids 来自同一快照，客户端翻页时带回 snapshot_id 即可锁定。
    return result


@router.get("/provenance/stats")
def provenance_stats() -> dict[str, Any]:
    return provenance_index.stats(refresh=True)


@router.get("/provenance/snapshots/{snapshot_id}")
def provenance_snapshot(snapshot_id: str) -> dict[str, Any]:
    try:
        return provenance_index.snapshot(snapshot_id)
    except SnapshotNotFoundError as exc:
        raise _error(410, str(exc)) from exc


@router.get("/provenance/evidence/{evidence_id}")
def provenance_evidence(evidence_id: str) -> dict[str, Any]:
    try:
        return provenance_index.get_evidence(evidence_id)
    except ProvenanceError as exc:
        raise _error(404, str(exc)) from exc


@router.post("/provenance/recompute")
def recompute_evidence(payload: RecomputePayload) -> dict[str, Any]:
    """用检索到的证据明细重算选定来源，并写回对应实体对。"""
    evidence = list(payload.evidence)
    if payload.snapshot_id:
        try:
            snapshot_records = provenance_index.snapshot_records(
                payload.snapshot_id,
                payload.evidence_ids or None,
            )
        except SnapshotMismatchError as exc:
            raise _error(409, str(exc)) from exc
        except SnapshotNotFoundError as exc:
            raise _error(410, str(exc)) from exc
        if not payload.evidence_ids:
            raise _error(400, "指定 snapshot_id 时必须同时给出 evidence_ids")
        evidence = snapshot_records
    elif payload.evidence_ids:
        try:
            evidence = provenance_index.resolve_evidence(payload.evidence_ids)
        except ProvenanceError as exc:
            raise _error(404, str(exc)) from exc

    if not evidence:
        raise _error(400, "未提供需要重算的证据：请传 evidence 或 snapshot_id + evidence_ids")

    if payload.document_id:
        foreign = sorted({str(item.get("doc_id") or "") for item in evidence} - {payload.document_id})
        if any(doc and doc != payload.document_id for doc in foreign):
            raise _error(
                409,
                "证据包含非目标文档，已拒绝重算：" + ", ".join(doc for doc in foreign if doc),
            )

    result = RelationExtractService().recompute_from_evidence(
        evidence,
        snapshot_id=payload.snapshot_id,
        evidence_ids=[item.get("evidence_id") for item in evidence if item.get("evidence_id")],
        reason=payload.reason,
    )
    if result.get("error"):
        raise _error(400, result["error"])
    return result


@router.get("/{entry_id}/provenance")
def entity_provenance(
    entry_id: int,
    scope: str = Query(default="all"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    snapshot_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """从已登记的三元组条目回到它的溯证证据，默认查已发布范围。"""
    service = KgBuildService()
    entry = service.get_entry(entry_id)
    if entry is None:
        raise _error(404, f"图谱三元组 {entry_id} 不存在或已归档")
    head = str(entry.get("头实体") or "").strip()
    relation = str(entry.get("谓词关系") or "").strip()
    if not head:
        raise _error(400, "该三元组缺少头实体，无法检索溯证证据")
    try:
        result = service.search_provenance(
            head,
            scope=scope,
            relation=relation or None,
            page=page,
            size=size,
            snapshot_id=snapshot_id,
        )
    except SnapshotNotFoundError as exc:
        raise _error(410, str(exc)) from exc
    except SnapshotMismatchError as exc:
        raise _error(409, str(exc)) from exc
    except ProvenanceError as exc:
        raise _error(400, str(exc)) from exc
    result["entry_id"] = entry_id
    return result
