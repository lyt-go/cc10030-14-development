"""轨迹质量工作流接口：多批围栏重算、巡检单纠偏、异常段批量提交与命中出处回查。

通用资源路由只处理“登记/动作/列表”，这里补上带业务语义的动作端点；
路由层不做任何状态判断，全部委托给 ``GeoQualityService`` 的领域方法。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import service_for

router = APIRouter(prefix="/api/geo_quality", tags=["轨迹质量工作流"])


def quality_service():
    service = service_for("geo_quality")
    if service is None:
        raise HTTPException(status_code=500, detail="轨迹质量服务未注册")
    return service


class BatchSeed(BaseModel):
    batch_key: str
    sampling_date: str | None = None
    state: str | None = None


class RecomputePayload(BaseModel):
    batch_keys: list[str] = Field(default_factory=list)
    batches: list[BatchSeed] = Field(default_factory=list)
    fence_id: int = 1
    round_key: str | None = None
    request_id: str | None = None


class InterruptPayload(BaseModel):
    batch_key: str
    reason: str = ""


class DispatchInspectionPayload(BaseModel):
    batch_key: str
    recipient: str = "巡检一组"
    request_id: str | None = None


class TicketPayload(BaseModel):
    batch_key: str
    ticket_id: str | None = None
    recipient: str = "巡检一组"
    status: str = "待复核"
    version: int = 1


class TicketWithdrawPayload(BaseModel):
    authorization_id: str | None = None
    correction_id: str | None = None


class TicketSettlePayload(BaseModel):
    correction_id: str
    version: int | None = None


class CorrectionItemPayload(BaseModel):
    batch_key: str
    segments: list[dict[str, Any]] = Field(default_factory=list)


class CorrectionPayload(BaseModel):
    round_key: str | None = None
    batch_key: str | None = None
    segments: list[dict[str, Any]] = Field(default_factory=list)
    items: list[CorrectionItemPayload] | None = None
    idempotency_key: str | None = None


class SignCorrectionPayload(BaseModel):
    version: int
    signer: str = "质量复核组"
    note: str = ""


class DispatchPayload(BaseModel):
    mode: str = "withdraw"
    version: int | None = None


class ReviewPayload(BaseModel):
    batch_key: str
    point_id: int | str
    verdict: str
    reviewer: str = "质量复核组"
    approved: bool = False
    late_point: dict[str, Any] | None = None


def _raise_if_bad(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "操作未成立"))
    return result


@router.post("/recompute")
def recompute(payload: RecomputePayload) -> dict[str, Any]:
    """对多批标坏坐标发起围栏重算；单批失败不阻塞其他批。"""
    batch_keys = list(payload.batch_keys) or [item.batch_key for item in payload.batches]
    if not batch_keys:
        raise HTTPException(status_code=422, detail="至少指定一个批次")
    seeds = {item.batch_key: item for item in payload.batches}
    return quality_service().recompute_batches(
        batch_keys,
        sampling_dates={key: item.sampling_date for key, item in seeds.items() if item.sampling_date},
        batch_states={key: item.state for key, item in seeds.items() if item.state},
        fence_id=payload.fence_id,
        round_key=payload.round_key,
        request_id=payload.request_id,
    )


@router.get("/recompute")
def list_rounds() -> dict[str, Any]:
    rounds = quality_service().list_recompute_rounds()
    return {"items": rounds, "total": len(rounds)}


@router.get("/recompute/{round_key}")
def get_round(round_key: str) -> dict[str, Any]:
    result = quality_service().get_recompute_round(round_key)
    if result is None:
        raise HTTPException(status_code=404, detail=f"重算轮次 {round_key} 不存在")
    return result


@router.post("/recompute/{round_key}/interrupt")
def interrupt(round_key: str, payload: InterruptPayload) -> dict[str, Any]:
    return _raise_if_bad(
        quality_service().interrupt_recompute(round_key, payload.batch_key, payload.reason)
    )


@router.post("/recompute/{round_key}/resume")
def resume(round_key: str) -> dict[str, Any]:
    return _raise_if_bad(quality_service().resume_recompute(round_key))


@router.post("/recompute/{round_key}/dispatch")
def dispatch_inspection(round_key: str, payload: DispatchInspectionPayload) -> dict[str, Any]:
    """把已算出命中的批次下派巡检。"""
    return _raise_if_bad(quality_service().dispatch_inspection(
        round_key, payload.batch_key,
        recipient=payload.recipient, request_id=payload.request_id,
    ))


@router.get("/tickets")
def list_tickets(batch_key: str | None = None) -> dict[str, Any]:
    tickets = quality_service().list_inspection_tickets(batch_key)
    return {"items": tickets, "total": len(tickets)}


@router.post("/tickets")
def register_ticket(payload: TicketPayload) -> dict[str, Any]:
    return quality_service().register_inspection_ticket(
        payload.batch_key,
        recipient=payload.recipient,
        status=payload.status,
        version=payload.version,
        ticket_id=payload.ticket_id,
    )


@router.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str) -> dict[str, Any]:
    ticket = quality_service().get_inspection_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"巡检单 {ticket_id} 不存在")
    return ticket


@router.post("/tickets/{ticket_id}/withdraw")
def withdraw_ticket(ticket_id: str, payload: TicketWithdrawPayload) -> dict[str, Any]:
    return _raise_if_bad(quality_service().withdraw_ticket(
        ticket_id,
        authorization_id=payload.authorization_id,
        correction_id=payload.correction_id,
    ))


@router.post("/tickets/{ticket_id}/settle")
def settle_ticket(ticket_id: str, payload: TicketSettlePayload) -> dict[str, Any]:
    return _raise_if_bad(quality_service().settle_ticket(
        ticket_id, correction_id=payload.correction_id, version=payload.version,
    ))


@router.get("/corrections")
def list_corrections(round_key: str | None = None) -> dict[str, Any]:
    corrections = quality_service().list_corrections(round_key)
    return {"items": corrections, "total": len(corrections)}


@router.post("/corrections")
def submit_correction(payload: CorrectionPayload) -> dict[str, Any]:
    """按已确认异常段批量提交，逐批列出可撤回命中与仍可用点。"""
    items = [item.model_dump() for item in payload.items] if payload.items else None
    return _raise_if_bad(quality_service().submit_correction(
        payload.round_key,
        items=items,
        batch_key=payload.batch_key,
        segments=payload.segments,
        idempotency_key=payload.idempotency_key,
    ))


@router.get("/corrections/{correction_id}")
def get_correction(correction_id: str) -> dict[str, Any]:
    correction = quality_service().get_correction(correction_id)
    if correction is None:
        raise HTTPException(status_code=404, detail=f"纠正单 {correction_id} 不存在")
    return correction


@router.post("/corrections/{correction_id}/sign")
def sign_correction(correction_id: str, payload: SignCorrectionPayload) -> dict[str, Any]:
    return _raise_if_bad(quality_service().sign_correction_version(
        correction_id, payload.version, signer=payload.signer, note=payload.note,
    ))


@router.post("/corrections/{correction_id}/dispatch")
def dispatch_correction(correction_id: str, payload: DispatchPayload) -> dict[str, Any]:
    return _raise_if_bad(quality_service().dispatch_correction(
        correction_id, mode=payload.mode, version=payload.version,
    ))


@router.get("/reviews")
def list_reviews(batch_key: str | None = None) -> dict[str, Any]:
    reviews = quality_service().list_reviews(batch_key)
    return {"items": reviews, "total": len(reviews)}


@router.post("/reviews")
def sign_review(payload: ReviewPayload) -> dict[str, Any]:
    return _raise_if_bad(quality_service().sign_review(
        payload.batch_key, payload.point_id, payload.verdict,
        reviewer=payload.reviewer, approved=payload.approved, late_point=payload.late_point,
    ))


@router.post("/reviews/{review_id}/approve")
def approve_review(review_id: str) -> dict[str, Any]:
    return _raise_if_bad(quality_service().approve_review(review_id))


@router.get("/provenance/{batch_key}")
def get_provenance(batch_key: str, point_id: str | None = None) -> dict[str, Any]:
    return quality_service().get_hit_provenance(batch_key, point_id)
