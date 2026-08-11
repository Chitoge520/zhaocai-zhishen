# 招采智审

招采智审是面向招投标审计场景的本地无监督异常线索发现系统。系统对投标文件进行解析和 OCR，后续通过文本相似度、报价异常、元数据关联和关系图谱生成可解释的复核线索。模型结果仅用于辅助审计，不直接等同于违规结论。

## 当前开发状态

- 已完成内部压缩包整理和投标文件分类
- 已建立统一 CLI、配置和测试入口
- 已完成 M1 统一多源审计数据层，支持 CSV/JSONL、跨项目 `bidder_id`、来源行追溯和四类覆盖率摘要
- 已完成 M2 IP、设备、账号和文件元数据关联，支持 11 类独立信号、公共出口排除、全组合覆盖和证据引用图谱
- 已完成 M5 多信号风险融合：统一网络、报价、跨项目共现与项目内模型线索；提供项目、投标人对、团体、文档四级待复核排序和三级脱敏证据链
- 支持 DOCX、文本型 PDF 和扫描 PDF 识别
- 支持 RTX/NVIDIA GPU PaddleOCR，自动模式优先使用 GPU，失败时回退 RapidOCR
- 可生成 `documents.jsonl`、`pages.jsonl`、`unsupervised_samples.jsonl` 和失败清单
- 已完成 63 份投标文件全量 GPU OCR：13,732 页、原始 OCR 7,872,221 字符、0 失败
- 已完成 `ocr-cleanup/v1` 有效内容清洗：分析文本 7,450,481 字符，原始页文本继续保留用于证据回放
- OCR 后增加有效内容清洗：分析文本去除页眉页脚和同页重复噪声，当前减少 421,740 个字符；原始页面文本仍保留用于证据回放
- 已生成正式无监督分析结果，并提供异常线索、共享实体、成段重复证据和证据页码驾驶舱

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

CPU OCR 使用国内镜像安装：

```powershell
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -e ".[ocr-cpu]"
```

RTX/NVIDIA GPU OCR 在 Windows 上按以下顺序安装。Paddle GPU 核心包使用 Paddle 官方 CUDA 12.6 源，其余依赖使用清华镜像：

```powershell
python -m pip install paddlepaddle-gpu==3.3.1 --index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -e ".[ocr-gpu]"
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --no-deps nvidia-cudnn-cu12==9.9.0.52
```

验证 Paddle 已连接 GPU：

```powershell
python -c "import paddle; print(paddle.device.get_device()); print(paddle.is_compiled_with_cuda()); paddle.utils.run_check()"
```

预期至少包含 `gpu:0`、`True` 和 `PaddlePaddle is installed successfully`。PaddleOCR 模型首次运行时从国内 ModelScope 下载到本机缓存，投标文件页面不会上传。

真实投标文件只在本机处理，不要提交到 Git 或上传到第三方服务。

## 数据检查

默认数据集为：

```text
data/training_internal/standard_dataset
```

检查清单和目录：

```powershell
.\run.ps1 check
```

## 导入统一多源审计数据

M1 支持项目、企业、投标、报价、网络事件、文件元数据、历史关系和文档记录。可直接使用统一表，也可按 `quotes.csv`、`network_events.jsonl` 等文件名拆分导入：

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m zhaocai_zhishen ingest `
  --input data/training_internal `
  --output data/audit_ingestion
```

输出包括 `audit_records.jsonl`、`validation_issues.jsonl`、`bidder_index.json` 和 `coverage_summary.json`。缺少报价或 IP 时不会阻断文本分析；`bidder_id` 只用于身份映射，不是异常证据。脱敏模板见 `docs/templates/`。

严格校验模式：

```powershell
.\.venv\Scripts\python.exe -m zhaocai_zhishen ingest `
  --input data/training_internal `
  --output data/audit_ingestion `
  --strict
```

## 生成 IP、设备和文件元数据关联线索

M2 只消费 M1 已校验标准记录，对同项目不同投标人执行全组合比较：

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m zhaocai_zhishen analyze-links `
  --input data/audit_ingestion `
  --output data/network_analysis
