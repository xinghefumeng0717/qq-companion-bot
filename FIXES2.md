# 计费升级任务书（FIXES2）

> 一次性任务：把 LLM 计费从"平摊单价"升级为 DeepSeek V4.1 Flash（`deepseek-flash`）的真实计费模型。
> 全局约束与 FIXES.md 相同：不加新依赖（用标准库 `zoneinfo`）；完成后 `./venv/Scripts/python.exe -m unittest discover -s tests -v` 全绿；与本文档矛盾时停下报告。

## 背景：DeepSeek Flash 真实定价（元 / 百万 tokens）

| 项目 | 空闲时段 | 高峰时段 |
|---|---|---|
| 输入（缓存命中） | 0.02 | 0.04 |
| 输入（缓存未命中） | 1.0 | 2.0 |
| 输出 | 4.0 | 8.0 |

**时段规则（北京时间）**：周一至周五（不含中国法定节假日）的 9:00–12:00、14:00–18:00 为高峰；其余时间（含周末、法定节假日全天）为空闲，空闲价为高峰价的一半。

DeepSeek API 的 `usage` 字段会返回 `prompt_cache_hit_tokens` 与 `prompt_cache_miss_tokens`（两者之和等于 prompt_tokens），必须利用。

## 任务 1：配置结构（config.example.toml / config.py）

将 `[llm]` 下的 `prompt_price_per_million` / `completion_price_per_million` 两个字段**替换**为：

```toml
# DeepSeek V4.1 Flash 计费（元/百万 tokens）。时段按北京时间判定。
[llm.pricing]
cache_hit_peak = 0.04        # 输入·缓存命中·高峰
cache_hit_offpeak = 0.02     # 输入·缓存命中·空闲
cache_miss_peak = 2.0        # 输入·缓存未命中·高峰
cache_miss_offpeak = 1.0     # 输入·缓存未命中·空闲
output_peak = 8.0            # 输出·高峰
output_offpeak = 4.0         # 输出·空闲
holidays = []                # 中国法定节假日列表，格式 ["2026-10-01", ...]，留空则仅按工作日规则
```

`config.py` 的 LLM 配置 dataclass 相应改为嵌套的 Pricing 结构；峰值判定规则（周一至周五 + 9-12/14-18 两个区间）写死在代码里，holidays 从配置读取。

## 任务 2：费用计算（gateway.py）

改造 `_estimate_cost` 与 usage 采集：

1. 从 API 返回的 `usage` 中读取 `prompt_cache_hit_tokens`、`prompt_cache_miss_tokens`（缺失时兜底：全部按未命中计）；流式路径 `stream_options.include_usage` 的 usage 同样要读这两个字段；
2. 判定调用发生时刻是否高峰：按北京时间（实现采用固定 UTC+8 偏移，中国无夏令时，与 zoneinfo 等价且不依赖系统时区数据库，Windows 开发机无需 tzdata 包）；周一~周五 且 小时 ∈ [9,12) ∪ [14,18) 且 日期不在 holidays 列表 → 高峰，否则空闲；
3. 费用 = 命中数×命中价 + 未命中数×未命中价 + 输出数×输出价（各自按当次时段取价，单位换算 /1e6）；
4. `llm_calls` 表新增两列：`cache_hit_tokens INTEGER DEFAULT 0`、`cache_miss_tokens INTEGER DEFAULT 0`。db.py 建表 SQL 同步更新；对已存在的旧数据库用 `ALTER TABLE llm_calls ADD COLUMN ...` 做迁移（try/except 包住"列已存在"错误），不允许要求用户删库；
5. 流式保底估算（API 未返回 usage 时）的兜底逻辑保留，费用按未命中估算。

## 任务 3：仪表盘（admin.py `/costs`）

- 费用统计口径不变（按 purpose 分组、近 24 小时），数值来源换为新算法落库的 cost_estimate；
- 页面增加一行汇总：**缓存命中率** = Σcache_hit / Σ(cache_hit+cache_miss)，以及今日费用、累计费用。

## 任务 4：测试

新增或扩展测试（unittest，mock API 返回）覆盖：

1. 高峰工作日（如周二 10:00）命中+未命中+输出的混合计费数值正确（手算期望值断言）；
2. 同时刻改为周末/节假日（holidays 配置生效）→ 半价；
3. usage 缺失 cache 字段时全部按未命中计费；
4. 旧库（无 cache 两列的 llm_calls 表）启动迁移后不报错、新列可用。

## 交付

逐项结论 + 全量测试结果；config.example.toml 的定价注释保留"按官网更新"提示。
