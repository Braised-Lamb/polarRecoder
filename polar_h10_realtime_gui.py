#!/usr/bin/env python3
"""polar_h10_realtime_gui.py

Real-time Polar dashboard for H10 and Verity Sense.

Features:
- Live streaming from a Polar H10 via `polar-python`
- Live streaming from a Polar Verity Sense via `polar-python`
- Smooth plotting with `PySide6` + `pyqtgraph`
- Optional CSV replay mode for recorded HR, waveform, and interval data

Examples:
  python polar_h10_realtime_gui.py --device-kind h10 --address A0:9E:1A:EA:3E:21
  python polar_h10_realtime_gui.py --device-kind verity --replay-dir .
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bleak import BleakScanner
from polar_python import PolarDevice
from polar_python.models import ECGData, HRData, PPGData, PPIData

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QDoubleSpinBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
import pyqtgraph as pg


pg.setConfigOptions(antialias=True, background="#0b1020", foreground="#d8e1ff")


DEVICE_PRESETS = {
    "h10": {
        "display_name": "Polar H10",
        "default_name": "Polar H10",
        "hr_file": "h10_hr_rr.csv",
        "waveform_file": "h10_ecg.csv",
        "interval_file": None,
        "interval_label": "RR",
        "waveform_label": "ECG",
        "waveform_unit": "raw",
        "waveform_channels": 1,
        "waveform_rate_hz": 130.0,
        "waveform_resolution": 14,
        "stream_kind": "ecg",
    },
    "verity": {
        "display_name": "Polar Verity Sense",
        "default_name": "Polar Verity Sense",
        "hr_file": "verity_hr_rr.csv",
        "waveform_file": "verity_ppg.csv",
        "interval_file": "verity_ppi.csv",
        "interval_label": "PPI",
        "waveform_label": "PPG",
        "waveform_unit": "raw",
        "waveform_channels": 4,
        "waveform_rate_hz": 55.0,
        "waveform_resolution": 22,
        "stream_kind": "ppg",
    },
}


class TimeAxisItem(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        labels = []
        for value in values:
            labels.append(datetime.fromtimestamp(value).strftime("%H:%M:%S"))
        return labels


def _first_attr(obj, names, default=None):
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def parse_hr_data(data: HRData):
    hr = _first_attr(data, ("heartrate", "hr", "heart_rate"), None)
    rr_list = _first_attr(data, ("rr_intervals", "rr"), []) or []
    return hr, list(rr_list)


def parse_ecg_data(data: ECGData):
    samples = _first_attr(data, ("data", "samples"), []) or []
    return list(samples)


def parse_ppg_data(data: PPGData):
    samples = _first_attr(data, ("data", "samples"), []) or []
    return [list(sample) for sample in samples]


def parse_ppi_data(data: PPIData):
    samples = _first_attr(data, ("data", "samples"), []) or []
    parsed = []
    for sample in samples:
        parsed.append(
            {
                "ppi": _first_attr(sample, ("ppi",), None),
                "error_estimate": _first_attr(sample, ("error_estimate",), None),
                "hr": _first_attr(sample, ("hr",), None),
                "invalid_ppi": _first_attr(sample, ("invalid_ppi",), None),
                "skin_contact_status": _first_attr(sample, ("skin_contact_status",), None),
                "skin_contact_supported": _first_attr(sample, ("skin_contact_supported",), None),
            }
        )
    return parsed


@dataclass
class ReplayPoint:
    kind: str
    t_sec: float
    value: object


def _find_existing_csv(replay_dir: Path, candidates: list[str]) -> Path | None:
    for name in candidates:
        path = replay_dir / name
        if path.exists():
            return path
    return None


def _read_csv_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def _row_time_seconds(row: dict[str, str], has_timestamp_cols: bool) -> float:
    if has_timestamp_cols and row.get("unixTimeStamp_ms"):
        return int(row["unixTimeStamp_ms"]) / 1000.0
    return int(row["pc_time_ns"]) / 1_000_000_000.0


def load_replay_points(replay_dir: Path, device_key: str) -> list[ReplayPoint]:
    spec = DEVICE_PRESETS[device_key]
    points: list[ReplayPoint] = []

    hr_path = _find_existing_csv(replay_dir, [spec["hr_file"]])
    if hr_path is not None:
        rows, fieldnames = _read_csv_rows(hr_path)
        has_timestamp_cols = "unixTimeStamp_ms" in fieldnames
        for row in rows:
            hr = row.get("hr_bpm")
            if hr in (None, ""):
                continue
            rr_values = []
            rr_ms = row.get("rr_ms")
            if rr_ms not in (None, ""):
                rr_values = [float(rr_ms)]
            points.append(ReplayPoint("hr", _row_time_seconds(row, has_timestamp_cols), (float(hr), rr_values)))

    if device_key == "h10":
        waveform_path = _find_existing_csv(replay_dir, [spec["waveform_file"]])
        if waveform_path is not None:
            rows, fieldnames = _read_csv_rows(waveform_path)
            has_timestamp_cols = "unixTimeStamp_ms" in fieldnames
            grouped: dict[float, list[tuple[int, float]]] = {}
            for row in rows:
                t_sec = _row_time_seconds(row, has_timestamp_cols)
                grouped.setdefault(t_sec, []).append((int(row["sample_index"]), float(row["ecg_raw"])))
            for t_sec in sorted(grouped.keys()):
                values = [sample for _, sample in sorted(grouped[t_sec], key=lambda item: item[0])]
                points.append(ReplayPoint("waveform", t_sec, values))
    else:
        waveform_path = _find_existing_csv(replay_dir, [spec["waveform_file"]])
        if waveform_path is not None:
            rows, fieldnames = _read_csv_rows(waveform_path)
            has_timestamp_cols = "unixTimeStamp_ms" in fieldnames
            grouped: dict[float, list[tuple[int, list[float]]]] = {}
            for row in rows:
                t_sec = _row_time_seconds(row, has_timestamp_cols)
                sample = [
                    float(row.get("ch1") or 0),
                    float(row.get("ch2") or 0),
                    float(row.get("ch3") or 0),
                    float(row.get("ch4") or 0),
                ]
                grouped.setdefault(t_sec, []).append((int(row["sample_index"]), sample))
            for t_sec in sorted(grouped.keys()):
                values = [sample for _, sample in sorted(grouped[t_sec], key=lambda item: item[0])]
                points.append(ReplayPoint("waveform", t_sec, values))

        interval_path = _find_existing_csv(replay_dir, [spec["interval_file"]])
        if interval_path is not None:
            rows, fieldnames = _read_csv_rows(interval_path)
            has_timestamp_cols = "unixTimeStamp_ms" in fieldnames
            for row in rows:
                ppi = row.get("ppi_ms")
                if ppi in (None, ""):
                    continue
                sample = {
                    "ppi": float(ppi),
                    "error_estimate": float(row["error_estimate_ms"]) if row.get("error_estimate_ms") not in (None, "") else None,
                    "hr": float(row["hr_bpm"]) if row.get("hr_bpm") not in (None, "") else None,
                    "invalid_ppi": row.get("invalid_ppi") == "True",
                    "skin_contact_status": row.get("skin_contact_status") == "True",
                    "skin_contact_supported": row.get("skin_contact_supported") == "True",
                }
                points.append(ReplayPoint("interval", _row_time_seconds(row, has_timestamp_cols), [sample]))

    points.sort(key=lambda item: item.t_sec)
    return points


class LivePolarWorker(QThread):
    status = Signal(str)
    connected = Signal(str, str)
    hr_sample = Signal(float, object, object)
    waveform_batch = Signal(float, object)
    interval_batch = Signal(float, object)
    error = Signal(str)
    finished_cleanly = Signal()

    def __init__(self, name: str, address: str | None, scan_timeout: float, device_key: str, save_dir: Path | None = None):
        super().__init__()
        self.name = name
        self.address = address.strip() if address else None
        self.scan_timeout = scan_timeout
        self.device_key = device_key
        self.save_dir = save_dir
        self._stop_event = threading.Event()
        self._hr_file = None
        self._waveform_file = None
        self._interval_file = None
        self._hr_writer = None
        self._waveform_writer = None
        self._interval_writer = None

    def stop(self):
        self._stop_event.set()

    def run(self):
        try:
            asyncio.run(self._main())
        except Exception as exc:
            self.error.emit(str(exc))

    async def _find_device(self):
        if self.address:
            self.status.emit(f"Scanning by address: {self.address}")
            return await BleakScanner.find_device_by_address(self.address, timeout=self.scan_timeout)

        self.status.emit(f"Scanning for device name containing: {self.name}")
        return await BleakScanner.find_device_by_filter(
            lambda d, ad: bool(d.name) and self.name.lower() in d.name.lower(),
            timeout=self.scan_timeout,
        )

    async def _main(self):
        device = await self._find_device()
        if device is None:
            self.error.emit(f"设备未找到: {self.address or self.name}")
            return

        spec = DEVICE_PRESETS[self.device_key]
        self.connected.emit(device.name or spec["display_name"], device.address)
        self.status.emit("Connecting...")

        async with PolarDevice(device) as polar_device:
            self.status.emit("Connected. Starting streams...")

            self._diag_first_hr_ts = None
            self._diag_first_waveform_ts = None
            self._diag_last_hr_ts = None
            self._diag_last_waveform_ts = None
            self._diag_log_path = Path(f"{spec['display_name'].replace(' ', '_').lower()}_latency.log")

            def _append_diag(msg: str):
                try:
                    with self._diag_log_path.open("a", encoding="utf-8") as fh:
                        fh.write(f"{datetime.now().isoformat()} {msg}\n")
                except Exception:
                    pass

            if self.save_dir:
                try:
                    self.save_dir.mkdir(parents=True, exist_ok=True)
                    self._hr_file = (self.save_dir / spec["hr_file"]).open("w", encoding="utf-8", newline="")
                    self._waveform_file = (self.save_dir / spec["waveform_file"]).open("w", encoding="utf-8", newline="")
                    self._hr_writer = csv.writer(self._hr_file)
                    self._waveform_writer = csv.writer(self._waveform_file)
                    self._hr_writer.writerow(["unixTimeStamp_ms", "dateTime", "pc_time_ns", "hr_bpm", "rr_index", "rr_ms"])
                    if spec["stream_kind"] == "ecg":
                        self._waveform_writer.writerow(["unixTimeStamp_ms", "dateTime", "pc_time_ns", "device_time_ns", "sample_index", "ecg_raw"])
                    else:
                        self._waveform_writer.writerow(["unixTimeStamp_ms", "dateTime", "pc_time_ns", "device_time_ns", "sample_index", "ch1", "ch2", "ch3", "ch4", "ppg_type"])
                        self._interval_file = (self.save_dir / spec["interval_file"]).open("w", encoding="utf-8", newline="")
                        self._interval_writer = csv.writer(self._interval_file)
                        self._interval_writer.writerow(["unixTimeStamp_ms", "dateTime", "pc_time_ns", "ppi_index", "ppi_ms", "error_estimate_ms", "hr_bpm", "invalid_ppi", "skin_contact_status", "skin_contact_supported"])
                    self._hr_file.flush()
                    self._waveform_file.flush()
                    if self._interval_file:
                        self._interval_file.flush()
                    self.status.emit(f"Recording to: {self.save_dir.name}")
                except Exception as exc:
                    self.status.emit(f"Failed to initialize CSV writers: {exc}")

            def on_hr(data: HRData):
                if self._stop_event.is_set():
                    return
                now = time.time()
                pc_time_ns = time.monotonic_ns()
                if self._diag_first_hr_ts is None:
                    self._diag_first_hr_ts = now
                    _append_diag(f"FIRST_HR {now}")
                self._diag_last_hr_ts = now
                hr, rr_list = parse_hr_data(data)

                if self._hr_writer:
                    try:
                        unix_ms = int(now * 1000)
                        dt_str = datetime.fromtimestamp(now).isoformat()
                        if rr_list:
                            for rr_index, rr in enumerate(rr_list):
                                self._hr_writer.writerow([unix_ms, dt_str, pc_time_ns, hr, rr_index, rr])
                        else:
                            self._hr_writer.writerow([unix_ms, dt_str, pc_time_ns, hr, "", ""])
                        self._hr_file.flush()
                    except Exception:
                        pass

                self.hr_sample.emit(now, hr, rr_list)

            def on_waveform(data):
                if self._stop_event.is_set():
                    return
                now = time.time()
                pc_time_ns = time.monotonic_ns()
                if self._diag_first_waveform_ts is None:
                    self._diag_first_waveform_ts = now
                    _append_diag(f"FIRST_WAVEFORM {now}")
                self._diag_last_waveform_ts = now

                if spec["stream_kind"] == "ecg":
                    samples = parse_ecg_data(data)
                    if samples:
                        if self._diag_last_hr_ts is not None:
                            _append_diag(
                                f"ECG_VS_LAST_HR delta={now - self._diag_last_hr_ts:.3f}s hr_ts={self._diag_last_hr_ts} ecg_ts={now}"
                            )
                        if self._waveform_writer:
                            try:
                                unix_ms = int(now * 1000)
                                dt_str = datetime.fromtimestamp(now).isoformat()
                                device_time_ns = getattr(data, "timestamp", None)
                                for sample_index, sample in enumerate(samples):
                                    self._waveform_writer.writerow([unix_ms, dt_str, pc_time_ns, device_time_ns, sample_index, sample])
                                self._waveform_file.flush()
                            except Exception:
                                pass
                        self.waveform_batch.emit(now, samples)
                else:
                    samples = parse_ppg_data(data)
                    if samples:
                        if self._diag_last_hr_ts is not None:
                            _append_diag(
                                f"PPG_VS_LAST_HR delta={now - self._diag_last_hr_ts:.3f}s hr_ts={self._diag_last_hr_ts} ppg_ts={now}"
                            )
                        if self._waveform_writer:
                            try:
                                unix_ms = int(now * 1000)
                                dt_str = datetime.fromtimestamp(now).isoformat()
                                device_time_ns = getattr(data, "timestamp", None)
                                ppg_type = getattr(data, "type", None)
                                for sample_index, sample in enumerate(samples):
                                    padded = list(sample[:4]) + [None] * max(0, 4 - len(sample))
                                    self._waveform_writer.writerow(
                                        [unix_ms, dt_str, pc_time_ns, device_time_ns, sample_index, padded[0], padded[1], padded[2], padded[3], ppg_type]
                                    )
                                self._waveform_file.flush()
                            except Exception:
                                pass
                        self.waveform_batch.emit(now, samples)

            def on_interval(data):
                if self._stop_event.is_set():
                    return
                now = time.time()
                pc_time_ns = time.monotonic_ns()
                samples = parse_ppi_data(data)
                if not samples:
                    return

                if self._interval_writer:
                    try:
                        unix_ms = int(now * 1000)
                        dt_str = datetime.fromtimestamp(now).isoformat()
                        for sample_index, sample in enumerate(samples):
                            self._interval_writer.writerow(
                                [
                                    unix_ms,
                                    dt_str,
                                    pc_time_ns,
                                    sample_index,
                                    sample["ppi"],
                                    sample["error_estimate"],
                                    sample["hr"],
                                    sample["invalid_ppi"],
                                    sample["skin_contact_status"],
                                    sample["skin_contact_supported"],
                                ]
                            )
                        self._interval_file.flush()
                    except Exception:
                        pass

                self.interval_batch.emit(now, samples)

            stream_tasks = []

            try:
                stream_tasks.append(asyncio.create_task(polar_device.start_hr_stream(hr_callback=on_hr)))
            except Exception as exc:
                self.status.emit(f"HR stream failed: {exc}")

            if spec["stream_kind"] == "ecg":
                try:
                    stream_tasks.append(
                        asyncio.create_task(
                            polar_device.start_ecg_stream(
                                ecg_callback=on_waveform,
                                sample_rate=int(spec["waveform_rate_hz"]),
                                resolution=int(spec["waveform_resolution"]),
                            )
                        )
                    )
                except Exception as exc:
                    self.status.emit(f"ECG stream failed: {exc}")
            else:
                try:
                    stream_tasks.append(
                        asyncio.create_task(
                            polar_device.start_ppg_stream(
                                ppg_callback=on_waveform,
                                sample_rate=int(spec["waveform_rate_hz"]),
                                resolution=int(spec["waveform_resolution"]),
                                channels=int(spec["waveform_channels"]),
                            )
                        )
                    )
                except Exception as exc:
                    self.status.emit(f"PPG stream failed: {exc}")

                try:
                    stream_tasks.append(asyncio.create_task(polar_device.start_ppi_stream(ppi_callback=on_interval)))
                except Exception as exc:
                    self.status.emit(f"PPI stream failed: {exc}")

            self.status.emit("Streaming live data...")
            while not self._stop_event.is_set():
                for task in list(stream_tasks):
                    if task.done():
                        try:
                            task.result()
                        except Exception as exc:
                            self.status.emit(f"Stream ended: {exc}")
                        stream_tasks.remove(task)
                await asyncio.sleep(0.1)

            for task in stream_tasks:
                task.cancel()
            if stream_tasks:
                await asyncio.gather(*stream_tasks, return_exceptions=True)

        try:
            if self._hr_file:
                self._hr_file.close()
            if self._waveform_file:
                self._waveform_file.close()
            if self._interval_file:
                self._interval_file.close()
        except Exception:
            pass

        self.finished_cleanly.emit()


class ReplayWorker(QThread):
    status = Signal(str)
    connected = Signal(str, str)
    hr_sample = Signal(float, object, object)
    waveform_batch = Signal(float, object)
    interval_batch = Signal(float, object)
    error = Signal(str)
    finished_cleanly = Signal()

    def __init__(self, replay_dir: Path, device_key: str, speed: float):
        super().__init__()
        self.replay_dir = replay_dir
        self.device_key = device_key
        self.speed = max(speed, 0.1)
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        try:
            points = load_replay_points(self.replay_dir, self.device_key)
            if not points:
                self.error.emit(f"未找到可回放数据: {self.replay_dir}")
                return

            self.connected.emit("CSV Replay", str(self.replay_dir))
            self.status.emit(f"Loaded {len(points)} replay points")

            start = time.perf_counter()
            base_t = points[0].t_sec
            for point in points:
                if self._stop_event.is_set():
                    break
                target = start + (point.t_sec - base_t) / self.speed
                while not self._stop_event.is_set() and time.perf_counter() < target:
                    time.sleep(0.001)

                if self._stop_event.is_set():
                    break

                if point.kind == "hr":
                    hr, rr_list = point.value
                    self.hr_sample.emit(time.time(), hr, rr_list)
                elif point.kind == "waveform":
                    self.waveform_batch.emit(time.time(), point.value)
                elif point.kind == "interval":
                    self.interval_batch.emit(time.time(), point.value)

            self.finished_cleanly.emit()
        except Exception as exc:
            self.error.emit(str(exc))


class DeviceScanWorker(QThread):
    """Scan for BLE devices and emit a list of (name, address) tuples."""

    results = Signal(object)
    status = Signal(str)

    def __init__(self, name_filter: str | None = None, timeout: float = 5.0):
        super().__init__()
        self.name_filter = name_filter
        self.timeout = timeout
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        try:
            self.status.emit("Scanning BLE devices...")
            # Use BleakScanner.discover with advertisement info when available so
            # we can read local_name from advertisement packets (some platforms
            # expose device name there). Handle both dict and list return types.
            devices = None
            try:
                devices = asyncio.run(BleakScanner.discover(timeout=self.timeout, return_adv=True))
            except TypeError:
                # Older Bleak may not accept return_adv; fall back
                devices = asyncio.run(BleakScanner.discover(timeout=self.timeout))

            results = []
            if isinstance(devices, dict):
                # devices: dict[address] -> (device, advertisement)
                for addr, pair in devices.items():
                    try:
                        device, adv = pair
                    except Exception:
                        continue
                    name = (getattr(device, "name", None) or getattr(adv, "local_name", None) or "")
                    address = addr or getattr(device, "address", None)
                    uuids = getattr(adv, "service_uuids", None) or []
                    if not address:
                        continue
                    # emit name, address, uuids for richer matching upstream
                    if self.name_filter:
                        if not name or self.name_filter.lower() not in name.lower():
                            continue
                    results.append((name, address, uuids))
            else:
                # devices: iterable of BLEDevice
                for d in devices:
                    address = getattr(d, "address", None)
                    if not address:
                        continue
                    # prefer d.name, fall back to metadata local_name if present
                    meta = getattr(d, "metadata", {}) or {}
                    name = getattr(d, "name", None) or meta.get("local_name") or ""
                    uuids = meta.get("uuids") or meta.get("service_uuids") or []
                    if self.name_filter:
                        if not name or self.name_filter.lower() not in name.lower():
                            continue
                    results.append((name, address, uuids))
            # results entries are (name, address, uuids)
            self.results.emit(results)
            self.status.emit(f"扫描完成 ({len(results)} 设备)")
        except Exception as exc:
            self.status.emit(f"扫描失败: {exc}")
            self.results.emit([])


class PolarDashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Polar H10 / Verity Sense 实时可视化")
        self.resize(1500, 980)

        self.worker: QThread | None = None
        self.device_key = "h10"

        self.hr_times = deque(maxlen=1200)
        self.hr_values = deque(maxlen=1200)
        self.interval_times = deque(maxlen=2400)
        self.interval_values = deque(maxlen=2400)
        self.ecg_times = deque(maxlen=24000)
        self.ecg_values = deque(maxlen=24000)
        self.ppg_times = [deque(maxlen=24000) for _ in range(4)]
        self.ppg_values = [deque(maxlen=24000) for _ in range(4)]

        # store last scan results as list[dict{name,address}]
        self._last_scan_results: list[dict] = []

        self.window_seconds = 30.0
        self.ecg_zoom_factor = 1.0
        self.paused = False
        self.paused_snapshot: dict | None = None
        self.latest_hr: float | None = None
        self.latest_interval: float | None = None
        self.latest_interval_peak: float | None = None
        self.latest_waveform_peak: float | None = None

        self._build_ui()
        self.load_settings()
        self.device_combo.currentIndexChanged.connect(self.on_device_changed)
        # Scan button and discovered device selection
        try:
            self.scan_devices_btn.clicked.connect(self.start_device_scan)
            self.device_list_combo.currentIndexChanged.connect(self.on_device_list_selected)
        except Exception:
            # in case widgets not present for some reason
            pass
        self.name_edit.editingFinished.connect(self.save_settings)
        self.interval_scale_combo.currentIndexChanged.connect(self.save_settings)
        self.interval_scale_combo.currentIndexChanged.connect(self.update_interval_controls)
        self.interval_min_spin.editingFinished.connect(self.save_settings)
        self.interval_max_spin.editingFinished.connect(self.save_settings)
        self.address_edit.editingFinished.connect(self.save_settings)
        self.scan_timeout_spin.valueChanged.connect(self.save_settings)
        self.device_combo.currentIndexChanged.connect(self.save_settings)
        self.window_spin.valueChanged.connect(self.save_settings)
        self.replay_speed_spin.valueChanged.connect(self.save_settings)
        self.replay_dir_edit.editingFinished.connect(self.save_settings)
        self.save_dir_edit.editingFinished.connect(self.save_settings)

        self.apply_device_preset()
        self._start_timer()

    def _make_metric_card(self, title: str, value: str, detail: str, accent: str) -> QWidget:
        card = QFrame()
        card.setObjectName("metricCard")
        card.setMinimumHeight(88)
        card.setStyleSheet(
            """
            QFrame#metricCard {
                background: linear-gradient(180deg, rgba(15, 23, 42, 0.98), rgba(8, 15, 30, 0.98));
                border: 1px solid rgba(148, 163, 184, 0.18);
                border-radius: 16px;
            }
            """
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(4)

        title_label = QLabel(title)
        title_label.setStyleSheet("color: #94a3b8; font-size: 12px; letter-spacing: 0.5px;")
        value_label = QLabel(value)
        value_label.setStyleSheet(f"color: {accent}; font-size: 30px; font-weight: 800;")
        detail_label = QLabel(detail)
        detail_label.setStyleSheet("color: #cbd5e1; font-size: 11px;")

        layout.addWidget(title_label)
        layout.addWidget(value_label)
        layout.addWidget(detail_label)

        card.value_label = value_label  # type: ignore[attr-defined]
        card.detail_label = detail_label  # type: ignore[attr-defined]
        card.title_label = title_label  # type: ignore[attr-defined]
        return card

    def update_interval_controls(self):
        """Show/hide the fixed-interval controls and their labels depending on mode."""
        try:
            mode = self.interval_scale_combo.currentData() or "auto"
        except Exception:
            mode = "auto"
        visible = mode == "fixed"
        # widget visibility
        try:
            self.interval_min_spin.setVisible(visible)
            self.interval_max_spin.setVisible(visible)
        except Exception:
            pass
        # label visibility via the form
        try:
            lbl_min = self.form.labelForField(self.interval_min_spin)
            if lbl_min is not None:
                lbl_min.setVisible(visible)
            lbl_max = self.form.labelForField(self.interval_max_spin)
            if lbl_max is not None:
                lbl_max.setVisible(visible)
        except Exception:
            pass

    def _set_card_value(self, card: QWidget, value: str, detail: str | None = None):
        card.value_label.setText(value)  # type: ignore[attr-defined]
        if detail is not None:
            card.detail_label.setText(detail)  # type: ignore[attr-defined]

    def _build_ui(self):
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        title = QLabel("Polar H10 / Verity Sense 实时心率与波形仪表盘")
        title.setStyleSheet("font-size: 22px; font-weight: 700; color: #f2f5ff;")
        subtitle = QLabel("支持实时采集与 CSV 回放，H10 使用 ECG / RR，Verity Sense 使用 PPG / PPI")
        subtitle.setStyleSheet("color: #94a3b8; font-size: 12px;")

        layout.addWidget(title)
        layout.addWidget(subtitle)

        content_split = QHBoxLayout()
        content_split.setSpacing(12)

        control_box = QWidget()
        control_box.setMaximumWidth(430)
        control_box.setStyleSheet(
            """
            QWidget {
                color: #d8e1ff;
                border: 1px solid #24314d;
                border-radius: 12px;
                background: #0f172a;
            }
            QLabel, QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
                color: #d8e1ff;
            }
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
                background: #111827;
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 6px 8px;
                selection-background-color: #0ea5e9;
            }
            QPushButton {
                background: #1f2937;
                border: 1px solid #334155;
                color: #e5e7eb;
                border-radius: 8px;
                padding: 8px 14px;
                font-weight: 600;
            }
            QPushButton:hover { background: #243244; }
            QPushButton:pressed { background: #0f172a; }
            QPushButton:disabled { color: #64748b; background: #111827; }
            """
        )

        control_layout = QVBoxLayout(control_box)
        control_layout.setContentsMargins(12, 12, 12, 12)
        control_layout.setSpacing(10)

        self.form = QFormLayout()
        self.form.setLabelAlignment(Qt.AlignRight)
        self.form.setVerticalSpacing(8)
        self.form.setHorizontalSpacing(10)

        self.device_combo = QComboBox()
        self.device_combo.addItem(DEVICE_PRESETS["h10"]["display_name"], "h10")
        self.device_combo.addItem(DEVICE_PRESETS["verity"]["display_name"], "verity")

        # Discovered device selection and scan button
        self.device_list_combo = QComboBox()
        self.device_list_combo.setToolTip("最近扫描到的 BLE 设备 - 选择后会填充设备地址")
        self.scan_devices_btn = QPushButton("扫描设备")

        self.name_edit = QLineEdit("")
        self.address_edit = QLineEdit("")
        self.scan_timeout_spin = QDoubleSpinBox()
        self.scan_timeout_spin.setRange(1.0, 60.0)
        self.scan_timeout_spin.setValue(10.0)
        self.scan_timeout_spin.setSingleStep(1.0)
        self.interval_scale_combo = QComboBox()
        self.interval_scale_combo.addItem("自动", "auto")
        self.interval_scale_combo.addItem("固定 (200-1800 ms)", "fixed")
        # Configurable fixed interval bounds (milliseconds)
        self.interval_min_spin = QDoubleSpinBox()
        self.interval_min_spin.setRange(0.0, 10000.0)
        self.interval_min_spin.setValue(200.0)
        self.interval_min_spin.setSingleStep(0.1)
        self.interval_min_spin.setDecimals(1)
        self.interval_max_spin = QDoubleSpinBox()
        self.interval_max_spin.setRange(0.0, 10000.0)
        self.interval_max_spin.setValue(1800.0)
        self.interval_max_spin.setSingleStep(0.1)
        self.interval_max_spin.setDecimals(1)
        self.window_spin = QSpinBox()
        self.window_spin.setRange(10, 300)
        self.window_spin.setValue(30)
        self.replay_speed_spin = QDoubleSpinBox()
        self.replay_speed_spin.setRange(0.1, 20.0)
        self.replay_speed_spin.setValue(1.0)
        self.replay_speed_spin.setSingleStep(0.5)
        self.replay_speed_spin.setDecimals(1)
        self.replay_dir_edit = QLineEdit(str(Path.cwd()))
        self.save_dir_edit = QLineEdit(str(Path.cwd()))

        self.form.addRow("设备类型", self.device_combo)
        self.form.addRow("设备列表", self.device_list_combo)
        self.form.addRow("设备名", self.name_edit)
        self.form.addRow("扫描", self.scan_devices_btn)
        self.form.addRow("设备地址", self.address_edit)
        self.form.addRow("扫描超时", self.scan_timeout_spin)
        self.form.addRow("间隔轴", self.interval_scale_combo)
        self.form.addRow("固定下限 (ms)", self.interval_min_spin)
        self.form.addRow("固定上限 (ms)", self.interval_max_spin)
        self.form.addRow("显示窗口", self.window_spin)
        self.form.addRow("回放速度", self.replay_speed_spin)
        self.form.addRow("回放目录", self.replay_dir_edit)
        self.form.addRow("保存目录", self.save_dir_edit)

        button_row = QHBoxLayout()
        self.start_live_btn = QPushButton("实时")
        self.start_replay_btn = QPushButton("回放")
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.pick_dir_btn = QPushButton("目录")
        button_row.addWidget(self.start_live_btn)
        button_row.addWidget(self.start_replay_btn)
        button_row.addWidget(self.stop_btn)
        button_row.addWidget(self.pick_dir_btn)
        button_row.addStretch(1)

        self.pause_btn = QPushButton("暂停")
        self.ecg_zoom_in_btn = QPushButton("放大")
        self.ecg_zoom_out_btn = QPushButton("缩小")
        self.ecg_zoom_reset_btn = QPushButton("重置")
        self.screenshot_btn = QPushButton("截图")

        playback_row = QHBoxLayout()
        playback_row.addWidget(self.pause_btn)
        playback_row.addWidget(self.ecg_zoom_in_btn)
        playback_row.addWidget(self.ecg_zoom_out_btn)
        playback_row.addWidget(self.ecg_zoom_reset_btn)
        playback_row.addWidget(self.screenshot_btn)

        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet("color: #38bdf8; font-size: 12px;")

        control_layout.addLayout(self.form)
        control_layout.addLayout(button_row)
        control_layout.addLayout(playback_row)
        control_layout.addWidget(self.status_label)

        dashboard = QWidget()
        dashboard_layout = QVBoxLayout(dashboard)
        dashboard_layout.setContentsMargins(0, 0, 0, 0)
        dashboard_layout.setSpacing(12)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(12)
        self.hr_card = self._make_metric_card("HEART RATE", "-- bpm", "等待接收 HR 数据", "#38bdf8")
        self.interval_card = self._make_metric_card("INTERVAL PEAK", "-- ms", "等待间隔数据", "#f59e0b")
        self.waveform_card = self._make_metric_card("WAVEFORM", "LIVE", "实时刷新中", "#22c55e")
        cards_row.addWidget(self.hr_card, 1)
        cards_row.addWidget(self.interval_card, 1)
        cards_row.addWidget(self.waveform_card, 1)

        dashboard_layout.addLayout(cards_row)

        self.hr_plot = pg.PlotWidget(title="HR (bpm)", axisItems={"bottom": TimeAxisItem(orientation="bottom")})
        self.interval_plot = pg.PlotWidget(title="RR (ms)", axisItems={"bottom": TimeAxisItem(orientation="bottom")})
        self.waveform_plot = pg.PlotWidget(title="ECG (raw)", axisItems={"bottom": TimeAxisItem(orientation="bottom")})
        self.waveform_plot.addLegend(offset=(10, 10))

        for plot, y_label in ((self.hr_plot, "bpm"), (self.interval_plot, "ms"), (self.waveform_plot, "raw")):
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.setLabel("left", y_label)
            plot.setLabel("bottom", "time", units="HH:MM:SS")
            plot.setMenuEnabled(False)
            plot.setClipToView(True)

        self.hr_curve = self.hr_plot.plot([], [], pen=pg.mkPen("#38bdf8", width=2), symbol="o", symbolBrush="#38bdf8", symbolSize=5)
        self.interval_curve = self.interval_plot.plot([], [], pen=pg.mkPen("#f59e0b", width=2), symbol="o", symbolBrush="#f59e0b", symbolSize=5)
        self.waveform_curves = [
            self.waveform_plot.plot([], [], pen=pg.mkPen("#22c55e", width=1.5), name="Waveform 1"),
            self.waveform_plot.plot([], [], pen=pg.mkPen("#38bdf8", width=1.5), name="Waveform 2"),
            self.waveform_plot.plot([], [], pen=pg.mkPen("#f59e0b", width=1.5), name="Waveform 3"),
            self.waveform_plot.plot([], [], pen=pg.mkPen("#e879f9", width=1.5), name="Waveform 4"),
        ]

        self.hr_plot.setMinimumHeight(180)
        self.interval_plot.setMinimumHeight(180)
        self.waveform_plot.setMinimumHeight(320)

        dashboard_layout.addWidget(self.hr_plot, 2)
        dashboard_layout.addWidget(self.interval_plot, 2)
        dashboard_layout.addWidget(self.waveform_plot, 5)

        content_split.addWidget(control_box)
        content_split.addWidget(dashboard, 1)
        layout.addLayout(content_split, 1)

        self.setCentralWidget(root)
        self.statusBar().showMessage("准备就绪")

        self.start_live_btn.clicked.connect(self.start_live)
        self.start_replay_btn.clicked.connect(self.start_replay)
        self.stop_btn.clicked.connect(self.stop_worker)
        self.pick_dir_btn.clicked.connect(self.pick_replay_dir)
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.ecg_zoom_in_btn.clicked.connect(lambda: self.adjust_ecg_zoom(0.8))
        self.ecg_zoom_out_btn.clicked.connect(lambda: self.adjust_ecg_zoom(1.25))
        self.ecg_zoom_reset_btn.clicked.connect(self.reset_ecg_zoom)
        self.screenshot_btn.clicked.connect(self.save_screenshot)

        help_action = QAction("使用说明", self)
        help_action.triggered.connect(self.show_help)
        self.menuBar().addAction(help_action)

    def _start_timer(self):
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_plots)
        self.refresh_timer.start(33)

    def current_spec(self):
        key = self.device_combo.currentData() or "h10"
        return DEVICE_PRESETS[key]

    def apply_device_preset(self):
        spec = self.current_spec()
        self.device_key = self.device_combo.currentData() or "h10"
        if self.name_edit.text().strip() in {"", DEVICE_PRESETS["h10"]["default_name"], DEVICE_PRESETS["verity"]["default_name"]}:
            self.name_edit.setText(spec["default_name"])

        self.interval_plot.setTitle(f"{spec['interval_label']} (ms)")
        self.waveform_plot.setTitle(f"{spec['waveform_label']} ({spec['waveform_unit']})")
        self.waveform_plot.setLabel("left", spec["waveform_unit"])

        if spec["stream_kind"] == "ecg":
            self.waveform_curves[0].setVisible(True)
            for idx in range(1, 4):
                self.waveform_curves[idx].setVisible(False)
        else:
            for idx in range(4):
                self.waveform_curves[idx].setVisible(True)

        self.update_interval_card()
        self.update_waveform_card()

    def on_device_changed(self, *_):
        self.apply_device_preset()
        self.save_settings()

    def show_help(self):
        QMessageBox.information(
            self,
            "使用说明",
            "实时模式：选择设备类型并填写设备名或地址后点击“实时”。\n\n"
            "回放模式：选择对应设备类型的 CSV 目录后点击“回放”。\n\n"
            "H10 支持 HR / RR / ECG；Verity Sense 支持 HR / PPI / PPG。",
        )

    def pick_replay_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择回放目录", self.replay_dir_edit.text())
        if path:
            self.replay_dir_edit.setText(path)
            self.save_settings()

    def start_device_scan(self):
        try:
            self.scan_devices_btn.setEnabled(False)
            # Auto-filter by selected device type unless user provided a name.
            # Use fixed keywords: 'polar h10' and 'polar sense'.
            user_name = self.name_edit.text().strip()
            device_kind = self.device_combo.currentData() or "h10"
            if user_name:
                name_filter = user_name
            else:
                name_filter = "polar h10" if device_kind == "h10" else "polar sense"
            timeout = float(self.scan_timeout_spin.value())
            self._scan_worker = DeviceScanWorker(name_filter=name_filter, timeout=timeout)
            self._scan_worker.results.connect(self._on_scan_results)
            self._scan_worker.status.connect(self.set_status)
            self._scan_worker.start()
        except Exception as exc:
            self.set_status(f"启动扫描失败: {exc}")

    def _on_scan_results(self, results):
        try:
            # Filter scan results to only show devices of the selected type
            device_kind = self.device_combo.currentData() or "h10"
            # If user provided a custom name, prefer that filter; otherwise enforce explicit keywords
            user_name = self.name_edit.text().strip()
            if user_name:
                filtered = results
            else:
                # Fixed keyword matching per request; also include devices whose
                # advertised service UUIDs include 'feee' (Polar vendor service).
                keyword = "polar h10" if device_kind == "h10" else "polar sense"
                filtered = []
                for item in results:
                    # item may be (name,address) or (name,address,uuids)
                    name = item[0] if len(item) > 0 else ""
                    addr = item[1] if len(item) > 1 else ""
                    uuids = item[2] if len(item) > 2 else []
                    if name and keyword in name.lower():
                        filtered.append(item)
                        continue
                    # check service UUIDs for Polar's FEED/FE?? vendor UUID (feee)
                    try:
                        if uuids and any(("feee" in (u or "").lower()) for u in uuids):
                            filtered.append(item)
                            continue
                    except Exception:
                        pass

            self.device_list_combo.clear()
            for item in filtered:
                # item may be (name, address) or (name, address, uuids)
                name = item[0] if len(item) > 0 else ""
                addr = item[1] if len(item) > 1 else ""
                display = f"{name} ({addr})" if name else addr
                self.device_list_combo.addItem(display, addr)
            # save scan results for persistence (only what is shown)
            self._last_scan_results = [{"name": (item[0] if len(item) > 0 else ""), "address": (item[1] if len(item) > 1 else "")} for item in filtered]
            try:
                self.save_settings()
            except Exception:
                pass
            if filtered:
                self.set_status(f"找到 {len(filtered)} 台设备")
            else:
                self.set_status("未找到设备")
        finally:
            try:
                self.scan_devices_btn.setEnabled(True)
            except Exception:
                pass

    def on_device_list_selected(self, index: int):
        if index < 0:
            return
        addr = self.device_list_combo.itemData(index)
        if addr:
            self.address_edit.setText(str(addr))
            text = self.device_list_combo.currentText()
            if text and (not self.name_edit.text().strip()):
                if "(" in text:
                    name = text.split("(", 1)[0].strip()
                else:
                    name = text
                self.name_edit.setText(name)

    def set_status(self, text: str):
        self.status_label.setText(text)
        self.statusBar().showMessage(text)

    def save_screenshot(self):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = Path.cwd() / f"polar_dashboard_{stamp}.png"
        pixmap = self.grab()
        if pixmap.save(str(path)):
            self.set_status(f"截图已保存: {path.name}")
        else:
            QMessageBox.warning(self, "保存失败", "截图保存失败")

    def save_settings(self, *_):
        try:
            settings = {
                "device_kind": self.device_combo.currentData(),
                "name": self.name_edit.text(),
                "address": self.address_edit.text(),
                "interval_scale_mode": self.interval_scale_combo.currentData(),
                "interval_min": float(self.interval_min_spin.value()),
                "interval_max": float(self.interval_max_spin.value()),
                "scan_timeout": float(self.scan_timeout_spin.value()),
                "window": int(self.window_spin.value()),
                "replay_speed": float(self.replay_speed_spin.value()),
                "replay_dir": self.replay_dir_edit.text(),
                "save_dir": self.save_dir_edit.text(),
                "recent_scan": self._last_scan_results,
            }
            path = Path.cwd() / "gui_settings.json"
            with path.open("w", encoding="utf-8") as fh:
                json.dump(settings, fh)
        except Exception:
            pass

    def load_settings(self):
        try:
            path = Path.cwd() / "gui_settings.json"
            if not path.exists():
                return
            with path.open("r", encoding="utf-8") as fh:
                settings = json.load(fh)
            device_kind = settings.get("device_kind")
            if device_kind in DEVICE_PRESETS:
                index = self.device_combo.findData(device_kind)
                if index >= 0:
                    self.device_combo.setCurrentIndex(index)
            if "name" in settings:
                self.name_edit.setText(str(settings.get("name", "")))
            if "address" in settings:
                self.address_edit.setText(str(settings.get("address", "")))
            if "scan_timeout" in settings:
                self.scan_timeout_spin.setValue(float(settings.get("scan_timeout", 10.0)))
            if "interval_min" in settings:
                try:
                    self.interval_min_spin.setValue(float(settings.get("interval_min", 200.0)))
                except Exception:
                    pass
            if "interval_max" in settings:
                try:
                    self.interval_max_spin.setValue(float(settings.get("interval_max", 1800.0)))
                except Exception:
                    pass
            # Ensure UI shows/hides controls according to loaded mode
            try:
                self.update_interval_controls()
            except Exception:
                pass
            if "interval_scale_mode" in settings:
                mode = settings.get("interval_scale_mode")
                idx = self.interval_scale_combo.findData(mode)
                if idx >= 0:
                    self.interval_scale_combo.setCurrentIndex(idx)
            if "window" in settings:
                self.window_spin.setValue(int(settings.get("window", 30)))
            if "replay_speed" in settings:
                self.replay_speed_spin.setValue(float(settings.get("replay_speed", 1.0)))
            if "replay_dir" in settings:
                self.replay_dir_edit.setText(str(settings.get("replay_dir", Path.cwd())))
            if "save_dir" in settings:
                self.save_dir_edit.setText(str(settings.get("save_dir", Path.cwd())))
            # restore recent scan results
            recent = settings.get("recent_scan")
            if recent and isinstance(recent, list):
                try:
                    self.device_list_combo.clear()
                    items = []
                    # Filter restored recent scans to match current device kind unless user saved a custom name
                    device_kind = settings.get("device_kind") or self.device_combo.currentData() or "h10"
                    user_name = str(settings.get("name", "")).strip()
                    if user_name:
                        iterator = recent
                    else:
                        keyword = "polar h10" if device_kind == "h10" else "polar sense"
                        iterator = [ent for ent in recent if ent.get("name") and keyword in str(ent.get("name", "")).lower()]
                    for ent in iterator:
                        name = str(ent.get("name", ""))
                        addr = str(ent.get("address", ""))
                        if not addr:
                            continue
                        display = f"{name} ({addr})" if name else addr
                        self.device_list_combo.addItem(display, addr)
                        items.append({"name": name, "address": addr})
                    self._last_scan_results = items
                except Exception:
                    pass
        except Exception:
            pass

    def toggle_ecg_freeze(self):
        return self.toggle_pause()

    def toggle_pause(self):
        self.paused = not self.paused
        if self.paused:
            self.capture_snapshot()
            self.pause_btn.setText("继续")
            self.set_status("显示已暂停")
        else:
            self.paused_snapshot = None
            self.pause_btn.setText("暂停")
            self.set_status("已恢复实时显示")
        self.update_hr_card()
        self.update_interval_card()
        self.update_waveform_card()

    def adjust_ecg_zoom(self, factor: float):
        self.ecg_zoom_factor = max(0.25, min(4.0, self.ecg_zoom_factor * factor))
        self.set_status(f"缩放: {self.ecg_zoom_factor:.2f}x")

    def reset_ecg_zoom(self):
        self.ecg_zoom_factor = 1.0
        self.set_status("缩放已重置")

    def capture_snapshot(self):
        if self.device_key == "h10":
            waveform = (list(self.ecg_times), list(self.ecg_values))
        else:
            waveform = ([list(times) for times in self.ppg_times], [list(values) for values in self.ppg_values])
        self.paused_snapshot = {
            "hr": (list(self.hr_times), list(self.hr_values)),
            "interval": (list(self.interval_times), list(self.interval_values)),
            "waveform": waveform,
        }

    def update_hr_card(self):
        suffix = " · 已暂停" if self.paused else ""
        if self.latest_hr is None:
            self._set_card_value(self.hr_card, "-- bpm", "等待接收 HR 数据" + suffix)
        else:
            self._set_card_value(self.hr_card, f"{self.latest_hr:.0f} bpm", "当前心率" + suffix)

    def update_interval_card(self):
        suffix = " · 已暂停" if self.paused else ""
        spec = self.current_spec()
        if self.latest_interval_peak is None:
            self._set_card_value(self.interval_card, "-- ms", f"等待 {spec['interval_label']} 数据" + suffix)
            return

        detail = f"最新 {self.latest_interval:.0f} ms" if self.latest_interval is not None else f"最新 {spec['interval_label']} 未更新"
        if self.latest_interval_peak >= 900:
            detail = f"峰值偏高 · {detail}"
        self._set_card_value(self.interval_card, f"{self.latest_interval_peak:.0f} ms", detail + suffix)

    def update_waveform_card(self):
        spec = self.current_spec()
        if self.paused:
            self._set_card_value(self.waveform_card, "PAUSED", "显示已暂停")
            return

        if self.latest_waveform_peak is None:
            self._set_card_value(self.waveform_card, "LIVE", f"{spec['waveform_label']} 实时刷新中")
        else:
            self._set_card_value(self.waveform_card, f"±{self.latest_waveform_peak:.0f}", f"{spec['waveform_label']} 最新峰值幅度")

    def _prepare_run(self):
        self.stop_worker()
        self.hr_times.clear()
        self.hr_values.clear()
        self.interval_times.clear()
        self.interval_values.clear()
        self.ecg_times.clear()
        self.ecg_values.clear()
        self.ppg_times = [deque(maxlen=24000) for _ in range(4)]
        self.ppg_values = [deque(maxlen=24000) for _ in range(4)]
        self.latest_hr = None
        self.latest_interval = None
        self.latest_interval_peak = None
        self.latest_waveform_peak = None
        self.paused = False
        self.paused_snapshot = None
        self.pause_btn.setText("暂停")
        self.refresh_plots()

    def start_live(self):
        self._prepare_run()
        spec = self.current_spec()

        save_dir = None
        save_dir_text = self.save_dir_edit.text().strip()
        if save_dir_text:
            base_dir = Path(save_dir_text)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_dir = base_dir / timestamp

        name = self.name_edit.text().strip() or spec["default_name"]
        self.worker = LivePolarWorker(
            name=name,
            address=self.address_edit.text().strip() or None,
            scan_timeout=float(self.scan_timeout_spin.value()),
            device_key=self.device_key,
            save_dir=save_dir,
        )
        self._wire_worker(self.worker)
        self._set_running(True)
        self.worker.start()
        self.set_status("正在启动实时连接...")

    def start_replay(self):
        self._prepare_run()
        replay_dir = Path(self.replay_dir_edit.text().strip())
        self.worker = ReplayWorker(replay_dir, self.device_key, float(self.replay_speed_spin.value()))
        self._wire_worker(self.worker)
        self._set_running(True)
        self.worker.start()
        self.set_status(f"正在回放: {replay_dir}")

    def _wire_worker(self, worker: QThread):
        worker.status.connect(self.set_status)
        worker.connected.connect(self.on_connected)
        worker.hr_sample.connect(self.on_hr_sample)
        worker.waveform_batch.connect(self.on_waveform_batch)
        worker.interval_batch.connect(self.on_interval_batch)
        worker.error.connect(self.on_worker_error)
        worker.finished_cleanly.connect(self.on_worker_finished)

    def _set_running(self, running: bool):
        self.start_live_btn.setEnabled(not running)
        self.start_replay_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.device_combo.setEnabled(not running)
        self.name_edit.setEnabled(not running)
        self.address_edit.setEnabled(not running)
        self.scan_timeout_spin.setEnabled(not running)
        self.window_spin.setEnabled(not running)
        self.replay_speed_spin.setEnabled(not running)
        self.replay_dir_edit.setEnabled(not running)
        self.save_dir_edit.setEnabled(not running)
        self.pick_dir_btn.setEnabled(not running)

    def stop_worker(self):
        if self.worker is not None:
            if hasattr(self.worker, "stop"):
                self.worker.stop()
            self.set_status("正在停止...")
            self.worker.wait(3000)
            if self.worker.isRunning():
                self.set_status("停止超时，等待后台退出")
            else:
                self.worker = None
                self.set_status("已停止")
        self._set_running(False)

    def on_connected(self, name: str, address: str):
        self.set_status(f"已连接: {name} ({address})")

    def on_worker_error(self, message: str):
        self._set_running(False)
        if self.worker is not None and hasattr(self.worker, "stop"):
            self.worker.stop()
        QMessageBox.critical(self, "运行错误", message)
        self.set_status(message)

    def on_worker_finished(self):
        self._set_running(False)
        self.worker = None
        self.set_status("已停止")

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            if hasattr(self.worker, "stop"):
                self.worker.stop()
            self.worker.wait(3000)
        event.accept()

    def on_hr_sample(self, t_sec: float, hr, rr_list):
        if hr is not None and not (isinstance(hr, float) and math.isnan(hr)):
            self.latest_hr = float(hr)
            self.hr_times.append(t_sec)
            self.hr_values.append(float(hr))

        if self.device_key == "h10" and rr_list:
            self.latest_interval = float(rr_list[-1])
            rr_peak = max(float(rr) for rr in rr_list if rr is not None)
            self.latest_interval_peak = rr_peak if self.latest_interval_peak is None else max(self.latest_interval_peak, rr_peak)
            for rr in rr_list:
                if rr is None:
                    continue
                self.interval_times.append(t_sec)
                self.interval_values.append(float(rr))

        self.update_hr_card()
        self.update_interval_card()

    def on_waveform_batch(self, t_sec: float, samples):
        spec = self.current_spec()
        if spec["stream_kind"] == "ecg":
            values = [float(sample) for sample in samples]
            if values:
                peak = max(abs(value) for value in values)
                self.latest_waveform_peak = peak if self.latest_waveform_peak is None else max(self.latest_waveform_peak, peak)

            dt = 1.0 / max(spec["waveform_rate_hz"], 1.0)
            for index, value in enumerate(values):
                self.ecg_times.append(t_sec + index * dt)
                self.ecg_values.append(value)
        else:
            values = [[float(value) for value in sample] for sample in samples]
            if values:
                peak = max(abs(value) for sample in values for value in sample)
                self.latest_waveform_peak = peak if self.latest_waveform_peak is None else max(self.latest_waveform_peak, peak)

            dt = 1.0 / max(spec["waveform_rate_hz"], 1.0)
            for index, sample in enumerate(values):
                timestamp = t_sec + index * dt
                padded = list(sample[:4]) + [0.0] * max(0, 4 - len(sample))
                for channel in range(4):
                    self.ppg_times[channel].append(timestamp)
                    self.ppg_values[channel].append(padded[channel])

        self.update_waveform_card()

    def on_interval_batch(self, t_sec: float, samples):
        spec = self.current_spec()
        if spec["stream_kind"] != "ppg":
            return

        for sample in samples:
            ppi = sample.get("ppi")
            if ppi is None:
                continue
            self.latest_interval = float(ppi)
            self.latest_interval_peak = float(ppi) if self.latest_interval_peak is None else max(self.latest_interval_peak, float(ppi))
            self.interval_times.append(t_sec)
            self.interval_values.append(float(ppi))

            hr_value = sample.get("hr")
            if hr_value is not None:
                self.latest_hr = float(hr_value)

        self.update_hr_card()
        self.update_interval_card()

    def refresh_plots(self):
        window = float(self.window_spin.value())
        self.window_seconds = window
        plot_window = max(15.0, self.window_seconds * self.ecg_zoom_factor)

        latest_t = None
        if self.hr_times:
            latest_t = self.hr_times[-1] if latest_t is None else max(latest_t, self.hr_times[-1])
        if self.interval_times:
            latest_t = self.interval_times[-1] if latest_t is None else max(latest_t, self.interval_times[-1])
        if self.device_key == "h10":
            if self.ecg_times:
                latest_t = self.ecg_times[-1] if latest_t is None else max(latest_t, self.ecg_times[-1])
        else:
            if self.ppg_times[0]:
                latest_t = self.ppg_times[0][-1] if latest_t is None else max(latest_t, self.ppg_times[0][-1])

        def _slice(times, values):
            if not times or latest_t is None:
                return [], []
            cutoff = latest_t - plot_window
            xs = []
            ys = []
            for t, y in zip(times, values):
                if t >= cutoff:
                    xs.append(t)
                    ys.append(y)
            return xs, ys

        if self.paused and self.paused_snapshot is not None:
            hr_x, hr_y = self.paused_snapshot.get("hr", ([], []))
            interval_x, interval_y = self.paused_snapshot.get("interval", ([], []))
            waveform_x, waveform_y = self.paused_snapshot.get("waveform", ([], []))
        else:
            hr_x, hr_y = _slice(self.hr_times, self.hr_values)
            interval_x, interval_y = _slice(self.interval_times, self.interval_values)
            if self.device_key == "h10":
                waveform_x, waveform_y = _slice(self.ecg_times, self.ecg_values)
            else:
                waveform_x = []
                waveform_y = []
                for channel in range(4):
                    xs, ys = _slice(self.ppg_times[channel], self.ppg_values[channel])
                    waveform_x.append(xs)
                    waveform_y.append(ys)
            if self.device_key == "h10" and waveform_x and waveform_y and not self.paused:
                self.paused_snapshot = None

        self.hr_curve.setData(hr_x, hr_y)
        self.interval_curve.setData(interval_x, interval_y)
        if self.device_key == "h10":
            self.waveform_curves[0].setData(waveform_x, waveform_y)
            for idx in range(1, 4):
                self.waveform_curves[idx].setData([], [])
        else:
            for idx, curve in enumerate(self.waveform_curves):
                curve.setData(waveform_x[idx], waveform_y[idx])

        self._update_view(self.hr_plot, hr_x, hr_y, (35, 220), custom_window=plot_window)
        # Interval axis mode: user-selectable auto or fixed range
        mode = getattr(self, "interval_scale_combo", None)
        if mode is None:
            interval_mode = "auto"
        else:
            interval_mode = self.interval_scale_combo.currentData() or "auto"

        if interval_mode == "fixed":
            # Use configured fixed bounds when available (float allowed)
            try:
                min_val = float(self.interval_min_spin.value())
            except Exception:
                min_val = 200.0
            try:
                max_val = float(self.interval_max_spin.value())
            except Exception:
                max_val = 1800.0
            # Ensure sensible ordering
            if min_val >= max_val:
                max_val = min_val + 0.1
            self._update_view(self.interval_plot, interval_x, interval_y, (min_val, max_val), custom_window=plot_window)
        else:
            self._update_view(self.interval_plot, interval_x, interval_y, None, custom_window=plot_window)
        if self.device_key == "h10":
            self._update_view(self.waveform_plot, waveform_x, waveform_y, None, custom_window=plot_window)
        else:
            combined_x = [x for channel_x in waveform_x for x in channel_x]
            combined_y = [y for channel_y in waveform_y for y in channel_y]
            self._update_view(self.waveform_plot, combined_x, combined_y, None, custom_window=plot_window)

        self.update_waveform_card()

    def _update_view(self, plot: pg.PlotWidget, xs, ys, fixed_range, custom_window: float | None = None):
        if not xs:
            if fixed_range is not None:
                plot.setYRange(fixed_range[0], fixed_range[1], padding=0)
            return

        x_max = max(xs[-1], xs[0] + 1e-3)
        window = custom_window if custom_window is not None else self.window_seconds
        plot.setXRange(max(0.0, x_max - window), x_max + 0.1, padding=0)

        if fixed_range is not None:
            plot.setYRange(fixed_range[0], fixed_range[1], padding=0)
            return

        y_min = min(ys)
        y_max = max(ys)
        if y_min == y_max:
            y_min -= 1.0
            y_max += 1.0
        else:
            y_pad = max((y_max - y_min) * 0.12, 10.0)
            y_min -= y_pad
            y_max += y_pad
        plot.setYRange(y_min, y_max, padding=0)


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Real-time Polar H10 / Verity Sense dashboard")
    parser.add_argument("--device-kind", choices=["h10", "verity"], default="h10", help="Device preset to use")
    parser.add_argument("--name", default=None, help="Device name filter")
    parser.add_argument("--address", default=None, help="Device address")
    parser.add_argument("--scan-timeout", type=float, default=10.0, help="Scan timeout seconds")
    parser.add_argument("--window-seconds", type=float, default=60.0, help="Visible time window")
    parser.add_argument("--replay-dir", default=None, help="Replay CSV directory instead of live BLE")
    parser.add_argument("--replay-speed", type=float, default=1.0, help="Replay speed multiplier")
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    app = QApplication([])
    app.setApplicationName("Polar Dashboard")
    app.setFont(QFont("Segoe UI", 10))

    window = PolarDashboard()
    device_index = window.device_combo.findData(args.device_kind)
    if device_index >= 0:
        window.device_combo.setCurrentIndex(device_index)
    window.apply_device_preset()

    if args.name is not None:
        window.name_edit.setText(args.name)
    else:
        window.name_edit.setText(DEVICE_PRESETS[args.device_kind]["default_name"])

    if args.address:
        window.address_edit.setText(args.address)
    window.scan_timeout_spin.setValue(args.scan_timeout)
    window.window_spin.setValue(int(args.window_seconds))
    window.replay_speed_spin.setValue(float(args.replay_speed))
    if args.replay_dir:
        window.replay_dir_edit.setText(args.replay_dir)

    window.show()

    if args.replay_dir:
        QTimer.singleShot(0, window.start_replay)

    app.exec()


if __name__ == "__main__":
    main()
