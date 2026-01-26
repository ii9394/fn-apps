#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
风扇自动调控服务
根据 CPU 和硬盘温度自动调整风扇 PWM 值
独立运行，使用内存存储，支持 HTTP API
"""

import json
import logging
import os
import re
import signal
import socket
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from subprocess import run, Popen, PIPE, DEVNULL
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fan_control")

###############################################################################
# 配置数据类
###############################################################################

# 默认风扇曲线：[{temp: 温度, pwm: PWM值}, ...]
# CPU: 20-90°C, 间隔10°C
DEFAULT_CPU_CURVE = [
    {"temp": 20, "pwm": 20},
    {"temp": 30, "pwm": 30},
    {"temp": 40, "pwm": 50},
    {"temp": 50, "pwm": 80},
    {"temp": 60, "pwm": 120},
    {"temp": 65, "pwm": 160},
    {"temp": 70, "pwm": 210},
    {"temp": 80, "pwm": 255},
]

# 硬盘: 20-70°C, 间隔约7°C
DEFAULT_DISK_CURVE = [
    {"temp": 20, "pwm": 20},
    {"temp": 26, "pwm": 35},
    {"temp": 32, "pwm": 55},
    {"temp": 38, "pwm": 85},
    {"temp": 44, "pwm": 130},
    {"temp": 50, "pwm": 175},
    {"temp": 55, "pwm": 220},
    {"temp": 60, "pwm": 255},
]


@dataclass
class FanConfig:
    """风扇控制配置"""
    enabled: bool = True
    check_interval: float = 2.5
    temp_history_size: int = 4  # 平均采样次数，同时也是预热次数
    pwm_change_threshold: int = 0
    
    # 温度告警配置
    alert_enabled: bool = True  # 是否启用温度告警推送
    cpu_alert_temp: int = 62  # CPU告警温度阈值
    disk_alert_temp: int = 42  # 硬盘告警温度阈值
    alert_interval: int = 60  # 告警推送间隔（秒），防止重复推送
    alert_hostname: str = "MainNAS"  # 告警消息中的主机名
    
    # PWM 控制文件
    pwm_control_file: str = "/sys/class/hwmon/hwmon4/pwm3"
    pwm_enable_file: str = "/sys/class/hwmon/hwmon4/pwm3_enable"
    
    # 风扇曲线模式（True=曲线模式，False=旧阈值模式）
    use_curve_mode: bool = True
    
    # 风扇曲线配置：[{temp, pwm}, ...]
    cpu_curve: List[Dict[str, int]] = field(default_factory=lambda: DEFAULT_CPU_CURVE.copy())
    disk_curve: List[Dict[str, int]] = field(default_factory=lambda: DEFAULT_DISK_CURVE.copy())
    
    # ====== 以下为旧阈值模式配置（向后兼容）======
    # CPU 温度阈值
    cpu_idle_temp_min: int = 45
    cpu_idle_temp_max: int = 50
    cpu_work_temp_max: int = 62
    cpu_warning_temp_max: int = 72
    cpu_critical_temp_max: int = 80
    
    # 硬盘温度阈值
    disk_idle_temp_min: int = 38
    disk_idle_temp_max: int = 40
    disk_work_temp_max: int = 42
    disk_warning_temp_max: int = 44
    disk_critical_temp_max: int = 46
    
    # PWM 值范围
    idle_pwm_min: int = 30
    idle_pwm_max: int = 60
    work_pwm_min: int = 60
    work_pwm_max: int = 150
    warning_pwm_min: int = 150
    warning_pwm_max: int = 220
    critical_pwm_min: int = 220
    critical_pwm_max: int = 255
    
    # 用户选择的参与调速的硬盘 ID 列表
    active_disks: List[str] = field(default_factory=list)
    
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
    def load(cls, path: str) -> "FanConfig":
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


@dataclass
class DiskInfo:
    """硬盘信息"""
    id: str  # 唯一标识，如 Disk1, SSD1
    device: str  # 设备名，如 sda, nvme0n1
    path: str  # 完整路径，如 /dev/sda
    pci_path: str  # PCI 路径
    model: str = ""  # 型号
    serial: str = ""  # 序列号
    size: str = ""  # 容量
    disk_type: str = "HDD"  # HDD 或 SSD/NVMe
    temp: Optional[int] = None  # 当前温度
    active: bool = False  # 是否参与调速


###############################################################################
# 硬盘检测
###############################################################################

def detect_all_disks() -> List[DiskInfo]:
    """自动检测所有硬盘"""
    disks = []
    by_path_dir = "/dev/disk/by-path"
    
    if not os.path.exists(by_path_dir):
        logger.warning("找不到 /dev/disk/by-path 目录")
        return disks
    
    seen_devices = set()
    disk_counter = {"ata": 0, "nvme": 0, "usb": 0}
    
    try:
        for entry in sorted(os.listdir(by_path_dir)):
            # 跳过分区
            if "part" in entry:
                continue
            
            link_path = os.path.join(by_path_dir, entry)
            if not os.path.islink(link_path):
                continue
            
            try:
                real_path = os.path.realpath(link_path)
                device = os.path.basename(real_path)
                
                # 跳过已处理的设备
                if device in seen_devices:
                    continue
                seen_devices.add(device)
                
                # 确定硬盘类型和 ID
                if "nvme" in entry:
                    disk_counter["nvme"] += 1
                    disk_id = f"NVMe{disk_counter['nvme']}"
                    disk_type = "NVMe"
                elif "usb" in entry:
                    disk_counter["usb"] += 1
                    disk_id = f"USB{disk_counter['usb']}"
                    disk_type = "USB"
                elif "ata" in entry:
                    disk_counter["ata"] += 1
                    disk_id = f"Disk{disk_counter['ata']}"
                    # 通过 rotational 判断是 HDD 还是 SSD
                    disk_type = "HDD"
                    rot_path = f"/sys/block/{device}/queue/rotational"
                    if os.path.exists(rot_path):
                        with open(rot_path) as f:
                            if f.read().strip() == "0":
                                disk_type = "SSD"
                else:
                    continue
                
                # 获取硬盘详细信息
                model, serial, size = get_disk_info(device)
                
                disks.append(DiskInfo(
                    id=disk_id,
                    device=device,
                    path=real_path,
                    pci_path=entry,
                    model=model,
                    serial=serial,
                    size=size,
                    disk_type=disk_type,
                ))
            except Exception as e:
                logger.debug(f"处理 {entry} 时出错: {e}")
    except Exception as e:
        logger.error(f"检测硬盘失败: {e}")
    
    return disks


def get_disk_info(device: str) -> tuple:
    """获取硬盘的型号、序列号、容量"""
    model, serial, size = "", "", ""
    
    try:
        # 使用 lsblk 获取基本信息
        result = run(
            ["lsblk", "-d", "-o", "MODEL,SERIAL,SIZE", "-n", f"/dev/{device}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split()
            if len(parts) >= 1:
                # MODEL 可能包含空格，SIZE 在最后
                size = parts[-1] if parts else ""
                model = " ".join(parts[:-1]) if len(parts) > 1 else parts[0] if parts else ""
        
        # 尝试从 smartctl 获取更详细信息
        result = run(
            ["smartctl", "-i", f"/dev/{device}", "-j"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                model = data.get("model_name", model) or model
                serial = data.get("serial_number", serial) or serial
            except json.JSONDecodeError:
                pass
    except Exception as e:
        logger.debug(f"获取硬盘 {device} 信息失败: {e}")
    
    return model.strip(), serial.strip(), size.strip()


###############################################################################
# 温度读取
###############################################################################

def read_cpu_temp() -> Optional[int]:
    """读取 CPU 温度"""
    try:
        result = run(["sensors"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                # 匹配常见的 CPU 温度标签
                if any(tag in line for tag in ["Package id", "Tctl", "Tdie", "Core 0"]):
                    match = re.search(r"[+]?(\d+(?:\.\d+)?)[°]?C", line)
                    if match:
                        return int(float(match.group(1)))
    except Exception as e:
        logger.debug(f"读取 CPU 温度失败: {e}")
    return None


def read_disk_temp(device: str) -> Optional[int]:
    """读取硬盘温度"""
    if not device:
        return None
    
    try:
        # 使用 standby 模式避免唤醒休眠的硬盘
        result = run(
            ["smartctl", "-n", "standby", "-A", f"/dev/{device}", "-j"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode in (0, 2):  # 2 表示硬盘处于待机状态
            try:
                data = json.loads(result.stdout)
                
                # SATA 硬盘
                if "ata_smart_attributes" in data:
                    for attr in data["ata_smart_attributes"].get("table", []):
                        name = attr.get("name", "")
                        if name in ("Temperature_Celsius", "Airflow_Temperature_Cel"):
                            raw = attr.get("raw", {}).get("value", 0)
                            return raw % 256
                
                # NVMe SSD
                if "temperature" in data:
                    return data["temperature"].get("current")
                if "nvme_smart_health_information_log" in data:
                    return data["nvme_smart_health_information_log"].get("temperature")
            except json.JSONDecodeError:
                pass
    except Exception as e:
        logger.debug(f"读取 {device} 温度失败: {e}")
    return None


def read_fan_rpm() -> Optional[int]:
    """读取风扇转速，返回第一个非零的转速值"""
    try:
        result = run(["sensors"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                # 匹配风扇转速
                if re.match(r"fan\d+:", line.lower()):
                    match = re.search(r"(\d+)\s*RPM", line)
                    if match:
                        rpm = int(match.group(1))
                        # 跳过转速为 0 的风扇，继续查找
                        if rpm > 0:
                            return rpm
    except Exception as e:
        logger.debug(f"读取风扇转速失败: {e}")
    return None


def read_pwm(pwm_file: str) -> Optional[int]:
    """读取当前 PWM 值"""
    try:
        if os.path.exists(pwm_file):
            with open(pwm_file) as f:
                return int(f.read().strip())
    except Exception as e:
        logger.debug(f"读取 PWM 失败: {e}")
    return None


def set_pwm(pwm_file: str, value: int) -> bool:
    """设置 PWM 值"""
    try:
        value = max(0, min(255, value))
        if os.path.exists(pwm_file):
            with open(pwm_file, "w") as f:
                f.write(str(value))
            return True
    except Exception as e:
        logger.warning(f"设置 PWM 失败: {e}")
    return False


def load_it87_module() -> bool:
    """加载 it87 内核模块"""
    try:
        result = run(
            ["modprobe", "it87", "force_id=0x8620"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            logger.info("成功加载 it87 内核模块 (force_id=0x8620)")
            return True
        else:
            logger.warning(f"加载 it87 模块失败: {result.stderr}")
            return False
    except Exception as e:
        logger.warning(f"加载 it87 模块异常: {e}")
        return False


def enable_manual_pwm(enable_file: str) -> bool:
    """启用 PWM 手动控制模式"""
    try:
        if os.path.exists(enable_file):
            # 先尝试使用 sudo tee（用户要求的方式）
            result = run(
                ["sh", "-c", f"echo 1 | sudo tee {enable_file}"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                logger.info(f"成功启用 PWM 手动控制: {enable_file}")
                return True
            else:
                # 如果 sudo tee 失败，尝试直接写入（可能已有足够权限）
                try:
                    with open(enable_file, "w") as f:
                        f.write("1")
                    logger.info(f"通过直接写入启用 PWM 手动控制: {enable_file}")
                    return True
                except PermissionError:
                    logger.warning(f"权限不足，无法启用 PWM 手动控制: {enable_file}")
                    return False
    except Exception as e:
        logger.warning(f"启用手动 PWM 控制失败: {e}")
    return False


###############################################################################
# PWM 计算
###############################################################################

def linear_map(value: int, in_min: int, in_max: int, out_min: int, out_max: int) -> int:
    """线性映射"""
    if in_max <= in_min:
        return out_min
    ratio = (value - in_min) / (in_max - in_min)
    result = out_min + ratio * (out_max - out_min)
    return max(out_min, min(out_max, int(result)))


def calculate_pwm_from_curve(temp: int, curve: List[Dict[str, int]]) -> tuple:
    """根据曲线计算 PWM 值，返回 (pwm, stage)"""
    if not curve:
        return 100, "unknown"
    
    # 按温度排序
    sorted_curve = sorted(curve, key=lambda p: p["temp"])
    
    # 温度低于曲线最低点
    if temp <= sorted_curve[0]["temp"]:
        return sorted_curve[0]["pwm"], "idle"
    
    # 温度高于曲线最高点
    if temp >= sorted_curve[-1]["temp"]:
        return sorted_curve[-1]["pwm"], "critical"
    
    # 在曲线中间，找到温度所在的区间进行线性插值
    for i in range(len(sorted_curve) - 1):
        p1, p2 = sorted_curve[i], sorted_curve[i + 1]
        if p1["temp"] <= temp <= p2["temp"]:
            pwm = linear_map(temp, p1["temp"], p2["temp"], p1["pwm"], p2["pwm"])
            # 根据 PWM 值判断阶段
            if pwm < 60:
                stage = "idle"
            elif pwm < 120:
                stage = "work"
            elif pwm < 200:
                stage = "warning"
            else:
                stage = "critical"
            return pwm, stage
    
    return 100, "unknown"


def calculate_pwm(temp: int, config: FanConfig, is_cpu: bool = True) -> tuple:
    """根据温度计算目标 PWM 值，返回 (pwm, stage)"""
    # 曲线模式
    if config.use_curve_mode:
        curve = config.cpu_curve if is_cpu else config.disk_curve
        return calculate_pwm_from_curve(temp, curve)
    
    # 旧阈值模式（向后兼容）
    if is_cpu:
        idle_min, idle_max = config.cpu_idle_temp_min, config.cpu_idle_temp_max
        work_max = config.cpu_work_temp_max
        warning_max = config.cpu_warning_temp_max
        critical_max = config.cpu_critical_temp_max
    else:
        idle_min, idle_max = config.disk_idle_temp_min, config.disk_idle_temp_max
        work_max = config.disk_work_temp_max
        warning_max = config.disk_warning_temp_max
        critical_max = config.disk_critical_temp_max
    
    if temp < idle_max:
        pwm = linear_map(temp, idle_min, idle_max, config.idle_pwm_min, config.idle_pwm_max)
        return pwm, "idle"
    elif temp < work_max:
        pwm = linear_map(temp, idle_max, work_max, config.work_pwm_min, config.work_pwm_max)
        return pwm, "work"
    elif temp < warning_max:
        pwm = linear_map(temp, work_max, warning_max, config.warning_pwm_min, config.warning_pwm_max)
        return pwm, "warning"
    elif temp < critical_max:
        pwm = linear_map(temp, warning_max, critical_max, config.critical_pwm_min, config.critical_pwm_max)
        return pwm, "critical"
    else:
        return config.critical_pwm_max, "emergency"


###############################################################################
# 风扇控制引擎
###############################################################################

class FanController:
    """风扇控制器"""
    
    def __init__(self, config_path: str = None):
        self.config_path = config_path
        # 从文件加载配置，如果文件不存在则使用默认值
        if config_path:
            self.config = FanConfig.load(config_path)
        else:
            self.config = FanConfig()
        
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.RLock()
        
        # 硬盘列表
        self.disks: List[DiskInfo] = []
        
        # 温度历史（内存存储）
        self.cpu_temp_history: List[int] = []
        self.disk_temp_history: Dict[str, List[int]] = {}
        
        # 预热计数
        self.warmup_counter = 0
        self.is_warmed_up = False
        
        # 告警时间记录（防重复推送）
        self.last_alert_time_cpu: float = 0
        self.last_alert_time_disk: Dict[str, float] = {}
        
        # 当前状态
        self.status = {
            "cpu_temp": None,
            "cpu_avg_temp": None,
            "disk_temps": {},
            "disk_avg_temps": {},
            "max_disk_temp": None,
            "fan_rpm": None,
            "current_pwm": None,
            "target_pwm": None,
            "trigger_source": None,
            "trigger_stage": None,
            "is_warmed_up": False,
            "warmup_progress": 0,
            "last_update": None,
        }
    
    def _save_config(self) -> None:
        """保存配置到文件"""
        if self.config_path:
            self.config.save(self.config_path)
    
    def detect_disks(self) -> None:
        """检测所有硬盘"""
        with self.lock:
            self.disks = detect_all_disks()
            # 保留用户之前的选择
            active_ids = set(self.config.active_disks)
            for disk in self.disks:
                disk.active = disk.id in active_ids
            logger.info(f"检测到 {len(self.disks)} 个硬盘")
    
    def get_disks(self) -> List[Dict[str, Any]]:
        """获取硬盘列表"""
        with self.lock:
            result = []
            for disk in self.disks:
                d = {
                    "id": disk.id,
                    "device": disk.device,
                    "path": disk.path,
                    "model": disk.model,
                    "serial": disk.serial,
                    "size": disk.size,
                    "type": disk.disk_type,
                    "temp": disk.temp,
                    "active": disk.active,
                }
                result.append(d)
            return result
    
    def set_active_disks(self, disk_ids: List[str]) -> None:
        """设置参与调速的硬盘"""
        with self.lock:
            self.config.active_disks = disk_ids
            for disk in self.disks:
                disk.active = disk.id in disk_ids
        self._save_config()
    
    def update_config(self, data: Dict[str, Any]) -> None:
        """更新配置"""
        with self.lock:
            self.config.update(data)
        self._save_config()
    
    def get_config(self) -> Dict[str, Any]:
        """获取配置"""
        with self.lock:
            return self.config.to_dict()
    
    def get_status(self) -> Dict[str, Any]:
        """获取状态"""
        with self.lock:
            return dict(self.status)
    
    def _calc_avg(self, history: List[int]) -> Optional[int]:
        """计算平均值"""
        valid = [t for t in history if t is not None and t > 0]
        return sum(valid) // len(valid) if valid else None
    
    def _read_temps(self) -> None:
        """读取所有温度"""
        history_size = self.config.temp_history_size
        
        # CPU 温度
        cpu_temp = read_cpu_temp()
        if cpu_temp is not None:
            self.cpu_temp_history.append(cpu_temp)
            while len(self.cpu_temp_history) > history_size:
                self.cpu_temp_history.pop(0)
        
        cpu_avg = self._calc_avg(self.cpu_temp_history)
        
        # 硬盘温度
        disk_temps = {}
        disk_avg_temps = {}
        max_disk_temp = None
        
        for disk in self.disks:
            temp = read_disk_temp(disk.device)
            disk.temp = temp
            disk_temps[disk.id] = temp
            
            if disk.id not in self.disk_temp_history:
                self.disk_temp_history[disk.id] = []
            
            if temp is not None:
                self.disk_temp_history[disk.id].append(temp)
                while len(self.disk_temp_history[disk.id]) > history_size:
                    self.disk_temp_history[disk.id].pop(0)
            else:
                # 硬盘休眠或读取失败时，清空历史记录
                self.disk_temp_history[disk.id].clear()
            
            avg = self._calc_avg(self.disk_temp_history.get(disk.id, []))
            disk_avg_temps[disk.id] = avg
            
            # 只计算激活的硬盘的最高温度
            if disk.active and avg is not None:
                if max_disk_temp is None or avg > max_disk_temp:
                    max_disk_temp = avg
        
        # 风扇状态
        fan_rpm = read_fan_rpm()
        current_pwm = read_pwm(self.config.pwm_control_file)
        
        # 更新状态
        with self.lock:
            self.status["cpu_temp"] = cpu_temp
            self.status["cpu_avg_temp"] = cpu_avg
            self.status["disk_temps"] = disk_temps
            self.status["disk_avg_temps"] = disk_avg_temps
            self.status["max_disk_temp"] = max_disk_temp
            self.status["fan_rpm"] = fan_rpm
            self.status["current_pwm"] = current_pwm
            self.status["last_update"] = datetime.now().isoformat()
    
    def _control_cycle(self) -> None:
        """单次控制循环"""
        self._read_temps()
        
        # 预热阶段
        if not self.is_warmed_up:
            self.warmup_counter += 1
            with self.lock:
                self.status["warmup_progress"] = min(100, int(self.warmup_counter / self.config.temp_history_size * 100))
            
            if self.warmup_counter >= self.config.temp_history_size:
                self.is_warmed_up = True
                with self.lock:
                    self.status["is_warmed_up"] = True
                logger.info("预热完成，开始控制风扇")
            else:
                logger.info(f"预热中 {self.warmup_counter}/{self.config.temp_history_size}")
                return
        
        if not self.config.enabled:
            return
        
        cpu_avg = self.status.get("cpu_avg_temp")
        max_disk = self.status.get("max_disk_temp")
        current_pwm = self.status.get("current_pwm")
        
        # 检查是否有有效温度数据
        has_cpu_temp = cpu_avg is not None and cpu_avg > 0
        has_disk_temp = max_disk is not None and max_disk > 0
        
        # 安全保护：如果所有温度都没采集到，使用安全PWM值（50%）
        if not has_cpu_temp and not has_disk_temp:
            safe_pwm = 128
            logger.warning("所有温度数据不可用，使用安全PWM值")
            with self.lock:
                self.status["target_pwm"] = safe_pwm
                self.status["trigger_source"] = "Safety"
                self.status["trigger_stage"] = "warning"
            if current_pwm != safe_pwm:
                set_pwm(self.config.pwm_control_file, safe_pwm)
            return
        
        # 计算 PWM
        cpu_pwm, cpu_stage = (0, "")
        disk_pwm, disk_stage = (0, "")
        
        if has_cpu_temp:
            cpu_pwm, cpu_stage = calculate_pwm(cpu_avg, self.config, is_cpu=True)
        
        if has_disk_temp:
            disk_pwm, disk_stage = calculate_pwm(max_disk, self.config, is_cpu=False)
        
        # 取较大值
        if cpu_pwm >= disk_pwm:
            target_pwm = cpu_pwm
            trigger_source = "CPU"
            trigger_stage = cpu_stage
        else:
            target_pwm = disk_pwm
            trigger_source = "Disk"
            trigger_stage = disk_stage
        
        # PWM 阈值检查
        threshold = self.config.pwm_change_threshold
        if current_pwm is not None and threshold > 0:
            if abs(target_pwm - current_pwm) < threshold:
                target_pwm = current_pwm
        
        # 应用 PWM
        if target_pwm != current_pwm:
            set_pwm(self.config.pwm_control_file, target_pwm)
        
        # 更新状态
        with self.lock:
            self.status["target_pwm"] = target_pwm
            self.status["trigger_source"] = trigger_source
            self.status["trigger_stage"] = trigger_stage
        
        # 温度告警检查
        self._check_temp_alert()
    
    def _check_temp_alert(self) -> None:
        """检查温度并推送告警"""
        if not self.config.alert_enabled:
            return
        
        current_time = time.time()
        interval = self.config.alert_interval
        fan_rpm = self.status.get("fan_rpm", "N/A")
        hostname = self.config.alert_hostname
        
        # 检查 CPU 温度告警
        cpu_avg = self.status.get("cpu_avg_temp")
        if cpu_avg and cpu_avg >= self.config.cpu_alert_temp:
            if current_time - self.last_alert_time_cpu >= interval:
                msg = f"[{hostname}]: 🔥 CPU: {cpu_avg}°C | RPM: {fan_rpm}"
                self._send_push(msg)
                self.last_alert_time_cpu = current_time
        
        # 检查各硬盘温度告警（合并成一条消息）
        disk_avg_temps = self.status.get("disk_avg_temps", {})
        alert_disks = []
        for disk_id, temp in disk_avg_temps.items():
            if temp and temp >= self.config.disk_alert_temp:
                last_time = self.last_alert_time_disk.get(disk_id, 0)
                if current_time - last_time >= interval:
                    alert_disks.append(f"{disk_id}: {temp}°C")
                    self.last_alert_time_disk[disk_id] = current_time
        
        if alert_disks:
            msg = f"[{hostname}]: 🔥 {', '.join(alert_disks)} | RPM: {fan_rpm}"
            self._send_push(msg)
    
    def _send_push(self, message: str) -> None:
        """发送推送消息（后台执行，不阻塞）"""
        try:
            # 检查 push 命令是否存在
            result = run(["which", "push"], capture_output=True)
            if result.returncode != 0:
                return
            
            # 后台执行，不等待结果
            Popen(
                ["push", message],
                stdout=DEVNULL,
                stderr=DEVNULL,
                start_new_session=True  # 避免僵尸进程
            )
            logger.info(f"推送告警: {message}")
        except Exception as e:
            logger.debug(f"推送失败: {e}")
    
    def _run_loop(self) -> None:
        """控制循环"""
        # 初始化前置处理
        logger.info("执行风扇控制前置处理...")
        load_it87_module()
        enable_manual_pwm(self.config.pwm_enable_file)
        
        # 检测硬盘
        self.detect_disks()
        
        while self.running:
            try:
                self._control_cycle()
            except Exception as e:
                logger.exception(f"控制循环异常: {e}")
            
            time.sleep(self.config.check_interval)
    
    def start(self) -> None:
        """启动"""
        if self.running:
            return
        self.running = True
        self.warmup_counter = 0
        self.is_warmed_up = False
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        logger.info("风扇控制器已启动")
    
    def stop(self) -> None:
        """停止"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("风扇控制器已停止")
    
    def set_manual_pwm(self, value: int) -> bool:
        """手动设置 PWM"""
        return set_pwm(self.config.pwm_control_file, value)
    
    def refresh(self) -> Dict[str, Any]:
        """立即刷新状态"""
        self._read_temps()
        return self.get_status()


