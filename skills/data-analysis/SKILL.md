---
name: data-analysis
description: 下载公开 CSV，生成列级汇总 CSV、HTML 报告和真实浏览器截图。
enabled: true
triggers:
  - 数据分析
  - CSV
  - 数据集
  - 汇总报告
---
# 数据分析工作流

适用于用户给出 CSV 直链，并要求下载、汇总、网页报告或截图的任务。

1. 调用 `http_download` 下载用户给出的原始 URL，不要自行替换数据源。
2. 汇总步骤把 `csv_summarize` 的 `artifact_id` 写成 `$http_download.artifact_id`。平台会在该步骤执行前，把它替换成下载工具的真实返回值；不得自行编造编号。
3. 截图步骤把 `browser_preview` 的 `artifact_id` 写成 `$csv_summarize.report_artifact_id`。平台会在该步骤执行前，把它替换成汇总工具的真实 HTML 报告编号。三个调用可以按此顺序一次提交，但不能调换顺序。
4. 最终回复列出原始 CSV、列级汇总 CSV、HTML 报告和 PNG 截图的产物链接；说明总行数、列数、缺失值及是否截断。
5. 数据源来自工具实际下载结果。若任一步失败或被拒绝，明确停止并报告失败阶段，不得声称后续产物已生成。
6. 本工作流生成列级统计和 HTML 表格，不得声称生成了图表或交互功能。
