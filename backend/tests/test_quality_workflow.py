"""轨迹质量围栏重算工作流测试：多批重算、巡检单纠偏、异常段批量提交与命中出处。"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def setup_data_dir(monkeypatch):
    """每个用例独立临时数据目录，并清空内存仓库，避免跨用例串状态。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="geo_quality_test_"))
    import app.services.base as base_mod
    original_root = base_mod.DATA_ROOT
    base_mod.DATA_ROOT = tmpdir
    for sub in [
        "geo_points", "dedup_merged", "quality_reports", "checkpoints",
        "quality_workflow", "geofence_hits",
    ]:
        (tmpdir / sub).mkdir(parents=True, exist_ok=True)

    from app.store import store
    original_tables = {
        name: [dict(row) for row in rows] for name, rows in store._tables.items()
    }
    yield tmpdir
    base_mod.DATA_ROOT = original_root
    store._tables = original_tables
    shutil.rmtree(tmpdir, ignore_errors=True)


INSIDE = [(39.900, 116.400), (39.920, 116.400), (39.920, 116.420), (39.900, 116.420)]


def _add_batch(batch_key: str, *, date: str = "2026-09-01") -> None:
    from app.store import store
    rows = store.rows("geo_source")
    rows.append({
        "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        "status": "已接入", "pending": False, "abnormal": False,
        "批次编号": batch_key, "采集终端": "TERM-1", "定位方式": "GPS",
        "采样频率": "1Hz", "记录条数": "5", "时间跨度": f"{date} ~ {date}",
        "采集水位": "已采集5条", "批次校验值": "x",
    })


def _write_points(service, batch_key: str, points: list[dict]) -> None:
    service._write_jsonl(service._data_dir("geo_points") / f"{batch_key}.jsonl", points)


def _sample_points(prefix: int = 1) -> list[dict]:
    """五个点：三个在围栏内、一个标坏坐标、一个标坏坐标之外。"""
    return [
        {"point_id": 1, "lat": 39.910, "lon": 116.410, "timestamp": 100, "terminal": "T"},
        {"point_id": 2, "lat": 39.911, "lon": 116.411, "timestamp": 200, "terminal": "T"},
        {"point_id": 3, "lat": 999.0, "lon": 999.0, "timestamp": 300, "terminal": "T", "coord_status": "bad"},
        {"point_id": 4, "lat": 39.912, "lon": 116.412, "timestamp": 400, "terminal": "T", "coord_status": "bad"},
        {"point_id": 5, "lat": 39.913, "lon": 116.413, "timestamp": 500, "terminal": "T"},
    ]


def _service():
    from app.services.geo_quality import GeoQualityService
    return GeoQualityService()


def _seed_three_batches(service) -> None:
    for key, date in (("B-001", "2026-09-01"), ("B-002", "2026-09-02"), ("B-003", "2026-09-03")):
        _add_batch(key, date=date)
        _write_points(service, key, _sample_points())


