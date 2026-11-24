"""轨迹质量围栏重算工作流：多批标坏坐标重算、巡检单关联、异常段批量纠正与撤回。

这一层只处理“重算 → 生成新命中 → 确认异常段 → 纠正/撤回 → 复核恢复”的领域状态，
定位点与围栏的几何判定仍复用采集批次数据文件和围栏顶点序列。

状态保存在 ``DATA_ROOT/quality_workflow/state.json``，每次操作读取-修改-写回，
因此检查点、中断续做和幂等提交都不依赖进程内变量，测试换临时目录即可隔离。
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.store import store

# 待补点由巡检班组确认，现行命中由围栏判定班组值守，两者互不覆盖。
DEFAULT_FENCE_VERTICES = "39.90,116.40;39.92,116.40;39.92,116.42;39.90,116.42"
DEFAULT_BACKFILL_OWNER = "巡检一组"
DEFAULT_HIT_OWNER = "围栏判定组"


def _empty_state() -> dict[str, Any]:
    return {
        "seq": {},
        "rounds": {},
        "hits": {},
        "tickets": {},
        "corrections": {},
        "reviews": {},
    }


class QualityWorkflowEngine:
    """轨迹质量工作流的文件状态机。

    所有方法都返回可直接 JSON 序列化的字典，HTTP 层不需要再做转换；
    ``ok`` 表达这一次操作是否成立，``duplicate`` 表达是否命中幂等键而跳过了重复处置。
    """

    def __init__(self, owner: Any) -> None:
        self.owner = owner

    # ------------------------------------------------------------------ 状态读写

    def _path(self):
        return self.owner._data_dir("quality_workflow") / "state.json"

    def _load(self) -> dict[str, Any]:
        path = self._path()
        state = _empty_state()
        if not path.exists():
            return state
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return state
        for key, value in _empty_state().items():
            state[key] = loaded.get(key, value)
        return state

    def _save(self, state: dict[str, Any]) -> None:
        path = self._path()
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    @staticmethod
    def _next(state: dict[str, Any], kind: str) -> int:
        value = int(state["seq"].get(kind, 0)) + 1
        state["seq"][kind] = value
        return value

    def _new_id(self, state: dict[str, Any], kind: str, prefix: str, width: int) -> str:
        return f"{prefix}-{self._next(state, kind):0{width}d}"

    # ------------------------------------------------------------------ 基础查询

    def _batch_entry(self, batch_key: str) -> dict[str, Any] | None:
        for row in store.rows("geo_source"):
            if str(row.get("批次编号")) == batch_key:
                return row
        return None

    def _batch_points(self, batch_key: str) -> list[dict[str, Any]]:
        """读取批次定位点；优先原始采集点，其次归并后的点。"""
        for sub, suffix in (("geo_points", ".jsonl"), ("dedup_merged", "_merged.jsonl")):
            path = self.owner._data_dir(sub) / f"{batch_key}{suffix}"
            if path.exists():
                return self.owner._read_jsonl(path)
        return []

    def _sampling_date(self, batch_key: str, entry: dict[str, Any] | None) -> str:
        """原采样日期：批次时间跨度优先，缺省用当天。"""
        if entry:
            span = str(entry.get("时间跨度") or "")
            head = span[:10]
            if len(head) == 10 and head[4] == "-" and head[7] == "-":
                return head
        for point in self._batch_points(batch_key):
            head = str(point.get("sampling_date") or "")[:10]
            if len(head) == 10:
                return head
        return time.strftime("%Y-%m-%d", time.localtime())

    @staticmethod
    def _coords(point: dict[str, Any]) -> tuple[float, float] | None:
        try:
            lat = float(point.get("lat"))
            lon = float(point.get("lon"))
        except (TypeError, ValueError, KeyError):
            return None
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return None
        return lat, lon

    @staticmethod
    def _is_bad_coordinate(point: dict[str, Any]) -> bool:
        if point.get("bad_coord") or point.get("coordinate_flagged"):
            return True
        status = str(point.get("coord_status") or "").strip().lower()
        return status in {"bad", "invalid", "标坏", "异常"}

    def _fence_vertices(self, fence_id: int) -> list[tuple[float, float]]:
        fence = store.find("geofence_rule", fence_id)
        raw = (fence or {}).get("顶点序列") or DEFAULT_FENCE_VERTICES
        vertices: list[tuple[float, float]] = []
        try:
            for item in str(raw).split(";"):
                parts = item.split(",")
                if len(parts) >= 2:
                    vertices.append((float(parts[0]), float(parts[1])))
        except (TypeError, ValueError):
            vertices = []
        if len(vertices) < 3:
            vertices = [
                (39.90, 116.40), (39.92, 116.40), (39.92, 116.42), (39.90, 116.42),
            ]
        return vertices

    @staticmethod
    def _point_in_polygon(lat: float, lon: float, vertices: list[tuple[float, float]]) -> bool:
        inside = False
        j = len(vertices) - 1
        for i in range(len(vertices)):
            vi_lat, vi_lon = vertices[i]
            vj_lat, vj_lon = vertices[j]
            if ((vi_lon > lon) != (vj_lon > lon)) and \
               (lat < (vj_lat - vi_lat) * (lon - vi_lon) / (vj_lon - vi_lon) + vi_lat):
                inside = not inside
            j = i
        return inside

    # ------------------------------------------------------------------ 巡检单

    def _latest_ticket(self, state: dict[str, Any], batch_key: str) -> dict[str, Any] | None:
        tickets = [
            ticket for ticket in state["tickets"].values()
            if ticket.get("batch_key") == batch_key
        ]
        if not tickets:
            return None
        tickets.sort(key=lambda item: (item.get("created_at", 0), item.get("ticket_id", "")))
        return tickets[-1]

    def _create_ticket(
        self,
        state: dict[str, Any],
        batch_key: str,
        *,
        recipient: str = DEFAULT_BACKFILL_OWNER,
        status: str = "待复核",
        source: str = "重算前旧单",
        version: int = 1,
        ticket_id: str | None = None,
    ) -> dict[str, Any]:
        ticket_id = ticket_id or self._new_id(state, "ticket", "IT", 4)
        ticket = {
            "ticket_id": ticket_id,
            "batch_key": batch_key,
            "recipient": recipient,
            "status": status,
            "version": int(version),
            "source": source,
            "created_at": time.time(),
            "withdrawals": [],
            "settled_by": None,
            "settled_version": None,
        }
        state["tickets"][ticket_id] = ticket
        return ticket

    def register_inspection_ticket(
        self,
        batch_key: str,
        *,
        recipient: str = DEFAULT_BACKFILL_OWNER,
        status: str = "待复核",
        version: int = 1,
        ticket_id: str | None = None,
    ) -> dict[str, Any]:
        """登记一张既有巡检单，供新命中生成前查回旧单。"""
        state = self._load()
        if ticket_id and ticket_id in state["tickets"]:
            return {"ok": True, "duplicate": True, "ticket": state["tickets"][ticket_id]}
        ticket = self._create_ticket(
            state, batch_key, recipient=recipient, status=status,
            source="手工登记", version=version, ticket_id=ticket_id,
        )
        self._save(state)
        return {"ok": True, "duplicate": False, "ticket": ticket}

    def get_inspection_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        return self._load()["tickets"].get(ticket_id)

    def lookup_previous_ticket(self, batch_key: str) -> dict[str, Any] | None:
        """生成新命中前查回该批次最近一张巡检单。"""
        return self._latest_ticket(self._load(), batch_key)

    def list_inspection_tickets(self, batch_key: str | None = None) -> list[dict[str, Any]]:
        tickets = list(self._load()["tickets"].values())
        if batch_key:
            tickets = [item for item in tickets if item.get("batch_key") == batch_key]
        tickets.sort(key=lambda item: item.get("created_at", 0))
        return tickets

    def dispatch_inspection(
        self,
        round_key: str,
        batch_key: str,
        *,
        recipient: str = DEFAULT_BACKFILL_OWNER,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """把已算出命中的批次下派巡检；重复下派只登记一次。"""
        state = self._load()
        round_record = state["rounds"].get(round_key)
        if round_record is None:
            return {"ok": False, "message": f"重算轮次 {round_key} 不存在"}
        batch = round_record["batches"].get(batch_key)
        if batch is None:
            return {"ok": False, "message": f"批次 {batch_key} 不在本轮重算范围内"}
        if request_id:
            for existing in round_record.setdefault("dispatch_requests", {}).values():
                if existing.get("request_id") == request_id:
                    return {
                        "ok": True, "duplicate": True, "batch": batch,
                        "ticket": state["tickets"].get(existing["ticket_id"]),
                        "message": "同一份下派申请已处理，本次未重复下派",
                    }
        if batch["status"] == "已下派巡检":
            return {"ok": True, "duplicate": True, "batch": batch,
                    "message": "该批已下派巡检，本次未重复下派"}
        if batch.get("disposition_confirmed"):
            return {"ok": False, "message": "该批处置已确认，不能再下派"}
        if batch["status"] != "围栏已算":
            return {
                "ok": False,
                "message": f"该批当前为{batch['status']}，不具备下派巡检条件",
                "batch": batch,
            }
        ticket = state["tickets"].get(batch.get("previous_ticket_id") or "")
        if ticket is None:
            ticket = self._create_ticket(
                state, batch_key, recipient=recipient,
                status="已下派巡检", source="重算后下派",
            )
            batch["previous_ticket_id"] = ticket["ticket_id"]
        else:
            ticket["status"] = "已下派巡检"
            ticket["recipient"] = recipient
            ticket["dispatched_at"] = time.time()
        batch["status"] = "已下派巡检"
        if request_id:
            round_record.setdefault("dispatch_requests", {})[ticket["ticket_id"]] = {
                "request_id": request_id, "batch_key": batch_key,
                "ticket_id": ticket["ticket_id"], "at": time.time(),
            }
        self._finalize_round(round_record)
        self._save(state)
        self._write_checkpoint(round_record)
        return {
            "ok": True, "duplicate": False, "batch": batch, "ticket": ticket,
            "message": f"已向 {recipient} 下派巡检",
        }

    def withdraw_ticket(
        self,
        ticket_id: str,
        *,
        authorization_id: str | None = None,
        correction_id: str | None = None,
    ) -> dict[str, Any]:
        """原纠正授权单独撤回旧单；重复撤回不再向接收方送出第二份。"""
        state = self._load()
        ticket = state["tickets"].get(ticket_id)
        if ticket is None:
            return {"ok": False, "message": f"巡检单 {ticket_id} 不存在", "ticket_id": ticket_id}
        if ticket["status"] == "已结清":
            return {"ok": False, "message": "旧单已结清，不能再单独撤回", "ticket": ticket}
        if ticket["status"] == "已撤回":
            return {
                "ok": True, "duplicate": True, "withdrawals_sent": 0,
                "message": "旧单此前已撤回，本次未重复送出", "ticket": ticket,
            }
        withdrawal = {
            "withdrawal_id": self._new_id(state, "withdrawal", "WD", 5),
            "ticket_id": ticket_id,
            "recipient": ticket.get("recipient"),
            "authorization_id": authorization_id,
            "correction_id": correction_id,
            "mode": "withdraw",
            "sent_at": time.time(),
        }
        ticket["withdrawals"].append(withdrawal)
        ticket["status"] = "已撤回"
        ticket["withdrawn_at"] = withdrawal["sent_at"]
        self._save(state)
        return {
            "ok": True, "duplicate": False, "withdrawals_sent": 1,
            "message": f"已向 {ticket.get('recipient')} 送出一份撤回",
            "ticket": ticket, "withdrawal": withdrawal,
        }

    def settle_ticket(
        self,
        ticket_id: str,
        *,
        correction_id: str,
        version: int | None = None,
    ) -> dict[str, Any]:
        """以补签的新版纠正记录结清旧单。"""
        state = self._load()
        ticket = state["tickets"].get(ticket_id)
        correction = state["corrections"].get(correction_id)
        if ticket is None:
            return {"ok": False, "message": f"巡检单 {ticket_id} 不存在"}
        if correction is None:
            return {"ok": False, "message": f"纠正单 {correction_id} 不存在"}
        signed = [item.get("version") for item in correction.get("signed_versions", [])]
        if not signed:
            return {"ok": False, "message": "纠正单尚未补签新版记录，不能结清旧单"}
        settle_version = int(version if version is not None else max(signed))
        if settle_version not in [int(item) for item in signed]:
            return {"ok": False, "message": f"纠正单未签过版本 {settle_version}"}
        if settle_version <= int(ticket.get("version", 1)):
            return {"ok": False, "message": "结清版本必须高于旧单当前版本"}
        if ticket["status"] == "已结清":
            return {"ok": True, "duplicate": True, "message": "旧单已结清", "ticket": ticket}
        if ticket["status"] == "已撤回":
            return {"ok": False, "message": "旧单已撤回，不能再用新版纠正记录结清", "ticket": ticket}
        ticket["status"] = "已结清"
        ticket["settled_by"] = correction_id
        ticket["settled_version"] = settle_version
        ticket["settled_at"] = time.time()
        self._save(state)
        return {
            "ok": True, "duplicate": False,
            "message": f"已用纠正单 {correction_id} 的 v{settle_version} 结清旧单",
            "ticket": ticket,
        }

    # ------------------------------------------------------------------ 围栏重算

    def recompute_batches(
        self,
        batch_keys: list[str],
        *,
        sampling_dates: dict[str, str] | None = None,
        fence_id: int = 1,
        round_key: str | None = None,
        request_id: str | None = None,
        batch_states: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """对多批标坏坐标发起围栏重算。

        - 每批独立处理，单批失败只进 ``failed_batches``，不阻塞其他批；
        - 新命中同步带上原采样日期与前一张巡检单；
        - 待补点范围与处理失败区间分开保存；
        - 同一 ``request_id`` 重复发起不再生成第二批命中。
        """
        state = self._load()
        if request_id:
            for existing in state["rounds"].values():
                if existing.get("request_key") == request_id:
                    payload = self._round_payload(existing, duplicate=True)
                    payload["message"] = "同一申请已重算过，本次未重复生成命中"
                    return payload
        if round_key and round_key in state["rounds"]:
            payload = self._round_payload(state["rounds"][round_key], duplicate=True)
            payload["message"] = "重算轮次已存在，本次未重复生成命中"
            return payload

        rid = round_key or self._new_id(state, "round", "QR", 4)
        round_record: dict[str, Any] = {
            "round_key": rid,
            "request_key": request_id,
            "fence_id": int(fence_id),
            "created_at": time.time(),
            "status": "重算中",
            "batch_keys": [],
            "batches": {},
            "pending_backfill_ranges": [],
            "failed_batches": [],
            "missing_evidence_batches": [],
            "corrections": [],
            "interrupted": None,
        }
        for batch_key in batch_keys:
            round_record["batch_keys"].append(batch_key)
            batch = self._recompute_one(
                state, round_record, batch_key,
                (sampling_dates or {}).get(batch_key),
                (batch_states or {}).get(batch_key),
            )
            round_record["batches"][batch_key] = batch
        self._finalize_round(round_record)
        state["rounds"][rid] = round_record
        self._save(state)
        self._write_checkpoint(round_record)
        return self._round_payload(round_record)

    def _recompute_one(
        self,
        state: dict[str, Any],
        round_record: dict[str, Any],
        batch_key: str,
        sampling_date: str | None,
        seeded_state: str | None,
    ) -> dict[str, Any]:
        entry = self._batch_entry(batch_key)
        date = sampling_date or self._sampling_date(batch_key, entry)
        ticket = self._latest_ticket(state, batch_key)
        if ticket is None:
            ticket = self._create_ticket(state, batch_key)
        record: dict[str, Any] = {
            "batch_key": batch_key,
            "sampling_date": date,
            "previous_ticket_id": ticket["ticket_id"],
            "status": "待重算",
            "hit_ids": [],
            "pending_backfill_ranges": [],
            "failed_ranges": [],
            "error": None,
            "disposition_confirmed": False,
            "confirmed_at": None,
            "confirmed_correction_id": None,
            "misjudged_hit_ids": [],
        }

        if seeded_state == "已下派巡检" or ticket.get("status") == "已下派巡检":
            record["status"] = "已下派巡检"
            record["error"] = "该批已下派巡检，重算只登记进度，不重复下派"
            return record
        if seeded_state == "缺定位证据":
            record["status"] = "缺定位证据"
            record["error"] = "缺少定位证据，未生成新命中"
            round_record["missing_evidence_batches"].append(batch_key)
            return record
        if seeded_state == "处理失败":
            record["status"] = "处理失败"
            record["error"] = "指定的处理失败状态"
            round_record["failed_batches"].append({"batch_key": batch_key, "error": record["error"]})
            return record

        try:
            points = self._batch_points(batch_key)
        except Exception as exc:  # 读取失败按批次失败处理
            record["status"] = "处理失败"
            record["error"] = str(exc)
            round_record["failed_batches"].append({"batch_key": batch_key, "error": str(exc)})
            return record
        if not points:
            record["status"] = "缺定位证据"
            record["error"] = "定位点数据为空或缺失"
            round_record["missing_evidence_batches"].append(batch_key)
            return record

        try:
            vertices = self._fence_vertices(round_record["fence_id"])
            batches_hits, backfill_ranges, failed_ranges = self._compute_batch_hits(
                state, round_record, record, points, vertices, ticket,
            )
        except Exception as exc:  # 单批异常不阻塞其他批
            record["status"] = "处理失败"
            record["error"] = str(exc)
            round_record["failed_batches"].append({"batch_key": batch_key, "error": str(exc)})
            return record

        record["hit_ids"] = [hit["hit_id"] for hit in batches_hits]
        record["pending_backfill_ranges"] = backfill_ranges
        record["failed_ranges"] = failed_ranges
        record["status"] = "围栏已算"
        round_record["pending_backfill_ranges"].extend(
            {"batch_key": batch_key, **item} for item in backfill_ranges
        )
        return record

    def _compute_batch_hits(
        self,
        state: dict[str, Any],
        round_record: dict[str, Any],
        record: dict[str, Any],
        points: list[dict[str, Any]],
        vertices: list[tuple[float, float]],
        ticket: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        hits: list[dict[str, Any]] = []
        bad_indexes: list[int] = []
        failed_indexes: list[int] = []
        for index, point in enumerate(points):
            point_id = point.get("point_id", index + 1)
            try:
                if self._is_bad_coordinate(point):
                    bad_indexes.append(index)
                    continue
                coords = self._coords(point)
                if coords is None:
                    bad_indexes.append(index)
                    continue
                lat, lon = coords
                if not self._point_in_polygon(lat, lon, vertices):
                    continue
                hit_id = self._new_id(state, "hit", "HIT", 6)
                hit = {
                    "hit_id": hit_id,
                    "batch_key": record["batch_key"],
                    "point_id": point_id,
                    "round_key": round_record["round_key"],
                    "fence_id": round_record["fence_id"],
                    "lat": lat,
                    "lon": lon,
                    "timestamp": point.get("timestamp"),
                    "sampling_date": record["sampling_date"],
                    "previous_ticket_id": ticket["ticket_id"],
                    "status": "现行",
                    "eligible": True,
                    "current_hit_owner": DEFAULT_HIT_OWNER,
                    "created_at": time.time(),
                    "misjudged_approved": False,
                    "provenance": [{
                        "event": "generated",
                        "round_key": round_record["round_key"],
                        "sampling_date": record["sampling_date"],
                        "previous_ticket_id": ticket["ticket_id"],
                        "hit_owner": DEFAULT_HIT_OWNER,
                        "at": time.time(),
                    }],
                }
                state["hits"][hit_id] = hit
                hits.append(hit)
            except Exception:
                failed_indexes.append(index)

        return (
            hits,
            self._index_ranges(points, bad_indexes, "待补点"),
            self._index_ranges(points, failed_indexes, "处理失败"),
        )

    @staticmethod
    def _index_ranges(
        points: list[dict[str, Any]], indexes: list[int], reason: str,
    ) -> list[dict[str, Any]]:
        """把离散点位下标合并成闭区间，待补点与处理失败各自成段。"""
        if not indexes:
            return []
        ranges: list[dict[str, Any]] = []
        start = prev = indexes[0]
        for index in indexes[1:]:
            if index == prev + 1:
                prev = index
                continue
            ranges.append(QualityWorkflowEngine._range_item(points, start, prev, reason))
            start = prev = index
        ranges.append(QualityWorkflowEngine._range_item(points, start, prev, reason))
        return ranges

    @staticmethod
    def _range_item(
        points: list[dict[str, Any]], start: int, end: int, reason: str,
    ) -> dict[str, Any]:
        return {
            "start_index": start,
            "end_index": end,
            "start_point_id": points[start].get("point_id", start + 1),
            "end_point_id": points[end].get("point_id", end + 1),
            "reason": reason,
        }

    @staticmethod
    def _finalize_round(round_record: dict[str, Any]) -> None:
        statuses = [item["status"] for item in round_record["batches"].values()]
        if not statuses:
            round_record["status"] = "已完成"
            return
        if "重算中断" in statuses:
            round_record["status"] = "部分完成"
        elif any(status in {"处理失败", "缺定位证据"} for status in statuses):
            round_record["status"] = "部分完成"
        else:
            round_record["status"] = "已完成"

    def _write_checkpoint(self, round_record: dict[str, Any]) -> None:
        self.owner._write_checkpoint(f"quality_recompute_{round_record['round_key']}", {
            "round_key": round_record["round_key"],
            "status": round_record["status"],
            "batch_progress": {
                key: item["status"] for key, item in round_record["batches"].items()
            },
            "pending_backfill_ranges": round_record["pending_backfill_ranges"],
            "failed_batches": round_record["failed_batches"],
        })

    def _round_payload(self, round_record: dict[str, Any], duplicate: bool = False) -> dict[str, Any]:
        batches = []
        for batch_key in round_record["batch_keys"]:
            item = round_record["batches"][batch_key]
            batches.append({
                "batch_key": item["batch_key"],
                "status": item["status"],
                "sampling_date": item["sampling_date"],
                "previous_ticket_id": item["previous_ticket_id"],
                "hit_ids": list(item["hit_ids"]),
                "hits_count": len(item["hit_ids"]),
                "pending_backfill_ranges": item["pending_backfill_ranges"],
                "failed_ranges": item["failed_ranges"],
                "error": item["error"],
                "disposition_confirmed": item["disposition_confirmed"],
                "confirmed_at": item["confirmed_at"],
                "confirmed_correction_id": item["confirmed_correction_id"],
                "misjudged_hit_ids": item["misjudged_hit_ids"],
            })
        return {
            "ok": True,
            "round_key": round_record["round_key"],
            "request_key": round_record.get("request_key"),
            "status": round_record["status"],
            "fence_id": round_record["fence_id"],
            "created_at": round_record["created_at"],
            "batch_keys": list(round_record["batch_keys"]),
            "batches": batches,
            "pending_backfill_ranges": round_record["pending_backfill_ranges"],
            "failed_batches": round_record["failed_batches"],
            "missing_evidence_batches": round_record["missing_evidence_batches"],
            "corrections": list(round_record.get("corrections", [])),
            "interrupted": round_record.get("interrupted"),
            "duplicate": duplicate,
        }

    def get_recompute_round(self, round_key: str) -> dict[str, Any] | None:
        round_record = self._load()["rounds"].get(round_key)
        return self._round_payload(round_record) if round_record else None

    def list_recompute_rounds(self) -> list[dict[str, Any]]:
        rounds = list(self._load()["rounds"].values())
        rounds.sort(key=lambda item: item.get("created_at", 0))
        return [self._round_payload(item) for item in rounds]

    def interrupt_recompute(self, round_key: str, batch_key: str, reason: str = "") -> dict[str, Any]:
        """标记某批中断，续做时只补该批，已确认处置的批次不重发。"""
        state = self._load()
        round_record = state["rounds"].get(round_key)
        if round_record is None:
            return {"ok": False, "message": f"重算轮次 {round_key} 不存在"}
        batch = round_record["batches"].get(batch_key)
        if batch is None:
            return {"ok": False, "message": f"批次 {batch_key} 不在本轮重算范围内"}
        if batch.get("disposition_confirmed"):
            return {"ok": False, "message": "该批处置已确认，不能回到中断状态", "batch": batch}
        if batch["status"] not in {"待重算", "围栏已算"}:
            return {
                "ok": False,
                "message": f"该批当前为{batch['status']}，没有可中断的重算进度",
                "batch": batch,
            }
        if batch["status"] == "重算中断":
            return {"ok": True, "duplicate": True, "message": "该批已处于中断状态", "batch": batch}
        # 未提交的命中撤回，避免续做时留下半成品命中；
        # 已被核准复核判定为误判的命中要留到续做后，不能随中断一起丢掉。
        retained: dict[str, Any] = {}
        for hit_id in batch["hit_ids"]:
            hit = state["hits"].get(hit_id)
            if hit and hit.get("misjudged_approved"):
                retained[hit_id] = {
                    "point_id": hit.get("point_id"),
                    "review_id": hit.get("misjudged_approved_review"),
                }
                continue
            state["hits"].pop(hit_id, None)
        batch["hit_ids"] = []
        batch["retained_misjudged"] = retained
        batch["status"] = "重算中断"
        batch["error"] = reason or "重算中断"
        batch["interrupted_at"] = time.time()
        round_record["interrupted"] = {
            "batch_key": batch_key, "reason": batch["error"], "at": batch["interrupted_at"],
        }
        self._finalize_round(round_record)
        self._save(state)
        self._write_checkpoint(round_record)
        return {"ok": True, "duplicate": False, "round": self._round_payload(round_record), "batch": batch}

    def resume_recompute(self, round_key: str) -> dict[str, Any]:
        """续做中断轮次：已确认处置或已算完的批次直接跳过。"""
        state = self._load()
        round_record = state["rounds"].get(round_key)
        if round_record is None:
            return {"ok": False, "message": f"重算轮次 {round_key} 不存在"}
        resumed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for batch_key in round_record["batch_keys"]:
            batch = round_record["batches"][batch_key]
            if batch.get("disposition_confirmed"):
                skipped.append({"batch_key": batch_key, "reason": "处置已确认，续做不重发"})
                continue
            if batch["status"] in {"围栏已算", "已下派巡检", "缺定位证据", "处理失败"}:
                skipped.append({"batch_key": batch_key, "reason": f"当前状态 {batch['status']}，无待续步骤"})
                continue
            # 只重做中断/待重算的批次，先清掉半成品
            for hit_id in batch["hit_ids"]:
                state["hits"].pop(hit_id, None)
            round_record["pending_backfill_ranges"] = [
                item for item in round_record["pending_backfill_ranges"]
                if item.get("batch_key") != batch_key
            ]
            refreshed = self._recompute_one(
                state, round_record, batch_key, batch.get("sampling_date"), None,
            )
            # 保留前一张巡检单的关联，避免续做换单
            refreshed["previous_ticket_id"] = batch.get("previous_ticket_id") or refreshed["previous_ticket_id"]
            for hit_id in refreshed["hit_ids"]:
                state["hits"][hit_id]["previous_ticket_id"] = refreshed["previous_ticket_id"]
            # 中断前已核准的误判复核，在新命中里重新挂回对应点位，方便纠正单汇总撤回
            retained = batch.get("retained_misjudged") or {}
            retained_by_point = {
                str(mark.get("point_id")): mark for mark in retained.values()
            }
            carried: list[str] = []
            for hit_id in refreshed["hit_ids"]:
                hit = state["hits"][hit_id]
                mark = retained_by_point.get(str(hit.get("point_id")))
                if mark and not hit.get("misjudged_approved"):
                    hit["misjudged"] = True
                    hit["misjudged_approved"] = True
                    hit["misjudged_approved_review"] = mark.get("review_id")
                    carried.append(hit_id)
            refreshed["misjudged_hit_ids"] = sorted(set(
                refreshed["misjudged_hit_ids"] + list(retained.keys()) + carried
            ))
            refreshed["retained_misjudged"] = retained
            round_record["batches"][batch_key] = refreshed
            resumed.append({"batch_key": batch_key, "status": refreshed["status"]})
        round_record["interrupted"] = None
        self._finalize_round(round_record)
        self._save(state)
        self._write_checkpoint(round_record)
        return {
            "ok": True,
            "round_key": round_key,
            "round": self._round_payload(round_record),
            "resumed": resumed,
            "skipped": skipped,
            "message": f"续做 {len(resumed)} 批，跳过 {len(skipped)} 批",
        }

    # ------------------------------------------------------------------ 纠正与撤回

    def submit_correction(
        self,
        round_key: str | None = None,
        *,
        items: list[dict[str, Any]] | None = None,
        batch_key: str | None = None,
        segments: list[dict[str, Any]] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """按已确认异常段批量提交；逐批列出可撤回命中，单批失败不阻塞其他批。"""
        state = self._load()
        if items is None:
            if batch_key is None:
                return {"ok": False, "message": "缺少批次或异常段"}
            items = [{"batch_key": batch_key, "segments": segments or []}]
        round_record = self._pick_round(state, round_key, [item["batch_key"] for item in items])
        if round_record is None:
            return {"ok": False, "message": "找不到包含这些批次的重算轮次，请先重算"}

        key = idempotency_key or self.owner._compute_checksum(json.dumps(
            {"round": round_record["round_key"], "items": items},
            ensure_ascii=False, sort_keys=True, default=str,
        ))
        for existing in state["corrections"].values():
            if existing.get("request_key") == key:
                payload = self._correction_payload(existing, duplicate=True)
                payload["message"] = "同一批异常段已提交过，本次只计一次修正"
                return payload

        correction = {
            "correction_id": self._new_id(state, "correction", "CORR", 5),
            "round_key": round_record["round_key"],
            "request_key": key,
            "status": "待发出",
            "created_at": time.time(),
            "items": [],
            "failed_batches": [],
            "signed_versions": [],
            "dispatched_at": None,
            "withdrawals": [],
            "settled_tickets": [],
        }
        for item in items:
            item_batch = str(item.get("batch_key") or "")
            try:
                entry = self._build_correction_item(
                    state, round_record, item_batch, item.get("segments") or [],
                )
            except Exception as exc:
                entry = {
                    "batch_key": item_batch, "ok": False, "error": str(exc),
                    "segments": [], "hits_to_withdraw": [], "usable_point_ids": [],
                }
            correction["items"].append(entry)
            if not entry["ok"]:
                correction["failed_batches"].append({
                    "batch_key": item_batch, "error": entry.get("error"),
                })

        if not any(item["ok"] for item in correction["items"]):
            return {
                "ok": False,
                "message": "没有可提交的批次，纠正单未创建",
                "items": correction["items"],
                "failed_batches": correction["failed_batches"],
            }

        state["corrections"][correction["correction_id"]] = correction
        round_record.setdefault("corrections", []).append(correction["correction_id"])
        self._save(state)
        return self._correction_payload(correction)

    def _pick_round(
        self, state: dict[str, Any], round_key: str | None, batch_keys: list[str],
    ) -> dict[str, Any] | None:
        if round_key:
            return state["rounds"].get(round_key)
        candidates = [
            item for item in state["rounds"].values()
            if all(key in item["batches"] for key in batch_keys)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: item.get("created_at", 0))
        return candidates[-1]

    def _build_correction_item(
        self,
        state: dict[str, Any],
        round_record: dict[str, Any],
        batch_key: str,
        segments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        batch = round_record["batches"].get(batch_key)
        if batch is None:
            return {
                "batch_key": batch_key, "ok": False,
                "error": "批次不在该重算轮次内", "segments": [],
                "hits_to_withdraw": [], "usable_point_ids": [],
            }
        if batch.get("status") != "围栏已算":
            return {
                "batch_key": batch_key, "ok": False,
                "error": f"批次当前为{batch.get('status')}，没有可提交的围栏结果",
                "segments": [], "hits_to_withdraw": [], "usable_point_ids": [],
            }
        if batch.get("disposition_confirmed"):
            return {
                "batch_key": batch_key, "ok": False,
                "error": "该批处置已确认，重复提交不再送出额外处置",
                "segments": [], "hits_to_withdraw": [], "usable_point_ids": [],
            }
        points = self._batch_points(batch_key)
        by_point = {point.get("point_id", index + 1): point for index, point in enumerate(points)}
        hits = [
            state["hits"][hit_id] for hit_id in batch["hit_ids"]
            if hit_id in state["hits"] and state["hits"][hit_id].get("batch_key") == batch_key
        ]
        covered: set[Any] = set()
        segment_results: list[dict[str, Any]] = []
        hits_to_withdraw: list[str] = []
        segments = [item for item in (segments or []) if isinstance(item, dict)]
        for segment in segments:
            point_ids = self._segment_point_ids(segment, points)
            covered.update(point_ids)
            matched = [
                hit for hit in hits
                if hit.get("point_id") in point_ids and hit.get("status") == "现行"
            ]
            for hit in matched:
                if hit["hit_id"] not in hits_to_withdraw:
                    hits_to_withdraw.append(hit["hit_id"])
            segment_results.append({
                "segment_id": segment.get("segment_id") or f"SEG-{len(segment_results) + 1}",
                "point_ids": sorted(str(item) for item in point_ids),
                "hit_ids": [hit["hit_id"] for hit in matched],
                "usable_point_ids": sorted(
                    str(item) for item in point_ids
                    if item in by_point and not self._is_bad_coordinate(by_point[item])
                    and self._coords(by_point[item]) is not None
                ),
            })
        # 已核准复核判为误判的命中，提交时一并纳入撤回
        for hit in hits:
            if hit.get("misjudged_approved") and hit["hit_id"] not in hits_to_withdraw:
                hits_to_withdraw.append(hit["hit_id"])
        usable = [
            point_id for point_id, point in by_point.items()
            if point_id not in covered
            and not self._is_bad_coordinate(point)
            and self._coords(point) is not None
        ]
        return {
            "batch_key": batch_key,
            "ok": True,
            "segments": segment_results,
            "hits_to_withdraw": hits_to_withdraw,
            "usable_point_ids": sorted(str(item) for item in usable),
            "previous_ticket_id": batch.get("previous_ticket_id"),
        }

    @staticmethod
    def _segment_point_ids(segment: dict[str, Any], points: list[dict[str, Any]]) -> set[Any]:
        ids: set[Any] = set()
        for raw in segment.get("point_ids") or []:
            try:
                ids.add(int(raw))
            except (TypeError, ValueError):
                ids.add(raw)
        point_id_by_index = {
            index: point.get("point_id", index + 1) for index, point in enumerate(points)
        }
        for bound_key in ("start_point_id", "end_point_id"):
            value = segment.get(bound_key)
            if value is not None:
                ids.add(int(value))
        range_spec = segment.get("range") or {}
        if range_spec:
            start = range_spec.get("start_index")
            end = range_spec.get("end_index")
            if start is None and range_spec.get("start_point_id") is not None:
                target = range_spec["start_point_id"]
                start = next((idx for idx, pid in point_id_by_index.items() if pid == target), None)
            if end is None and range_spec.get("end_point_id") is not None:
                target = range_spec["end_point_id"]
                end = next((idx for idx, pid in point_id_by_index.items() if pid == target), None)
            if start is not None and end is not None:
                for index in range(int(start), int(end) + 1):
                    ids.add(point_id_by_index.get(index, index + 1))
        # 起止点都在时补齐中间点，避免调用方只给端点
        if "start_point_id" in segment and "end_point_id" in segment:
            ordered = [point.get("point_id") for point in points]
            try:
                start = ordered.index(segment["start_point_id"])
                end = ordered.index(segment["end_point_id"])
            except ValueError:
                return ids
            if start > end:
                start, end = end, start
            ids.update(point_id_by_index[index] for index in range(start, end + 1))
        return ids

    def _correction_payload(self, correction: dict[str, Any], duplicate: bool = False) -> dict[str, Any]:
        return {
            "ok": True,
            "correction_id": correction["correction_id"],
            "round_key": correction["round_key"],
            "status": correction["status"],
            "duplicate": duplicate,
            "request_key": correction.get("request_key"),
            "items": correction["items"],
            "failed_batches": correction.get("failed_batches", []),
            "signed_versions": correction.get("signed_versions", []),
            "dispatched_at": correction.get("dispatched_at"),
            "withdrawals": correction.get("withdrawals", []),
            "settled_tickets": correction.get("settled_tickets", []),
            "refused_settlements": correction.get("refused_settlements", []),
        }

    def get_correction(self, correction_id: str) -> dict[str, Any] | None:
        correction = self._load()["corrections"].get(correction_id)
        return self._correction_payload(correction) if correction else None

    def list_corrections(self, round_key: str | None = None) -> list[dict[str, Any]]:
        corrections = list(self._load()["corrections"].values())
        if round_key:
            corrections = [item for item in corrections if item.get("round_key") == round_key]
        corrections.sort(key=lambda item: item.get("created_at", 0))
        return [self._correction_payload(item) for item in corrections]

    def sign_correction_version(
        self,
        correction_id: str,
        version: int,
        *,
        signer: str = "质量复核组",
        note: str = "",
    ) -> dict[str, Any]:
        """补签新版纠正记录；后续可用它结清旧巡检单。"""
        state = self._load()
        correction = state["corrections"].get(correction_id)
        if correction is None:
            return {"ok": False, "message": f"纠正单 {correction_id} 不存在"}
        if any(int(item.get("version")) == int(version) for item in correction["signed_versions"]):
            return {
                "ok": True, "duplicate": True,
                "message": f"版本 v{version} 已签过", "correction": self._correction_payload(correction),
            }
        correction["signed_versions"].append({
            "version": int(version), "signer": signer, "note": note, "signed_at": time.time(),
        })
        self._save(state)
        return {
            "ok": True, "duplicate": False,
            "message": f"已补签纠正单 v{version}",
            "correction": self._correction_payload(correction),
        }

    def dispatch_correction(
        self,
        correction_id: str,
        *,
        mode: str = "withdraw",
        version: int | None = None,
    ) -> dict[str, Any]:
        """发出纠正单：撤回命中，并按 ``mode`` 撤回旧单或以其新版结清旧单。

        重复发出不会产生第二份撤回，也不会重复扣减命中。
        """
        state = self._load()
        correction = state["corrections"].get(correction_id)
        if correction is None:
            return {"ok": False, "message": f"纠正单 {correction_id} 不存在"}
        if correction["status"] != "待发出":
            payload = self._correction_payload(correction, duplicate=True)
            payload["withdrawals_sent"] = 0
            payload["message"] = "纠正单已发出，本次未送出额外处置"
            return payload
        if mode not in {"withdraw", "settle"}:
            return {"ok": False, "message": f"未知处置方式 {mode}"}
        if mode == "settle" and not correction.get("signed_versions"):
            return {"ok": False, "message": "未补签新版纠正记录，不能以结清方式发出"}

        round_record = state["rounds"].get(correction["round_key"])
        per_batch: list[dict[str, Any]] = []
        withdrawals_sent = 0
        settled: list[str] = []
        withdrawn: list[str] = []
        refused: list[dict[str, Any]] = []
        for item in correction["items"]:
            if not item.get("ok"):
                per_batch.append({
                    "batch_key": item["batch_key"], "ok": False,
                    "error": item.get("error"), "withdrawn_hits": [], "ticket": None,
                })
                continue
            withdrawn_hits = []
            restored_kept: list[str] = []
            for hit_id in item["hits_to_withdraw"]:
                hit = state["hits"].get(hit_id)
                if hit is None or hit.get("status") == "已撤回":
                    continue
                # 提交之后又被已签复核用有效晚报点恢复的命中，不再跟着撤回
                if hit.get("restored_at") and float(hit["restored_at"]) >= float(correction["created_at"]):
                    restored_kept.append(hit_id)
                    continue
                hit["status"] = "已撤回"
                hit["eligible"] = False
                hit["withdrawn_at"] = time.time()
                hit["provenance"].append({
                    "event": "withdrawn",
                    "correction_id": correction_id,
                    "mode": mode,
                    "at": hit["withdrawn_at"],
                })
                withdrawn_hits.append(hit_id)
            ticket_info = None
            ticket = state["tickets"].get(item.get("previous_ticket_id") or "")
            if ticket is not None:
                if mode == "withdraw":
                    result = self._send_withdrawal(state, ticket, correction_id)
                    withdrawals_sent += result["withdrawals_sent"]
                    ticket_info = {"ticket_id": ticket["ticket_id"], "action": "withdraw", **result}
                    if result["withdrawals_sent"]:
                        withdrawn.append(ticket["ticket_id"])
                else:
                    result = self._settle_from_correction(state, ticket, correction, version)
                    ticket_info = {"ticket_id": ticket["ticket_id"], "action": "settle", **result}
                    if not result.get("ok"):
                        # 结清被拒（旧单已撤回等）只记在该批，不影响其他批落的命中
                        refused.append({
                            "batch_key": item["batch_key"],
                            "ticket_id": ticket["ticket_id"],
                            "error": result.get("message"),
                        })
                    elif not result.get("duplicate"):
                        settled.append(ticket["ticket_id"])
            batch = round_record["batches"].get(item["batch_key"]) if round_record else None
            if batch is not None:
                batch["disposition_confirmed"] = True
                batch["confirmed_at"] = time.time()
                batch["confirmed_correction_id"] = correction_id
            per_batch.append({
                "batch_key": item["batch_key"], "ok": True, "error": None,
                "withdrawn_hits": withdrawn_hits, "ticket": ticket_info,
                "hits_kept_by_review": restored_kept,
            })

        correction["status"] = "已发出"
        correction["dispatched_at"] = time.time()
        correction["withdrawals"].extend(
            withdrawal for ticket in state["tickets"].values()
            for withdrawal in ticket.get("withdrawals", [])
            if withdrawal.get("correction_id") == correction_id
        )
        correction["settled_tickets"] = settled
        correction["refused_settlements"] = refused
        if round_record is not None:
            self._finalize_round(round_record)
        self._save(state)
        if round_record is not None:
            self._write_checkpoint(round_record)
        payload = self._correction_payload(correction)
        payload.update({
            "withdrawals_sent": withdrawals_sent,
            "withdrawn_tickets": withdrawn,
            "settled_tickets": settled,
            "refused_settlements": refused,
            "per_batch": per_batch,
            "mode": mode,
            "message": "纠正单已发出",
        })
        return payload

    def _send_withdrawal(
        self, state: dict[str, Any], ticket: dict[str, Any], correction_id: str,
    ) -> dict[str, Any]:
        if ticket["status"] in {"已撤回", "已结清"}:
            return {
                "withdrawals_sent": 0, "duplicate": True,
                "message": f"旧单当前为{ticket['status']}，未重复送出撤回",
            }
        withdrawal = {
            "withdrawal_id": self._new_id(state, "withdrawal", "WD", 5),
            "ticket_id": ticket["ticket_id"],
            "recipient": ticket.get("recipient"),
            "correction_id": correction_id,
            "mode": "withdraw",
            "sent_at": time.time(),
        }
        ticket["withdrawals"].append(withdrawal)
        ticket["status"] = "已撤回"
        ticket["withdrawn_at"] = withdrawal["sent_at"]
        return {"withdrawals_sent": 1, "duplicate": False, "withdrawal": withdrawal, "message": "已送出一份撤回"}

    def _settle_from_correction(
        self,
        state: dict[str, Any],
        ticket: dict[str, Any],
        correction: dict[str, Any],
        version: int | None,
    ) -> dict[str, Any]:
        signed = [int(item.get("version")) for item in correction.get("signed_versions", [])]
        settle_version = int(version if version is not None else max(signed))
        if settle_version not in signed:
            return {"ok": False, "message": f"纠正单未签过版本 {settle_version}"}
        if ticket["status"] == "已结清":
            return {"ok": True, "duplicate": True, "message": "旧单已结清"}
        if ticket["status"] == "已撤回":
            return {"ok": False, "message": "旧单已撤回，不能再结清"}
        ticket["status"] = "已结清"
        ticket["settled_by"] = correction["correction_id"]
        ticket["settled_version"] = settle_version
        ticket["settled_at"] = time.time()
        return {"ok": True, "duplicate": False, "message": f"已用 v{settle_version} 结清旧单"}

    # ------------------------------------------------------------------ 复核

    def sign_review(
        self,
        batch_key: str,
        point_id: Any,
        verdict: str,
        *,
        reviewer: str = "质量复核组",
        approved: bool = False,
        late_point: dict[str, Any] | None = None,
        review_id: str | None = None,
    ) -> dict[str, Any]:
        """登记并签署一份复核：判为误判进入待撤回，带有效晚报点则恢复命中资格。"""
        state = self._load()
        hit = self._latest_hit(state, batch_key, point_id)
        if hit is None:
            return {"ok": False, "message": f"批次 {batch_key} 的点 {point_id} 没有命中记录"}
        normalized = str(verdict).strip()
        withdraw_verdicts = {"误判", "撤销", "撤回", "misjudged", "withdraw"}
        restore_verdicts = {"恢复", "复核通过", "有效", "restore", "valid"}
        if normalized in withdraw_verdicts:
            kind = "withdraw"
        elif normalized in restore_verdicts:
            kind = "restore"
        else:
            return {"ok": False, "message": f"未知复核结论 {verdict}"}

        review_id = review_id or self._new_id(state, "review", "REV", 5)
        review: dict[str, Any] = {
            "review_id": review_id,
            "batch_key": batch_key,
            "point_id": point_id,
            "hit_id": hit["hit_id"],
            "verdict": normalized,
            "kind": kind,
            "reviewer": reviewer,
            "signed": True,
            "approved": bool(approved),
            "late_point": late_point,
            "created_at": time.time(),
            "applied": False,
            "error": None,
        }
        if kind == "withdraw":
            review["applied"] = True
            hit["misjudged"] = True
            hit["misjudged_review"] = review_id
            if approved:
                hit["misjudged_approved"] = True
                hit["misjudged_approved_review"] = review_id
            round_record = state["rounds"].get(hit["round_key"])
            batch = round_record["batches"].get(batch_key) if round_record else None
            if batch is not None and hit["hit_id"] not in batch["misjudged_hit_ids"]:
                batch["misjudged_hit_ids"].append(hit["hit_id"])
            state["reviews"][review_id] = review
            self._save(state)
            return {
                "ok": True, "duplicate": False,
                "message": "复核已登记，误判命中进入待撤回清单",
                "review": review, "hit": hit,
            }

        valid, reason = self._valid_late_point(hit, late_point)
        if not valid:
            review["error"] = reason
            review["applied"] = False
            state["reviews"][review_id] = review
            self._save(state)
            return {"ok": False, "message": reason, "review": review, "hit": hit}

        review["applied"] = True
        previous_status = hit.get("status")
        hit["status"] = "现行"
        hit["eligible"] = True
        hit["restored_at"] = time.time()
        hit["provenance"].append({
            "event": "restored",
            "review_id": review_id,
            "late_point": late_point,
            "previous_status": previous_status,
            "mode": "signed_review",
            "at": hit["restored_at"],
        })
        state["reviews"][review_id] = review
        self._save(state)
        return {
            "ok": True, "duplicate": False,
            "message": "已按有效晚报点恢复该点的现行命中资格",
            "review": review, "hit": hit,
        }

    def approve_review(self, review_id: str) -> dict[str, Any]:
        state = self._load()
        review = state["reviews"].get(review_id)
        if review is None:
            return {"ok": False, "message": f"复核 {review_id} 不存在"}
        if review.get("approved"):
            return {"ok": True, "duplicate": True, "message": "复核已核准", "review": review}
        review["approved"] = True
        review["approved_at"] = time.time()
        hit = state["hits"].get(review.get("hit_id") or "")
        if hit is not None and review.get("kind") == "withdraw":
            hit["misjudged_approved"] = True
            hit["misjudged_approved_review"] = review_id
            round_record = state["rounds"].get(hit["round_key"])
            batch = round_record["batches"].get(hit["batch_key"]) if round_record else None
            if batch is not None and hit["hit_id"] not in batch["misjudged_hit_ids"]:
                batch["misjudged_hit_ids"].append(hit["hit_id"])
        self._save(state)
        return {"ok": True, "duplicate": False, "message": "复核已核准", "review": review}

    def _latest_hit(
        self, state: dict[str, Any], batch_key: str, point_id: Any,
    ) -> dict[str, Any] | None:
        candidates = [
            hit for hit in state["hits"].values()
            if hit.get("batch_key") == batch_key and str(hit.get("point_id")) == str(point_id)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: item.get("created_at", 0))
        return candidates[-1]

    @staticmethod
    def _valid_late_point(
        hit: dict[str, Any], late_point: dict[str, Any] | None,
    ) -> tuple[bool, str]:
        if not late_point:
            return False, "缺少晚报点，不能恢复命中资格"
        if QualityWorkflowEngine._coords(late_point) is None:
            return False, "晚报点坐标无效，不能恢复命中资格"
        try:
            timestamp = float(late_point.get("timestamp"))
        except (TypeError, ValueError):
            return False, "晚报点缺少有效时间戳，不能恢复命中资格"
        if timestamp <= 0:
            return False, "晚报点时间戳无效，不能恢复命中资格"
        return True, ""

    # ------------------------------------------------------------------ 出处追溯

    def get_hit_provenance(
        self, batch_key: str, point_id: Any | None = None,
    ) -> dict[str, Any]:
        """回查现行命中的有效出处；旧单撤回或结清都不影响命中来源。"""
        state = self._load()
        hits = [
            hit for hit in state["hits"].values()
            if hit.get("batch_key") == batch_key
            and (point_id is None or str(hit.get("point_id")) == str(point_id))
        ]
        hits.sort(key=lambda item: item.get("created_at", 0))
        current = None
        for hit in reversed(hits):
            if hit.get("status") == "现行":
                current = hit
                break
        tickets = [
            ticket for ticket in state["tickets"].values()
            if ticket.get("batch_key") == batch_key
        ]
        tickets.sort(key=lambda item: item.get("created_at", 0))
        provenance = current.get("provenance", []) if current else []
        valid_source = provenance[-1] if provenance else None
        return {
            "ok": True,
            "batch_key": batch_key,
            "point_id": point_id,
            "current_hit": current,
            "current_hit_eligible": bool(current and current.get("status") == "现行"),
            "provenance": provenance,
            "valid_source": valid_source,
            "ticket_dispositions": [
                {
                    "ticket_id": ticket["ticket_id"],
                    "status": ticket["status"],
                    "recipient": ticket.get("recipient"),
                    "withdrawals": ticket.get("withdrawals", []),
                    "settled_by": ticket.get("settled_by"),
                    "settled_version": ticket.get("settled_version"),
                    "version": ticket.get("version"),
                }
                for ticket in tickets
            ],
            "hits": hits,
        }

    def list_reviews(self, batch_key: str | None = None) -> list[dict[str, Any]]:
        reviews = list(self._load()["reviews"].values())
        if batch_key:
            reviews = [item for item in reviews if item.get("batch_key") == batch_key]
        reviews.sort(key=lambda item: item.get("created_at", 0))
        return reviews


class QualityWorkflowMixin:
    """把工作流入口挂到轨迹质量服务上，保持单模块单文件对外的方法面。"""

    _workflow_engine_instance: QualityWorkflowEngine | None = None

    def workflow_engine(self) -> QualityWorkflowEngine:
        engine = self.__dict__.get("_workflow_engine_instance")
        if engine is None:
            engine = QualityWorkflowEngine(self)
            self.__dict__["_workflow_engine_instance"] = engine
        return engine

    # 重算
    def recompute_batches(self, batch_keys: list[str], **kwargs: Any) -> dict[str, Any]:
        return self.workflow_engine().recompute_batches(batch_keys, **kwargs)

    def get_recompute_round(self, round_key: str) -> dict[str, Any] | None:
        return self.workflow_engine().get_recompute_round(round_key)

    def list_recompute_rounds(self) -> list[dict[str, Any]]:
        return self.workflow_engine().list_recompute_rounds()

    def interrupt_recompute(self, round_key: str, batch_key: str, reason: str = "") -> dict[str, Any]:
        return self.workflow_engine().interrupt_recompute(round_key, batch_key, reason)

    def resume_recompute(self, round_key: str) -> dict[str, Any]:
        return self.workflow_engine().resume_recompute(round_key)

    # 巡检单
    def register_inspection_ticket(self, batch_key: str, **kwargs: Any) -> dict[str, Any]:
        return self.workflow_engine().register_inspection_ticket(batch_key, **kwargs)

    def get_inspection_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        return self.workflow_engine().get_inspection_ticket(ticket_id)

    def lookup_previous_ticket(self, batch_key: str) -> dict[str, Any] | None:
        return self.workflow_engine().lookup_previous_ticket(batch_key)

    def list_inspection_tickets(self, batch_key: str | None = None) -> list[dict[str, Any]]:
        return self.workflow_engine().list_inspection_tickets(batch_key)

    def withdraw_ticket(self, ticket_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.workflow_engine().withdraw_ticket(ticket_id, **kwargs)

    def dispatch_inspection(self, round_key: str, batch_key: str, **kwargs: Any) -> dict[str, Any]:
        return self.workflow_engine().dispatch_inspection(round_key, batch_key, **kwargs)

    def settle_ticket(self, ticket_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.workflow_engine().settle_ticket(ticket_id, **kwargs)

    # 纠正与撤回
    def submit_correction(self, round_key: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.workflow_engine().submit_correction(round_key, **kwargs)

    def confirm_anomaly_segments(
        self, batch_key: str, segments: list[dict[str, Any]],
        *, round_key: str | None = None, idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.workflow_engine().submit_correction(
            round_key, batch_key=batch_key, segments=segments, idempotency_key=idempotency_key,
        )

    def get_correction(self, correction_id: str) -> dict[str, Any] | None:
        return self.workflow_engine().get_correction(correction_id)

    def list_corrections(self, round_key: str | None = None) -> list[dict[str, Any]]:
        return self.workflow_engine().list_corrections(round_key)

    def sign_correction_version(self, correction_id: str, version: int, **kwargs: Any) -> dict[str, Any]:
        return self.workflow_engine().sign_correction_version(correction_id, version, **kwargs)

    def dispatch_correction(self, correction_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.workflow_engine().dispatch_correction(correction_id, **kwargs)

    # 复核
    def sign_review(self, batch_key: str, point_id: Any, verdict: str, **kwargs: Any) -> dict[str, Any]:
        return self.workflow_engine().sign_review(batch_key, point_id, verdict, **kwargs)

    def approve_review(self, review_id: str) -> dict[str, Any]:
        return self.workflow_engine().approve_review(review_id)

    def list_reviews(self, batch_key: str | None = None) -> list[dict[str, Any]]:
        return self.workflow_engine().list_reviews(batch_key)

    # 出处
    def get_hit_provenance(self, batch_key: str, point_id: Any | None = None) -> dict[str, Any]:
        return self.workflow_engine().get_hit_provenance(batch_key, point_id)

    # 兼容入口：同一能力的不同叫法，方便调用方按业务词选择。
    start_recompute = recompute_batches
    resume_round = resume_recompute
    submit_confirmed_segments = submit_correction
    send_correction = dispatch_correction
    query_hit_provenance = get_hit_provenance