```

输出包括 `network_features.jsonl`、`network_graph.json` 和 `network_analysis_summary.json`。默认网络窗口为 30 分钟、文件时间窗口为 300 秒；可用 `--exclude-ip` 指定公共出口 IP。

每类信号分别标记 `triggered`、`no_signal`、`not_provided` 或 `excluded`。系统只输出待复核异常线索；单一 IP、设备、账号、作者、哈希或时间接近不能直接定性。共享网络和元数据值在结果中脱敏。模块说明见 `docs/module-reports/module-15-network-metadata-linkage.md`。


## 生成报价聚类与规律性差异线索

M3 只消费 M1 已校验标准记录，严格区分总价与分项报价，对同一项目不同投标人执行全组合无监督比较：

~~~powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m zhaocai_zhishen analyze-quotes `
  --input data/audit_ingestion `
  --output data/quote_analysis
~~~

输出包括 price_features.jsonl、price_graph.json 和 price_analysis_summary.json；服务接口为 GET /api/quote-analysis。金额会按元、千元、万元等单位归一；未知金额单位、币种不一致和冲突总价会被标记为 excluded，缺少报价、控制价或共同分项会标记为 not_provided。

所有报价结果均为待复核异常线索，不能宣传为真实准确率或直接认定围标、串标、违法违规。单一价格接近、尾数、固定差额、统计偏离或分项相关性必须结合网络、设备、文件和业务流程证据交叉验证。模块说明见 docs/module-reports/module-16-quote-clustering.md，脱敏模板见 docs/templates/quote-analysis-example.jsonl。

## 生成多信号风险融合与三级证据链

M5 将网络、报价、跨项目共现和项目内无监督结果聚合为可解释的待复核排序：

~~~powershell
.\run.ps1 analyze-risk-fusion `
  --analysis data/analysis `
  --network data/network_analysis `
  --quotes data/quote_analysis `
  --cooccurrence data/cooccurrence_analysis `
  --model data/inference `
  --output data/risk_fusion
~~~

输出包括 `project_risk_results.jsonl`、`bidder_pair_risk_results.jsonl`、`group_risk_results.jsonl`、`document_risk_results.jsonl`、`evidence_events.jsonl` 和 `risk_fusion_summary.json`；服务接口为 `GET /api/risk-fusion`，上传任务结果位于 `GET /api/projects/jobs/{job_id}/results` 的 `risk_fusion` 字段。

融合规则按强、佐证和弱信号分层：仅弱信号最高为低风险；高风险必须同时具备至少一个强信号和另一独立分析维度的佐证。缺失字段不会被解释为低风险。三级证据链不复制 IP、设备、账号、联系人、地址或原始文件路径，仅保留分数贡献、`record_id`、来源哈希及行号索引，供受控环境回溯。
## 生成无监督样本

```powershell
.\run.ps1 prepare --ocr auto
```

输出目录：

```text
data/processed/
  documents.jsonl
  pages.jsonl
  unsupervised_samples.jsonl
  parse_failures.csv
```

小规模冒烟测试：

```powershell
.\run.ps1 prepare --ocr auto --max-documents 1 --max-pages-per-document 3 --output data/processed_smoke
```

OCR 模式：

- `auto`：有本地 OCR 运行库时自动处理扫描 PDF，否则标记 `needs_ocr`
- `on`：强制要求 OCR，缺少运行库时直接报错
- `off`：只做原生文本提取和扫描件检测

OCR 引擎：

- `--ocr-engine auto`：优先使用 `paddle-gpu`，初始化失败时回退 `rapidocr`
- `--ocr-engine paddle-gpu`：强制使用本机 NVIDIA GPU
- `--ocr-engine rapidocr`：强制使用 CPU RapidOCR

GPU 冒烟测试：

```powershell
.\run.ps1 prepare --ocr on --ocr-engine paddle-gpu --workers 1 --max-documents 1 --max-pages-per-document 5 --force --output data/processed_gpu_smoke
```

同一张 GPU 默认只使用一个 OCR 进程；GPU 模式下传入更大的 `--workers` 会自动调整为 1。

## 生成无监督异常线索

完成 `prepare` 后运行：

```powershell
.\run.ps1 analyze
```

默认读取 `data/processed`，输出到 `data/analysis`：

```text
data/analysis/
  document_entities.jsonl
  pairwise_similarity.csv
  anomaly_results.csv
  analysis_summary.json
