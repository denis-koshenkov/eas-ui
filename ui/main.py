from __future__ import annotations

import asyncio
import sys
import threading
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, Qt, QRegularExpression, Signal
from PySide6.QtGui import QCloseEvent, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


VARIABLES = {
    "Temperature": {
        "id": 0x00,
        "min": -50.0,
        "max": 70.0,
        "decimals": 1,
        "suffix": " C",
        "scale": 10,
        "bytes": 2,
        "signed": True,
    },
    "Pressure": {
        "id": 0x01,
        "min": 0.0,
        "max": 1500.0,
        "decimals": 1,
        "suffix": " hPa",
        "scale": 10,
        "bytes": 2,
        "signed": False,
    },
    "Humidity": {
        "id": 0x02,
        "min": 0.0,
        "max": 100.0,
        "decimals": 1,
        "suffix": " %",
        "scale": 10,
        "bytes": 2,
        "signed": False,
    },
    "Light intensity": {
        "id": 0x03,
        "min": 0,
        "max": 130000,
        "decimals": 0,
        "suffix": " lx",
        "scale": 1,
        "bytes": 4,
        "signed": False,
    },
}

OPERATORS = {
    ">=": 0x00,
    "<=": 0x01,
}

LED_COLORS = {
    "Red": 0x00,
    "Green": 0x01,
    "Blue": 0x02,
}

LED_PATTERNS = {
    "Static": 0x00,
    "Alert": 0x01,
}

STATUS_NAMES = {
    0x00: "Silenced",
    0x01: "Raised",
}

UINT32_MAX = 0xFFFFFFFF


@dataclass
class VariableRequirement:
    variable_name: str
    operator: str
    value: float

    def to_payload(self) -> bytes:
        spec = VARIABLES[self.variable_name]
        encoded_value = int(round(self.value * spec["scale"]))
        return bytes((spec["id"], OPERATORS[self.operator])) + encoded_value.to_bytes(
            spec["bytes"], "little", signed=spec["signed"]
        )

    def to_summary(self) -> str:
        spec = VARIABLES[self.variable_name]
        if spec["decimals"] == 0:
            value = str(int(self.value))
        else:
            value = f"{self.value:.1f}"
        return f"{self.variable_name} {self.operator} {value}{spec['suffix']}"


@dataclass
class AlertRecord:
    alert_id: int
    warmup_ms: int
    cooldown_ms: int
    notification_summary: str
    condition_summary: str
    payload: bytes
    status: str = "Unknown"


