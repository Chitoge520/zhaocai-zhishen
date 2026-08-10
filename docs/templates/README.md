# M1/M2 统一审计数据模板

本目录只保存脱敏、虚构的字段模板，不保存真实企业、项目、IP、设备号、账号、内部路径或投标文件。

## 文件

- `audit-records-template.csv`：统一表头模板，可在同一个 CSV 中通过 `record_type` 混合导入不同记录；
- `audit-records-example.jsonl`：覆盖项目、企业、报价、网络事件、文档和历史关系的 M1 脱敏示例；
- `network-linkage-example.jsonl`：用于 M2 端到端验收的关联穿透示例，演示共享设备、同网段短时上传、作者/创建者复用和公共出口排除。

## 支持的记录类型

`project`、`bidder`、`bid`、`quote`、`network_event`、`file_metadata`、`historical_relation`、`document`。

也可以按文件名拆分为 `projects.csv`、`bidders.csv`、`bids.csv`、`quotes.csv`、`network_events.jsonl`、`file_metadata.csv`、`historical_relations.csv` 或 `documents.csv`。拆分文件可以省略 `record_type`，导入器会按文件名推断。

## M2 扩展字段

| 字段 | 用途 |
| --- | --- |
| `device_id` / `device_fingerprint` | 设备指纹；只作为独立关联信号 |
| `account_id` | 投标账号或平台账号 |
| `file_creator` | 文档创建工具或创建者 |
| `pdf_producer` | PDF Producer 元数据 |
| `uploaded_at` | 文件上传或提交时间 |
| `network_role` | 网络出口角色，如 `bidder`、`platform_exit` |
| `is_public_exit` | 是否为平台、代理机构或采购单位公共出口 |

DOCX/PDF 实际文件位于输入目录内且路径安全时，导入器会补充 SHA-256 和可读取的文件元数据；文件损坏或元数据缺失不会阻断其他记录导入。

## 导入与关联分析

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m zhaocai_zhishen ingest `
  --input docs/templates `
  --output data/audit_ingestion_template_check `
  --strict

.\.venv\Scripts\python.exe -m zhaocai_zhishen analyze-links `
  --input data/audit_ingestion_template_check `
  --output data/network_analysis_template_check
```

`analyze-links` 默认使用 IPv4 `/24`、IPv6 `/64` 网段，30 分钟网络事件窗口和 300 秒文件时间窗口。可用 `--exclude-ip` 重复指定已知公共出口 IP，并通过 `--network-window-minutes`、`--metadata-window-seconds` 调整窗口。

生产数据必须放在 `data/` 等 Git 忽略目录。`--strict` 会在存在 schema 错误时返回失败；默认模式会隔离错误、保留可用记录，并生成覆盖率与质量问题摘要。

## 风险边界

- 系统输出是“待复核异常线索”，不直接认定围标、串标或违法违规；
- 单一 IP、设备、账号、作者、哈希或时间接近不能直接定性；
- 公共出口记录不会参与完整 IP 或网段关联；若排除后没有可用 IP，该信号状态为 `excluded`；
- `not_provided` 表示未提供可比较数据，不能解释为安全或低风险；
- `no_signal` 表示已完成比较但没有触发当前规则；
- 每条触发信号通过 `source_record_ids` 和 `evidence_refs` 回到标准记录及来源行；
- 输出中的共享 IP、账号、设备和作者等值只提供脱敏提示，不暴露原值；
- 示例地址仅使用 `192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24` 文档保留段，不代表真实网络主体。
