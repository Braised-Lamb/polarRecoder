#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
polar_auto_scan_multi_recorder.py

Single-script multi-device Polar Sense recorder.

功能：
1. 从配置文件读取设备名关键字，例如 "polar sense"。
2. 启动时扫描 BLE 设备。
3. 自动筛选名称匹配的设备。
4. 自动取得 BLE address。
5. 按 RSSI 从强到弱选择 num_devices 个设备。
6. 使用解析出的 address 连接设备。
7. 采集 HR、PPI、PPG，并分别保存 CSV。
8. 不需要单独生成配置文件。
9. 不需要命令行指定 --num-devices。

默认配置文件：
    polar_auto_scan_recorder_config.json

运行：
    python polar_auto_scan_multi_recorder.py

指定配置：
    python polar_auto_scan_multi_recorder.py --config polar_auto_scan_recorder_config.json

配置文件示例：
{
  "name": "polar sense",
  "num_devices": 3,
  "scan_timeout": 15.0,
  "save_dir": "./records",
  "label_prefix": "polar_sense",
  "mode": "all",
  "reconnect_delay": 5.0,
  "flush_interval": 1.0,
  "queue_maxsize": 100000,
  "status_interval": 5.0,
  "ppg_sample_rate": 55,
  "ppg_resolution": 22,
  "ppg_channels": 4,
  "startup_gap": 8.0,
  "startup_grace": 90.0,
  "stall_timeout": 180.0
}

mode:
    all       采集 HR + PPI + PPG
    ppg_only 只采 PPG
    hr_only  只采 HR
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import signal
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from bleak import BleakScanner
from polar_python import PolarDevice
from polar_python.models import HRData, PPGData, PPIData


DEFAULT_CONFIG_PATH = Path("polar_auto_scan_recorder_config.json")
CACHE_SCHEMA_VERSION = 1