class TestMultiBatchRecompute:
    def test_multi_batch_recompute_records_date_and_previous_ticket(self, setup_data_dir):
        service = _service()
        _seed_three_batches(service)
        old_ticket = service.register_inspection_ticket("B-001", recipient="巡检一组")["ticket"]

        result = service.recompute_batches(["B-001", "B-002", "B-003"])

        assert result["round_key"].startswith("QR-")
        assert [item["status"] for item in result["batches"]] == ["围栏已算"] * 3
        first = result["batches"][0]
        assert first["sampling_date"] == "2026-09-01"
        assert first["previous_ticket_id"] == old_ticket["ticket_id"]
        assert first["hits_count"] == 3
        # 待补点范围合并成闭区间，且带原采样日期
        assert first["pending_backfill_ranges"] == [{
            "start_index": 2, "end_index": 3,
            "start_point_id": 3, "end_point_id": 4, "reason": "待补点",
        }]
        assert first["failed_ranges"] == []

    def test_bad_batch_does_not_block_other_batches(self, setup_data_dir):
        service = _service()
        _add_batch("B-001")
        _add_batch("B-002")
        _write_points(service, "B-001", _sample_points())
        # B-002 没有定位点文件 → 缺定位证据

        result = service.recompute_batches(["B-001", "B-002"])

        statuses = {item["batch_key"]: item["status"] for item in result["batches"]}
        assert statuses == {"B-001": "围栏已算", "B-002": "缺定位证据"}
        assert result["missing_evidence_batches"] == ["B-002"]
        assert result["batches"][0]["hits_count"] == 3

    def test_repeated_request_generates_hits_once(self, setup_data_dir):
        service = _service()
        _add_batch("B-001")
        _write_points(service, "B-001", _sample_points())

        first = service.recompute_batches(["B-001"], request_id="REQ-1")
        second = service.recompute_batches(["B-001"], request_id="REQ-1")

        assert second["duplicate"] is True
        assert second["round_key"] == first["round_key"]
        assert second["batches"][0]["hits_count"] == first["batches"][0]["hits_count"]

    def test_missing_evidence_batch_cannot_be_corrected(self, setup_data_dir):
        service = _service()
        _add_batch("B-001")
        _write_points(service, "B-001", _sample_points())
        result = service.recompute_batches(["B-001", "B-404"])

        correction = service.submit_correction(
            result["round_key"], batch_key="B-404", segments=[],
        )
        assert correction["ok"] is False
        assert service.list_corrections(result["round_key"]) == []

    def test_interrupted_round_resume_skips_confirmed_batches(self, setup_data_dir):
        service = _service()
        _seed_three_batches(service)
        result = service.recompute_batches(["B-001", "B-002", "B-003"])
        round_key = result["round_key"]

        # 第一批先按异常段确认处置
        correction = service.submit_correction(
            round_key, batch_key="B-001",
            segments=[{"start_point_id": 1, "end_point_id": 2}],
        )
        dispatched = service.dispatch_correction(correction["correction_id"], mode="withdraw")
        assert dispatched["per_batch"][0]["withdrawn_hits"]

        interrupted = service.interrupt_recompute(round_key, "B-002", reason="网络中断")
        assert interrupted["ok"] is True
        assert interrupted["batch"]["status"] == "重算中断"

        resumed = service.resume_recompute(round_key)

        skipped = {item["batch_key"]: item["reason"] for item in resumed["skipped"]}
        assert "B-001" in skipped
        assert "处置已确认" in skipped["B-001"]
        assert [item["batch_key"] for item in resumed["resumed"]] == ["B-002"]
        # 续做没有把已确认批次再算一遍
        after = service.get_recompute_round(round_key)
        by_key = {item["batch_key"]: item for item in after["batches"]}
        assert by_key["B-001"]["confirmed_correction_id"] == correction["correction_id"]
        assert by_key["B-002"]["status"] == "围栏已算"
        assert service.withdraw_ticket(
            by_key["B-001"]["previous_ticket_id"]
        )["duplicate"] is True

    def test_interrupted_batch_keeps_approved_misjudgement(self, setup_data_dir):
        service = _service()
        _add_batch("B-001")
        _write_points(service, "B-001", _sample_points())
        result = service.recompute_batches(["B-001"])
        round_key = result["round_key"]

        review = service.sign_review("B-001", 1, "误判", approved=True)
        assert review["ok"] is True
        service.interrupt_recompute(round_key, "B-001")
        resumed = service.resume_recompute(round_key)
        assert [item["batch_key"] for item in resumed["resumed"]] == ["B-001"]

        # 续做后误判复核仍要能撤回对应命中
        correction = service.submit_correction(round_key, batch_key="B-001", segments=[])
        assert correction["items"][0]["hits_to_withdraw"]


