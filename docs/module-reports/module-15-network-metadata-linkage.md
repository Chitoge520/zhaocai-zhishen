# 模块 15：IP、设备和文件元数据关联

> 完成日期：2026-08-10<br>
> 分析 schema：`bid-audit-network-analysis/v1`<br>
> 特征 schema：`bid-audit-network-feature/v1`<br>
> 算法版本：`network-linkage/1.0.0`

## 1. 目标

在 M1 已校验标准记录之上，计算同项目不同投标人的网络、设备、账号和文件元数据关联，形成可排序、可解释、可追溯的“待复核异常线索”。模块不引入人工标签，不以单一 IP 或元数据直接认定围标串标。

## 2. 输入与处理边界

- 只消费 M1 生成的 `audit_records.jsonl`；
- 只比较同一项目中的不同投标人，项目内投标人组合全量枚举；
- IPv4 规范为 `/24`，IPv6 规范为 `/64`；
- 默认网络事件时间窗口为 30 分钟，文件与上传时间窗口为 300 秒；
- 平台、代理机构、采购单位和显式配置的公共出口 IP 不参与共享 IP/网段信号；
- 风险分数仅用于线索排序，不输出违法违规结论。

## 3. 文件元数据抽取

新增 `metadata_analysis.py`，在统一导入阶段对安全的相对文件路径进行补充抽取：

| 文件类型 | 可抽取字段 |
| --- | --- |
| DOCX | SHA-256、作者、最后修改者、创建时间、修改时间 |
| PDF | SHA-256、Author、Creator、Producer、CreationDate、ModDate |
| 其他文件 | SHA-256 |

单文件上限为 512 MB。绝对路径、目录穿越、损坏文件、加密 PDF 或不可解析元数据不会阻断主流程；只保留成功计算的字段和来源引用。

## 4. 独立关联信号

| 信号 | 说明 | 默认贡献分 |
| --- | --- | ---: |
| `shared_full_ip` | 不同投标人共享完整 IP，公共出口排除 | 22 |
| `shared_subnet_time` | 不同 IP 位于同一规范化网段且短时上传 | 12 |
| `shared_device` | 设备指纹一致 | 24 |
| `shared_account` | 投标账号一致 | 20 |
| `shared_file_hash` | 文件 SHA-256 一致 | 30 |
| `shared_author` | 文档作者一致 | 8 |
| `shared_file_creator` | 文件创建者一致 | 10 |
| `shared_pdf_producer` | PDF Producer 一致 | 5 |
| `created_time_close` | 文件创建时间接近 | 6 |
| `modified_time_close` | 文件修改时间接近 | 7 |
| `upload_time_close` | 上传时间接近 | 8 |

PDF Producer 等常见软件元数据是弱信号。它们可以解释文件生成环境，但必须与设备、账号、哈希、时间或其他业务信号组合复核，不能单独作为高风险判断。

## 5. 缺失、排除与证据追溯

每一类信号为每个投标人组合记录独立状态：

- `triggered`：已比较并触发；
- `no_signal`：数据可比较但未触发；
- `not_provided`：至少一方没有提供可比较数据；
- `excluded`：相关记录存在，但只包含公共出口等应排除数据。

每条触发信号包含 `source_record_ids` 和 `evidence_refs`，可回到 M1 标准记录、原始相对路径、格式、行号和行摘要。共享 IP、网段、设备、账号、作者等值只输出哈希提示或类型提示，不输出原值。

## 6. 输出、CLI 与 API

默认输出目录为 `data/network_analysis/`：

```text
network_features.jsonl
network_graph.json
network_analysis_summary.json
```

执行命令：

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m zhaocai_zhishen analyze-links `
  --input data/audit_ingestion `
  --output data/network_analysis
```

可选参数：`--exclude-ip`、`--network-window-minutes`、`--metadata-window-seconds`。服务新增 `GET /api/network-analysis`，`GET /api/ingest/status` 和新项目结果接口同步返回网络关联摘要。

## 7. 驾驶舱

新增“关联穿透”页面，展示：

- 纳入项目、投标人、应比较组合和完成组合；
- 触发组合、证据追溯率和可用信号类型；
- 投标人对、独立信号、贡献分、证据引用数和风险级别；
- 公共出口排除、未提供数据和单一信号不可定性的解释。

## 8. 脱敏模板验收

使用 `docs/templates/` 导入后得到 16 条标准记录、0 个质量问题。M2 示例形成 1 个应比较组合，完成率 100%，触发 7 类独立信号，证据追溯率 100%。完整 IP、网段、设备、账号、作者和创建者原值均未出现在关联分析输出中。

公共出口示例与投标人自有网段同时存在：公共出口记录不参与共享完整 IP 和同网段信号；剩余可用 IP 已完成比较，因此组合级状态为 `no_signal` 而不是 `excluded`。只有一方或双方除公共出口外没有可用 IP 时，状态才为 `excluded`。

## 9. 本地数据聚合验收

在不披露企业、项目和内部路径的前提下，本地聚合结果为：

| 指标 | 结果 |
| --- | ---: |
| 标准记录 | 63 |
| 项目 | 15 |
| 投标人 | 51 |
| 应比较组合 | 112 |
| 完成组合 | 112 |
| 触发组合 | 74 |
| 中风险组合 | 3 |
| 低风险组合 | 71 |
| 证据追溯率 | 100% |

当前本地数据没有提供 IP、设备、账号和上传时间，因此这些信号状态为 `not_provided`，不能表述为“未发现 IP/设备风险”。可用信号主要来自文件哈希和文档元数据；其中 PDF Producer 触发数量较多，属于常见工具弱信号，需要与更强证据组合复核。

## 10. 测试与验收

M2 专项测试 10 项，覆盖：同 IP、公共出口排除、同网段时间窗口、网段输出脱敏、同设备不同账号、元数据缺失、IPv6、组合元数据、标准输出和 DOCX 元数据抽取。全量回归 65 项通过；Python 语法检查和前端脚本语法检查纳入最终提交前验收。

## 11. 竞赛展示价值

- 从“文档相似度”扩展为“网络行为 + 终端 + 账号 + 文件指纹 + 时间链”的多信号穿透；
- 对每个项目执行全组合比较，能明确展示全样本覆盖和完成率；
- 把缺失、排除和未触发分开，避免为了好看而虚报风险覆盖；
- 图谱边携带来源记录和证据引用，支持从异常组合回放到原始数据；
- 无 IP 日志时仍可依靠文件元数据降级运行，同时如实展示数据缺口。
