# 文档解析与知识抽取平台

面向多格式文档批量接入、版式解析、分块切分、实体关系抽取、向量化嵌入与抽取质量校验入库的非结构化知识抽取平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 文件接入 | `file_intake` | 接入批次 | 批次编号、来源系统、文件清单 |
| 格式解析 | `format_parse` | 解析任务 | 任务编号、文档编号、解析器类型 |
| 版式还原 | `layout_restore` | 版式结构 | 文档编号、段落结构、标题层级 |
| 表格抽取 | `table_extract` | 表格区域 | 表格编号、文档编号、页码位置 |
| OCR识别 | `ocr_engine` | 识别任务 | 任务编号、图片区域、识别语言 |
| 分块切分 | `chunk_split` | 文本分块 | 分块编号、文档编号、分块长度 |
| 实体识别 | `entity_recognize` | 命名实体 | 实体编号、分块编号、实体类型 |
| 关系抽取 | `relation_extract` | 实体关系 | 关系编号、头实体、尾实体 |
| 术语归一 | `term_normalize` | 术语映射 | 映射编号、源术语、标准术语 |
| 敏感词过滤 | `sensitive_filter` | 过滤规则 | 规则编号、匹配模式、命中词条 |
| 向量嵌入 | `vector_embed` | 向量化任务 | 任务编号、分块编号、嵌入模型 |
| 知识图谱 | `kg_build` | 图谱三元组 | 三元组编号、头实体、谓词关系 |
| 图谱溯证 | `kg_provenance` | 关系证据 | 证据编号、文档编号、来源分块、抽取版本 |
| 抽取质量 | `extract_quality` | 质检报告 | 报告编号、批次编号、抽样比例 |
| 人工复核 | `annot_review` | 复核任务 | 复核编号、分块编号、复核对象 |
| 文档版本 | `doc_version` | 版本快照 | 快照编号、文档编号、版本号 |
| 抽取血缘 | `extract_lineage` | 血缘链路 | 链路编号、源文件、解析节点 |
| 结果交付 | `result_deliver` | 交付任务 | 交付编号、快照编号、交付格式 |
| 失败重采 | `parse_retry` | 重采任务 | 重采编号、失败环节、原始批次 |

## 图谱溯证检索

知识图谱按实体检索时，同一个实体文本可能来自不同文档、不同图谱版本或不同抽取批次，
而图谱边本身不记录这些区别。`kg_provenance` 把「图谱边 / 关系来源 / 分块 / 抽取版本」
汇总成一条条独立证据，检索结果稳定、可回查、可重算：

- **同名实体不合并**：每条证据都带 `doc_id`，不同文档的同名实体分别是独立证据。
- **检索范围可选**：`published` 只查已入图的边（当前边的来历）；`pending` 只查
  尚未进入已发布图的新关系来源（需要补抽/重算的证据）；`all` 两者都查。
- **一次查询锁定来源集合**：检索返回 `snapshot_id` 与 `source_ids`，翻页时带上
  `snapshot_id` 即可。排序、分页、甚至并发新来源到达并重建索引，都不会把这次查询
  换成另一组证据；快照条件不一致会返回 409。
- **边/分块/抽取版本同时回查**：证据条目的 `provenance` 同时给出 `edge`（三元组与
  图谱版本）、`chunk`（来源分块）、`extraction`（关系编号、模型版本与全部历史版本）。
- **重算只动选中的来源**：把检索到的证据明细交给关系抽取，重算结果写在
  `relations/{集合}_relations_recompute_{批次}.jsonl`，原始产物原样保留为历史版本；
  未选中的文档与同一文档里未选中的实体对都不会被写入。

接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/kg/provenance/search` | 实体检索，支持 scope/doc_id/relation/match/page/size/sort/snapshot_id |
| GET | `/api/kg/provenance/snapshots/{id}` | 回查某次检索固定的来源集合 |
| GET | `/api/kg/provenance/evidence/{id}` | 单条证据的边、分块、抽取版本明细 |
| GET | `/api/kg/provenance/stats` | 索引统计（重建后可用） |
| POST | `/api/kg/provenance/recompute` | 传入 `snapshot_id + evidence_ids`（或证据明细）重算选定来源 |
| GET | `/api/kg/{三元组id}/provenance` | 从已登记三元组回到它的溯证证据 |

前端入口：`/kg_provenance`（左侧导航「图谱溯证检索」）。服务启动时会写入一组演示产物
（`DOC-DEMO-A/B` 已入图、`DOC-DEMO-C` 未入图），可直接检索 `某科技集团` 体验。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
