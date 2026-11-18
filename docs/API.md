# 接口

模块GET使用仓内样例，模块POST可传csv或rows与context，直接计算当前规则。/api/runs使用完整流水线并原子保存。每个阶段返回rows、quarantine、metrics；后续阶段还返回累计隔离。/api/upload接收multipart文件file。/api/runs/<id>/archive只导出一份带清单与证据的CSV。

## 终止处置

- `GET /api/dispositions`：对仓内样例生成处置单。
- `POST /api/dispositions`：传 `sample`、`csv` 或 `run_id`；可选 `parameters.terminations`、`parameters.explanations`、`parameters.reviewed_partitions`。
- `GET /api/dispositions/<event_id>/history`：返回该事件的全部版本，旧版在前。
- 处置单字段：`decision`（`principal` 停用整个主体 / `partitions` 只收失效分区 / `noop` 无需处置）、`frozen`、`restore`、`pending`、`basis`、`evidence`、`evidence_digest`、`version`。
- `parameters.terminations` 为事件数组，每项含 `event_id`、`principal`、`resource`、`effective_at`、`note`，事件号不得重复，时间必须带偏移。
- `parameters.explanations` 为事件号到说明的对象；解释过的越权放行与对账差异不再阻断恢复。
- `parameters.reviewed_partitions` 为处置事件号到已核实分区数组的对象；只有已核实且在授权窗内的分区可恢复。
- 版本规则：同事件、同证据摘要的重试复用版本；冲突或对账证据变化时追加新版本，旧版与原始证据保留。
- 归档同源：冻结范围内事件时刻之后仍有未解释放行、或来源行未对平时，`GET /api/runs/<id>/archive` 返回 409；结论已更新时导出在原始 `disposition` 行后追加 `disposition_current` 行。
