# 溢油应急响应与任务追踪

围控、回收、岸线保护和废弃物处置任务，按证据和监测结果闭环。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8320
```

默认端口为`8320`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/escalation-confirmation`，指挥官升级确认（report/assessing阶段）
- `POST /api/items/{id}/estimate`，更正估算油量，旧升级确认自动作废
- `GET /api/audit`

允许角色：observer, response_commander, operations, viewer。估算油量、海况和未完成任务数影响响应等级；关闭前必须完成回收和岸线监测记录。

### 升级确认

估算油量越过升级线（`quantity>=threshold`或severity为`catastrophic`）后，事件从`assessing`进入`containing`前，必须由`response_commander`提交升级确认，登记当时的估算值、判据（`quantity_at_threshold`/`severity_catastrophic`）和事件版本。缺少有效确认时过渡返回409，响应体`details.step`指明卡在`escalation_confirmation`，`reason`说明缺失或失效原因（`missing_confirmation`/`quantity_corrected`）。

在`reported`/`assessing`阶段更正估算油量会使事件版本+1，并将既有有效确认置为`invalidated`（原因`quantity_corrected`），需要重新确认。事件详情和列表均带出`escalation_confirmation`（确认状态、登记值、判据、版本、失效原因与时间）。确认（`escalation_confirmed`）与更正（`estimate_corrected`）均写入审计链。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