def first_attr(obj: Any, names: Iterable[str], default: Any = None) -> Any:
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def safe_filename(value: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", value.strip())
    return safe.strip("._") or "device"


def default_device_label(device_name: str, fallback: str) -> str:
    parts = device_name.strip().split()
    if parts:
        return safe_filename(parts[-1])
    return safe_filename(fallback)


def now_fields() -> tuple[int, str, int]:
    now = time.time()
    return int(now * 1000), datetime.fromtimestamp(now).isoformat(), time.monotonic_ns()


def parse_hr_data(data: HRData) -> tuple[Any, list[Any]]:
    hr = first_attr(data, ("heartrate", "hr", "heart_rate"), None)
    rr_list = first_attr(data, ("rr_intervals", "rr"), []) or []
    return hr, list(rr_list)


def parse_ppg_data(data: PPGData) -> tuple[Any, Any, list[list[Any]]]:
    device_time_ns = getattr(data, "timestamp", None)
    ppg_type = getattr(data, "type", None)
    samples = first_attr(data, ("data", "samples"), []) or []
    return device_time_ns, ppg_type, [list(sample) for sample in samples]


def parse_ppi_data(data: PPIData) -> list[dict[str, Any]]:
    samples = first_attr(data, ("data", "samples"), []) or []
    parsed: list[dict[str, Any]] = []
    for sample in samples:
        parsed.append(
            {
                "ppi": first_attr(sample, ("ppi",), None),
                "error_estimate": first_attr(sample, ("error_estimate",), None),
                "hr": first_attr(sample, ("hr",), None),
                "invalid_ppi": first_attr(sample, ("invalid_ppi",), None),
                "skin_contact_status": first_attr(sample, ("skin_contact_status",), None),
                "skin_contact_supported": first_attr(sample, ("skin_contact_supported",), None),
            }
        )
    return parsed


def parse_configured_devices(raw: dict[str, Any]) -> list[dict[str, str | None]]:
    devices = raw.get("devices")
    if devices is None:
        devices = raw.get("device_names")

    if devices is None:
        return []
    if not isinstance(devices, list):
        raise ValueError("devices/device_names 必须是数组")

    parsed: list[dict[str, str | None]] = []
    for index, item in enumerate(devices, start=1):
        if isinstance(item, str):
            name = item.strip()
            if name:
                parsed.append({"label": None, "name": name, "address": None})
            continue

        if not isinstance(item, dict):
            raise ValueError(f"devices 第 {index} 项必须是字符串或 object")

        name = str(item.get("name") or item.get("device_name") or "").strip()
        address = str(item.get("address") or item.get("device_address") or "").strip()
        label = str(item.get("label") or "").strip()
        if not name and not address:
            raise ValueError(f"devices 第 {index} 项至少需要 name 或 address")
        parsed.append(
            {
                "label": label or None,
                "name": name or None,
                "address": address or None,
            }
        )

    return parsed


@dataclass
class FoundDevice:
    name: str
    address: str
    rssi: int | None


@dataclass
class DeviceConfig:
    label: str
    name: str
    address: str
    rssi: int | None
    prefer_name_lookup: bool = False


@dataclass
class AppConfig:
    name: str
    num_devices: int
    scan_timeout: float
    save_dir: Path
    label_prefix: str

    enable_hr: bool
    enable_ppi: bool
    enable_ppg: bool

    reconnect_delay: float
    flush_interval: float
    queue_maxsize: int
    status_interval: float

    ppg_sample_rate: int
    ppg_resolution: int
    ppg_channels: int

    startup_gap: float
    startup_grace: float
    stall_timeout: float
    configured_devices: list[dict[str, str | None]]


@dataclass
class Stats:
    hr_batches: int = 0
    hr_rows: int = 0
    ppi_batches: int = 0
    ppi_rows: int = 0
    ppg_batches: int = 0
    ppg_rows: int = 0

    hr_last: float | None = None
    ppi_last: float | None = None
    ppg_last: float | None = None

    hr_drop: int = 0
    ppi_drop: int = 0
    ppg_drop: int = 0

    connects: int = 0
    reconnect_by_stall: int = 0
    exceptions: int = 0


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        default = {
            "name": "polar sense",
            "num_devices": 3,
            "scan_timeout": 15.0,
            "save_dir": "./records",
            "label_prefix": "polar_sense",
            "mode": "all",
            "reconnect_delay": 5.0,
            "flush_interval": 1.0,
            "queue_maxsize": 100000,
            "status_interval": 5.0,
            "ppg_sample_rate": 55,
            "ppg_resolution": 22,
            "ppg_channels": 4,
            "startup_gap": 8.0,
            "startup_grace": 90.0,
            "stall_timeout": 180.0,
        }
        path.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"默认配置文件不存在，已创建: {path}")
        print("请检查配置文件后重新运行。")
        raise SystemExit(0)

    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, dict):
        raise ValueError("配置文件必须是 JSON object")

    mode = str(raw.get("mode", "all")).strip().lower()
    if mode == "ppg_only":
        enable_hr = False
        enable_ppi = False
        enable_ppg = True
    elif mode == "hr_only":
        enable_hr = True
        enable_ppi = False
        enable_ppg = False
    else:
        enable_hr = bool(raw.get("enable_hr", True))
        enable_ppi = bool(raw.get("enable_ppi", True))
        enable_ppg = bool(raw.get("enable_ppg", True))

    configured_devices = parse_configured_devices(raw)
    num_devices = len(configured_devices) if configured_devices else int(raw.get("num_devices", 1))

    return AppConfig(
        name=str(raw.get("name", "polar sense")),
        num_devices=num_devices,
        scan_timeout=float(raw.get("scan_timeout", 15.0)),
        save_dir=Path(str(raw.get("save_dir", "./records"))).expanduser(),
        label_prefix=str(raw.get("label_prefix", "polar_sense")),
        enable_hr=enable_hr,
        enable_ppi=enable_ppi,
        enable_ppg=enable_ppg,
        reconnect_delay=float(raw.get("reconnect_delay", 5.0)),
        flush_interval=float(raw.get("flush_interval", 1.0)),
        queue_maxsize=int(raw.get("queue_maxsize", 100000)),
        status_interval=float(raw.get("status_interval", 5.0)),
        ppg_sample_rate=int(raw.get("ppg_sample_rate", 55)),
        ppg_resolution=int(raw.get("ppg_resolution", 22)),
        ppg_channels=int(raw.get("ppg_channels", 4)),
        startup_gap=float(raw.get("startup_gap", 8.0)),
        startup_grace=float(raw.get("startup_grace", 90.0)),
        stall_timeout=float(raw.get("stall_timeout", 180.0)),
        configured_devices=configured_devices,
    )


