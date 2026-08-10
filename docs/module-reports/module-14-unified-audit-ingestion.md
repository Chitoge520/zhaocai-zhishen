# 模块 14：M1 统一多源审计数据层

> 完成日期：2026-08-10
> 导入 schema：`bid-audit-ingestion/v1`
> 标准记录 schema：`bid-audit-record/v1`

## 1. 目标

把文档、报价、网络事件、文件元数据和历史投标关系接入同一个可追溯入口，为后续 IP 关联、报价聚类、投标人共现和全局异常行为图谱提供一致的数据契约。M1 只负责身份映射、质量校验、覆盖率和来源追溯，不引入人工标签，也不直接认定围标串标。

## 2. 核心实现

- 新增 `src/zhaocai_zhishen/audit_schema.py`，定义统一字段、记录类型、规范化和校验规则；
- 新增 `src/zhaocai_zhishen/audit_ingestion.py`，递归发现并导入 CSV/JSONL；
- CLI 新增 `ingest` 命令及 `--strict` 模式；
- 新项目 ZIP 整理完成后自动生成统一审计记录与覆盖率摘要；
- 服务新增历史数据和单任务覆盖率 API；
- 驾驶舱新增“数据覆盖率”页面；
- 新增 CSV 表头模板、JSONL 脱敏示例和模板说明。

## 3. 统一记录类型

| 类型 | 用途 |
| --- | --- |
| `project` | 项目主数据 |
| `bidder` | 企业身份与别名 |
| `bid` | 投标行为主记录 |
| `quote` | 报价金额与时间 |
| `network_event` | IP、设备和提交时间 |
| `file_metadata` | 文件哈希、作者和时间元数据 |
| `historical_relation` | 历史共同参标或其他关系 |
| `document` | 投标文件及标准样本映射 |

导入器支持中英文字段别名，也兼容现有 `standard_dataset/samples.csv` 的 `bid_file` 字段和旧版 `standard_path` 字段。

## 4. 全局企业映射

企业映射按以下顺序生成稳定 `bidder_id`：

1. 有效统一社会信用代码；
2. 能唯一指向规范化企业名称的别名；
3. 规范化企业名称哈希。

企业名称会进行 Unicode NFKC、标点空格统一以及“有限责任公司/有限公司”兼容处理。同一规范化名称对应多个信用代码，或同一别名指向多个主体时，系统记录质量冲突，不对无代码记录强制归并。

`bidder_id` 只是跨项目身份关联键，不是异常或违规证据。

## 5. 来源追溯与质量降级

每条标准记录包含 `source_refs`：

- 原始相对路径；
- CSV/JSONL 格式；
- 原始行号；
- 原始行 SHA-256。

无效金额、时间、IP、文件 SHA-256 和信用代码会写入校验问题并降级为空。默认模式保留其他可用记录；`--strict` 在存在 schema 错误时失败。校验日志对企业名称、信用代码、IP、设备号、作者和内部路径只保留哈希或类型提示，不写入原始敏感值。

缺少报价或 IP 只意味着对应信号不可计算，不会阻断 OCR、文本相似度、实体复用和现有无监督排序，也不会把“未提供数据”解释为低风险。

## 6. 输出与接口

默认输出目录为 `data/audit_ingestion/`：

```text
audit_records.jsonl
validation_issues.jsonl
bidder_index.json
coverage_summary.json
```

覆盖率摘要分别统计文件、报价、IP、历史关系的记录数、可用记录数、覆盖项目数和项目覆盖率，并汇总输入行、去重数、企业映射数、缺失字段和冲突数量。

接口：

- `GET /api/coverage`：历史统一数据覆盖率；
- `GET /api/projects/jobs/{job_id}/coverage`：指定新项目任务覆盖率；
- `GET /api/ingest/status`：在原状态中增加 `audit` 摘要；
- `GET /api/projects/jobs/{job_id}/results`：增加 `audit_coverage`。

## 7. 脱敏模板验收

使用 `docs/templates/audit-records-example.jsonl` 导入结果：

```text
发现结构化源文件：2
识别结构化源文件：2
空模板：1
输入行：7
标准记录：7
企业映射：1
项目：2
校验问题：0
文件/报价/IP/历史关系：均有可用记录
```

示例中的企业、项目、设备、文件路径和作者均为虚构脱敏内容；`192.0.2.10` 为文档保留 IP 地址。

## 8. 测试与验收

M1 专项测试覆盖：

- 企业别名归并和信用代码优先映射；
- 同企业跨项目稳定映射；
- 缺少报价或 IP 的降级处理；
- 重复记录去重并保留多个来源引用；
- 信用代码冲突；
- 非法金额、时间和敏感日志脱敏；
- CSV/JSONL 混合输入、空表和现有 `samples.csv` 适配。

专项测试 9 项通过；全量回归 55 项通过。Python 语法检查、前端脚本语法检查和脱敏模板导入均通过。

## 9. M2 输入契约

M2 只能基于 M1 已校验的 `network_event`、`file_metadata` 和 `document` 记录计算关联信号，并必须：

- 区分“没有风险信号”和“没有提供数据”；
- 把完整 IP、网段、设备、作者、哈希和时间接近度拆成独立证据；
- 保留到 `source_refs` 的反向追溯；
- 使用无监督统计或规则贡献，不把身份键本身当作风险；
- 不将真实明细、日志或任务产物提交到 Git。