class TestCorrectionWorkflow:
    def test_segments_list_withdrawable_hits_disjoint_from_backfill(self, setup_data_dir):
        service = _service()
        _add_batch("B-001")
        _write_points(service, "B-001", _sample_points())
        round_result = service.recompute_batches(["B-001"])

        correction = service.submit_correction(
            round_result["round_key"], batch_key="B-001",
            segments=[{"start_point_id": 1, "end_point_id": 2}],
        )
        item = correction["items"][0]
        assert len(item["hits_to_withdraw"]) == 2
        # 仍可用的点不含标坏坐标
        assert item["usable_point_ids"] == ["5"]
        assert correction["status"] == "待发出"

    def test_duplicate_segment_submission_counts_once(self, setup_data_dir):
        service = _service()
        _add_batch("B-001")
        _write_points(service, "B-001", _sample_points())
        round_result = service.recompute_batches(["B-001"])
        payload = dict(batch_key="B-001", segments=[{"start_point_id": 1, "end_point_id": 2}])

        first = service.submit_correction(round_result["round_key"], **payload)
        second = service.submit_correction(round_result["round_key"], **payload)

        assert second["duplicate"] is True
        assert second["correction_id"] == first["correction_id"]
        assert len(service.list_corrections(round_result["round_key"])) == 1

    def test_one_failed_batch_keeps_other_batch_submission(self, setup_data_dir):
        service = _service()
        _add_batch("B-001")
        _add_batch("B-002")
        _write_points(service, "B-001", _sample_points())
        _write_points(service, "B-002", _sample_points())
        round_result = service.recompute_batches(["B-001", "B-002"])

        correction = service.submit_correction(
            round_result["round_key"],
            items=[
                {"batch_key": "B-001", "segments": [{"start_point_id": 1, "end_point_id": 1}]},
                {"batch_key": "B-999", "segments": []},
            ],
        )

        assert correction["ok"] is True
        assert [item["batch_key"] for item in correction["failed_batches"]] == ["B-999"]
        assert correction["items"][0]["ok"] is True
        assert correction["items"][0]["hits_to_withdraw"]


class TestOldTicketDisposition:
    def test_withdraw_ticket_sends_single_withdrawal(self, setup_data_dir):
        service = _service()
        _add_batch("B-001")
        _write_points(service, "B-001", _sample_points())
        service.recompute_batches(["B-001"], sampling_dates={"B-001": "2026-09-01"})

        first = service.withdraw_ticket("IT-0001", authorization_id="AUTH-1")
        second = service.withdraw_ticket("IT-0001", authorization_id="AUTH-1")

        assert first["withdrawals_sent"] == 1
        assert second["withdrawals_sent"] == 0
        assert second["duplicate"] is True
        ticket = service.get_inspection_ticket("IT-0001")
        assert len(ticket["withdrawals"]) == 1
        assert ticket["status"] == "已撤回"

    def test_settle_ticket_with_new_signed_version(self, setup_data_dir):
        service = _service()
        _add_batch("B-001")
        _write_points(service, "B-001", _sample_points())
        round_result = service.recompute_batches(["B-001"])
        correction = service.submit_correction(
            round_result["round_key"], batch_key="B-001",
            segments=[{"start_point_id": 1, "end_point_id": 1}],
        )
        ticket_id = correction["items"][0]["previous_ticket_id"]

        too_early = service.settle_ticket(ticket_id, correction_id=correction["correction_id"])
        assert too_early["ok"] is False

        service.sign_correction_version(correction["correction_id"], 2, note="补签新版")
        settled = service.settle_ticket(
            ticket_id, correction_id=correction["correction_id"], version=2,
        )
        assert settled["ok"] is True
        assert settled["ticket"]["settled_version"] == 2

    def test_dispatch_withdraw_and_settle_recheck_provenance(self, setup_data_dir):
        """同一轮两批：一批撤回旧单、一批补签新版结清，均能回查现行命中出处。"""
        service = _service()
        _add_batch("B-001")
        _add_batch("B-002")
        _write_points(service, "B-001", _sample_points())
        _write_points(service, "B-002", _sample_points())
        round_result = service.recompute_batches(["B-001", "B-002"])

        correction = service.submit_correction(
            round_result["round_key"],
            items=[
                {"batch_key": "B-001", "segments": [{"start_point_id": 1, "end_point_id": 1}]},
                {"batch_key": "B-002", "segments": [{"start_point_id": 2, "end_point_id": 2}]},
            ],
        )
        service.sign_correction_version(correction["correction_id"], 2)
        dispatched = service.dispatch_correction(correction["correction_id"], mode="settle", version=2)

        assert dispatched["withdrawals_sent"] == 0
        assert len(dispatched["settled_tickets"]) == 2
        for batch_key in ("B-001", "B-002"):
            provenance = service.get_hit_provenance(batch_key)
            assert provenance["current_hit_eligible"] is True
            assert provenance["valid_source"]["event"] == "generated"
            assert provenance["current_hit"]["status"] == "现行"

    def test_dispatch_is_idempotent_for_duplicate_confirmation(self, setup_data_dir):
        service = _service()
        _add_batch("B-001")
        _write_points(service, "B-001", _sample_points())
        round_result = service.recompute_batches(["B-001"])
        correction = service.submit_correction(
            round_result["round_key"], batch_key="B-001",
            segments=[{"start_point_id": 1, "end_point_id": 2}],
        )
        service.dispatch_correction(correction["correction_id"], mode="withdraw")
        again = service.dispatch_correction(correction["correction_id"], mode="withdraw")

        assert again["withdrawals_sent"] == 0
        assert again["duplicate"] is True
        ticket = service.get_inspection_ticket("IT-0001")
        assert len(ticket["withdrawals"]) == 1


