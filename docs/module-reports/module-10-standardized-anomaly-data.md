# 模块 10：标准化异常场景数据

## 1. 目标

现有内部投标文件没有项目级确认标签，不能直接训练出真实串标或围标分类器。本模块增加一套可重复、可审计、项目隔离的标准化异常场景数据，用于：

- 检查模型能否识别明确的结构化异常信号；
- 校准异常阈值和特征权重；
- 给比赛演示提供可解释的受控样本；
- 为后续接入业务复核标签保留统一字段。

## 2. 数据边界

合成标签只表示程序施加了某种受控变换，不代表真实串标、围标、违法或违规结论。每条样本都保留来源项目、来源文件对、生成版本和证据字段。不同项目中的同一单位不会被作为异常样本生成。

## 3. 当前场景

| 场景 | 主要字段 | 含义 |
| --- | --- | --- |
| `shared_phone` | `shared_phones` | 共同电话 |
| `shared_email` | `shared_emails` | 共同邮箱 |
| `shared_credit_code` | `shared_credit_codes` | 共同统一社会信用代码 |
| `same_file_author` | `shared_file_authors` | 文件作者或创建者元数据一致 |
| `same_network_fingerprint` | `shared_network_fingerprints` | 网络或设备指纹一致 |
| `same_bid_account` | `shared_bid_accounts` | 投标或收款账户线索一致 |
| `mixed_bid_documents` | `mixed_bid_documents` | 文件混装或跨单位文件痕迹 |
| `price_pattern` | `price_pattern_score` | 报价偏差模式异常一致 |
| `coordinated_price_pattern` | `price_pattern_score` | 阶梯或固定差额式报价模式 |
| `copied_custom_error` | `repeated_segments` | 共同非公共错误或复制片段 |
| `high_text_similarity` | `similarity` | 高文本相似度 |
| `multi_signal` | 多个字段 | 多种信号同时出现 |

## 4. 生成结果

基于现有 150 条同一项目、不同投标人的原始文件对生成：

```text
标准化场景：14 类
训练集：1764 条
测试集：336 条
训练项目：9 个
测试项目：3 个
项目隔离：通过
```

当前冻结阈值为 40 分，受控测试结果为：

```text
Precision：1.0000
Recall：0.9231
F1：0.9600
误报率：0.0000
```

这些指标只表示模型对受控场景的响应，不是现实案件准确率。

## 5. 运行命令

```powershell
$env:PYTHONPATH='src'
.venv/Scripts/python.exe -c "from pathlib import Path; from zhaocai_zhishen.synthetic_benchmark import generate_benchmark; generate_benchmark(Path('data/analysis/pairwise_similarity.csv'), Path('data/synthetic_benchmark'), test_fraction=0.25, seed=20260808)"
.venv/Scripts/python.exe -c "from pathlib import Path; from zhaocai_zhishen.model_training import train_model; train_model(Path('data/analysis'), Path('data/models'), folds=5, benchmark_dir=Path('data/synthetic_benchmark'))"
```

## 6. 公开数据路线

公开政府采购公告和公开附件适合训练公告解析、项目字段抽取和文件分类；公开渠道通常不能提供成套的不同投标人原始文件及确认标签。因此，公开数据不能直接替代内部异常训练数据。后续可将公开处罚公告或裁判文书整理为“案例规则库”，但必须保留来源、发布日期、原文链接和人工核验状态，不能把案例描述直接伪造成投标文件样本。

## 7. 后续工作

下一步应接入真实业务复核结果，将线索标记为“确认异常、排除、无法判断”，再按项目划分训练和验证集。只有完成这一步，系统才有条件报告真实项目级的 Precision、Recall 和 F1。
