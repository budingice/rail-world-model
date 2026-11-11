# auto-train

| 项目阶段 | 目录 | 状态 |
| --- | --- | --- |
| 数据处理 / 补充指标 | `data_processing/derived_metrics/six_site/` | 六候选停车点与站距，已实现 |
| 数据处理 / 补充指标 | `data_processing/derived_metrics/arrival_time/` | 预留设计位置，到站时间指标尚未实现 |
| 速度预测 | `models/speed_prediction/` | 规划中，参考 JEPA |
| 列车行为预测 | `models/behavior_prediction/` | 规划中，参考 JEPA |

规划中的目录随实现创建。两个预测任务各自维护配置、训练入口和评估，确定有共享代码后再提取。到站时间若从完整运行记录回算，放在补充指标；若从历史观测预测未来到站时刻，则属于预测任务。

六点结果依赖未来停车点和完整运行段。用于预测实验时，可作为离线标签或分析指标。在线输入只使用预测时刻可获得的信息。

主工作区的 `rail_processing/six_site/` 保留六点模块工作副本，`rail_processing/process_six_site_model.py` 供现有实验调用。数据、实验输出和项目工作说明保留在主工作区。

## WSL 验证

使用 Python 3.11 或更新版本。在 WSL Ubuntu 的 VS Code 终端运行：

```bash
cd /mnt/d/Research/project/auto-train
source ~/.venvs/auto-train/bin/activate
module=data_processing/derived_metrics/six_site
python -m pip install -r "$module/requirements.txt"
bash "$module/scripts/verify_wsl.sh" '/mnt/d/Documents/列车自主运行codex/rail_processing'
```

## 首次本地提交

验证显示 `PASS` 后，从仓库根目录执行：

```bash
git init -b main
git add .gitattributes .gitignore README.md data_processing/derived_metrics/six_site
git diff --cached --check
git diff --cached --stat
git commit -m "Add six-site derived metrics baseline"
git status --short
git log -1 --oneline
```

Git 若提示缺少提交身份，在本仓库用 `git config user.name` 和 `git config user.email` 设置后再提交。
