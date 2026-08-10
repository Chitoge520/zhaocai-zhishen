# 模块三开发报告：双栏证据回放和证据页展示

## 1. 模块定位

本模块解决“系统为什么把两家投标人放进复核范围”这个比赛展示中的关键问题。用户从异常线索列表进入证据回放后，可以同时看到两份投标文件的投标人、文件名、页码和原文片段，并切换同一线索涉及的其他证据页。

输出仍然保持待复核边界：页面引用是辅助定位信息，最终判断必须回到原始 PDF/DOCX 文件。

## 2. 功能介绍

### 2.1 稳定线索 ID

新增公共线索 ID 生成逻辑：

```text
finding:{project_id + document_id_a + document_id_b 的稳定摘要}
```

历史结果、新上传任务结果和关系图谱使用同一规则，避免同一条线索在不同页面之间无法互相跳转。

### 2.2 证据详情接口

历史线索接口：

```text
GET /api/findings/{finding_id}
```

新项目任务接口：

```text
GET /api/projects/jobs/{job_id}/findings/{finding_id}
```

接口从对应的 `documents.jsonl` 和 `pages.jsonl` 中读取：

- A/B 投标人和文档 ID
- 原始相对路径
- 首个证据页
- 其他证据页列表
- 页码对应的原文片段
- 算法证据摘要
- 引用校验边界提示

页面文本读取使用进程内缓存，同一数据文件不会被每次点击重复解析。

### 2.3 双栏证据回放页面

前端新增独立的“证据回放”页面，包含：

- A/B 两栏投标文件
- 文件路径和页码
- 页面原文片段
- 证据页切换按钮
- 共同实体和算法证据摘要
- 已校验的大模型辅助复核说明
- 返回异常线索列表的操作

从“异常线索”页面点击“打开双栏证据回放”即可进入。历史数据和上传任务结果使用同一交互流程。

## 3. 代码变更

新增文件：

- `src/zhaocai_zhishen/finding_ids.py`
- `src/zhaocai_zhishen/evidence_replay.py`
- `tests/test_evidence_replay.py`
- `docs/module-reports/module-03-evidence-replay.md`

修改文件：

- `src/zhaocai_zhishen/analysis_results.py`
- `src/zhaocai_zhishen/evidence_graph.py`
- `src/zhaocai_zhishen/server.py`
- `src/zhaocai_zhishen/static/index.html`

## 4. 真实历史线索验证

当前本地历史结果中的唯一待复核线索 ID 为：

```text
finding:1244334f6a84
```

通过接口读取到：

- 投标人 A：上海振华重工集团机械设备服务有限公司
- 投标人 B：Terminexus Co., Ltd.
- A 证据页：第 3 页起
- B 证据页：第 3 页起
- A 首页片段字符数：1,044
- B 首页片段字符数：807
- 返回引用数：10

该验证只说明“证据可以被定位和回放”，不说明线索已经被人工确认。

## 5. 验证结果

执行命令：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q src
```

结果：

- 23 项 unittest 全部通过
- Python 源码编译检查通过
- 临时数据夹具验证了双文档、双页码、原文片段和引用列表
- 真实 HTTP 冒烟验证通过：历史线索能够返回 A/B 原文和页码
- 首页 HTML 已包含双栏回放视图和接口调用钩子

## 6. 已知限制

- 当前页面展示的是解析后的文本片段，不是 PDF 页面截图；后续可以增加原始 PDF 页面渲染，但不能影响文本引用和页码回溯。
- 当前引用列表默认最多返回双方各 8 页，避免一次性把长文档全部送到浏览器。
- 大模型辅助结果只有在任务实际启用并通过本地引用校验后才展示，不能用大模型补齐不存在的原文证据。
