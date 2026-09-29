# 天文瞬变事件警报与后续观测

只使用Python标准库和SQLite的模块化服务，默认端口`8338`。支持全天巡天来源、候选事件去重、坐标与亮度测量合并、优先级计算、观测申请、望远镜按优先级竞争排程、顶掉待排、撤回、重分类、修正、观测队冲突、角色权限和审计。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：状态机、优先级、测量合并和排程冲突约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等和审计查询。
- `src/service.py`：用例编排、权限校验和版本控制。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8338
```

## 核心对象

`source`为巡天来源，`candidate`为瞬变候选，`telescope`为望远镜，`observation`为后续观测申请。

## 望远镜时间窗竞争

观测申请创建时进入`requested`（待排），并快照候选事件当前的`priority_score`。协调员执行`schedule`时：

- 只与同一望远镜上状态为`scheduled`且时间窗重叠的计划竞争；时间窗使用半开区间，边界相接不算重叠。
- 新计划优先级更高时，可以顶掉尚未开始的低优先级计划。被顶掉的计划回到`requested`，并在`data.queue_reason`中写明原因，同时写入`preempt`审计。
- 优先级相同则保留提交时间更早的计划；提交顺序由数据库行序保证。
- 已经开始（开始时间不晚于当前UTC时间）的计划不能顶掉。
- 同一队伍在重叠时间内的计划仍然硬性冲突，即使使用不同望远镜也不能顶掉。
- `completed`或`withdrawn`计划不再占用时间窗，原时段可以重新排程。

时间字段必须使用带时区的ISO-8601格式（如`2026-09-29T10:00:00Z`）。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

观测计划使用UTC ISO-8601时间窗比较，但不包含真实天文历表、可见性预报、望远镜控制系统和观测数据存储。