async def scan_devices_by_name(name_keyword: str, timeout: float) -> list[FoundDevice]:
    keyword = name_keyword.lower().strip()
    found: dict[str, FoundDevice] = {}

    try:
        results = await BleakScanner.discover(timeout=timeout, return_adv=True)
        for _key, value in results.items():
            if isinstance(value, tuple) and len(value) == 2:
                device, adv = value
            else:
                device, adv = value, None

            dev_name = (
                getattr(device, "name", None)
                or getattr(adv, "local_name", None)
                or ""
            )
            address = getattr(device, "address", None)
            rssi = getattr(adv, "rssi", None)
            if rssi is None:
                rssi = getattr(device, "rssi", None)

            if address and keyword in dev_name.lower():
                found[address] = FoundDevice(name=dev_name, address=address, rssi=rssi)

    except TypeError:
        devices = await BleakScanner.discover(timeout=timeout)
        for device in devices:
            dev_name = getattr(device, "name", None) or ""
            address = getattr(device, "address", None)
            rssi = getattr(device, "rssi", None)
            if address and keyword in dev_name.lower():
                found[address] = FoundDevice(name=dev_name, address=address, rssi=rssi)

    return sorted(
        found.values(),
        key=lambda x: (x.rssi if x.rssi is not None else -999, x.address),
        reverse=True,
    )


def make_device_configs(app_config: AppConfig, found_devices: list[FoundDevice]) -> list[DeviceConfig]:
    if len(found_devices) < app_config.num_devices:
        raise RuntimeError(
            f"只发现 {len(found_devices)} 个匹配设备，但配置要求 {app_config.num_devices} 个。"
            f"请确认设备已开启，或增加 scan_timeout。"
        )

    selected = found_devices[: app_config.num_devices]
    width = max(2, len(str(app_config.num_devices)))

    configs: list[DeviceConfig] = []
    for index, dev in enumerate(selected, start=1):
        fallback = f"{app_config.label_prefix}_{index:0{width}d}"
        label = default_device_label(dev.name, fallback)
        configs.append(DeviceConfig(label=label, name=dev.name, address=dev.address, rssi=dev.rssi))
    return configs


def make_configured_device_configs(app_config: AppConfig) -> list[DeviceConfig]:
    width = max(2, len(str(app_config.num_devices)))
    configs: list[DeviceConfig] = []
    for index, item in enumerate(app_config.configured_devices, start=1):
        fallback = f"{app_config.label_prefix}_{index:0{width}d}"
        name = item.get("name") or app_config.name
        label = item.get("label") or default_device_label(name, fallback)
        address = item.get("address") or ""
        configs.append(
            DeviceConfig(
                label=label,
                name=name,
                address=address,
                rssi=None,
                prefer_name_lookup=bool(item.get("name")),
            )
        )
    return configs


def device_cache_path(config_path: Path) -> Path:
    return config_path.with_name(f"{config_path.stem}.devices.json")


