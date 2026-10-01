# 大型储能电池全生命周期协同平台

本项目是一套可离线运行的 Python 服务端平台，服务于大型储能电池从入库、状态评估、组件检测、场站调拨到退役处置的协同管理。平台将资产流转、评估协议、质量决定、幂等结果和审计事件保存在 SQLite 中，供运营、质量、维修和审计人员在单个 Linux 应用容器内使用。

## 目录

- `src/battery_logistics/`：储能场站、调拨走廊、资产批次、容量申请、分配与处置情景；
- `src/battery_assurance/`：电池资产、证据版本、评估协议、观测导入、排除复核、分析任务与准入决定；
- `src/component_quality/`：电芯组件批次、响应测量、统计分析、账号权限和质量审批；
- `src/maintenance_planning/`：确定版本健康证据与风险规则、待办优先级、约束维修窗口、批准冻结、证据/告警/召回失效、现场回执与延期风险；
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
PYTHONPATH=src python3 -m component_quality.acceptance
PYTHONPATH=src python3 -m maintenance_planning.acceptance --workspace .
```

四条命令会在临时 SQLite 数据库中完成资产调拨、状态评估、组件质量和维修计划全流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m battery_logistics.api --database battery-logistics.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m battery_assurance.api --database battery-assurance.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m component_quality.api --database component-quality.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m maintenance_planning.api --database maintenance-planning.sqlite3 --host 127.0.0.1 --port 8083
```

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 维修计划能力

维修计划服务把容量衰减、近期告警与召回限制按**确定版本的风险规则**合成为待办优先级，
并在场站每日可停机额度、班组资质人数、备件库存与电气隔离约束内生成维修窗口：

- **证据与规则版本化**：健康证据（额定/可用容量、循环数、告警、召回级别）以内容摘要不可变存储，
  风险规则版本带权重和阈值；评分、排序与排程均为纯确定性函数，可随时复算。
- **批准即冻结**：批准草稿时冻结证据快照与规则版本，并写入场站额度、班组、备件的资源占用
  （`resource_holds`）；证据已变化或资源被并发计划占满时拒绝批准，必须重新生成。
- **精准失效**：证据新版本、critical 紧急告警或召回升级只使**受影响电池仍未执行**的窗口失效并
  释放占用；已开工、已暂停、已完工窗口不受波及。
- **现场回执幂等防复活**：开始/暂停/复工/完工/复测回执以 `(窗口, 类型, 客户端回执键)` 去重，
  可重复、可乱序投递；完工后允许一次复测，复测后为终态，迟到消息一律拒绝，不会恢复已关闭工单。
- **延期留痕**：计划员只能申请延期并暴露容量风险，风险负责人接受后窗口与冻结占用才移动到新日期，
  记录风险接受人和新的最迟执行日期。
- **运营可解释**：每个窗口保留入选理由（风险分/优先级、额度余量、班组资质、备件扣减、隔离），
  排程冲突记录裁决（高风险优先取更早日期），`/operations/dashboard` 汇总待执行动作、
  延期暴露容量、未排期高风险设备与冲突裁决。
