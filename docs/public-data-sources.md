# 公开训练数据来源

## 目标

公开训练集用于训练和验证通用文件解析、字段抽取、公告分类、风险特征构造能力。公司内部招投标文件只作为测试集和业务适配验证，不参与公开训练集上传。

## 首选来源

### 中国政府采购网

- 地址：https://www.ccgp.gov.cn/
- 用途：公开招标公告、中标公告、部分附件链接、采购人、地区、发布时间等元数据。
- 优点：全国统一入口，公告格式相对稳定，适合做文本解析与元数据抽取训练。
- 限制：并非每条公告都有可直接下载的招标文件或投标文件；真实投标文件通常不会公开。

### 全国公共资源交易平台

- 地址：https://www.ggzy.gov.cn/
- 用途：交易公告、成交公示、交易诚信、数据服务入口。
- 优点：覆盖工程建设、政府采购等多类型公共资源交易。
- 限制：各地平台落地页面差异较大，附件下载和字段结构不完全统一。

## 训练集定位

公开数据不能替代公司内部测试集。合理分工如下：

- 公开数据：训练通用解析能力、公告/中标信息抽取、字段标准化、项目级数据组织。
- 公司数据：测试投标文件解析、报价表抽取、企业信息抽取、偏离表抽取和风险评分效果。

## 当前采集器

初版采集器：

```powershell
python -m zhaocai_zhishen.collectors.ccgp --output data/public_training/ccgp --pages 1 --max-notices 30
```

输出：

```text
data/public_training/ccgp/
  notices.csv
  notices.jsonl
  raw_html/
```

字段：

- title
- url
- published_at
- region
- purchaser
- source_list
- html_path
- attachments

## 试采集结论

已完成小规模联网试采集验证。中国政府采购网公开招标公告列表可稳定解析以下字段：

- 公告标题
- 公告 URL
- 发布时间
- 地域
- 采购人
- 详情页 HTML
- 公开附件链接

需要注意：

- 并非每条公告都包含可直接下载的招标文件附件。
- 部分附件位于地方政府采购平台，例如广东政府采购智慧云平台。
- 公开公告通常不包含投标文件本体，因此公开数据更适合作为“公告/招标文件解析训练集”，公司内部投标文件仍应用作“投标文件解析和风险识别测试集”。

## 合规要求

- 只采集公开页面和公开附件链接。
- 控制请求频率，避免高频抓取。
- 不绕过登录、验证码、下载限制或反爬机制。
- 不将公司内部数据、联系人、电话、账号等敏感信息上传到公开仓库。

## 标准化输出

采集后的 `notices.jsonl` 可转换为标准训练样本表：

```powershell
python -m zhaocai_zhishen.datasets.prepare_public_notices `
  --input data/public_training/ccgp/notices.jsonl `
  --output data/public_training/ccgp_prepared
```

输出：

```text
public_notice_samples.csv
public_notice_samples.jsonl
summary.json
```

样本字段包括：

- sample_id
- source
- task_type
- title
- url
- published_at
- region
- purchaser
- category
- has_attachment
- has_tender_file
- attachment_count
- attachment_names
- html_path
- text_excerpt
- label_level
- label_source
- label_reason

## 公开附件下载

如公告中包含公开附件，可下载招标/采购文件类附件作为公开训练样本：

```powershell
python -m zhaocai_zhishen.datasets.download_public_attachments `
  --input data/public_training/ccgp/notices.jsonl `
  --output data/public_training/ccgp_attachments `
  --max-files 20 `
  --delay 1
```

默认只下载疑似招标文件、采购文件、资格声明、投标格式等附件。使用 `--all` 可下载所有支持类型附件。

输出：

```text
attachment_manifest.csv
attachment_manifest.json
公告目录/
  附件文件
```

## 附件解包与索引

下载后的公开附件可进一步解包和索引：

```powershell
python -m zhaocai_zhishen.datasets.index_public_attachments `
  --input-dir data/public_training/ccgp_attachments `
  --manifest data/public_training/ccgp_attachments/attachment_manifest.json `
  --output data/public_training/ccgp_file_index
```

输出：

```text
file_index.csv
file_index.json
summary.json
extracted/
```

其中 `file_index` 会记录文件路径、扩展名、大小、是否文档、是否压缩包、来源公告和来源附件。

## 招标文件字段解析

可对公开附件中的 `.docx` 和文字型 `.pdf` 做基础字段解析：

```powershell
python -m zhaocai_zhishen.datasets.parse_tender_files `
  --file-index data/public_training/ccgp_file_index/file_index.csv `
  --attachment-root data/public_training/ccgp_attachments `
  --extracted-root data/public_training/ccgp_file_index/extracted `
  --output data/public_training/ccgp_tender_parsed
```

输出：

```text
parsed_tender_files.csv
parsed_tender_files.jsonl
summary.json
```

当前抽取字段：

- 项目名称
- 项目编号/采购编号/招标编号
- 采购人/招标人
- 预算金额
- 最高限价
- 投标截止时间/开标时间
- 是否包含评分办法
- 是否包含资格要求
- 是否包含合同条款
- 是否包含投标文件格式
- 是否包含采购需求