def load_device_cache(path: Path, app_config: AppConfig) -> list[dict[str, str | None]]:
    if not path.exists():
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 读取设备缓存失败，忽略: {exc}", flush=True)
        return []

    if not isinstance(raw, dict):
        return []

    devices = raw.get("devices")
    if not isinstance(devices, list):
        return []

    try:
        cached_num_devices = int(raw.get("num_devices", 0))
    except (TypeError, ValueError):
        return []
    if cached_num_devices != app_config.num_devices:
        return []

    cached_keyword = str(raw.get("name_keyword") or "").strip()
    if cached_keyword and cached_keyword != app_config.name:
        return []

    cached: list[dict[str, str | None]] = []
    for index, item in enumerate(devices, start=1):
        if not isinstance(item, dict):
            return []

        name = str(item.get("name") or "").strip()
        address = str(item.get("address") or "").strip()
        label = str(item.get("label") or "").strip()
        if not name:
            return []

        cached.append(
            {
                "label": label or None,
                "name": name,
                "address": address or None,
            }
        )

    if len(cached) != app_config.num_devices:
        return []

    return cached


def save_device_cache(path: Path, app_config: AppConfig, device_configs: list[DeviceConfig]) -> None:
    payload = {
        "version": CACHE_SCHEMA_VERSION,
        "name_keyword": app_config.name,
        "num_devices": app_config.num_devices,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "devices": [
            {
                "label": device.label,
                "name": device.name,
                "address": device.address,
            }
            for device in device_configs
        ],
    }

    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 已保存设备缓存: {path}", flush=True)
    except OSError as exc:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 保存设备缓存失败: {exc}", flush=True)


async def resolve_cached_device_configs(
    app_config: AppConfig,
    cached_devices: list[dict[str, str | None]],
) -> list[DeviceConfig]:
    width = max(2, len(str(app_config.num_devices)))
    resolved: list[DeviceConfig] = []
    used_addresses: set[str] = set()
    found_by_name: dict[str, list[FoundDevice]] = {}

    for cached in cached_devices:
        name = cached.get("name") or ""
        if not name:
            return []
        if name not in found_by_name:
            found_by_name[name] = await scan_devices_by_name(name, app_config.scan_timeout)

    for index, cached in enumerate(cached_devices, start=1):
        name = cached.get("name") or ""
        if not name:
            return []

        cached_address = cached.get("address")
        found = found_by_name.get(name, [])
        selected = None
        if cached_address:
            selected = next(
                (
                    device
                    for device in found
                    if device.address == cached_address and device.address not in used_addresses
                ),
                None,
            )
        if selected is None:
            selected = next(
                (
                    device
                    for device in found
                    if device.name == name and device.address not in used_addresses
                ),
                None,
            )
        if selected is None:
            selected = next((device for device in found if device.address not in used_addresses), None)
        if selected is None or selected.address in used_addresses:
            return []

        used_addresses.add(selected.address)
        label = cached.get("label") or f"{app_config.label_prefix}_{index:0{width}d}"
        resolved.append(
            DeviceConfig(
                label=label,
                name=selected.name,
                address=selected.address,
                rssi=selected.rssi,
                prefer_name_lookup=False,
            )
        )

    if len(resolved) != app_config.num_devices:
        return []

    return resolved


