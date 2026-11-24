"""轨迹质量领域服务：对采集批次定位点执行漂移检测、重复率和缺失率校验，生成质量报告数据集。"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.quality_workflow import QualityWorkflowMixin
from app.store import store


class GeoQualityService(QualityWorkflowMixin, BaseModuleService):
    MODULE = "geo_quality"
    ENTITY = "质量报告"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["报告编号", "批次编号", "漂移点数", "重复点数", "缺失率", "异常时段", "校验规则", "质检结论"]
    REQUIRED_FIELDS = ["报告编号", "批次编号", "漂移点数"]
    STATUS_ORDER = ["草稿", "待质检", "质检中", "已出报告"]
    ACTION_RULES = {"启动质检": "待质检", "出具报告": "已出报告", "标记驳回": "已出报告"}
    NEGATIVE_ACTIONS = []

    def check_quality(self, batch_key: str, max_speed_mps: float = 50.0,
                       max_gap_sec: float = 300.0) -> dict[str, Any]:
        """读取采集批次定位点数据文件，执行漂移检测、重复率统计和缺失率判定，生成质量校验报告数据集。"""
        points_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        if not points_file.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的定位点数据文件不存在")

        points = self._read_jsonl(points_file)
        if not points:
            return {"batch_key": batch_key, "total_points": 0}

        total = len(points)
        # 时间排序
        points.sort(key=lambda p: p.get("timestamp", 0))

        drift_count = 0
        duplicate_count = 0
        gap_count = 0
        anomaly_periods = []

        for i in range(1, total):
            prev = points[i - 1]
            curr = points[i]
            time_diff = abs(curr.get("timestamp", 0) - prev.get("timestamp", 0))
            dist = self._haversine(
                prev.get("lat", 0), prev.get("lon", 0),
                curr.get("lat", 0), curr.get("lon", 0)
            )

            # 漂移检测：速度超过阈值
            if time_diff > 0:
                speed = dist / time_diff
                if speed > max_speed_mps:
                    drift_count += 1
                    anomaly_periods.append({
                        "point_id": curr.get("point_id"),
                        "speed_mps": round(speed, 2),
                        "time": time.strftime("%H:%M:%S", time.localtime(curr["timestamp"])),
                        "type": "drift",
                    })

            # 重复点检测：同一终端时间和距离相近
            if (prev.get("terminal") == curr.get("terminal") and
                time_diff < 5 and dist < 1.0):
                duplicate_count += 1

            # 缺失检测：时间间隔过大
            if time_diff > max_gap_sec:
                gap_count += 1
                anomaly_periods.append({
                    "time": time.strftime("%H:%M:%S", time.localtime(prev["timestamp"])),
                    "gap_sec": round(time_diff, 1),
                    "type": "gap",
                })

        missing_rate = round(gap_count / max(total, 1), 4)
        drift_rate = round(drift_count / max(total, 1), 4)
        dup_rate = round(duplicate_count / max(total, 1), 4)

        # 质检结论
        if drift_rate < 0.05 and missing_rate < 0.1:
            conclusion = "合格"
        elif drift_rate < 0.2 and missing_rate < 0.3:
            conclusion = "待复核"
        else:
            conclusion = "不合格"

        report = {
            "batch_key": batch_key,
            "total_points": total,
            "drift_points": drift_count,
            "duplicate_points": duplicate_count,
            "gap_points": gap_count,
            "drift_rate": drift_rate,
            "duplicate_rate": dup_rate,
            "missing_rate": missing_rate,
            "max_speed_mps": max_speed_mps,
            "max_gap_sec": max_gap_sec,
            "anomaly_periods": anomaly_periods[:10],  # 只保留前10个异常
            "conclusion": conclusion,
            "checked_at": time.time(),
        }

        output_file = self._data_dir("quality_reports") / f"{batch_key}_quality.jsonl"
        written = self._write_jsonl(output_file, [report])

        # 创建质检报告记录
        entry, _ = self.create_entry({
            "报告编号": f"QUAL-{batch_key}",
            "批次编号": batch_key,
            "漂移点数": str(drift_count),
            "重复点数": str(duplicate_count),
            "缺失率": f"{missing_rate:.1%}",
            "异常时段": f"间隙{gap_count}段/漂移{drift_count}点",
            "校验规则": f"最大速度{max_speed_mps}m/s,最大间隙{max_gap_sec}s",
            "质检结论": conclusion,
        })
        if entry:
            self.run_action(entry["id"], "出具报告")

        return {
            "batch_key": batch_key,
            "total_points": total,
            "drift_count": drift_count,
            "duplicate_count": duplicate_count,
            "gap_count": gap_count,
            "missing_rate": f"{missing_rate:.1%}",
            "conclusion": conclusion,
            "output_file": str(output_file),
        }

    def get_quality_report(self, batch_key: str) -> dict[str, Any]:
        """读取批次的轨迹质量报告数据集。"""
        file_path = self._data_dir("quality_reports") / f"{batch_key}_quality.jsonl"
        reports = self._read_jsonl(file_path)
        return reports[0] if reports else {}