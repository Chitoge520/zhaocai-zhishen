# 模块四开发报告：报告导出与现场交付

## 1. 模块定位

本模块把本地无监督分析、异常关系图谱、证据页和大模型辅助结果汇总为可交付的项目报告。报告只呈现可追溯的异常线索和复核入口，不把模型分数直接表述为串标、围标或违法违规结论。

比赛演示链路为：

```text
项目总览 -> 异常线索 -> 关系图谱 -> 双栏证据回放 -> 导出 DOCX / HTML 打印版
```

## 2. 已完成内容

### 2.1 报告数据组装

新增 `src/zhaocai_zhishen/reporting.py`，提供三个核心函数：

- `build_report_payload`：读取分析结果、处理统计、训练统计、冻结模型和图谱数据，组装统一报告对象。
- `render_html_report`：生成带打印样式的 HTML 报告。
- `build_docx_report`：使用 `python-docx` 生成可编辑的 Word 报告。

报告包含：

- 分析范围、项目数、投标文件数、解析页数和比较数量；
- 关系图谱节点和关系摘要；
- 异常线索 ID、投标人双方、异常分数、文本相似度、证据摘要和证据页码；
- 冻结模型类型、无监督标签状态和训练比较数；
- 大模型调用状态及本地引用校验数量；
- 人工复核建议和结论边界。

### 2.2 服务端下载接口

修改 `src/zhaocai_zhishen/server.py`，新增：

```text
GET /api/report.html
GET /api/report.docx
GET /api/projects/jobs/{job_id}/report.html
GET /api/projects/jobs/{job_id}/report.docx
```

历史项目读取 `data/analysis`、`data/processed`、`data/training_internal` 和 `data/models`。新上传任务读取对应任务目录，并复用冻结模型目录。

响应层增加以下保护：

- 明确的 `Content-Type`；
- `Content-Length`；
- UTF-8 文件名的 `Content-Disposition`；
- `X-Content-Type-Options: nosniff`。

### 2.3 前端报告页面

报告页面由禁用占位按钮改为真实操作：

- `导出 DOCX`：下载 Word 报告；
- `打开 HTML 打印版`：打开可打印 HTML，可使用浏览器打印为 PDF；
- 页面会根据当前状态自动选择历史报告或当前已完成任务报告；
- 没有加载分析结果时给出明确提示。

当前没有引入 PDF 生成依赖，因此前端不宣称系统已经直接生成 PDF，而是提供稳定的 HTML 打印版。

## 3. 数据与隐私边界

- 报告只读取本地分析产物，不把完整 ZIP 或完整原始正文发送给大模型。
- 大模型状态只记录是否启用、候选线索数量和引用校验结果，不写入 API 密钥。
- 报告中的证据页码用于回到原始文件定位，最终判断仍需人工复核。
- 所有输出均使用“异常线索”“待复核”“辅助解释”等表述。

## 4. 测试与验证

### 自动化测试

```text
24 项 unittest 全部通过
src 全量 compileall 通过
```

新增测试文件：

```text
tests/test_reporting.py
```

测试覆盖：

- HTML 报告包含线索、证据和结论边界；
- DOCX 报告可以被 `python-docx` 重新读取；
- 报告包含共同实体证据。

### 真实 HTTP 验证

```text
/api/report.html  -> 200
Content-Type      -> text/html; charset=utf-8
Content-Disposition -> inline; UTF-8 文件名

/api/report.docx  -> 200
Content-Type      -> application/vnd.openxmlformats-officedocument.wordprocessingml.document
文件大小          -> 38,257 bytes 左右
```

### 视觉验证状态

已按文档生成技能调用 `render_docx.py`。当前 Windows 环境未发现 LibreOffice 或 `soffice`，渲染器因缺少转换程序退出，无法生成页面 PNG。因此本模块的 DOCX 已完成结构读取验证，但尚未完成 LibreOffice 页面级视觉 QA。

## 5. 比赛现场价值

该模块把“模型算出了一个分数”转换成“评委可以现场点击、查看证据、导出材料”的闭环。演示时可以从异常线索进入关系图谱和双方证据页，再生成正式报告，直观展示系统的可解释性、可审计性和交付能力。

## 6. 后续改进

- 有条件时安装 LibreOffice，完成 DOCX 页面级渲染检查；
- 根据比赛模板增加单位 Logo、封面、目录和页眉页脚；
- 对新上传任务的报告增加原始压缩包名、处理时间和失败步骤摘要；
- 在报告中增加“排除线索”和“人工复核结论”的后续登记入口。

