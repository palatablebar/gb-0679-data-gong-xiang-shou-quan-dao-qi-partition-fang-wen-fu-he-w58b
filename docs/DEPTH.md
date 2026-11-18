# 深度登记 数据共享授权到期与分区访问复核

规模：中型。使用者：企业数据共享审计专员。核心规则：拒绝规则优先于允许，授权必须同时命中资源、分区和生效窗；显式拒绝不得被宽授权覆盖；终止事件的冻结结论与审计归档同源。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 访问采信 | 查看并运行访问采信 | `/api/accesses` | `app/modules/m1_accesses/rules.py` | `tests/test_m1.py` |
| M2 资源映射 | 查看并运行资源映射 | `/api/resources` | `app/modules/m2_resources/rules.py` | `tests/test_m2.py` |
| M3 授权定版 | 查看并运行授权定版 | `/api/grants` | `app/modules/m3_grants/rules.py` | `tests/test_m3.py` |
| M4 冲突裁决 | 查看并运行冲突裁决 | `/api/conflicts` | `app/modules/m4_conflicts/rules.py` | `tests/test_m4.py` |
| M5 分区判定 | 查看并运行分区判定 | `/api/partitions` | `app/modules/m5_partitions/rules.py` | `tests/test_m5.py` |
| M6 访问对账 | 查看并运行访问对账 | `/api/audit` | `app/modules/m6_audit/rules.py` | `tests/test_m6.py` |
| M7 审计归档 | 查看并运行审计归档 | `/api/seals` | `app/modules/m7_seals/rules.py` | `tests/test_m7.py` |
| M8 终止处置 | 核对终止事件处置单 | `/api/dispositions` | `app/modules/m8_dispositions/rules.py` | `tests/test_m8.py` |

## 终止事件处置

终止事件到达时冻结范围有取舍：

- **停用整个主体**：立刻控险，但会中断仍在授权窗内的作业。
- **只收失效分区**：误伤更少，但只有在“主体全部授权均已失效、资源别名可规范化、分区可按授权窗穷举”三者同时成立时才完备。

规则：
1. 事件资源上有授权在事件时刻仍生效，或事件资源别名无法规范化且主体仍有生效授权，则冻结整个主体，把受影响授权分区列入待核。
2. 主体的全部授权均已失效，则按规范化资源取授权分区并集收失效分区；别名无法归一时退化为全部分区并集。
3. 主体在该资源上从无授权，则无需处置（默认拒绝已生效）；但已有越权放行仍在对账单列示。
4. 每张单写明冻结、恢复、待核分区及依据；恢复只能逐分区放行已核实且仍在授权内的分区。
5. 未解释的越权放行/对账差异阻断恢复；来源行未对平阻断全部恢复。
6. 归档只接受与处置单一致的结论：冻结范围内事件时刻之后的放行未解释时拒绝导出（409）。
7. 版本按事件号独立演进：同事件、同结论证据摘要重试复用版本、不重复生成；冲突或对账证据变化时追加新版本，旧版与原始证据保留。
8. 结论变化后，旧批次的归档导出在原始处置单行之后追加 `disposition_current` 行，不改写历史行。

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 旧编码 | 默认解码失败 | 回退GB18030 | `app/modules/m1_accesses/rules.py` | `tests/test_m1.py::test_legacy_encoding_real_sample` |
| TRAP-02 | BOM | 首列名多字符 | utf-8-sig | `app/modules/m1_accesses/rules.py` | `tests/test_m1.py::test_bom_real_sample` |
| TRAP-03 | 带引号换行 | 拆成两条 | csv逻辑行 | `app/modules/m1_accesses/rules.py` | `tests/test_m1.py::test_quoted_newline_is_one_record` |
| TRAP-05 | 列数不匹配 | 静默错列 | 隔离原行 | `app/modules/m1_accesses/rules.py` | `tests/test_m1.py::test_bad_columns_real_sample` |
| TRAP-08 | 前导零ID | 编码变形 | 全程字符串 | `app/modules/m1_accesses/rules.py` | `tests/test_m1.py::test_identifier_preserved_real_sample` |
| TRAP-14 | 重复键冲突 | 后写覆盖 | 保留首条并隔离冲突行 | `app/modules/m1_accesses/rules.py` | `tests/test_m1.py::test_duplicate_conflict_real_sample` |
| TRAP-18 | 空态三态 | 一律成功 | 分别错误码 | `app/modules/m1_accesses/rules.py` | `tests/test_m1.py::test_three_empty_states` |
| TRAP-26 | 旧版本提交 | 覆盖新结果 | 版本条件更新 | `app/store.py` | `tests/test_e2e.py::test_stale_review_409` |
| TRAP-07 | 上游结果空值 | 误报拒绝 | 隔离缺失 | `app/values.py` | `tests/test_m3.py::test_real_missing_decision_not_zero` |
| TRAP-19 | 允许与拒绝重叠 | 后写覆盖 | 拒绝优先且排序证据 | `app/modules/m4_conflicts/rules.py` | `tests/test_m4.py::test_deny_priority_counterexample` |
| TRAP-23 | 授权到期点 | 多放行一次 | 半开生效窗 | `app/modules/m3_grants/rules.py` | `tests/test_m3.py::test_expiry_endpoint_excluded` |
| TRAP-27 | 终止时仍有生效授权 | 只收事件资源分区，漏别名与跨月访问 | 升级为停用整个主体并逐分区待核 | `app/modules/m8_dispositions/rules.py` | `tests/test_m8.py::test_live_grant_forces_principal_freeze` |
| TRAP-28 | 终止资源别名 | 未登记别名导致范围为空 | 按最保守的主体冻结，依据写明别名缺失 | `app/modules/m8_dispositions/rules.py` | `tests/test_m8.py::test_unknown_alias_upgrades_to_principal_freeze` |
| TRAP-29 | 终止后仍有放行 | 归档照常导出 | 冻结范围内未解释放行拒绝归档 | `app/pipeline.py`、`app/main.py` | `tests/test_m8.py::test_archive_blocked_when_scope_conflict` |
| TRAP-30 | 重试重复生成版本 | 每次运行都升版 | 同证据摘要复用版本 | `app/store.py` | `tests/test_m8.py::test_same_event_retry_reuses_version` |
| TRAP-31 | 结论变化覆盖旧单 | 旧版被新结论替换 | 追加新版本并保留旧版与原始证据 | `app/store.py`、`app/archive.py` | `tests/test_m8.py::test_conclusion_change_appends_version_and_keeps_old` |

## 对账规则

输入原始逻辑行=最终采纳来源行+隔离来源行+重复行+空白行+分段表头。派生记录用_sources保存完整来源，因此分组不丢行。业务预期合计与上游申报合计之差等于逐行差异之和。结果按输入摘要、规则版本及参数产生唯一批次，事务写入；同源重跑只复用已有批次。

## 已知不覆盖

4MB以上文件需预先按业务键分批；不模拟生产支付与外部回调。人工复核只变更批次状态，不改写原始记录。