class BluetoothController(QObject):
    devices_found = Signal(list)
    connection_changed = Signal(bool, str)
    info = Signal(str)
    error = Signal(str)
    alert_status_changed = Signal(int, int)
    write_succeeded = Signal(str)
    write_failed = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._ready = threading.Event()
        self._client: Any = None
        self._rx_uuid = ""
        self._tx_uuid = ""
        self._thread.start()
        self._ready.wait(timeout=3.0)

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    def scan(self, service_uuid: str) -> None:
        self._schedule(self._scan(service_uuid.strip()))

    async def _scan(self, service_uuid: str) -> None:
        try:
            from bleak import BleakScanner

            kwargs = {"timeout": 5.0}
            if service_uuid:
                kwargs["service_uuids"] = [service_uuid]
            self.info.emit("Scanning for Bluetooth LE devices...")
            devices = await BleakScanner.discover(**kwargs)
            device_rows = sorted(
                [(device.name or "Unknown device", device.address) for device in devices],
                key=lambda row: (row[0].lower(), row[1]),
            )
            self.devices_found.emit(device_rows)
            self.info.emit(f"Scan complete: {len(device_rows)} device(s) found.")
        except Exception as exc:
            self.error.emit(f"Bluetooth scan failed: {exc}")

    def connect_to_device(self, address: str, service_uuid: str, tx_uuid: str, rx_uuid: str) -> None:
        self._schedule(self._connect(address, service_uuid.strip(), tx_uuid.strip(), rx_uuid.strip()))

    async def _connect(self, address: str, service_uuid: str, tx_uuid: str, rx_uuid: str) -> None:
        try:
            from bleak import BleakClient

            if self._client and self._client.is_connected:
                await self._client.disconnect()

            self.info.emit(f"Connecting to {address}...")
            client = BleakClient(address)
            await client.connect()

            if service_uuid:
                services = client.services
                service_ids = {str(service.uuid).lower() for service in services}
                if service_uuid.lower() not in service_ids:
                    await client.disconnect()
                    self.connection_changed.emit(False, "Disconnected")
                    self.error.emit("Connected device does not expose the configured EASS service UUID.")
                    return

            self._client = client
            self._tx_uuid = tx_uuid
            self._rx_uuid = rx_uuid
            await client.start_notify(rx_uuid, self._handle_notification)
            self.connection_changed.emit(True, f"Connected to {address}")
            self.info.emit("Notifications enabled on the EASS RX characteristic.")
        except Exception as exc:
            self._client = None
            self.connection_changed.emit(False, "Disconnected")
            self.error.emit(f"Bluetooth connection failed: {exc}")

    def disconnect_from_device(self) -> None:
        self._schedule(self._disconnect())

    async def _disconnect(self) -> None:
        try:
            if self._client and self._client.is_connected:
                if self._rx_uuid:
                    try:
                        await self._client.stop_notify(self._rx_uuid)
                    except Exception:
                        pass
                await self._client.disconnect()
            self.info.emit("Bluetooth disconnected.")
        except Exception as exc:
            self.error.emit(f"Bluetooth disconnect failed: {exc}")
        finally:
            self._client = None
            self.connection_changed.emit(False, "Disconnected")

    def send_payload(self, action_key: str, payload: bytes) -> None:
        self._schedule(self._write(action_key, payload))

    async def _write(self, action_key: str, payload: bytes) -> None:
        try:
            if not self._client or not self._client.is_connected:
                raise RuntimeError("EAS is not connected.")
            await self._client.write_gatt_char(self._tx_uuid, payload, response=True)
            self.write_succeeded.emit(action_key)
            self.info.emit(f"Sent {len(payload)} byte(s): {payload.hex(' ')}")
        except Exception as exc:
            self.write_failed.emit(action_key, str(exc))
            self.error.emit(f"Bluetooth write failed: {exc}")

    def _handle_notification(self, _sender: Any, data: bytearray) -> None:
        payload = bytes(data)
        if len(payload) >= 3 and payload[0] == 0x00:
            self.alert_status_changed.emit(payload[1], payload[2])
            return
        self.info.emit(f"Received unsupported message: {payload.hex(' ')}")

    def shutdown(self) -> None:
        if not self._loop:
            return
        future = asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop)
        try:
            future.result(timeout=3.0)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)

    def _schedule(self, coroutine: Any) -> None:
        if not self._loop:
            self.error.emit("Bluetooth event loop is not available.")
            return
        asyncio.run_coroutine_threadsafe(coroutine, self._loop)


class UInt32LineEdit(QLineEdit):
    def __init__(self, value: int = 0) -> None:
        super().__init__(str(value))
        self.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d{1,10}"), self))
        self.setMaximumWidth(160)

    def value(self) -> int:
        text = self.text().strip()
        if not text:
            raise ValueError("Enter a value between 0 and 4294967295.")
        value = int(text)
        if value > UINT32_MAX:
            raise ValueError("Enter a value between 0 and 4294967295.")
        return value


class RequirementWidget(QWidget):
    remove_requested = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.variable_combo = QComboBox()
        for variable_name, spec in VARIABLES.items():
            self.variable_combo.addItem(f"{variable_name} ({spec['id']})", variable_name)

        self.operator_combo = QComboBox()
        for operator in OPERATORS:
            self.operator_combo.addItem(operator, operator)

        self.value_spin = QDoubleSpinBox()
        self.value_spin.setMaximumWidth(180)

        remove_button = QPushButton("Remove")
        remove_button.clicked.connect(lambda: self.remove_requested.emit(self))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.variable_combo, 2)
        layout.addWidget(self.operator_combo, 1)
        layout.addWidget(self.value_spin, 2)
        layout.addWidget(remove_button)

        self.variable_combo.currentIndexChanged.connect(self._update_value_editor)
        self._update_value_editor()

    def _update_value_editor(self) -> None:
        variable_name = self.variable_combo.currentData()
        spec = VARIABLES[variable_name]
        previous_value = self.value_spin.value()
        self.value_spin.setRange(spec["min"], spec["max"])
        self.value_spin.setDecimals(spec["decimals"])
        self.value_spin.setSingleStep(0.1 if spec["decimals"] else 1.0)
        self.value_spin.setSuffix(spec["suffix"])
        self.value_spin.setValue(
            min(max(previous_value, self.value_spin.minimum()), self.value_spin.maximum())
        )

    def requirement(self) -> VariableRequirement:
        variable_name = self.variable_combo.currentData()
        operator = self.operator_combo.currentData()
        spec = VARIABLES[variable_name]
        return VariableRequirement(
            variable_name=variable_name,
            operator=operator,
            value=self.value_spin.value(),
        )


