"""业务路由汇总：轨迹质量工作流路由要先于通用资源路由注册。

通用资源路由含 ``GET /api/{module}/{entry_id}``，若不先注册工作流的
``GET /api/geo_quality/recompute`` 等固定路径，会被当成 entry_id 抢先匹配。
"""
from __future__ import annotations

from app.routers import quality, resource

ROUTERS = [quality, resource]
