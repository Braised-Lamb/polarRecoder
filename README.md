# polarRecoder

[![polar-python](https://img.shields.io/badge/polar--python-1f6feb?style=for-the-badge&logo=python&logoColor=white)](https://github.com/zHElEARN/polar-python)
[![License: MIT](https://img.shields.io/badge/license-MIT-2f855a?style=for-the-badge)](LICENSE)

基于 `polar-python` 的 Polar BLE 设备采集、回放与批量录制工具。

## 仓库中主要文件

- `polar_h10_realtime_gui.py`：带 GUI 的实时显示与回放（支持 H10 与 Verity Sense 的回放模式）。
- `polar_auto_scan_multi_recorder.py`：自动扫描并批量录制（终端模式）。
- `polar_auto_scan_recorder_config.json`、`polar_auto_scan_recorder_config.devices.json`：自动录制的配置与设备预设。
- `gui_settings.json`：运行 GUI 时记录的界面设置（首次运行后生成/更新）。
- `requirements.txt`：Python 依赖列表。
- `heartrate/` 与 `records/`：示例数据与录制输出目录。

## 功能

- 扫描并连接 Polar H10 / Polar Verity Sense
- 实时显示心率（HR）、RR/PPI、ECG/PPG（GUI 模式）
- 支持本地 CSV 回放
- 自动扫描并批量录制（命令行，保存到 `records/`）

## 环境要求

- Windows 10 / 11
- Python 3.11+
- 可用的蓝牙适配器（仅实时采集需要）

## 安装

推荐创建并激活虚拟环境，并安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 运行示例

### 实时 GUI（带可视化）

```powershell
python polar_h10_realtime_gui.py
```

可选参数（回放或筛选设备）：`--device-kind`、`--replay-dir`、`--name`、`--address` 等。

### 自动扫描并录制（终端/批量模式）

```powershell
python polar_auto_scan_multi_recorder.py --config polar_auto_scan_recorder_config.json
```

常用配置字段示例：`name`（按设备名过滤）、`address`（指定地址）、`scan_timeout`（秒）、`save_dir`（保存目录）。

## 数据与输出文件

程序会读取或生成常见的 CSV 文件，例如：

- H10: `h10_hr_rr.csv`, `h10_ecg.csv`, `h10_acc.csv`
- Verity Sense: `verity_hr_rr.csv`, `verity_ppi.csv`, `verity_ppg.csv`
- 界面配置: `gui_settings.json`
- 自动录制输出通常保存在 `records/` 目录下，按时间戳和设备分层存放。

## 常见问题

- 扫描不到设备：确认蓝牙已开启、设备已开机且没有被其他设备占用；尝试增加扫描超时。
- 连接失败：在系统蓝牙中先完成配对，或重启蓝牙适配器重试。
- 回放无数据：确认回放目录存在对应 CSV 文件，且文件名与设备类型匹配。

## 许可证

本仓库采用 MIT 许可证，详见 [LICENSE](LICENSE)。

## 致谢

本项目依赖并借鉴 `polar-python`，项目地址：https://github.com/zHElEARN/polar-python。