class CsvWriter:
    def __init__(self, output_dir: Path, flush_interval: float, queue_maxsize: int):
        self.output_dir = output_dir
        self.flush_interval = flush_interval
        self.hr_queue: asyncio.Queue[list[Any]] = asyncio.Queue(maxsize=queue_maxsize)
        self.ppi_queue: asyncio.Queue[list[Any]] = asyncio.Queue(maxsize=queue_maxsize)
        self.ppg_queue: asyncio.Queue[list[Any]] = asyncio.Queue(maxsize=queue_maxsize)

    def open_csv(self, filename: str, header: list[str]):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        handle = (self.output_dir / filename).open("w", encoding="utf-8", newline="")
        writer = csv.writer(handle)
        writer.writerow(header)
        handle.flush()
        return writer, handle

    async def write_loop(
        self,
        queue: asyncio.Queue[list[Any]],
        filename: str,
        header: list[str],
        stop_event: asyncio.Event,
    ):
        writer, handle = self.open_csv(filename, header)
        pending = 0
        last_flush = time.monotonic()

        try:
            while not stop_event.is_set():
                timeout = max(0.05, self.flush_interval - (time.monotonic() - last_flush))
                try:
                    row = await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    row = None

                if row is not None:
                    writer.writerow(row)
                    pending += 1

                if pending and (pending >= 2000 or time.monotonic() - last_flush >= self.flush_interval):
                    handle.flush()
                    pending = 0
                    last_flush = time.monotonic()

            while True:
                try:
                    row = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                writer.writerow(row)

            handle.flush()
        finally:
            handle.flush()
            handle.close()

    async def run(self, stop_event: asyncio.Event):
        await asyncio.gather(
            self.write_loop(
                self.hr_queue,
                "verity_hr_rr.csv",
                ["unixTimeStamp_ms", "dateTime", "pc_time_ns", "hr_bpm", "rr_index", "rr_ms"],
                stop_event,
            ),
            self.write_loop(
                self.ppi_queue,
                "verity_ppi.csv",
                [
                    "unixTimeStamp_ms",
                    "dateTime",
                    "pc_time_ns",
                    "ppi_index",
                    "ppi_ms",
                    "error_estimate_ms",
                    "hr_bpm",
                    "invalid_ppi",
                    "skin_contact_status",
                    "skin_contact_supported",
                ],
                stop_event,
            ),
            self.write_loop(
                self.ppg_queue,
                "verity_ppg.csv",
                [
                    "unixTimeStamp_ms",
                    "dateTime",
                    "pc_time_ns",
                    "device_time_ns",
                    "sample_index",
                    "ch1",
                    "ch2",
                    "ch3",
                    "ch4",
                    "ppg_type",
                ],
                stop_event,
            ),
        )


