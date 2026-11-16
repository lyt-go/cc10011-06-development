# 实时指标计算与监控大盘平台

面向实时指标计算引擎、多维度下钻分析、告警阈值管理、指标异常检测、数据服务开放与可视化看板配置的数据分析与应用工程平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
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
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 实时指标 | `realtimemetric` | 指标定义 | 指标编号、指标名称、计算窗口秒 |
| 监控大盘 | `dashboard` | 大盘配置 | 大盘编号、大盘名称、包含指标组 |
| 多维下钻 | `drilldown` | 下钻路径 | 路径编号、起始维度、下钻层级序列 |
| 阈值管理 | `threshold` | 阈值规则 | 规则编号、关联指标、阈值下限 |
| 异常检测 | `anomalydetect` | 检测模型 | 模型编号、监控指标、检测算法 |
| 数据服务 | `dataservice` | 服务接口 | 服务编号、服务名称、接口路径 |
| 告警历史 | `alarmhistory` | 告警记录 | 记录编号、告警规则、触发时间 |
| 同比环比 | `comparison` | 对比配置 | 配置编号、目标指标、对比类型 |
| 聚合配置 | `aggregate` | 聚合规则 | 规则编号、源数据粒度、目标粒度 |
| 筛选预设 | `filterpreset` | 预设模板 | 预设编号、预设名称、维度条件组 |
| 用户洞察 | `userinsight` | 洞察报告 | 报告编号、分析主题、目标人群 |
| 事件分析 | `eventanalysis` | 分析模型 | 模型编号、事件名称、分组维度 |
| 分群画像 | `segmentprofile` | 画像定义 | 画像编号、分群名称、筛选规则 |
| 趋势预测 | `trendforecast` | 预测方案 | 方案编号、预测指标、算法模型 |
| 可视化模板 | `viztemplate` | 图表模板 | 模板编号、图表类型、数据绑定 |
| 订阅推送 | `subscription` | 订阅配置 | 订阅编号、订阅内容、接收人 |
| 维度映射 | `dimensionmap` | 映射规则 | 规则编号、原始维度名、标准维度名 |
| 数据质量分 | `qualityscore` | 评分规则 | 规则编号、数据集名称、完整性权重 |

## 多维下钻路径版本快照

多维下钻除路径配置外，还落一张「下钻路径版本快照」，用来固定每一层结果的口径边界：

- `POST /api/drilldown/{id}/snapshots`：校验上游并生成快照。上游（路径未启用、层级序列与起始维度
  不一致、路径深度不符、维度缺已生效标准映射、度量字段不存在）未完成时直接拒绝生成；多个提交同时
  到达时只接纳首个通过校验的处理顺序，重复提交合并到已受理快照，台账记录每个提交的顺序。
- `GET /api/drilldown/{id}/precheck`：生成前检查上游与逐层维度绑定。
- `GET /api/drilldown/snapshots`：快照列表，可按 `start_dimension` 过滤。
- `GET /api/drilldown/snapshots/{id}/layers`：分层聚合明细，支持 `start_dimension` 筛选、
  `level` / `locate` 定位层级、`sort_field` / `sort_order` 层内排序。
- `GET /api/drilldown/snapshots/{id}/check`：核对快照绑定版本与当前标准口径，识别口径漂移。
- `GET /api/drilldown/{id}/cache`：明细下钻缓存结果，口径漂移时仍按旧维度展示并给出提示。
- `GET /api/drilldown/ledger`：快照生成提交台账（受理顺序、拒绝原因）。

标准维度映射的落库点：

- `dimensionmap` 的登记 / 评审 / 生效 / 废弃 → `dimensionmap_standard`（当前口径）
  + `dimensionmap_version`（口径变化追加一版，含冲突权衡说明）。
- `realtimemetric`、`comparison`、`filterpreset`、`eventanalysis`、`segmentprofile`、`drilldown`
  的登记与状态流转 → 同步刷新标准结果的「覆盖模块 / 引用字段」，口径未变不追加版本。
- 同一源维度存在多条已生效映射时，按 映射优先级 > 最近更新时间 > 登记顺序 选一条，
  被替换规则、权衡代价写进版本记录；快照每一层只绑定一个标准维度版本。
- 查询入口：`GET /api/dimensionmap/standards`、`GET /api/dimensionmap/versions`、
  `POST /api/dimensionmap/sync`。

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用
