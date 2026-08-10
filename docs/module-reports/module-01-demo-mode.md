# 模块一开发报告：演示项目模式和数据概览

## 1. 模块定位

本模块面向比赛现场展示，解决首次启动时 OCR、模型加载或大模型网络调用耗时，导致评委无法快速看到系统效果的问题。

模块提供一个只读的“历史基线离线演示案例”。演示案例直接读取项目中已经生成的本地数据处理、无监督分析和模型产物，不重新解压文件、不启动 OCR、不训练模型，也不调用 DeepSeek。

系统对外仍保持准确边界：展示内容是异常线索和待复核证据，不代表已经确认串标、围标或其他违规行为。

## 2. 功能介绍

### 2.1 离线演示快照接口

新增接口：

```text
GET /api/demo
```

接口聚合以下本地结果：

- `data/training_internal/summary.json`
- `data/processed/summary.json`
- `data/analysis/analysis_summary.json`
- `data/analysis/pairwise_similarity.csv`
- `data/analysis/anomaly_results.csv`
- `data/models/bid_anomaly_model.json`
- `data/models/training_summary.json`

接口返回 `ready`、演示模式标识、数据统计、历史分析结果和模型状态。缺少关键产物时返回未就绪状态，不会抛出未经处理的异常。

### 2.2 首页比赛演示入口

项目总览页新增：

- “加载离线演示”按钮
- 演示数据就绪状态
- “查看演示线索”快捷入口
- 真实数据概览指标

展示指标包括：

- 历史可分析项目数
- 原始压缩包数
- 投标文件数
- 解析页数
- GPU OCR 文档数
- 项目内文档比较数
- 待复核异常线索数

### 2.3 演示模式与新项目模式隔离

前端增加运行模式状态：

- `history`：历史基线回放
- `demo`：本地离线演示
- `live`：新上传项目的真实分析结果

上传新项目后仍使用原有异步任务链路，分析结果不会被演示快照覆盖，也不会自动写入历史训练集。

## 3. 代码变更

新增文件：

- `src/zhaocai_zhishen/demo_mode.py`
- `tests/test_demo_mode.py`
- `docs/module-reports/module-01-demo-mode.md`

修改文件：

- `src/zhaocai_zhishen/server.py`
- `src/zhaocai_zhishen/static/index.html`

## 4. 当前真实演示数据

接口从当前本地项目产物读取到的统计为：

| 指标 | 数值 |
| --- | ---: |
| 历史项目 | 15 |
| 原始压缩包 | 19 |
| 原始文件 | 135 |
| 投标文件 | 63 |
| 解析页数 | 13,732 |
| GPU OCR 缓存文档 | 63 |
| 项目内文档比较 | 162 |
| 训练比较 | 150 |
| 待复核异常线索 | 1 |

上述数字是当前本地文件的统计结果，不是模型准确率、召回率或违规案件数量。

## 5. 比赛现场演示流程

1. 启动本地服务并打开项目总览页。
2. 点击“加载离线演示”，确认页面显示演示数据就绪。
3. 查看数据概览和六阶段处理链路。
4. 点击“查看演示线索”，展示本地模型的异常分数、相似度和待复核状态。
5. 回到“新项目分析”页，说明现场可以上传新的项目 ZIP，演示数据不会混入新项目。
6. 后续模块完成后，从异常线索进入关系图、双栏证据和正式报告。

## 6. 验证结果

执行命令：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q src
```

结果：

- 21 项 unittest 全部通过
- Python 源码编译检查通过
- 演示数据缺失时能够返回未就绪状态
- 临时数据夹具能够验证统计聚合、GPU OCR 文件计数和分析结果嵌入

## 7. 已知限制

- 当前演示快照使用历史项目汇总结果，不代表一个全新的上传任务。
- GPU OCR 文档数根据本地 `processed/cache` 中包含 `paddle-gpu` 标识的缓存文件统计；后续可以在处理摘要中写入更严格的来源字段。
- 当前演示入口主要负责快速进入案例，关系图和证据回放将在后续模块提供。
- 真实项目首次分析仍可能受文件数量、页数、GPU 初始化和大模型网络状态影响。
