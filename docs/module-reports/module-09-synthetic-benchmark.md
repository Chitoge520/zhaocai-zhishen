# 模块九：可追溯合成训练与测试基准

> M0 口径更新时间：2026-08-10

## 1. 目标

现有 15 个历史项目中没有项目级确认的“正常/异常”标签，不能把真实投标文件直接伪造为串标、围标或违法违规样本。本模块提供一套可重复、可审计、项目隔离的受控基准，用来验证无监督模型能否响应明确的异常信号。

## 2. 数据边界

- 真实投标文件保留在 `data/training_internal/standard_dataset`，只作为真实无标签基线；
- 合成数据由 `data/analysis/pairwise_similarity.csv` 的两两特征生成，不复制、不改写原始 PDF；
- `synthetic_is_positive=1` 只表示样本经过指定的受控变换，不代表真实串标、围标或违法违规；
- 训练集和测试集按 `project_id` 隔离，同一项目不会同时出现在两侧；
- 合成样本只用于回归评估，不参与真实无监督模型的统计拟合，避免人为异常改变真实基线分布。

## 3. 当前 14 类变换

每条来源两两记录生成 14 个版本：

| 变换 | 含义 |
| --- | --- |
| `clean_pair` | 清除共享实体和高相似片段的低信号控制样本 |
| `shared_phone` | 注入合成共享电话字段 |
| `shared_address` | 注入合成共享地址字段 |
| `shared_email` | 注入合成共享邮箱字段 |
| `shared_credit_code` | 注入合成统一社会信用代码字段 |
| `copied_custom_error` | 注入高文本相似度和共同非公共错误片段 |
| `high_text_similarity` | 注入高文本相似度和重复片段 |
| `multi_signal` | 同时注入多种实体和文本信号 |
| `same_file_author` | 注入相同文件作者或创建者元数据 |
| `same_network_fingerprint` | 注入相同网络或设备指纹 |
| `same_bid_account` | 注入相同投标或收款账户线索 |
| `mixed_bid_documents` | 注入文件混装、错放或跨单位文件痕迹 |
| `price_pattern` | 注入异常一致的报价偏差模式 |
| `coordinated_price_pattern` | 注入阶梯或固定差额式协同报价模式 |

来源和变换记录位于 `manifest.csv`，主要审计字段包括：

- `source_project_id`；
- `source_document_id_a`、`source_document_id_b`；
- `source_row_id`；
- `transform_type`；
- `evidence_fields`；
- `split`；
- `generation_version`。

## 4. 当前生成结果

基于 150 条同一项目、不同投标人的来源文件对生成：

```text
schema_version：bid-synthetic-benchmark/v2
来源文件对：150 条
合成样本：2,100 条
受控变换：14 类
训练集：1,764 条
测试集：336 条
训练项目：9 个
测试项目：3 个
项目隔离：通过
随机种子：20260808
```

M0 的机器可读聚合清单位于 `docs/baselines/competition-m0-baseline.json`。真实合成明细仍在 `data/synthetic_benchmark/`，不进入 Git。

## 5. 当前受控评估

冻结阈值为 40 分，受控测试集结果为：

```text
Precision：1.0000
Recall：0.9231
F1：0.9600
误报率：0.0000
```

主要解释：

- 低信号控制样本没有越过阈值；
- 电话、邮箱、统一社会信用代码、文件作者、网络指纹、账户、文件混装、报价模式、高文本相似度和多信号场景能够越过阈值；
- 仅共享地址场景得分低于阈值，继续作为需要其他证据配合的弱信号；
- 上述指标只衡量模型是否响应程序施加的受控变换，不能宣传为真实案件识别准确率。

## 6. 运行命令

生成基准：

```powershell
$env:PYTHONPATH='src'
.venv/Scripts/python.exe -m zhaocai_zhishen make-benchmark `
  --analysis data/analysis/pairwise_similarity.csv `
  --output data/synthetic_benchmark `
  --test-fraction 0.25 `
  --seed 20260808
```

重新训练真实无监督基线，并附带受控测试摘要：

```powershell
$env:PYTHONPATH='src'
.venv/Scripts/python.exe -m zhaocai_zhishen train `
  --input data/analysis `
  --output data/models `
  --folds 5 `
  --benchmark data/synthetic_benchmark
```

生成不含真实主体信息的 M0 聚合基线：

```powershell
$env:PYTHONPATH='src'
.venv/Scripts/python.exe -m zhaocai_zhishen baseline --tests 46
```

## 7. 后续改进

1. 将 IP、设备、报价和跨项目共现信号接入正式计算链路，而不是只存在于受控变换中；
2. 接入真实业务复核结果，单独建立项目级复核测试集；
3. 对地址、联系人等弱信号引入上下文校验，避免单字段决定高风险；
4. 将合成基准与真实无标签 Top-K 线索在报告中分开展示；
5. 不把合成指标写成“串标围标识别准确率”。