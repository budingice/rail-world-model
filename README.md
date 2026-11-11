# Railway World Model

面向离线铁路视频和列车状态预测的研究项目，参考 JEPA 和 V-JEPA2。项目用于研究和仿真，禁止用于真实列车控制或安全关键系统。

| 路径 | 用途 |
| --- | --- |
| `data_processing/derived_metrics/six_site/` | 已验证的六候选停车点、前后停车距离和质量标记 |
| `data_processing/derived_metrics/arrival_time/` | 计划中的离线到站时间回算 |
| `app/`、`configs/` | 预测任务的训练入口和实验配置，目前为骨架 |
| `src/`、`evals/` | 数据集、模型、掩码、工具和评估入口，目前为骨架 |
| `docs/`、`scripts/`、`tests/` | 文档、运行脚本和自动检查 |

速度预测和列车行为预测尚未实现。后续训练入口放在 `app/`，模型放在 `src/models/`，实验配置和评估分别放在 `configs/` 和 `evals/`。根目录的训练配置含数据清单和运行目录占位符。

六点指标依赖完整运行段和未来停车点，适用于离线标签与分析。预测模型的输入限于预测时刻已知的信息。原始数据、调试代码和实验输出保存在主工作区。主工作区的 `rail_processing/six_site/` 是六点模块工作副本。

## WSL 验证六点指标

使用 Python 3.11 或更新版本。在仓库根目录运行：

```bash
source ~/.venvs/auto-train/bin/activate
module=data_processing/derived_metrics/six_site
python -m pip install -r "$module/requirements.txt"
bash "$module/scripts/verify_wsl.sh" '/mnt/d/Documents/列车自主运行codex/rail_processing'
```

脚本读取主工作区的五个上游 CSV，将结果写入新建的临时目录，并检查输入哈希、基准摘要及导出结果。