class OrGroupWidget(QGroupBox):
    remove_requested = Signal(object)

    def __init__(self, title: str) -> None:
        super().__init__(title)
        self.requirement_layout = QVBoxLayout()

        add_requirement_button = QPushButton("Add OR requirement")
        add_requirement_button.clicked.connect(self.add_requirement)

        remove_group_button = QPushButton("Remove AND group")
        remove_group_button.clicked.connect(lambda: self.remove_requested.emit(self))

        button_layout = QHBoxLayout()
        button_layout.addWidget(add_requirement_button)
        button_layout.addWidget(remove_group_button)
        button_layout.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(self.requirement_layout)
        layout.addLayout(button_layout)
        self.add_requirement()

    def add_requirement(self) -> None:
        requirement = RequirementWidget()
        requirement.remove_requested.connect(self.remove_requirement)
        self.requirement_layout.addWidget(requirement)
        self._sync_remove_buttons()

    def remove_requirement(self, requirement: RequirementWidget) -> None:
        if self.requirement_layout.count() <= 1:
            return
        self.requirement_layout.removeWidget(requirement)
        requirement.deleteLater()
        self._sync_remove_buttons()

    def requirements(self) -> list[VariableRequirement]:
        return [
            self.requirement_layout.itemAt(index).widget().requirement()
            for index in range(self.requirement_layout.count())
        ]

    def _sync_remove_buttons(self) -> None:
        enabled = self.requirement_layout.count() > 1
        for index in range(self.requirement_layout.count()):
            widget = self.requirement_layout.itemAt(index).widget()
            button = widget.layout().itemAt(3).widget()
            button.setEnabled(enabled)