class DeviceRecorder:
    def __init__(
        self,
        device_config: DeviceConfig,
        app_config: AppConfig,
        session_dir: Path,
        global_stop: asyncio.Event,
        stream_locks: dict[str, asyncio.Lock],
    ):
        self.device_config = device_config
        self.app_config = app_config
        self.global_stop = global_stop
        self.output_dir = session_dir / safe_filename(device_config.label)
        self.writer = CsvWriter(self.output_dir, app_config.flush_interval, app_config.queue_maxsize)
        self.stats = Stats()
        self.stream_locks = stream_locks
        self.connection_started_at: float | None = None

    def log(self, message: str):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [{self.device_config.label}] {message}", flush=True)

    def put_row(self, queue: asyncio.Queue, row: list[Any], stream: str):
        try:
            queue.put_nowait(row)
        except asyncio.QueueFull:
            if stream == "hr":
                self.stats.hr_drop += 1
            elif stream == "ppi":
                self.stats.ppi_drop += 1
            elif stream == "ppg":
                self.stats.ppg_drop += 1

    def hr_callback(self, data: HRData):
        unix_ms, dt_str, pc_time_ns = now_fields()
        hr, rr_list = parse_hr_data(data)
        self.stats.hr_batches += 1
        self.stats.hr_last = time.monotonic()

        if rr_list:
            for i, rr in enumerate(rr_list):
                self.put_row(self.writer.hr_queue, [unix_ms, dt_str, pc_time_ns, hr, i, rr], "hr")
                self.stats.hr_rows += 1
        else:
            self.put_row(self.writer.hr_queue, [unix_ms, dt_str, pc_time_ns, hr, "", ""], "hr")
            self.stats.hr_rows += 1

    def ppi_callback(self, data: PPIData):
        unix_ms, dt_str, pc_time_ns = now_fields()
        samples = parse_ppi_data(data)
        if not samples:
            return

        self.stats.ppi_batches += 1
        self.stats.ppi_last = time.monotonic()

        for i, sample in enumerate(samples):
            self.put_row(
                self.writer.ppi_queue,
                [
                    unix_ms,
                    dt_str,
                    pc_time_ns,
                    i,
                    sample["ppi"],
                    sample["error_estimate"],
                    sample["hr"],
                    sample["invalid_ppi"],
                    sample["skin_contact_status"],
                    sample["skin_contact_supported"],
                ],
                "ppi",
            )
            self.stats.ppi_rows += 1

    def ppg_callback(self, data: PPGData):
        unix_ms, dt_str, pc_time_ns = now_fields()
        device_time_ns, ppg_type, samples = parse_ppg_data(data)
        if not samples:
            return

        self.stats.ppg_batches += 1
        self.stats.ppg_last = time.monotonic()

        for i, sample in enumerate(samples):
            padded = list(sample[:4]) + [None] * max(0, 4 - len(sample))
            self.put_row(
                self.writer.ppg_queue,
                [
                    unix_ms,
                    dt_str,
                    pc_time_ns,
                    device_time_ns,
                    i,
                    padded[0],
                    padded[1],
                    padded[2],
                    padded[3],
                    ppg_type,
                ],
                "ppg",
            )
            self.stats.ppg_rows += 1

    def enabled_streams(self) -> list[tuple[str, float | None]]:
        streams: list[tuple[str, float | None]] = []
        if self.app_config.enable_hr:
            streams.append(("HR", self.stats.hr_last))
        if self.app_config.enable_ppi:
            streams.append(("PPI", self.stats.ppi_last))
        if self.app_config.enable_ppg:
            streams.append(("PPG", self.stats.ppg_last))
        return streams

    def has_stream_stalled(self) -> bool:
        if self.connection_started_at is None:
            return False

        now = time.monotonic()
        if now - self.connection_started_at < self.app_config.startup_grace:
            return False

        for name, last in self.enabled_streams():
            if last is None:
                self.log(f"{name} 在宽限期后仍无数据，准备重连")
                self.stats.reconnect_by_stall += 1
                return True

            age = now - last
            if age > self.app_config.stall_timeout:
                self.log(f"{name} 超过 {self.app_config.stall_timeout:g}s 无数据，last={age:.1f}s，准备重连")
                self.stats.reconnect_by_stall += 1
                return True

        return False

    @staticmethod
    def polar_device_is_connected(polar_device: PolarDevice) -> bool:
        client = getattr(polar_device, "_client", None)
        if client is None:
            return True

        is_connected = getattr(client, "is_connected", True)
        if callable(is_connected):
            is_connected = is_connected()
        return bool(is_connected)

    async def sleep_or_stop(self, delay: float):
        deadline = time.monotonic() + delay
        while not self.global_stop.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    async def keep_alive(self, polar_device: PolarDevice):
        while not self.global_stop.is_set():
            await asyncio.sleep(1.0)
            if not self.polar_device_is_connected(polar_device):
                self.log("检测到 BLE 已断开，准备重连")
                break
            if self.has_stream_stalled():
                break

    async def start_streams(self, polar_device: PolarDevice):
        if self.app_config.enable_hr:
            async with self.stream_locks["hr"]:
                self.log("启动 HR stream")
                await polar_device.start_hr_stream(hr_callback=self.hr_callback)
                self.log("HR stream 已订阅")
                await asyncio.sleep(self.app_config.startup_gap)

        if self.app_config.enable_ppi:
            async with self.stream_locks["ppi"]:
                self.log("启动 PPI stream")
                await polar_device.start_ppi_stream(ppi_callback=self.ppi_callback)
                self.log("PPI stream 已订阅")
                await asyncio.sleep(self.app_config.startup_gap)

        if self.app_config.enable_ppg:
            async with self.stream_locks["ppg"]:
                self.log(
                    f"启动 PPG stream: sample_rate={self.app_config.ppg_sample_rate}, "
                    f"resolution={self.app_config.ppg_resolution}, channels={self.app_config.ppg_channels}"
                )
                await polar_device.start_ppg_stream(
                    ppg_callback=self.ppg_callback,
                    sample_rate=int(self.app_config.ppg_sample_rate),
                    resolution=int(self.app_config.ppg_resolution),
                    channels=int(self.app_config.ppg_channels),
                )
                self.log("PPG stream 已订阅")
                await asyncio.sleep(self.app_config.startup_gap)

    async def find_device_for_connection(self):
        if self.device_config.prefer_name_lookup and self.device_config.name:
            found = await scan_devices_by_name(self.device_config.name, self.app_config.scan_timeout)
            if found:
                selected = found[0]
                if selected.address != self.device_config.address:
                    self.log(f"按名称找到设备，更新地址: {self.device_config.address or '未记录'} -> {selected.address}")
                    self.device_config.address = selected.address
                self.device_config.name = selected.name
                self.device_config.rssi = selected.rssi
                return await BleakScanner.find_device_by_address(selected.address, timeout=self.app_config.scan_timeout)
            self.log(f"未找到名称 {self.device_config.name!r}，继续等待")
            return None

        if self.device_config.address:
            device = await BleakScanner.find_device_by_address(
                self.device_config.address,
                timeout=self.app_config.scan_timeout,
            )
            if device is not None:
                return device

            self.log(f"未找到旧地址 {self.device_config.address}，改用名称重新扫描")

        found = await scan_devices_by_name(self.device_config.name or self.app_config.name, self.app_config.scan_timeout)
        if not found:
            found = await scan_devices_by_name(self.app_config.name, self.app_config.scan_timeout)
        if not found:
            return None

        selected = found[0]
        if selected.address != self.device_config.address:
            self.log(f"更新设备地址: {self.device_config.address} -> {selected.address}")
            self.device_config.address = selected.address
        self.device_config.name = selected.name
        self.device_config.rssi = selected.rssi
        return await BleakScanner.find_device_by_address(selected.address, timeout=self.app_config.scan_timeout)

    async def connect_once(self):
        self.log(f"正在连接: name={self.device_config.name!r}, address={self.device_config.address}, rssi={self.device_config.rssi}")

        device = await self.find_device_for_connection()

        if device is None:
            self.log(f"未找到设备，{self.app_config.reconnect_delay:g}s 后重试")
            return

        async with PolarDevice(device) as polar_device:
            self.stats.connects += 1
            self.connection_started_at = time.monotonic()
            if self.app_config.enable_hr:
                self.stats.hr_last = None
            if self.app_config.enable_ppi:
                self.stats.ppi_last = None
            if self.app_config.enable_ppg:
                self.stats.ppg_last = None
            self.log("连接成功")

            await self.start_streams(polar_device)

            self.log("开始保持连接")
            await self.keep_alive(polar_device)

    async def run(self):
        self.log(f"输出目录: {self.output_dir}")
        writer_task = asyncio.create_task(self.writer.run(self.global_stop))

        try:
            while not self.global_stop.is_set():
                try:
                    await self.connect_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.stats.exceptions += 1
                    self.log(f"连接或采集中断: {type(exc).__name__}: {exc}")

                if not self.global_stop.is_set():
                    self.log(f"{self.app_config.reconnect_delay:g}s 后重新连接")
                    await self.sleep_or_stop(self.app_config.reconnect_delay)
        finally:
            writer_task.cancel()
            await asyncio.gather(writer_task, return_exceptions=True)

    def status_text(self) -> str:
        def age(last: float | None) -> str:
            if last is None:
                return "None"
            return f"{time.monotonic() - last:.1f}s"

        return (
            f"HR batches={self.stats.hr_batches}, rows={self.stats.hr_rows}, last={age(self.stats.hr_last)}, drop={self.stats.hr_drop}; "
            f"PPI batches={self.stats.ppi_batches}, rows={self.stats.ppi_rows}, last={age(self.stats.ppi_last)}, drop={self.stats.ppi_drop}; "
            f"PPG batches={self.stats.ppg_batches}, rows={self.stats.ppg_rows}, last={age(self.stats.ppg_last)}, drop={self.stats.ppg_drop}; "
            f"connects={self.stats.connects}, stall_reconnects={self.stats.reconnect_by_stall}, exceptions={self.stats.exceptions}"
        )