class TestSignedReviewRestore:
    def test_late_point_restores_current_hit_eligibility(self, setup_data_dir):
        service = _service()
        _add_batch("B-001")
        _write_points(service, "B-001", _sample_points())
        round_result = service.recompute_batches(["B-001"])
        point_id = 1
        service.sign_review("B-001", point_id, "误判", approved=True)
        correction = service.submit_correction(
            round_result["round_key"], batch_key="B-001", segments=[],
        )
        assert set(correction["items"][0]["hits_to_withdraw"])

        rejected = service.sign_review("B-001", point_id, "恢复")
        assert rejected["ok"] is False

        restored = service.sign_review("B-001", point_id, "恢复", late_point={
            "lat": 39.9105, "lon": 116.4105, "timestamp": 1500,
        })
        assert restored["ok"] is True
        provenance = service.get_hit_provenance("B-001", point_id)
        assert provenance["current_hit_eligible"] is True
        assert provenance["provenance"][-1]["event"] == "restored"

        # 恢复之后才发出的纠正单不再撤回这个点
        dispatched = service.dispatch_correction(correction["correction_id"], mode="withdraw")
        assert point_id in [
            state_hit["point_id"] for state_hit in provenance["hits"]
            if state_hit["status"] == "现行"
        ]
        assert dispatched["per_batch"][0]["hits_kept_by_review"]

    def test_other_batches_remain_readable_after_confirmation(self, setup_data_dir):
        service = _service()
        _seed_three_batches(service)
        round_result = service.recompute_batches(["B-001", "B-002", "B-003"])
        correction = service.submit_correction(
            round_result["round_key"], batch_key="B-001",
            segments=[{"start_point_id": 1, "end_point_id": 1}],
        )
        service.dispatch_correction(correction["correction_id"], mode="withdraw")

        latest = service.get_recompute_round(round_result["round_key"])
        by_key = {item["batch_key"]: item for item in latest["batches"]}
        assert by_key["B-001"]["disposition_confirmed"] is True
        assert by_key["B-002"]["hits_count"] == 3
        assert by_key["B-003"]["status"] == "围栏已算"
        assert by_key["B-002"]["disposition_confirmed"] is False

    def test_round_checkpoint_written(self, setup_data_dir):
        service = _service()
        _seed_three_batches(service)
        round_result = service.recompute_batches(["B-001", "B-002", "B-003"])
        checkpoint = setup_data_dir / "checkpoints" / f"quality_recompute_{round_result['round_key']}.json"
        assert checkpoint.exists()
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        assert state["batch_progress"]["B-002"] == "围栏已算"


