# M1 统一审计数据导入模板

本目录只保存脱敏、虚构的字段模板，不保存真实企业、项目、IP、设备号、内部路径或投标文件。

## 文件

- `audit-records-template.csv`：统一表头模板，可在同一个 CSV 中通过 `record_type` 混合导入不同记录；
- `audit-records-example.jsonl`：覆盖项目、企业、报价、网络事件、文档和历史关系的脱敏示例。

## 支持的记录类型

`project`、`bidder`、`bid`、`quote`、`network_event`、`file_metadata`、`historical_relation`、`document`。

也可以按文件名拆分为 `projects.csv`、`bidders.csv`、`bids.csv`、`quotes.csv`、`network_events.jsonl`、`file_metadata.csv`、`historical_relations.csv` 或 `documents.csv`。拆分文件可以省略 `record_type`，导入器会按文件名推断。

## 导入

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m zhaocai_zhishen ingest `
  --input docs/templates `
  --output data/audit_ingestion_template_check
```

生产数据必须放在 `data/` 等 Git 忽略目录。`--strict` 会在存在 schema 错误时返回失败；默认模式会隔离错误、保留可用记录，并生成覆盖率与质量问题摘要。

## 边界

- 缺少报价或 IP 不阻断文本分析；
- `bidder_id` 是身份映射键，不是异常证据；
- 信用代码冲突不会强制合并；
- 每条输出记录通过 `source_refs` 追溯到原始相对路径、格式、行号和原始行 SHA-256；
- 示例 IP `192.0.2.10` 属于文档保留地址，不代表真实网络主体。
