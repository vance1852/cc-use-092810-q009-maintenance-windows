# 大型储能电池全生命周期协同平台

本项目是一套可离线运行的 Python 服务端平台，服务于大型储能电池从入库、状态评估、组件检测、场站调拨到退役处置的协同管理。平台将资产流转、评估协议、质量决定、幂等结果和审计事件保存在 SQLite 中，供运营、质量、维修和审计人员在单个 Linux 应用容器内使用。

## 目录

- `src/battery_logistics/`：储能场站、调拨走廊、资产批次、容量申请、分配与处置情景；
- `src/battery_assurance/`：电池资产、证据版本、评估协议、观测导入、排除复核、分析任务与准入决定；
- `src/battery_maintenance/`：检修计划——按确定版本的健康证据与风险规则生成待办优先级，
  在场站停机额度、人员资质、备件与隔离位约束内排程；批准冻结输入与资源占用，
  证据变化/紧急告警/召回升级只失效受影响的未执行窗口，现场回执幂等去重、终态不可恢复，
  延期记录风险接受人与暴露容量，运营视图给出排程理由、冲突裁决与待执行动作；
- `src/component_quality/`：电芯组件批次、响应测量、统计分析、账号权限和质量审批；
- `fixtures/`：离线验收使用的评估协议与结构化观测；
- `tests/`：领域规则、错误边界、事务、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m battery_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m battery_assurance.acceptance --workspace .
PYTHONPATH=src python3 -m battery_maintenance.acceptance --workspace .
PYTHONPATH=src python3 -m component_quality.acceptance
```

四条命令会在临时 SQLite 数据库中完成资产调拨、状态评估、检修计划和组件质量流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m battery_logistics.api --database battery-logistics.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m battery_assurance.api --database battery-assurance.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m battery_maintenance.api --database battery-maintenance.sqlite3 --host 127.0.0.1 --port 8083
PYTHONPATH=src python3 -m component_quality.api --database component-quality.sqlite3 --host 127.0.0.1 --port 8082
```

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 检修计划服务接口（端口 8083）

所有写接口需携带 `X-Actor-Id` 头。角色：`planner`（台账/证据/计划/信号/延期）、
`risk`（风险规则发布、计划批准、作为延期风险接受人）、`technician`（现场回执）、`auditor`（只读与审计）。

- `POST /stations`、`POST /devices`、`POST /technicians`、
  `POST /technicians/{id}/unavailability`、`POST /bays`、`POST /spare-parts`：资源台账；
- `POST /evidence`：登记确定版本的健康证据（容量保持率、循环次数、30 日告警、召回级别）；
- `POST /risk-rule-sets`：发布可版本化的风险评分规则；
- `POST /plans`：生成待办优先级与维修窗口（幂等键去重），结果含每个窗口的评分因素、
  逐日被拒原因（冲突裁决）、场站每日负载和无法排程设备清单；
- `POST /plans/{id}/approve`：批准时重算输入摘要并与生成时比对（证据/规则变化则拒绝），
  冻结证据快照并占用停机容量、人员、隔离位和备件；
- `POST /signals`：证据变化、紧急告警或召回升级，仅使受影响设备处于未执行状态的窗口失效并释放资源；
- `POST /windows/{id}/receipts`：现场开始/暂停/恢复/完工/复测回执，必须带 `idempotency_key`，
  重复投递回放同一结果，乱序消息被拒，已关闭工单不会被迟到消息恢复；
- `POST /windows/{id}/extend`：延期需提供新的最迟日期、原因与 `risk` 角色风险接受人，记录容量暴露；
- `GET /operations/view`：运营视图——每个窗口的安排理由、回执与延期历史、资源占用、
  冲突与待执行动作、逾期窗口、场站每日容量负载和延期暴露总量；
- `GET /plans/{id}`：计划冻结的证据版本、冲突裁决、每日负载与未排程设备；
- `GET /audit/chain`：校验带哈希链的审计事件。
