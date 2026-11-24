# 路灯分批换装节电量核算服务

FastAPI + SQLite 的路灯换装节电核算接口。按路段记录换灯日和逐日单灯耗电量，
用同期未换灯的对照路段扣掉季节性/用电量变化，再给出目标路段的净节电量。
所有报告是出具时刻的不可变快照；补正数据后受影响报告标为 `pending_recalc`，
原结果继续可查。

## 启动

Docker Compose：

```bash
docker compose up --build
```

- Swagger UI: http://127.0.0.1:8000/docs
- 健康检查: http://127.0.0.1:8000/healthz
- SQLite 文件在命名卷 `lights-data` 的 `/data/lights.db`

本地开发（不依赖 Docker）：

```bash
pip install -r requirements-dev.txt
LIGHTS_DB_PATH=./data/lights.db uvicorn app.main:app --reload
pytest -q
```

`scripts/demo.py` 对运行中的服务做一次完整试算 + 补正，可核对数字：

```bash
python scripts/demo.py --base-url http://127.0.0.1:8000
```

## 口径

- **比较窗口**：换灯日前 7 个日历日 + 换灯日后 7 个日历日，换灯日当天跳过。
  日 `d` 的换灯用前段为 `[d-7, d-1]`，后段为 `[d+1, d+7]`。
- **对照路段**：未填换灯日，或换灯日晚于后段末日（`d+7`），并且 14 天窗口内
  每天都有读数。没有换灯日的路段按“到后段末日仍未换灯”处理。
- **节电量**（单灯日均，kWh/灯·天）：
  - 目标减少量 = 目标前段日均 − 目标后段日均
  - 对照平均减少量 = 各对照路段减少量的算术平均
  - 净节电 = 目标减少量 − 对照平均减少量
- **暂缓出报告**：目标路段没填换灯日、窗口内有缺数、或没有任何合格对照路段。
  返回 `status=deferred` 和 `reasons`，不落报告。
- **报告内容**：比较日期（前后段起止 + 跳过日）、目标与全部对照的逐日读数及
  前后段均值、对照名单、净节电量、出具时间。
- **补正**：改换灯日（`PATCH`）、改/补日报（`PUT`）、删错误日报（`DELETE`）。
  每次补正只重新读取**被改路段**的读数，与历史报告快照逐日比对：
  - 目标：窗口变化或所用读数变化 → 待复算；
  - 对照：读数变化、不再合格（换灯日落入窗口/缺数）→ 待复算；
  - 原本不合格、补正后变合格的对照会被计入 → 待复算（对照名单变化）。
- **待复算**：报告 `status` 变 `pending_recalc`，记录 `stale_at`、
  `stale_reasons`、`stale_segment_ids`；原快照字段不变，详情接口照常返回。

## 并发正确性

出报告和所有补正都使用 `BEGIN IMMEDIATE` 显式写事务。SQLite 会把两者串行化：
补正要么在出报告的快照读取前提交（新报告直接采用新值），要么在报告插入后执行
（一定能看到并标记该报告），不存在“报告插入前读取、补正提交后漏标”的交错窗口。
测试 `test_correction_interleaved_with_report_issuance_is_not_missed` 用线程和
快照读钩子重现了该交错。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查 |
| POST | `/api/segments` | 建路段；`retrofit_date` 省略/null 表示未换灯 |
| GET | `/api/segments` | 路段列表 |
| GET | `/api/segments/{id}` | 路段详情 |
| PATCH | `/api/segments/{id}` | 补正换灯日（`null` 清空），返回影响报告 |
| GET | `/api/segments/{id}/readings` | 日报查询，可带 `start`/`end` |
| PUT | `/api/segments/{id}/readings` | 批量新增/覆盖日报 |
| DELETE | `/api/segments/{id}/readings/{date}` | 删除单日错误日报 |
| POST | `/api/reports?segment_id=` | 出报告（201）或暂缓（200 + `deferred` + reasons） |
| GET | `/api/reports` | 报告列表，可按目标路段/状态筛选 |
| GET | `/api/reports/{id}` | 报告详情（含快照、待复算原因） |

补正响应统一含：`changed`、`changed_readings`、`affected_reports`
（`report_id` / `previous_status` / `status`）、`impact`（复核报告数、新标记数、
已待复算数、未受影响数、本次重读路段——只有被补正的那个路段）。

## Swagger 试算 + 补正示例

`POST /api/segments`

```json
{"name": "人民路", "retrofit_date": "2026-03-10"}
```

`PUT /api/segments/1/readings`

```json
{"readings":[
  {"date":"2026-03-03","kwh_per_lamp":10.0},
  {"date":"2026-03-04","kwh_per_lamp":10.0},
  {"date":"2026-03-05","kwh_per_lamp":10.0},
  {"date":"2026-03-06","kwh_per_lamp":10.0},
  {"date":"2026-03-07","kwh_per_lamp":10.0},
  {"date":"2026-03-08","kwh_per_lamp":10.0},
  {"date":"2026-03-09","kwh_per_lamp":10.0},
  {"date":"2026-03-11","kwh_per_lamp":8.0},
  {"date":"2026-03-12","kwh_per_lamp":8.0},
  {"date":"2026-03-13","kwh_per_lamp":8.0},
  {"date":"2026-03-14","kwh_per_lamp":8.0},
  {"date":"2026-03-15","kwh_per_lamp":8.0},
  {"date":"2026-03-16","kwh_per_lamp":8.0},
  {"date":"2026-03-17","kwh_per_lamp":8.0}
]}
```

对照路段不填 `retrofit_date`，同样补齐 14 天读数，然后
`POST /api/reports?segment_id=1`。
补正对照读数：`PUT /api/segments/2/readings` 只传被改的那一天，响应
`affected_reports` 会列出被标为 `pending_recalc` 的报告；`GET /api/reports/{id}`
仍能看到原始净节电量与补正原因。

## 测试

```bash
pytest -q
```

覆盖：窗口边界与跳过当天、对照筛选（未换灯 / 后段末日后换灯 / 缺数排除）、
净节电算术、目标缺数与无对照暂缓、报告快照不可变、换灯日/目标读数/对照读数/
对照增减各类补正标记、只重读被改路段、重复补正不重复标记、重复日期 upsert、
以及出报告与补正并发的交错场景。
