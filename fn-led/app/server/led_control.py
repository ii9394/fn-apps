#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LED控制服务
根据硬盘和网络状态自动控制机箱LED指示灯
支持 HTTP API 配置
"""

import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# ============================================================================
# 日志配置
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("led_control")


# ============================================================================
# 默认配置常量
# ============================================================================

# 预设颜色 (R, G, B)
COLORS = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "white": (255, 255, 255),
    "yellow": (255, 255, 0),
    "cyan": (0, 255, 255),
    "purple": (255, 0, 255),
    "orange": (255, 165, 0),
    "off": (0, 0, 0),
}

# 闪烁速度预设 (on_ms, off_ms)
BLINK_SPEEDS = {
    "veryfast": (81, 76),
    "fast": (125, 250),
    "normal": (274, 271),
    "slow": (495, 483),
}

# 呼吸灯速度预设 (cycle_ms, on_ms)
BREATH_SPEEDS = {
    "fast": (1500, 1000),
    "normal": (2000, 1000),
    "slow": (3000, 1000),
}

# 默认硬盘PCI路径映射
DEFAULT_DISK_PCI_PATHS = {
    "SSD1": "pci-0000:05:00.0-nvme-1",
    "SSD2": "pci-0000:04:00.0-nvme-1",
    "Disk0": "pci-0000:00:0d.0-usb-0:1:1.0-scsi-0:0:0:0",
    "Disk1": "pci-0000:01:00.0-ata-1",
    "Disk2": "pci-0000:01:00.0-ata-2",
    "Disk3": "pci-0000:01:00.0-ata-3",
    "Disk4": "pci-0000:01:00.0-ata-4",
}

# 默认硬盘ID到LED名称映射
DEFAULT_DISK_LED_MAP = {
    "Disk0": "netdev",
    "Disk1": "disk1",
    "Disk2": "disk2",
    "Disk3": "disk3",
    "Disk4": "disk4",
}


# ============================================================================
# 配置数据类
# ============================================================================

@dataclass
class LedConfig:
    """LED控制配置"""
    # LED控制开关
    led_enabled: bool = True
    
    # 网络检测
    internal_gateway: str = "10.0.0.254"
    external_dns: str = "223.5.5.5"
    
    # 推送配置
    push_scheduled_hours: List[int] = field(default_factory=lambda: [8, 12, 18, 22])
    push_confirm_delay: int = 10
    push_hostname: str = "MainNAS"
    
    # LED亮度 (0-255)
    led_brightness: int = 32
    led_brightness_startup: int = 64
    
    # 硬盘PCI路径映射
    disk_pci_paths: Dict[str, str] = field(default_factory=lambda: DEFAULT_DISK_PCI_PATHS.copy())
    
    # 硬盘ID到LED名称映射
    disk_led_map: Dict[str, str] = field(default_factory=lambda: DEFAULT_DISK_LED_MAP.copy())
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    def update(self, data: Dict[str, Any]) -> None:
        for key, value in data.items():
            if hasattr(self, key):
                setattr(self, key, value)
    
    def save(self, path: str) -> bool:
        """保存配置到文件"""
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
            logger.info(f"配置已保存到 {path}")
            return True
        except Exception as e:
            logger.warning(f"保存配置失败: {e}")
            return False
    
    @classmethod
    def load(cls, path: str) -> "LedConfig":
        """从文件加载配置"""
        config = cls()
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                config.update(data)
                logger.info(f"已加载配置: {path}")
        except Exception as e:
            logger.warning(f"加载配置失败，使用默认值: {e}")
        return config


# ============================================================================
# LED控制器
# ============================================================================

class LedController:
    """LED控制器 - 封装对 ugreen_leds_cli 的调用"""
    
    def __init__(self, config: LedConfig):
        self.config = config
        self.cli_path = self._find_cli()
        if not self.cli_path:
            logger.error("未找到 ugreen_leds_cli 程序")
    
    def _find_cli(self) -> Optional[str]:
        """查找LED控制程序"""
        search_paths = [
            Path(__file__).parent / "ugreen_leds_cli",
            Path(__file__).parent.parent / "bin" / "ugreen_leds_cli",
            Path("/opt/ugreen-led-controller/ugreen_leds_cli"),
            Path("/usr/bin/ugreen_leds_cli"),
            Path("/usr/local/bin/ugreen_leds_cli"),
        ]
        for path in search_paths:
            if path.exists() and os.access(path, os.X_OK):
                return str(path)
        return None
    
    def _run_cmd(self, args: List[str]) -> bool:
        """执行LED命令"""
        if not self.cli_path:
            return False
        try:
            cmd = [self.cli_path] + args
            result = subprocess.run(cmd, capture_output=True, timeout=5)
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"执行LED命令失败: {e}")
            return False
    
    def set_led(self, name: str, color: str, brightness: int = None) -> bool:
        """设置LED颜色"""
        if brightness is None:
            brightness = self.config.led_brightness
        rgb = COLORS.get(color, COLORS["white"])
        args = [name, "-color", str(rgb[0]), str(rgb[1]), str(rgb[2]),
                "-brightness", str(brightness), "-on"]
        return self._run_cmd(args)
    
    def set_blink(self, name: str, color: str, brightness: int = None, 
                  speed: str = "normal") -> bool:
        """设置LED闪烁"""
        if brightness is None:
            brightness = self.config.led_brightness
        rgb = COLORS.get(color, COLORS["white"])
        on_ms, off_ms = BLINK_SPEEDS.get(speed, BLINK_SPEEDS["normal"])
        args = [name, "-color", str(rgb[0]), str(rgb[1]), str(rgb[2]),
                "-brightness", str(brightness), "-blink", str(on_ms), str(off_ms)]
        return self._run_cmd(args)
    
    def set_breath(self, name: str, color: str, brightness: int = None,
                   speed: str = "normal") -> bool:
        """设置LED呼吸灯"""
        if brightness is None:
            brightness = self.config.led_brightness
        rgb = COLORS.get(color, COLORS["white"])
        cycle_ms, on_ms = BREATH_SPEEDS.get(speed, BREATH_SPEEDS["normal"])
        args = [name, "-color", str(rgb[0]), str(rgb[1]), str(rgb[2]),
                "-brightness", str(brightness), "-breath", str(cycle_ms), str(on_ms)]
        return self._run_cmd(args)
    
    def turn_off(self, name: str) -> bool:
        """关闭LED"""
        return self._run_cmd([name, "-off"])
    
    def turn_off_all(self) -> bool:
        """关闭所有LED"""
        return self._run_cmd(["all", "-off"])
    
    def blink_all(self, color: str, brightness: int = None, speed: str = "fast") -> bool:
        """所有LED闪烁"""
        if brightness is None:
            brightness = self.config.led_brightness_startup
        leds = ["power", "netdev", "disk1", "disk2", "disk3", "disk4"]
        success = True
        for led in leds:
            if not self.set_blink(led, color, brightness, speed):
                success = False
        return success


# ============================================================================
# 硬盘监控
# ============================================================================

@dataclass
class DiskInfo:
    """硬盘信息"""
    disk_id: str          # Disk1, SSD1 等
    device: str = ""      # sda, nvme0n1 等
    is_sleeping: bool = False
    busy_percent: int = 0


class DiskMonitor:
    """硬盘监控器"""
    
    def __init__(self, config: LedConfig):
        self.config = config
        self.disks: Dict[str, DiskInfo] = {}
        self._iostat_thread: Optional[threading.Thread] = None
        self._iostat_running = False
        self._busy_data: Dict[str, int] = {}
        self._busy_lock = threading.Lock()
    
    def find_disks(self) -> Dict[str, str]:
        """检测所有硬盘，返回 {disk_id: device_name}"""
        result = {}
        by_path = Path("/dev/disk/by-path")
        
        if not by_path.exists():
            return result
        
        for disk_id, pci_pattern in self.config.disk_pci_paths.items():
            try:
                for entry in by_path.iterdir():
                    if pci_pattern in entry.name and "part" not in entry.name:
                        real_path = entry.resolve()
                        device = real_path.name
                        result[disk_id] = device
                        break
            except Exception as e:
                logger.debug(f"检测硬盘 {disk_id} 失败: {e}")
        
        return result
    
    def update_disk_map(self):
        """更新硬盘映射"""
        disk_map = self.find_disks()
        for disk_id, device in disk_map.items():
            if disk_id not in self.disks:
                self.disks[disk_id] = DiskInfo(disk_id=disk_id)
            self.disks[disk_id].device = device
    
    def check_sleep_status(self, device: str) -> bool:
        """检查硬盘是否休眠"""
        if not device or device.startswith("nvme"):
            return False
        try:
            result = subprocess.run(
                ["hdparm", "-C", f"/dev/{device}"],
                capture_output=True, text=True, timeout=5
            )
            return "standby" in result.stdout.lower()
        except Exception:
            return False
    
    def get_busy_percent(self, device: str) -> int:
        """获取硬盘繁忙度"""
        with self._busy_lock:
            return self._busy_data.get(device, 0)
    
    def start_iostat_monitor(self):
        """启动 iostat 监控线程"""
        if self._iostat_thread and self._iostat_thread.is_alive():
            return
        self._iostat_running = True
        self._iostat_thread = threading.Thread(target=self._iostat_loop, daemon=True)
        self._iostat_thread.start()
        logger.info("iostat 监控线程已启动")
    
    def stop_iostat_monitor(self):
        """停止 iostat 监控"""
        self._iostat_running = False
        if self._iostat_thread:
            self._iostat_thread.join(timeout=2)
    
    def _iostat_loop(self):
        """iostat 监控循环"""
        try:
            process = subprocess.Popen(
                ["iostat", "-x", "1"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
            )
            while self._iostat_running and process.poll() is None:
                line = process.stdout.readline()
                if line.startswith(("sd", "nvme")):
                    parts = line.split()
                    if len(parts) >= 2:
                        device = parts[0]
                        try:
                            util = int(float(parts[-1].replace("%", "")))
                            with self._busy_lock:
                                self._busy_data[device] = util
                        except ValueError:
                            pass
            process.terminate()
        except Exception as e:
            logger.error(f"iostat 监控异常: {e}")
    
    def update_all_status(self):
        """更新所有硬盘状态"""
        self.update_disk_map()
        for disk_id, info in self.disks.items():
            if not info.device:
                continue
            info.is_sleeping = self.check_sleep_status(info.device)
            if not info.is_sleeping:
                info.busy_percent = self.get_busy_percent(info.device)
    
    def get_status(self) -> Dict[str, Any]:
        """获取硬盘状态"""
        return {
            disk_id: {
                "device": info.device,
                "is_sleeping": info.is_sleeping,
                "busy_percent": info.busy_percent
            }
            for disk_id, info in self.disks.items()
        }


# ============================================================================
# 网络监控
# ============================================================================

@dataclass
class NetworkStatus:
    """网络状态"""
    internal_ok: bool = False
    external_ok: bool = False


class NetworkMonitor:
    """网络监控器（后台异步检测）"""
    
    def __init__(self, config: LedConfig):
        self.config = config
        self.status = NetworkStatus()
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
    
    def _ping(self, ip: str, count: int = 1, timeout: int = 1) -> bool:
        """Ping 检测"""
        try:
            result = subprocess.run(
                ["ping", "-c", str(count), "-W", str(timeout), ip],
                capture_output=True, timeout=timeout + 1
            )
            return result.returncode == 0
        except Exception:
            return False
    
    def _check_loop(self):
        """后台检测循环"""
        while self._running:
            internal_ok = self._ping(self.config.internal_gateway)
            external_ok = self._ping(self.config.external_dns) if internal_ok else False
            with self._lock:
                self.status.internal_ok = internal_ok
                self.status.external_ok = external_ok
            time.sleep(2)  # 每2秒检测一次
    
    def start(self):
        """启动后台检测"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()
        logger.info("网络监控后台线程已启动")
    
    def stop(self):
        """停止后台检测"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
    
    def get_status(self) -> NetworkStatus:
        """获取当前网络状态（非阻塞）"""
        with self._lock:
            return NetworkStatus(self.status.internal_ok, self.status.external_ok)


# ============================================================================
# 消息推送
# ============================================================================

class PushNotifier:
    """消息推送器"""
    
    def __init__(self, config: LedConfig):
        self.config = config
        self._last_sleep_states: Dict[str, bool] = {}
        self._last_health_states: Dict[str, bool] = {}
        self._last_push_hour: int = -1
        self._pending_change_time: float = 0
        self._pending_change_data: Optional[Dict[str, bool]] = None
    
    def _send_push(self, message: str, tag: str = "消息推送") -> bool:
        """发送推送消息（后台执行，不阻塞）"""
        try:
            # 检查 push 命令是否存在
            result = subprocess.run(["which", "push"], capture_output=True)
            if result.returncode != 0:
                return False
            
            # 后台执行，不等待结果
            subprocess.Popen(
                ["push", message],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True  # 避免僵尸进程
            )
            logger.info(f"{tag}: 推送已发送 - {message}")
            return True
        except Exception as e:
            logger.error(f"{tag}: 推送异常 - {e}")
            return False
    
    def check_sleep_change(self, disks: Dict[str, DiskInfo]) -> None:
        """检查硬盘休眠状态变化并推送（只检查Disk1-4）"""
        current_states = {}
        changed = []
        
        # 只检查 Disk1-4，不检查 USB 硬盘和 SSD
        push_disk_ids = ["Disk1", "Disk2", "Disk3", "Disk4"]
        
        for disk_id in push_disk_ids:
            disk = disks.get(disk_id)
            if disk and disk.device:
                current_states[disk_id] = disk.is_sleeping
                last_state = self._last_sleep_states.get(disk_id)
                if last_state is not None and last_state != disk.is_sleeping:
                    status = "休眠" if disk.is_sleeping else "唤醒"
                    changed.append(f"{disk_id}({status})")
        
        if changed:
            if self._pending_change_data is None:
                self._pending_change_time = time.time()
                self._pending_change_data = current_states.copy()
                return
            
            if time.time() - self._pending_change_time < self.config.push_confirm_delay:
                return
            
            status_icons = []
            for disk_id in push_disk_ids:
                disk = disks.get(disk_id)
                if disk and disk.device:
                    status_icons.append("🔵" if disk.is_sleeping else "🔴")
                else:
                    status_icons.append("⚪")
            
            message = f"[{self.config.push_hostname}]: {' '.join(status_icons)}"
            self._send_push(message, "硬盘状态变化")
            self._pending_change_data = None
        else:
            self._pending_change_data = None
        
        self._last_sleep_states = current_states
    
    def check_offline_change(self, disks: Dict[str, DiskInfo]) -> None:
        """检查硬盘离线状态并推送（只检查Disk1-4）"""
        current_hour = time.localtime().tm_hour
        offline_disks = []
        new_offline = []
        
        # 只检查 Disk1-4，不检查 USB 硬盘和 SSD
        push_disk_ids = ["Disk1", "Disk2", "Disk3", "Disk4"]
        for disk_id in push_disk_ids:
            disk = disks.get(disk_id)
            was_online = self._last_health_states.get(disk_id, True)
            
            if disk is None or not disk.device:
                offline_disks.append(disk_id)
                if was_online:
                    new_offline.append(disk_id)
                self._last_health_states[disk_id] = False
            else:
                self._last_health_states[disk_id] = True
        
        if new_offline:
            self._send_push(f"[{self.config.push_hostname}]: ⚠️ 硬盘离线: {', '.join(new_offline)}", "硬盘离线")
            return
        
        if current_hour in self.config.push_scheduled_hours and self._last_push_hour != current_hour:
            self._last_push_hour = current_hour
            if offline_disks:
                self._send_push(f"[{self.config.push_hostname}]: ⚠️ 硬盘离线: {', '.join(offline_disks)}", "定时推送")


# ============================================================================
# LED状态管理
# ============================================================================

class LedState(Enum):
    """LED状态枚举"""
    OFF = "off"
    RED_ON = "red_on"
    RED_BLINK = "red_blink"
    BLUE_ON = "blue_on"
    BLUE_BREATH = "blue_breath"
    YELLOW_ON = "yellow_on"
    YELLOW_BLINK_SLOW = "yellow_blink_slow"
    YELLOW_BLINK_NORMAL = "yellow_blink_normal"
    YELLOW_BLINK_FAST = "yellow_blink_fast"
    YELLOW_BLINK_VERYFAST = "yellow_blink_veryfast"
    WHITE_BLINK = "white_blink"


class LedStateManager:
    """LED状态管理器"""
    
    def __init__(self, controller: LedController, config: LedConfig):
        self.controller = controller
        self.config = config
        self._current_states: Dict[str, LedState] = {}
    
    def _apply_state(self, led_name: str, state: LedState) -> bool:
        """应用LED状态"""
        b = self.config.led_brightness
        if state == LedState.OFF:
            return self.controller.turn_off(led_name)
        elif state == LedState.RED_ON:
            return self.controller.set_led(led_name, "red", b)
        elif state == LedState.RED_BLINK:
            return self.controller.set_blink(led_name, "red", b, "normal")
        elif state == LedState.BLUE_ON:
            return self.controller.set_led(led_name, "blue", b)
        elif state == LedState.BLUE_BREATH:
            return self.controller.set_breath(led_name, "blue", b, "fast")
        elif state == LedState.YELLOW_ON:
            return self.controller.set_led(led_name, "yellow", b)
        elif state == LedState.YELLOW_BLINK_SLOW:
            return self.controller.set_blink(led_name, "yellow", b, "slow")
        elif state == LedState.YELLOW_BLINK_NORMAL:
            return self.controller.set_blink(led_name, "yellow", b, "normal")
        elif state == LedState.YELLOW_BLINK_FAST:
            return self.controller.set_blink(led_name, "yellow", b, "fast")
        elif state == LedState.YELLOW_BLINK_VERYFAST:
            return self.controller.set_blink(led_name, "yellow", b, "veryfast")
        elif state == LedState.WHITE_BLINK:
            return self.controller.set_blink(led_name, "white", self.config.led_brightness_startup, "fast")
        return False
    
    def set_state(self, led_name: str, state: LedState) -> bool:
        """设置LED状态"""
        current = self._current_states.get(led_name)
        if current == state:
            return True
        if self._apply_state(led_name, state):
            self._current_states[led_name] = state
            logger.info(f"LED {led_name}: {current} -> {state.value}")
            return True
        return False
    
    def get_current_states(self) -> Dict[str, str]:
        """获取当前LED状态"""
        return {name: state.value for name, state in self._current_states.items()}
    
    def determine_power_state(self, network: NetworkStatus) -> LedState:
        """根据网络状态确定POWER灯状态"""
        if network.internal_ok and network.external_ok:
            return LedState.BLUE_BREATH
        elif not network.internal_ok and not network.external_ok:
            return LedState.RED_BLINK
        else:
            return LedState.YELLOW_BLINK_NORMAL
    
    def determine_disk_state(self, disk: DiskInfo) -> LedState:
        """根据硬盘状态确定LED状态"""
        if not disk.device:
            return LedState.RED_BLINK
        if disk.is_sleeping:
            return LedState.BLUE_ON
        busy = disk.busy_percent
        if busy == 0:
            return LedState.YELLOW_ON
        elif busy <= 25:
            return LedState.YELLOW_BLINK_SLOW
        elif busy <= 50:
            return LedState.YELLOW_BLINK_NORMAL
        elif busy <= 75:
            return LedState.YELLOW_BLINK_FAST
        else:
            return LedState.YELLOW_BLINK_VERYFAST


# ============================================================================
# 主服务
# ============================================================================

class MonitorService:
    """LED监控服务"""
    
    def __init__(self, config_path: str = None):
        self.config_path = config_path
        self.config = LedConfig.load(config_path) if config_path else LedConfig()
        
        self.running = False
        self.controller = LedController(self.config)
        self.disk_monitor = DiskMonitor(self.config)
        self.network_monitor = NetworkMonitor(self.config)
        self.led_manager = LedStateManager(self.controller, self.config)
        self.push_notifier = PushNotifier(self.config)
        self.lock = threading.RLock()
        
        self._last_network_status = NetworkStatus()
        self._simulated_states: Dict[str, str] = {}
        self._load_i2c_module()
    
    def _load_i2c_module(self):
        """加载 i2c-dev 内核模块"""
        try:
            subprocess.run(["modprobe", "i2c-dev"], capture_output=True, timeout=5)
        except Exception as e:
            logger.warning(f"加载 i2c-dev 模块失败: {e}")
    
    def _show_startup_indicator(self):
        """显示启动提示"""
        logger.info("启动提示: LED白色闪烁")
        self.controller.blink_all("white")
        time.sleep(5)
        self.controller.turn_off_all()
        time.sleep(2)
    
    def _update_leds(self):
        """更新所有LED状态"""
        # 更新网络和硬盘状态（用于前端显示）
        network = self.network_monitor.get_status()
        self._last_network_status = network
        self.disk_monitor.update_all_status()
        
        # 计算LED状态（用于前端模拟）
        power_state = self.led_manager.determine_power_state(network)
        disk_states = {}
        for disk_id, led_name in self.config.disk_led_map.items():
            disk = self.disk_monitor.disks.get(disk_id)
            if disk:
                disk_states[led_name] = self.led_manager.determine_disk_state(disk)
            else:
                disk_states[led_name] = LedState.RED_BLINK
        
        # 只有启用时才控制物理LED
        if self.config.led_enabled:
            self.led_manager.set_state("power", power_state)
            for led_name, state in disk_states.items():
                self.led_manager.set_state(led_name, state)
        
        # 更新模拟状态（即使LED关闭也更新，用于前端显示）
        with self.lock:
            self._simulated_states = {"power": power_state.value}
            for led_name, state in disk_states.items():
                self._simulated_states[led_name] = state.value
        
        self.push_notifier.check_sleep_change(self.disk_monitor.disks)
        self.push_notifier.check_offline_change(self.disk_monitor.disks)
    
    def get_status(self) -> Dict[str, Any]:
        """获取服务状态"""
        with self.lock:
            # 返回模拟状态（即使物理LED关闭也显示应有的状态）
            leds = self._simulated_states if self._simulated_states else self.led_manager.get_current_states()
            return {
                "running": self.running,
                "led_enabled": self.config.led_enabled,
                "network": {
                    "internal_ok": self._last_network_status.internal_ok,
                    "external_ok": self._last_network_status.external_ok,
                },
                "disks": self.disk_monitor.get_status(),
                "leds": leds,
            }
    
    def toggle_leds(self, enabled: bool) -> bool:
        """开关LED控制"""
        with self.lock:
            self.config.led_enabled = enabled
            if not enabled:
                # 关闭所有物理LED
                self.controller.turn_off_all()
                self.led_manager._current_states.clear()
            if self.config_path:
                self.config.save(self.config_path)
            logger.info(f"LED控制已{'启用' if enabled else '禁用'}")
            return True
    
    def get_config(self) -> Dict[str, Any]:
        """获取配置"""
        with self.lock:
            return self.config.to_dict()
    
    def update_config(self, data: Dict[str, Any]) -> None:
        """更新配置"""
        with self.lock:
            self.config.update(data)
            if self.config_path:
                self.config.save(self.config_path)
    
    def start(self):
        """启动服务"""
        logger.info("=" * 50)
        logger.info("LED控制服务启动")
        logger.info("=" * 50)
        
        self.running = True
        self.network_monitor.start()
        self.disk_monitor.start_iostat_monitor()
        self._show_startup_indicator()
        
        logger.info("进入主循环")
        while self.running:
            try:
                self._update_leds()
            except Exception as e:
                logger.exception(f"更新LED状态异常: {e}")
            time.sleep(1)
    
    def stop(self):
        """停止服务"""
        logger.info("正在停止服务...")
        self.running = False
        self.network_monitor.stop()
        self.disk_monitor.stop_iostat_monitor()
        self.controller.turn_off_all()
        logger.info("服务已停止")


# ============================================================================
# HTTP API
# ============================================================================

service: Optional[MonitorService] = None


class APIHandler(BaseHTTPRequestHandler):
    """API 请求处理"""
    
    def log_message(self, format, *args):
        logger.debug(f"API: {format % args}")
    
    def _json_response(self, data: Any, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    
    def _read_json(self) -> Dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                body = self.rfile.read(length)
                return json.loads(body.decode("utf-8"))
        except Exception:
            pass
        return {}
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
    
    def do_GET(self):
        self._handle_request("GET")
    
    def do_POST(self):
        self._handle_request("POST")
    
    def do_PUT(self):
        self._handle_request("PUT")
    
    def _handle_request(self, method: str) -> None:
        global service
        if service is None:
            self._json_response({"error": "service not initialized"}, 500)
            return
        
        path = urlparse(self.path).path.rstrip("/")
        
        try:
            if path == "/api/status" and method == "GET":
                self._json_response({"status": service.get_status()})
                return
            
            if path == "/api/config" and method == "GET":
                self._json_response({"config": service.get_config()})
                return
            
            if path == "/api/config" and method == "PUT":
                data = self._read_json()
                service.update_config(data)
                self._json_response({"success": True, "config": service.get_config()})
                return
            
            if path == "/api/toggle" and method == "POST":
                data = self._read_json()
                enabled = data.get("enabled", True)
                service.toggle_leds(bool(enabled))
                self._json_response({"success": True, "led_enabled": service.config.led_enabled})
                return
            
            self._json_response({"error": "not found"}, 404)
        except Exception as e:
            logger.exception(f"API 错误: {e}")
            self._json_response({"error": str(e)}, 500)


def run_server(unix_socket: str = None, config_path: str = None):
    """运行 HTTP 服务"""
    global service
    
    if not config_path and unix_socket:
        config_path = os.path.join(os.path.dirname(unix_socket), "config.json")
    
    service = MonitorService(config_path=config_path)
    
    # 在后台线程运行监控服务
    monitor_thread = threading.Thread(target=service.start, daemon=True)
    monitor_thread.start()
    
    if unix_socket:
        if os.path.exists(unix_socket):
            os.unlink(unix_socket)
        
        server = ThreadingHTTPServer(("", 0), APIHandler, bind_and_activate=False)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(unix_socket)
        server.socket = sock
        server.address_family = socket.AF_UNIX
        server.server_address = unix_socket
        server.server_activate()
        logger.info(f"LED控制服务启动于 unix://{unix_socket}")
    else:
        server = ThreadingHTTPServer(("0.0.0.0", 28258), APIHandler)
        logger.info("LED控制服务启动于 http://0.0.0.0:28258")
    
    shutdown_event = threading.Event()
    
    def handle_signal(signum, frame):
        if shutdown_event.is_set():
            return
        shutdown_event.set()
        logger.info("正在关闭...")
        service.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()
    
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if unix_socket and os.path.exists(unix_socket):
            os.unlink(unix_socket)


# ============================================================================
# 主入口
# ============================================================================

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="LED控制服务")
    parser.add_argument("--unix-socket", help="Unix socket 路径")
    parser.add_argument("--config", help="配置文件路径")
    
    args = parser.parse_args()
    run_server(unix_socket=args.unix_socket, config_path=args.config)


if __name__ == "__main__":
    main()