###############################################################################
# HTTP API
###############################################################################

# 全局控制器实例
controller: Optional[FanController] = None


class APIHandler(BaseHTTPRequestHandler):
    """API 请求处理"""
    
    def log_message(self, format, *args):
        # Unix socket 下 client_address 可能是空字符串，需要特殊处理
        ca = getattr(self, "client_address", None)
        if isinstance(ca, (list, tuple)) and ca:
            addr = ca[0]
        else:
            addr = ca or "-"
        logger.debug(f"{addr} - {format % args}")
    
    def _json_response(self, data: Any, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    
    def _read_json(self) -> Optional[Dict]:
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                body = self.rfile.read(length)
                return json.loads(body.decode("utf-8"))
        except Exception as e:
            logger.warning(f"解析 JSON 失败: {e}")
        return {}
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
    
    def do_GET(self):
        self._handle_request("GET")
    
    def do_POST(self):
        self._handle_request("POST")
    
    def do_PUT(self):
        self._handle_request("PUT")
    
    def _handle_request(self, method: str) -> None:
        global controller
        if controller is None:
            self._json_response({"error": "controller not initialized"}, 500)
            return
        
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        
        try:
            # GET /api/status - 获取状态
            if path == "/api/status" and method == "GET":
                self._json_response({
                    "status": controller.get_status(),
                    "enabled": controller.config.enabled,
                })
                return
            
            # GET /api/config - 获取配置
            if path == "/api/config" and method == "GET":
                self._json_response({"config": controller.get_config()})
                return
            
            # PUT /api/config - 更新配置
            if path == "/api/config" and method == "PUT":
                data = self._read_json()
                controller.update_config(data)
                self._json_response({"success": True, "config": controller.get_config()})
                return
            
            # GET /api/disks - 获取硬盘列表
            if path == "/api/disks" and method == "GET":
                self._json_response({"disks": controller.get_disks()})
                return
            
            # POST /api/disks/refresh - 刷新硬盘列表
            if path == "/api/disks/refresh" and method == "POST":
                controller.detect_disks()
                self._json_response({"disks": controller.get_disks()})
                return
            
            # PUT /api/disks/active - 设置激活的硬盘
            if path == "/api/disks/active" and method == "PUT":
                data = self._read_json()
                disk_ids = data.get("disk_ids", [])
                controller.set_active_disks(disk_ids)
                self._json_response({"success": True, "active_disks": disk_ids})
                return
            
            # POST /api/control/pwm - 手动设置 PWM
            if path == "/api/control/pwm" and method == "POST":
                data = self._read_json()
                pwm = int(data.get("pwm", 0))
                success = controller.set_manual_pwm(pwm)
                self._json_response({"success": success, "pwm": pwm})
                return
            
            # POST /api/control/toggle - 启用/禁用自动控制
            if path == "/api/control/toggle" and method == "POST":
                data = self._read_json()
                enabled = data.get("enabled", True)
                controller.config.enabled = bool(enabled)
                self._json_response({"success": True, "enabled": controller.config.enabled})
                return
            
            # POST /api/refresh - 刷新状态
            if path == "/api/refresh" and method == "POST":
                status = controller.refresh()
                self._json_response({"status": status})
                return
            
            self._json_response({"error": "not found"}, 404)
        
        except Exception as e:
            logger.exception(f"API 错误: {e}")
            self._json_response({"error": str(e)}, 500)


def run_server(host: str = "0.0.0.0", port: int = 28257, unix_socket: str = None, config_path: str = None):
    """运行 HTTP 服务"""
    global controller
    
    # 如果没有指定配置文件路径，根据 unix_socket 路径自动推断
    if not config_path and unix_socket:
        config_path = os.path.join(os.path.dirname(unix_socket), "config.json")
    
    controller = FanController(config_path=config_path)
    controller.start()
    
    if unix_socket:
        # Unix socket 模式
        if os.path.exists(unix_socket):
            os.unlink(unix_socket)
        
        # 初始化服务器但不绑定默认 socket
        server = ThreadingHTTPServer(("", 0), APIHandler, bind_and_activate=False)
        
        # 创建 Unix socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(unix_socket)
        
        # 替换服务器 socket 并设置正确的地址族
        server.socket = sock
        server.address_family = socket.AF_UNIX
        server.server_address = unix_socket
        
        # 激活服务器（调用 listen）
        server.server_activate()
        
        logger.info(f"风扇调控服务启动于 unix://{unix_socket}")
    else:
        server = ThreadingHTTPServer((host, port), APIHandler)
        logger.info(f"风扇调控服务启动于 http://{host}:{port}")
    
    shutdown_event = threading.Event()
    
    def handle_signal(signum, frame):
        if shutdown_event.is_set():
            return
        shutdown_event.set()
        logger.info("正在关闭...")
        threading.Thread(target=server.shutdown, daemon=True).start()
    
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    
    try:
        server.serve_forever()
    finally:
        controller.stop()
        server.server_close()
        if unix_socket and os.path.exists(unix_socket):
            os.unlink(unix_socket)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="风扇自动调控服务")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=28257, help="监听端口")
    parser.add_argument("--unix-socket", help="Unix socket 路径")
    parser.add_argument("--config", help="配置文件路径（默认与 socket 同目录的 config.json）")
    
    args = parser.parse_args()
    run_server(host=args.host, port=args.port, unix_socket=args.unix_socket, config_path=args.config)
