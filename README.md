# 天文瞬变事件警报与后续观测

只使用Python标准库和SQLite的模块化服务，默认端口`8338`。支持全天巡天来源、候选事件去重、坐标与亮度测量合并、优先级计算、观测申请、望远镜排程、按优先级竞争时间窗（抢占）、撤回、重分类、修正、观测队冲突、角色权限和审计。

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

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/queue`（可选`?telescope_id=`）：按望远镜分组返回观测排队结果、优先级和被顶回待排的原因
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 时间窗竞争规则

巡天夜里多个瞬变事件争抢同一台望远镜时，对`observation`执行`schedule`按以下规则竞争：

1. 观测申请的`priority`在提交时从候选事件的`priority_score`快照，申请人无需手填。
2. 与同一望远镜上已排程计划时间窗重叠时：
   - 新计划优先级更高：顶掉所有重叠的、**尚未开始**的低优先级计划。被顶计划状态由`scheduled`回到`requested`，并在`data.preempt_reason`写明原因（谁顶的、优先级对比、让出的时间窗），审计记为`preempt`。
   - 优先级相同：保留先提交的（以数据库写入顺序为准），后来者排程失败。
   - 优先级更低：排程失败，返回409冲突。
   - 对方计划的开始时间已到（已经开始观测）：无论优先级高低都不能顶，排程失败。
3. 同一观测队伍不能同时在两台望远镜上执行重叠计划，队伍冲突始终为硬约束（不会被顶掉）。
4. 抢占在单个数据库事务中完成，冲突或失败时全部回滚。
5. 计划`complete`或`withdraw`后状态离开`scheduled`，原时间窗立即释放，可重新排程；被顶申请重新排程成功后会清除旧的顶回原因。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

轨道和观测计划使用简化的字符串时间窗比较，不包含真实天文历表、可见性预报、望远镜控制系统和观测数据存储。
