# 模块 16：报价聚类与规律性差异分析

> 完成日期：2026-08-10
> 分析 schema：bid-audit-quote-analysis/v1
> 特征 schema：bid-audit-quote-feature/v1
> 算法版本：quote-pattern-unsupervised/1.0.0

## 1. 目标

M3 在 M1 统一审计数据层之上，对同一项目内、不同投标人的总价和分项报价进行无监督稳健统计与结构规律分析。输出是带有公式输入、原始记录 ID 与来源引用的“待复核异常线索”，用于全样本筛查和人工复核排序；不使用人工标签训练，不输出违规概率或违法违规结论。

## 2. 输入边界与归一化

- 只消费 M1 产生的 audit_records.jsonl，仅接收 quote 与 bid 记录；
- amount/total_price 代表总价；unit_price 与 item_code/item_name 代表分项报价；两类口径严格分开，不把分项报价混入总价比较；
- 金额单位支持元、千元、万元以及 CNY/RMB/Yuan 等常见别名，统一归一至元；未知单位标记为 excluded，不参与比较；
- 同一项目、同一投标人、同一币种出现互相冲突的总价时，相关总价比较降级为 excluded；多轮报价需在后续接入轮次字段后再分析；
- 不同币种不横向比较；缺失控制价不阻断其他可用报价比较。

## 3. 无监督特征与线索

| 信号 | 作用 | 默认贡献分 |
| --- | --- | ---: |
| control_price_ratio | 两家报价同时接近控制价且比例高度接近 | 8 |
| median_deviation | 相对项目中位数的稳健 Z 分数极端偏离 | 10 |
| pairwise_price_distance | 投标人对总价相对距离过近 | 15 |
| fixed_difference | 三家及以上相邻总价近似固定差额 | 14 |
| fixed_ratio | 三家及以上相邻总价近似固定比例 | 14 |
| repeated_price_tail | 三家及以上共享非常规四位尾数 | 8 |
| staircase_quote | 总价排序呈规则阶梯 | 12 |
| accompaniment_structure | 一低多高且高价集中 | 15 |
| item_price_correlation | 至少三个共同分项报价高度相关 | 12 |
| item_rank_consistency | 至少三个共同分项报价排序高度一致 | 10 |

风险分只用于排序。多个独立但相关性有限的信号可以提高复核优先级；单一报价接近、尾数、统计偏离或分项相关性均不能直接认定围标、串标或违法违规。

## 4. 状态、证据链与输出

每个项目内投标人对为每类信号分别输出：

- triggered：完成比较且触发线索；
- no_signal：完成可用比较但未触发；
- not_provided：缺少可比较报价、控制价或共同分项；
- excluded：未知金额单位、币种不一致或冲突报价等应排除情形。

每个触发信号保留 formula、inputs、source_record_ids 与 evidence_refs。默认输出目录是 data/quote_analysis/，其中包括 price_features.jsonl、price_graph.json 和 price_analysis_summary.json。

## 5. CLI、API 与驾驶舱

~~~powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m zhaocai_zhishen analyze-quotes `
  --input data/audit_ingestion `
  --output data/quote_analysis
~~~

服务提供 GET /api/quote-analysis。新项目异步任务会在“网络、设备和文件元数据关联”之后执行报价分析，并在任务结果中返回 quote_analysis。驾驶舱新增“报价聚类”页面，展示分析范围、有效总价、比较组合、触发组合、证据追溯率、信号可用性与投标人对明细。

## 6. 脱敏模板验收

docs/templates/quote-analysis-example.jsonl 使用虚构项目、虚构主体和合成金额，覆盖：

- 元/万元归一与控制价比；
- 固定差额、规则阶梯和分项相关性/排序一致性；
- 缺失控制价时的 not_provided 降级，但保留可用总价比较；
- 未知金额单位时的 excluded 降级。

模板仅用于功能验收和比赛演示，不代表真实准确率、检出率或真实案件分布。

## 7. 测试与竞赛展示价值

M3 专项测试覆盖固定差额、固定比例、离散报价、缺失控制价、单位归一、未知单位排除、稳健离群、非常规尾数、一低多高、分项相关性、证据追溯、标准输出及导入别名与校验。展示时可从“全量项目 × 投标人对”的比较覆盖率进入，逐层展示报价信号、公式输入、来源行引用，再与 M2 的网络和文件元数据线索交叉复核，形成可解释的异常行为图谱。