```

分析会在同一项目内比较不同投标人的字符级 TF-IDF 相似度，并结合前部核心区域提取的电话、邮箱、统一社会信用代码、联系人、地址等实体生成异常分数。纯文本相似度达到 75% 才会单独形成线索；固定电话会校验中国区号和位数，项目内至少 3 份文档共同出现的实体会作为公共模板信息排除。成段重复文本只作为已有实体或高相似度线索的佐证，不会单独触发异常。`anomaly_results.csv` 中的页码用于回到原文件复核，结果仅表示异常线索。

项目内同一投标人的技术、商务、扫描件分册会按完整公司名和短别名归并，不会互相生成异常线索。当前正式数据结果为 15 个项目、63 份文档、162 个项目内文档对和 1 条高风险待复核线索。启动服务后，驾驶舱和 `/api/unsupervised` 会读取上述本地分析产物；结果不存在时接口返回 `ready: false`。

## 启动驾驶舱

```powershell
.\run.ps1 serve
```

## 大模型辅助 OCR 校正

DeepSeek 目前不仅可以解释异常线索，也可以对疑似 OCR 错字、断行和字段结构进行选择性校正。校正默认只发送疑似问题页面的脱敏片段，不发送 ZIP 或完整文档；原始 OCR 文本始终保留，校正结果不会自动覆盖分析文本。

配置大模型后运行：

```powershell
.\run.ps1 enhance-ocr --input data/processed --output data/ocr_llm
```

输出：

```text
data/ocr_llm/ocr_llm_result.json
data/ocr_llm/ocr_llm_raw_response.json
```

只有通过本地页面、占位符和格式校验的结果才会进入 `ocr_llm_result.json`。当前版本先生成旁路校正结果，人工确认后再应用到 `documents.jsonl`。

访问 `http://127.0.0.1:4180`。

## 测试

标准库测试入口：

```powershell
$env:PYTHONPATH=".\src"
python -m unittest discover -s tests -v
```

当前全量回归共有 65 项单元测试，其中 M1 统一数据层专项测试 9 项、M2 关联分析专项测试 10 项。M0 基线冻结时为 46 项，历史口径保持不变。安装开发依赖后也可以使用 `pytest`。

## 比赛版本与安全基线

当前比赛开发基线为 `0.3.0` / `competition-m0-2026.08`。统一 schema、算法版本、数据边界和聚合基线说明见 `docs/versioning-and-baseline.md`。

重新生成不包含真实投标主体信息的聚合基线：

```powershell
$env:PYTHONPATH='src'
.venv/Scripts/python.exe -m zhaocai_zhishen baseline --tests 46
```

生成结果写入 `docs/baselines/competition-m0-baseline.json`，只包含聚合数量、版本号和汇总产物 SHA-256，不包含投标人、项目编号、联系方式、正文或本地绝对路径。

## 开发原则

- 默认采用无监督异常检测，不要求预先人工标注
- 人工复核结果只作为反馈数据沉淀
- 所有风险线索必须能追溯到文件和页码
- 真实投标文件和处理结果不得进入公开仓库
- 日志不得输出完整敏感字段或大段原文

## 训练历史异常基线

历史项目只用于训练和项目级交叉验证，新上传项目应使用冻结模型单独推理。训练时不会把同一项目的文件拆到训练集和验证集。

```powershell
.\run.ps1 train --input data/analysis --output data/models --folds 5
```

模型文件：

```text
data/models/bid_anomaly_model.json
data/models/training_summary.json
```

对一个已经完成 OCR 和项目内分析的新项目运行模型推理：

```powershell
.\run.ps1 infer --input data/new_project_analysis --model data/models/bid_anomaly_model.json --output data/new_project_inference
```

当前模型是无监督异常排序基线，输出为待复核异常线索，不是违规概率，也不能直接确认串标围标。数据补充要求见 `docs/data-collection-requirements.md`。
## 新项目上传

启动服务后，在“新项目分析”界面上传 ZIP。每个上传任务使用独立目录：

```text
data/jobs/<job_id>/
  raw/
  dataset/
  processed/
  analysis/
  inference/
  llm/
  status.json
```

新项目会依次执行文件整理、GPU OCR、项目内异常分析、冻结模型推理和可选的大模型辅助复核，不会自动写入历史训练集。完成后可从任务列表进入异常线索界面查看结果。

## 大模型辅助分析

在“大模型配置”界面可以配置 DeepSeek。API 密钥只保存在当前服务进程的环境变量中，不写入项目文件，服务重启后需要重新配置。

大模型不会接收原始 ZIP 或完整投标正文。系统最多选择 8 组本地候选线索，仅发送对应证据页中经过脱敏的局部文本，总字符数不超过 24,000。大模型返回的引用必须匹配本地文档 ID、页码和已发送原文，无法验证的结论会被丢弃。

未启用、未配置或调用失败时，本地规则和冻结模型仍会正常完成。所有大模型输出仍属于“待复核线索”，不能直接认定串标、围标或违规。

## 报告导出与现场演示

启动服务后，在“项目报告”页面可以导出 DOCX，或打开 HTML 打印版并使用浏览器打印为 PDF。系统不把 HTML 打印版误称为直接生成的 PDF。

竞赛优化开发计划见 `docs/competition-optimization-development-plan.md`。

比赛现场流程和故障排查见 `docs/competition-demo-runbook.md`，模块开发记录见 `docs/module-reports/`。
