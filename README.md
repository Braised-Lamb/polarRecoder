# polarRecoder

[![polar-python](https://img.shields.io/badge/polar--python-1f6feb?style=for-the-badge&logo=python&logoColor=white)](https://github.com/zHElEARN/polar-python)
[![License: MIT](https://img.shields.io/badge/license-MIT-2f855a?style=for-the-badge)](LICENSE)

[中文](#中文) | [English](#english)

<a id="中文"></a>

## 中文

基于 `polar-python` 的 Polar BLE 设备采集与可视化工具。

### 功能

- 扫描并连接 Polar H10 / Polar Verity Sense
- 实时显示 HR、RR/PPI、ECG/PPG
- 支持 CSV 回放
- 保存最近扫描结果和界面设置到 `gui_settings.json`

### 环境要求

- Windows 10 / 11
- Python 3.11+
- 可用的蓝牙适配器，且系统蓝牙已开启

### 安装

推荐先创建虚拟环境：

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

### 启动

#### 实时模式

```bash
python polar_h10_realtime_gui.py
```

#### 指定设备预设

```bash
python polar_h10_realtime_gui.py --device-kind h10 --name "Polar H10"
python polar_h10_realtime_gui.py --device-kind verity --name "Polar Sense"
python polar_h10_realtime_gui.py --device-kind verity --address AA:BB:CC:DD:EE:FF
```

#### 回放本地 CSV

```bash
python polar_h10_realtime_gui.py --device-kind h10 --replay-dir .
python polar_h10_realtime_gui.py --device-kind verity --replay-dir .
```

### 命令行参数

- `--device-kind {h10,verity}`：选择设备预设
- `--name`：按设备名过滤
- `--address`：直接指定设备地址
- `--scan-timeout`：扫描超时，单位秒
- `--window-seconds`：可视化窗口时长
- `--replay-dir`：回放 CSV 所在目录
- `--replay-speed`：回放速度倍数

### 数据文件

程序会读取或生成以下文件：

- `h10_hr_rr.csv`
- `h10_ecg.csv`
- `h10_acc.csv`
- `verity_hr_rr.csv`
- `verity_ppi.csv`
- `verity_ppg.csv`
- `gui_settings.json`

### 界面说明

- `HR` 面板：心率
- `INTERVAL` 面板：H10 显示 RR，Verity Sense 显示 PPI，支持自动 / 固定范围
- `WAVEFORM` 面板：H10 显示 ECG，Verity Sense 显示 PPG

### 常见问题

- 扫描不到设备：确认蓝牙已开启，设备已开机并可被发现，且没有被手机或其他程序占用
- 连接失败：先在系统蓝牙中完成配对，必要时重试扫描或调整扫描超时
- 回放无数据：确认回放目录中存在对应 CSV 文件，且文件名与设备类型匹配

### 许可证

本仓库采用 MIT 许可证，详见 [LICENSE](LICENSE)。

### 致谢与第三方库

本项目引用并依赖开源库 `polar-python`，用于与 Polar 设备的协议交互与数据流处理。项目地址： [zHElEARN/polar-python](https://github.com/zHElEARN/polar-python)。

`polar-python` 在 GitHub 上采用 MIT 许可证（见其仓库 [LICENSE](https://github.com/zHElEARN/polar-python/blob/main/LICENSE)）。

简短声明：`polar-python` 以 MIT 许可证发布。使用本仓库时，请同时参阅并遵守 `polar-python` 的许可证与贡献条款。

<a id="english"></a>

## English

`polarRecoder` is a Polar BLE data collection and visualization tool based on `polar-python`.

### Features

- Scan and connect to Polar H10 / Polar Verity Sense
- Real-time HR, RR/PPI, ECG/PPG visualization
- CSV replay support
- Saves recent scans and UI settings to `gui_settings.json`

### Requirements

- Windows 10 / 11
- Python 3.11+
- A working Bluetooth adapter with system Bluetooth enabled

### Installation

Create and activate a virtual environment first:

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

### Launch

#### Live mode

```bash
python polar_h10_realtime_gui.py
```

#### Select a device preset

```bash
python polar_h10_realtime_gui.py --device-kind h10 --name "Polar H10"
python polar_h10_realtime_gui.py --device-kind verity --name "Polar Sense"
python polar_h10_realtime_gui.py --device-kind verity --address AA:BB:CC:DD:EE:FF
```

#### Replay local CSV files

```bash
python polar_h10_realtime_gui.py --device-kind h10 --replay-dir .
python polar_h10_realtime_gui.py --device-kind verity --replay-dir .
```

### Command-line options

- `--device-kind {h10,verity}`: choose a device preset
- `--name`: filter by device name
- `--address`: specify the device address directly
- `--scan-timeout`: scan timeout in seconds
- `--window-seconds`: visible time window
- `--replay-dir`: directory for replay CSV files
- `--replay-speed`: replay speed multiplier

### Data files

The app reads or generates the following files:

- `h10_hr_rr.csv`
- `h10_ecg.csv`
- `h10_acc.csv`
- `verity_hr_rr.csv`
- `verity_ppi.csv`
- `verity_ppg.csv`
- `gui_settings.json`

### UI overview

- `HR` panel: heart rate
- `INTERVAL` panel: RR for H10, PPI for Verity Sense, with auto / fixed scaling
- `WAVEFORM` panel: ECG for H10, PPG for Verity Sense

### Troubleshooting

- Device not found: make sure Bluetooth is enabled, the device is awake and discoverable, and no other app is using it
- Connection failure: pair the device in system Bluetooth first, then retry scanning or adjust the scan timeout
- No replay data: confirm the replay directory contains matching CSV files for the selected device type

### License

This project is licensed under the MIT License. See [LICENSE](LICENSE).

### Acknowledgements and third-party library

This project uses `polar-python` for Polar device protocol handling and data streaming. Repository: [zHElEARN/polar-python](https://github.com/zHElEARN/polar-python).

`polar-python` is MIT licensed on GitHub (see its [LICENSE](https://github.com/zHElEARN/polar-python/blob/main/LICENSE)).

Short note: `polar-python` is released under the MIT License. Please also review and comply with its license and contribution terms when using this repository.