async def status_loop(recorders: list[DeviceRecorder], stop_event: asyncio.Event, interval: float):
    while not stop_event.is_set():
        await asyncio.sleep(interval)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ===== 全局状态 =====", flush=True)
        for recorder in recorders:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [{recorder.device_config.label}] {recorder.status_text()}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Auto scan and record Polar Sense devices")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="配置文件路径")
    return parser


async def async_main():
    args = build_parser().parse_args()
    app_config = load_config(args.config)
    cache_path = device_cache_path(args.config)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 配置文件: {args.config}", flush=True)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 设备名关键字: {app_config.name!r}", flush=True)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 目标设备数量: {app_config.num_devices}", flush=True)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 扫描时长: {app_config.scan_timeout:g}s", flush=True)

    if app_config.configured_devices:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 使用配置文件中的明确设备列表", flush=True)
        device_configs = make_configured_device_configs(app_config)
        found: list[FoundDevice] = []
    else:
        cached_devices = load_device_cache(cache_path, app_config)
        device_configs = []
        if cached_devices:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 发现设备缓存，先按缓存设备名检索: {cache_path}", flush=True)
            device_configs = await resolve_cached_device_configs(app_config, cached_devices)
            if len(device_configs) == app_config.num_devices:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 缓存设备名检索成功，直接使用这些设备连接采集", flush=True)
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 缓存设备名检索数量不匹配，改为按关键字全量扫描", flush=True)

        if device_configs:
            found = [
                FoundDevice(name=device.name, address=device.address, rssi=device.rssi)
                for device in device_configs
            ]
        else:
            found = await scan_devices_by_name(app_config.name, app_config.scan_timeout)

            print(f"[{datetime.now().strftime('%H:%M:%S')}] 匹配设备数量: {len(found)}", flush=True)
            for i, dev in enumerate(found, start=1):
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] 匹配 {i}: "
                    f"name={dev.name!r}, address={dev.address}, rssi={dev.rssi}",
                    flush=True,
                )

            device_configs = make_device_configs(app_config, found)
            save_device_cache(cache_path, app_config, device_configs)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 选中设备:", flush=True)
    for dev in device_configs:
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] {dev.label}: "
            f"name={dev.name!r}, address={dev.address}, rssi={dev.rssi}",
            flush=True,
        )

    session_dir = app_config.save_dir / datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir.mkdir(parents=True, exist_ok=True)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 保存目录: {session_dir}", flush=True)
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] 启用流: "
        f"HR={app_config.enable_hr}, PPI={app_config.enable_ppi}, PPG={app_config.enable_ppg}",
        flush=True,
    )

    stop_event = asyncio.Event()

    stream_locks = {
        "hr": asyncio.Lock(),
        "ppi": asyncio.Lock(),
        "ppg": asyncio.Lock(),
    }

    loop = asyncio.get_running_loop()

    def request_stop():
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 收到停止信号", flush=True)
        stop_event.set()

    try:
        loop.add_signal_handler(signal.SIGINT, request_stop)
        loop.add_signal_handler(signal.SIGTERM, request_stop)
    except NotImplementedError:
        pass

    recorders = [
        DeviceRecorder(
            device_config=device,
            app_config=app_config,
            session_dir=session_dir,
            global_stop=stop_event,
            stream_locks=stream_locks,
        )
        for device in device_configs
    ]

    tasks = [asyncio.create_task(recorder.run()) for recorder in recorders]
    tasks.append(asyncio.create_task(status_loop(recorders, stop_event, app_config.status_interval)))

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        stop_event.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 已停止", flush=True)


def main():
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