class ConditionEditor(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.group_layout = QVBoxLayout()
        self.group_layout.setContentsMargins(0, 0, 0, 0)

        add_group_button = QPushButton("Add AND group")
        add_group_button.clicked.connect(self.add_group)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.addLayout(self.group_layout)
        content_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(220)
        scroll.setWidget(content)

        layout = QVBoxLayout(self)
        layout.addWidget(scroll)
        layout.addWidget(add_group_button, alignment=Qt.AlignLeft)
        self.add_group()

    def add_group(self) -> None:
        group = OrGroupWidget(f"AND group {self.group_layout.count() + 1}")
        group.remove_requested.connect(self.remove_group)
        self.group_layout.addWidget(group)
        self._sync_groups()

    def remove_group(self, group: OrGroupWidget) -> None:
        if self.group_layout.count() <= 1:
            return
        self.group_layout.removeWidget(group)
        group.deleteLater()
        self._sync_groups()

    def condition_groups(self) -> list[list[VariableRequirement]]:
        return [
            self.group_layout.itemAt(index).widget().requirements()
            for index in range(self.group_layout.count())
        ]

    def condition_payload(self) -> bytes:
        groups = self.condition_groups()
        if len(groups) > 255:
            raise ValueError("A condition cannot contain more than 255 AND groups.")

        payload = bytearray((len(groups),))
        for requirements in groups:
            if len(requirements) > 255:
                raise ValueError("An AND group cannot contain more than 255 OR requirements.")
            payload.append(len(requirements))
            for requirement in requirements:
                payload.extend(requirement.to_payload())
        return bytes(payload)

    def summary(self) -> str:
        return " AND ".join(
            "(" + " OR ".join(requirement.to_summary() for requirement in group) + ")"
            for group in self.condition_groups()
        )

    def _sync_groups(self) -> None:
        enabled = self.group_layout.count() > 1
        for index in range(self.group_layout.count()):
            group = self.group_layout.itemAt(index).widget()
            group.setTitle(f"AND group {index + 1}")
            button_layout = group.layout().itemAt(1).layout()
            button_layout.itemAt(1).widget().setEnabled(enabled)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("EAS Alert Control")
        self.resize(1180, 760)
        self.bluetooth = BluetoothController()
        self.alerts: dict[int, AlertRecord] = {}
        self.pending_adds: dict[str, AlertRecord] = {}
        self.pending_removes: dict[str, int] = {}
        self.connected = False

        self._build_ui()
        self._connect_signals()
        self._set_connected(False)

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addWidget(self._build_connection_box())

        main_split = QHBoxLayout()
        main_split.addWidget(self._build_alert_editor(), 3)
        main_split.addWidget(self._build_alert_table(), 4)
        layout.addLayout(main_split, 1)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(130)
        layout.addWidget(self.log)

        self.setCentralWidget(root)

    def _build_connection_box(self) -> QGroupBox:
        box = QGroupBox("Bluetooth connection")
        layout = QGridLayout(box)

        self.service_uuid_edit = QLineEdit()
        self.service_uuid_edit.setPlaceholderText("EASS service UUID")
        self.tx_uuid_edit = QLineEdit()
        self.tx_uuid_edit.setPlaceholderText("TX characteristic UUID")
        self.rx_uuid_edit = QLineEdit()
        self.rx_uuid_edit.setPlaceholderText("RX characteristic UUID")

        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(260)
        self.scan_button = QPushButton("Scan")
        self.connect_button = QPushButton("Connect")
        self.disconnect_button = QPushButton("Disconnect")
        self.connection_status = QLabel("Disconnected")

        layout.addWidget(QLabel("Service"), 0, 0)
        layout.addWidget(self.service_uuid_edit, 0, 1)
        layout.addWidget(QLabel("TX"), 0, 2)
        layout.addWidget(self.tx_uuid_edit, 0, 3)
        layout.addWidget(QLabel("RX"), 0, 4)
        layout.addWidget(self.rx_uuid_edit, 0, 5)
        layout.addWidget(QLabel("Device"), 1, 0)
        layout.addWidget(self.device_combo, 1, 1, 1, 2)
        layout.addWidget(self.scan_button, 1, 3)
        layout.addWidget(self.connect_button, 1, 4)
        layout.addWidget(self.disconnect_button, 1, 5)
        layout.addWidget(self.connection_status, 2, 0, 1, 6)
        layout.setColumnStretch(1, 2)
        layout.setColumnStretch(3, 2)
        layout.setColumnStretch(5, 2)
        return box

    def _build_alert_editor(self) -> QGroupBox:
        self.alert_editor = QGroupBox("Add alert")
        layout = QVBoxLayout(self.alert_editor)

        form = QFormLayout()
        self.alert_id_spin = QSpinBox()
        self.alert_id_spin.setRange(0, 255)
        self.warmup_edit = UInt32LineEdit(0)
        self.cooldown_edit = UInt32LineEdit(0)
        form.addRow("Alert ID", self.alert_id_spin)
        form.addRow("Warmup period (ms)", self.warmup_edit)
        form.addRow("Cooldown period (ms)", self.cooldown_edit)
        layout.addLayout(form)

        notification_box = QGroupBox("Notification")
        notification_layout = QGridLayout(notification_box)
        self.notify_computer_check = QCheckBox("Computer message")
        self.notify_led_check = QCheckBox("LED")
        self.led_color_combo = QComboBox()
        self.led_pattern_combo = QComboBox()
        for color_name, color_value in LED_COLORS.items():
            self.led_color_combo.addItem(color_name, color_value)
        for pattern_name, pattern_value in LED_PATTERNS.items():
            self.led_pattern_combo.addItem(pattern_name, pattern_value)
        notification_layout.addWidget(self.notify_computer_check, 0, 0)
        notification_layout.addWidget(self.notify_led_check, 0, 1)
        notification_layout.addWidget(QLabel("LED color"), 1, 0)
        notification_layout.addWidget(self.led_color_combo, 1, 1)
        notification_layout.addWidget(QLabel("LED pattern"), 2, 0)
        notification_layout.addWidget(self.led_pattern_combo, 2, 1)
        layout.addWidget(notification_box)

        self.condition_editor = ConditionEditor()
        condition_frame = QFrame()
        condition_layout = QVBoxLayout(condition_frame)
        condition_layout.setContentsMargins(0, 0, 0, 0)
        condition_layout.addWidget(QLabel("Alert condition"))
        condition_layout.addWidget(self.condition_editor)
        layout.addWidget(condition_frame, 1)

        self.add_alert_button = QPushButton("Add alert")
        layout.addWidget(self.add_alert_button, alignment=Qt.AlignRight)
        self.notify_computer_check.setChecked(True)
        self._sync_led_controls()
        return self.alert_editor

    def _build_alert_table(self) -> QGroupBox:
        box = QGroupBox("Current alerts")
        layout = QVBoxLayout(box)
        self.alert_table = QTableWidget(0, 6)
        self.alert_table.setHorizontalHeaderLabels(
            ["ID", "Warmup", "Cooldown", "Notification", "Condition", "Status"]
        )
        self.alert_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.alert_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.alert_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.alert_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.alert_table.verticalHeader().setVisible(False)
        layout.addWidget(self.alert_table)

        button_layout = QHBoxLayout()
        self.remove_alert_button = QPushButton("Remove selected alert")
        button_layout.addStretch(1)
        button_layout.addWidget(self.remove_alert_button)
        layout.addLayout(button_layout)
        return box

    def _connect_signals(self) -> None:
        self.scan_button.clicked.connect(self._scan)
        self.connect_button.clicked.connect(self._connect_bluetooth)
        self.disconnect_button.clicked.connect(self.bluetooth.disconnect_from_device)
        self.add_alert_button.clicked.connect(self._queue_add_alert)
        self.remove_alert_button.clicked.connect(self._queue_remove_alert)
        self.notify_led_check.toggled.connect(self._sync_led_controls)

        self.bluetooth.devices_found.connect(self._populate_devices)
        self.bluetooth.connection_changed.connect(self._set_connection_state)
        self.bluetooth.info.connect(self._log_info)
        self.bluetooth.error.connect(self._log_error)
        self.bluetooth.alert_status_changed.connect(self._handle_alert_status)
        self.bluetooth.write_succeeded.connect(self._handle_write_success)
        self.bluetooth.write_failed.connect(self._handle_write_failure)

    def _scan(self) -> None:
        self.scan_button.setEnabled(False)
        self.bluetooth.scan(self.service_uuid_edit.text())

    def _populate_devices(self, devices: list[tuple[str, str]]) -> None:
        self.device_combo.clear()
        for name, address in devices:
            self.device_combo.addItem(f"{name} - {address}", address)
        self.scan_button.setEnabled(True)

    def _connect_bluetooth(self) -> None:
        address = self.device_combo.currentData()
        tx_uuid = self.tx_uuid_edit.text().strip()
        rx_uuid = self.rx_uuid_edit.text().strip()
        if not address:
            self._show_validation_error("Scan for and select an EAS device first.")
            return
        if not tx_uuid or not rx_uuid:
            self._show_validation_error("Enter both TX and RX characteristic UUIDs.")
            return
        self.connect_button.setEnabled(False)
        self.bluetooth.connect_to_device(
            address,
            self.service_uuid_edit.text(),
            tx_uuid,
            rx_uuid,
        )

    def _set_connection_state(self, connected: bool, message: str) -> None:
        self.connection_status.setText(message)
        self._set_connected(connected)

    def _set_connected(self, connected: bool) -> None:
        self.connected = connected
        self.alert_editor.setEnabled(connected)
        self.alert_table.setEnabled(connected)
        self.remove_alert_button.setEnabled(connected)
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        self.scan_button.setEnabled(not connected)
        self.device_combo.setEnabled(not connected)
        self.service_uuid_edit.setEnabled(not connected)
        self.tx_uuid_edit.setEnabled(not connected)
        self.rx_uuid_edit.setEnabled(not connected)

    def _sync_led_controls(self) -> None:
        enabled = self.notify_led_check.isChecked()
        self.led_color_combo.setEnabled(enabled)
        self.led_pattern_combo.setEnabled(enabled)

    def _queue_add_alert(self) -> None:
        try:
            record = self._build_alert_record()
        except ValueError as exc:
            self._show_validation_error(str(exc))
            return

        if record.alert_id in self.alerts:
            self._show_validation_error(f"Alert {record.alert_id} is already in the current alert list.")
            return
        if record.alert_id in self._pending_alert_ids(self.pending_adds):
            self._show_validation_error(f"Alert {record.alert_id} is already pending.")
            return

        action_key = f"add:{record.alert_id}"
        self.pending_adds[action_key] = record
        self.add_alert_button.setEnabled(False)
        self.bluetooth.send_payload(action_key, record.payload)

    def _build_alert_record(self) -> AlertRecord:
        notification_payload, notification_summary = self._notification_payload()
        alert_id = self.alert_id_spin.value()
        warmup_ms = self.warmup_edit.value()
        cooldown_ms = self.cooldown_edit.value()
        condition_payload = self.condition_editor.condition_payload()

        payload = (
            bytes((0x02, alert_id))
            + warmup_ms.to_bytes(4, "little")
            + cooldown_ms.to_bytes(4, "little")
            + notification_payload
            + condition_payload
        )
        return AlertRecord(
            alert_id=alert_id,
            warmup_ms=warmup_ms,
            cooldown_ms=cooldown_ms,
            notification_summary=notification_summary,
            condition_summary=self.condition_editor.summary(),
            payload=payload,
        )

    def _notification_payload(self) -> tuple[bytes, str]:
        bitmap = 0
        summaries = []
        if self.notify_computer_check.isChecked():
            bitmap |= 0x01
            summaries.append("Computer")
        if self.notify_led_check.isChecked():
            bitmap |= 0x02
            color = self.led_color_combo.currentData()
            pattern = self.led_pattern_combo.currentData()
            summaries.append(f"LED {self.led_color_combo.currentText()} {self.led_pattern_combo.currentText()}")
            return bytes((bitmap, color, pattern)), ", ".join(summaries)
        if bitmap == 0:
            raise ValueError("Select at least one notification type.")
        return bytes((bitmap,)), ", ".join(summaries)

    def _queue_remove_alert(self) -> None:
        selected_rows = sorted({index.row() for index in self.alert_table.selectedIndexes()})
        if not selected_rows:
            self._show_validation_error("Select an alert to remove.")
            return
        alert_id_item = self.alert_table.item(selected_rows[0], 0)
        alert_id = int(alert_id_item.text())
        action_key = f"remove:{alert_id}"
        self.pending_removes[action_key] = alert_id
        self.remove_alert_button.setEnabled(False)
        self.bluetooth.send_payload(action_key, bytes((0x01, alert_id)))

    def _handle_write_success(self, action_key: str) -> None:
        if action_key.startswith("add:"):
            record = self.pending_adds.pop(action_key, None)
            if record:
                self.alerts[record.alert_id] = record
                self._upsert_alert_row(record)
                self._log_info(f"Alert {record.alert_id} added.")
            self.add_alert_button.setEnabled(self.connected)
        elif action_key.startswith("remove:"):
            alert_id = self.pending_removes.pop(action_key, None)
            if alert_id is not None:
                self.alerts.pop(alert_id, None)
                self._remove_alert_row(alert_id)
                self._log_info(f"Alert {alert_id} removed.")
            self.remove_alert_button.setEnabled(self.connected)

    def _handle_write_failure(self, action_key: str, reason: str) -> None:
        if action_key.startswith("add:"):
            self.pending_adds.pop(action_key, None)
            self.add_alert_button.setEnabled(self.connected)
        elif action_key.startswith("remove:"):
            self.pending_removes.pop(action_key, None)
            self.remove_alert_button.setEnabled(self.connected)
        self._log_error(f"{action_key} failed: {reason}")

    def _upsert_alert_row(self, record: AlertRecord) -> None:
        row = self._row_for_alert(record.alert_id)
        if row is None:
            row = self.alert_table.rowCount()
            self.alert_table.insertRow(row)
        values = [
            str(record.alert_id),
            f"{record.warmup_ms} ms",
            f"{record.cooldown_ms} ms",
            record.notification_summary,
            record.condition_summary,
            record.status,
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 0:
                item.setData(Qt.UserRole, record.alert_id)
            self.alert_table.setItem(row, column, item)

    def _remove_alert_row(self, alert_id: int) -> None:
        row = self._row_for_alert(alert_id)
        if row is not None:
            self.alert_table.removeRow(row)

    def _handle_alert_status(self, alert_id: int, status_value: int) -> None:
        status = STATUS_NAMES.get(status_value, f"Unknown ({status_value})")
        record = self.alerts.get(alert_id)
        if not record:
            self._log_info(f"Status received for alert {alert_id}: {status}")
            return
        record.status = status
        row = self._row_for_alert(alert_id)
        if row is not None:
            self.alert_table.item(row, 5).setText(status)

    def _row_for_alert(self, alert_id: int) -> int | None:
        for row in range(self.alert_table.rowCount()):
            item = self.alert_table.item(row, 0)
            if item and int(item.text()) == alert_id:
                return row
        return None

    @staticmethod
    def _pending_alert_ids(pending: dict[str, AlertRecord]) -> set[int]:
        return {record.alert_id for record in pending.values()}

    def _show_validation_error(self, message: str) -> None:
        QMessageBox.warning(self, "EAS Alert Control", message)

    def _log_info(self, message: str) -> None:
        self.log.append(message)

    def _log_error(self, message: str) -> None:
        self.log.append(f"ERROR: {message}")
        self.scan_button.setEnabled(not self.connected)
        self.connect_button.setEnabled(not self.connected)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.bluetooth.shutdown()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
