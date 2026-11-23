"""业务路由汇总：溯证专用路由在前，通用资源路由兜底，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import provenance, resource

# 溯证路由含 /api/kg/... 固定路径，必须先于 /api/{module}/{entry_id} 注册，
# 否则 "provenance" 会被当成 entry_id 解析。
ROUTERS = [provenance, resource]
