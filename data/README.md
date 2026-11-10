# 模拟数据

所有数据均由 `generate_data.py` 以固定随机种子生成，不包含任何真实个人健康信息。

## 文件

- `sample_health_data.csv`：35 天干净数据，供 Demo 使用。
- `fixtures/`：15 组评估与测试夹具：
  - `clean_30d.csv` / `clean_35d.csv`：正常数据；
  - `unsorted.csv`：行顺序打乱；
  - `flat_sleep.csv`：睡眠基本稳定；
  - `variable_30d.csv`：波动较大但合法；
  - `missing_sleep.csv`：睡眠列存在空值；
  - `missing_exercise_column.csv`：缺少整列；
  - `negative_values.csv`：负数；
  - `duplicate_dates.csv`：重复日期；
  - `invalid_date.csv`：非法日期；
  - `non_numeric.csv`：非数字；
  - `insufficient_7d.csv` / `insufficient_12d.csv`：天数不足；
  - `gappy_30d.csv`：跨度 30 天但有日期缺口；
  - `empty.csv`：只有表头。

## 重新生成

```bash
python data/generate_data.py
```

固定结束日期为 `2026-11-09`，输出确定，便于重复评估。
