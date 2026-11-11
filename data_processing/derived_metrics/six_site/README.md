# 六候选停车点补充指标

本模块读取五个上游 CSV，计算条件六点坐标、前后停车点距离及质量标记。方向与站点均为条件假设，计算依赖未来信息，仅用于离线分析。到站时间指标尚未实现。

- `main.py` 为入口，`src/` 保存计算和读写代码。
- `configs/six_site.json` 保存输入路径、路线和拟合阈值。
- `configs/reference.json` 保存基准输入哈希与结果摘要。
- `evals/` 验证导出结果，`scripts/verify_wsl.sh` 将结果写到新的临时目录。

使用 Python 3.11 或更新版本。在本模块目录执行：

```bash
python -m pip install -r requirements.txt
bash scripts/verify_wsl.sh '/mnt/d/Documents/列车自主运行codex/rail_processing'
```
