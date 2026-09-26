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
- `POST /api/items/{id}/escalation-confirmation`，指挥官确认越过升级线（自动登记当前估算、判据和事件版本）
- `PATCH /api/items/{id}/estimate`（也支持POST），提交`quantity`和`expected_version`更正估算；更正会使旧升级确认失效
- `POST /api/items/{id}/transition`，必须提交`expected_version`；从`assessing`进入`containing`时，越过升级线必须持有当前版本的有效确认
- `GET /api/audit`

允许角色：observer, response_commander, operations, viewer。估算油量、海况和未完成任务数影响响应等级；估算越过升级线（quantity >= threshold）或severity为catastrophic时，必须由response_commander确认后才能围控。更正估算会递增事件版本，并自动将旧确认标记为invalidated。详情和列表返回`escalation_confirmation_status`与`escalation_confirmation_invalidated_reason`；确认、估算更正和确认失效均进入审计。关闭前必须完成回收和岸线监测记录。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