class TestWorkflowHttpRoutes:
    """工作流固定路径不能被通用 ``/{module}/{entry_id}`` 抢先匹配。"""

    @staticmethod
    def _asgi_get(path: str):
        import asyncio

        from app.main import app

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": b"", "root_path": "", "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 1234), "server": ("testserver", 80),
        }
        messages: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        asyncio.run(app(scope, receive, send))
        status = next(item["status"] for item in messages if item["type"] == "http.response.start")
        body = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
        return status, json.loads(body or b"{}")

    @pytest.mark.parametrize("path", [
        "/api/geo_quality/recompute",
        "/api/geo_quality/tickets",
        "/api/geo_quality/corrections",
        "/api/geo_quality/reviews",
    ])
    def test_workflow_list_routes_win_over_generic_entry_route(self, setup_data_dir, path):
        status, payload = self._asgi_get(path)
        assert status == 200
        assert "items" in payload

    def test_generic_module_routes_still_work(self, setup_data_dir):
        status, payload = self._asgi_get("/api/geo_quality")
        assert status == 200
        assert "items" in payload and "total" in payload
        status, payload = self._asgi_get("/api/geo_quality/3")
        assert status == 200
        assert payload["id"] == 3

    def test_unknown_round_returns_404(self, setup_data_dir):
        status, _ = self._asgi_get("/api/geo_quality/recompute/QR-404")
        assert status == 404


class TestMixedRoundStates:
    def test_same_round_mixes_dispatched_computed_and_missing(self, setup_data_dir):
        service = _service()
        _add_batch("B-001")
        _add_batch("B-002")
        _write_points(service, "B-001", _sample_points())
        _write_points(service, "B-002", _sample_points())

        result = service.recompute_batches(["B-001", "B-002"])
        round_key = result["round_key"]
        dispatched = service.dispatch_inspection(round_key, "B-001", recipient="巡检二组")
        assert dispatched["ok"] is True
        again = service.dispatch_inspection(round_key, "B-001", request_id="DSP-1")
        assert again["duplicate"] is True
        third = service.recompute_batches(["B-004"])
        assert third["batches"][0]["status"] == "缺定位证据"

        latest = service.get_recompute_round(round_key)
        by_key = {item["batch_key"]: item for item in latest["batches"]}
        assert by_key["B-001"]["status"] == "已下派巡检"
        assert by_key["B-002"]["status"] == "围栏已算"
        assert by_key["B-002"]["disposition_confirmed"] is False
        ticket = service.get_inspection_ticket(by_key["B-001"]["previous_ticket_id"])
        assert ticket["status"] == "已下派巡检"
        assert ticket["recipient"] == "巡检二组"

    def test_dispatched_batch_cannot_be_corrected(self, setup_data_dir):
        service = _service()
        _add_batch("B-001")
        _write_points(service, "B-001", _sample_points())
        result = service.recompute_batches(["B-001"])
        service.dispatch_inspection(result["round_key"], "B-001")

        correction = service.submit_correction(
            result["round_key"], batch_key="B-001",
            segments=[{"start_point_id": 1, "end_point_id": 1}],
        )
        assert correction["ok"] is False

    def test_dispatch_inspection_route(self, setup_data_dir):
        service = _service()
        _add_batch("B-001")
        _write_points(service, "B-001", _sample_points())
        service.recompute_batches(["B-001"])
        import asyncio

        from app.main import app

        body = json.dumps({"batch_key": "B-001", "recipient": "巡检三组"}).encode()
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http",
            "path": "/api/geo_quality/recompute/QR-0001/dispatch",
            "raw_path": b"/api/geo_quality/recompute/QR-0001/dispatch",
            "query_string": b"", "root_path": "",
            "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
            "client": ("127.0.0.1", 1234), "server": ("testserver", 80),
        }
        messages: list[dict] = []
        sent = {"done": False}

        async def receive():
            if not sent["done"]:
                sent["done"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        asyncio.run(app(scope, receive, send))
        status = next(item["status"] for item in messages if item["type"] == "http.response.start")
        payload = json.loads(b"".join(
            item.get("body", b"") for item in messages if item["type"] == "http.response.body"
        ))
        assert status == 200
        assert payload["ticket"]["recipient"] == "巡检三组"
