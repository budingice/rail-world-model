# Railway World Model

面向离线铁路视频与列车状态预测的研究项目。逐圈数据处理入口位于 `data_processing/exports/speed_circle/`。

## 在 WSL 复现逐圈 13 列数据

在原生 WSL Python 3.12 环境和仓库根目录运行。输出目录使用尚不存在的仓库外路径。

```bash
module=data_processing/exports/speed_circle
python -m pip install -r "$module/requirements.txt"
bash "$module/scripts/verify_wsl.sh" \
  '/mnt/d/Code/AutoTrain/Dataset/地铁环线运行数据' \
  '/mnt/d/Documents/列车自主运行codex/experiments/stable_speed_circle_import/wsl-output'
```

入口从五份 Excel 重建秒级观测、运行链和停车距离。结果包含 109 份逐圈 CSV、8 份未配对段 CSV、圈段和逐秒索引。去回程分别从 0 秒和 0 米开始。无观测秒仅保留时间。限速和目标速度暂按原值为 dm/s 换算。验证逐文件对照 117 份已审核 SHA-256。

参考站距与 R050 两站测试确认位于模块的 `source/`。原始 Excel 和运行结果由仓库外目录提供或保存。
