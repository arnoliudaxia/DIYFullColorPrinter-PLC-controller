# -*- coding: utf-8 -*-
"""
设备远程控制程序
协议：TCP / ASCII 指令
  轴移动:  x=100 (绝对)  x+10 / x-5 (增量)，X/Y/Z 大小写均可
  UV 灯:   D (状态翻转)
  滚子:    G (状态翻转)
设备每隔几秒主动上报状态帧，例如：
  POS:X=+014.88,Y=+000.00,Z=+000.00,D=0,G=0
 行程范围: X [-38, 232]  Y [-7, 123]  Z [-65, 119.5]
 多条指令间隔 >= 50ms（程序内置发送队列自动保证）

附加：扫描服务器连接（127.0.0.1:19090），仅建连并显示状态。
  连接成功后服务器发送: NETSCAN_SERVER_READY
  闪喷命令 FLASH，回复单行: OK FLASH BEFORE=OFF AFTER=ON（回送修改前/后的闪喷状态）
  压墨命令 PRESS_INK <秒数>，完成回送: OK PRESS_INK SECONDS=.. RESULT=COMPLETED STATE=OFF
    请手动刮墨→点击确定后 Z 回升→继续闪喷（Z 下降量/压墨时长在 config.toml 配置）
"""

import ctypes
import os
import queue
import re
import socket
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import ttk, messagebox

# ---------- 协议常量 ----------

DEFAULT_HOST = "192.168.1.88"
DEFAULT_PORT = 2001
MIN_INTERVAL = 0.05  # 指令最小间隔 50ms
POS_TOLERANCE = 0.1  # 判定到达目标位置的容差

# 扫描服务器连接
SCAN_HOST = "127.0.0.1"
SCAN_PORT = 19090
SCAN_READY = "NETSCAN_SERVER_READY"  # 连接成功后服务器发送的就绪帧
CMD_FLASH = "FLASH"                  # 闪喷命令
FLASH_ACCEPTED = "OK FLASH_ACCEPTED" # 旧版闪喷接受回复（兼容）
DEFAULT_FLASH_INTERVAL = 10          # 默认闪喷间隔（X 到达终点的次数）
# 闪喷回复（机器可解析单行）: OK FLASH BEFORE=OFF AFTER=ON
FLASH_RESPONSE_PATTERN = re.compile(r"OK FLASH BEFORE=(\w+) AFTER=(\w+)")
# 服务器主动事件: 图层开始 / PASS 就绪
START_JOB_PATTERN = re.compile(r"EVENT START_JOB TOTAL_LAYERS=(\d+)")
PRINT_JOB_COMPLETED_PATTERN = re.compile(
    r"EVENT PRINT_JOB_COMPLETED TOTAL_LAYERS=(\d+)")
LAYER_START_PATTERN = re.compile(r"EVENT LAYER_START LAYER=(\d+)")
PASS_READY_PATTERN = re.compile(
    r"EVENT PASS_READY CURRENT=(\d+) TOTAL=(\d+) STEP=([+-]?\d+) EMPTY=(\d+)")
PASS_REMAINING_ZERO_PATTERN = re.compile(
    r"EVENT PASS_REMAINING_ZERO LAYER=(\d+) PASS=(\d+) REMAINING=0")

# ---------- 配置文件 ----------

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml")

_DEFAULT_AXIS_LIMITS = {
    "X": (-10.0, 230.0),
    "Y": (-5.0, 120.0),
    "Z": (-60.0, 125.0),
}
_DEFAULT_X_HOME = 0.0
_DEFAULT_X_END = 220.0
_DEFAULT_X_END_WITH_UV = 220.0
_DEFAULT_UV_OFFSET = 120.0


def _load_config() -> dict:
    """读取 config.toml；缺失或格式错误时返回空 dict（使用默认值）"""
    try:
        import tomllib
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError, ImportError):
        return {}


def _load_axis_limits(cfg: dict) -> dict:
    limits = dict(_DEFAULT_AXIS_LIMITS)
    try:
        for axis, v in cfg["axis_limits"].items():
            limits[axis.upper()] = (float(v["min"]), float(v["max"]))
    except (KeyError, TypeError, ValueError):
        pass
    return limits


def _load_cycle_endpoints(cfg: dict):
    home, end, end_with_uv = _DEFAULT_X_HOME, _DEFAULT_X_END, _DEFAULT_X_END_WITH_UV
    try:
        home = float(cfg["auto_cycle"]["x_home"])
        end = float(cfg["auto_cycle"]["x_end"])
        end_with_uv = float(cfg["auto_cycle"].get("x_end_withuv", end_with_uv))
    except (KeyError, TypeError, ValueError):
        pass
    return home, end, end_with_uv


def _load_uv_offset(cfg: dict) -> float:
    try:
        value = float(cfg["auto_cycle"].get("uv_offset", _DEFAULT_UV_OFFSET))
        return value if value >= 0 else _DEFAULT_UV_OFFSET
    except (KeyError, TypeError, ValueError):
        return _DEFAULT_UV_OFFSET


_DEFAULT_PASS_STOP_WAIT_SECONDS = 2.0


def _load_pass_stop_wait_seconds(cfg: dict) -> float:
    """PASS 急停后的等待时间（秒），来自 auto_cycle.pass_stop_wait_seconds。"""
    try:
        seconds = float(cfg["auto_cycle"]["pass_stop_wait_seconds"])
        if seconds < 0:
            raise ValueError
        return seconds
    except (KeyError, TypeError, ValueError):
        return _DEFAULT_PASS_STOP_WAIT_SECONDS


_DEFAULT_JOG_STEPS = (1, 5, 10, 50)


def _load_jog_steps(cfg: dict) -> tuple:
    steps = _DEFAULT_JOG_STEPS
    try:
        raw = cfg["jog"]["steps"]
        parsed = []
        for s in raw:
            f = float(s)
            parsed.append(int(f) if f.is_integer() else f)
        if not parsed:
            raise ValueError
        steps = tuple(parsed)
    except (KeyError, TypeError, ValueError):
        pass
    return steps


_DEFAULT_Z_LAYERS = 200   # 默认"200 层 0.5mm"
_DEFAULT_Z_STEP = 0.5


def _load_z_step(cfg: dict):
    """Z 下降: (触发阈值, 下降量)，阈值由 layers 直接决定"""
    layers, step = _DEFAULT_Z_LAYERS, _DEFAULT_Z_STEP
    try:
        layers = int(float(cfg["z_step"]["layers"]))
        step = float(cfg["z_step"]["step"])
        if layers < 1 or step <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        pass
    return layers, step


_DEFAULT_FLASH_PAUSE_MS = 2000
_DEFAULT_FLASH_X_OFFSET = 0.0


def _load_flash_interval(cfg: dict) -> int:
    """维护闪喷间隔，单位为累计 X 循环/急停次数。"""
    interval = DEFAULT_FLASH_INTERVAL
    try:
        raw_interval = cfg["flash"]["interval"]
        if isinstance(raw_interval, bool) or not isinstance(raw_interval, int):
            raise ValueError
        interval = raw_interval
        if interval < 1:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        interval = DEFAULT_FLASH_INTERVAL
    return interval


def _load_flash_pause(cfg: dict) -> int:
    ms = _DEFAULT_FLASH_PAUSE_MS
    try:
        ms = int(float(cfg["flash"]["pause_ms"]))
        if ms < 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        pass
    return ms


def _load_flash_x_offset(cfg: dict) -> float:
    """维护闪喷前 X 轴正向偏移量（mm）。"""
    offset = _DEFAULT_FLASH_X_OFFSET
    try:
        offset = float(cfg["flash"]["x_offset"])
        if offset < 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        offset = _DEFAULT_FLASH_X_OFFSET
    return offset


_DEFAULT_PRESS_Z_PRESET = 70.0
_DEFAULT_PRESS_INK_DURATIONS = (2.0, 5.0, 10.0)  # 压墨时长可选值 (s)


def _load_press_ink(cfg: dict):
    press_z = _DEFAULT_PRESS_Z_PRESET
    try:
        press_z = float(cfg["press_ink"]["press_z"])
    except (KeyError, TypeError, ValueError):
        pass
    return press_z


def _load_press_ink_durations(cfg: dict) -> tuple:
    durations = _DEFAULT_PRESS_INK_DURATIONS
    try:
        raw = cfg["press_ink"]["durations"]
        durations = tuple(float(v) for v in raw)
        if not durations or any(v <= 0 or v > 3600 for v in durations):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        pass
    return durations


_DEFAULT_UI_SCALE = 1.6
_DEFAULT_WIN_SIZE = (860, 520)
_DEFAULT_COL_WIDTHS = (420.0, 440.0)  # 两列布局: (左列宽, 右列宽) 逻辑像素


def _load_ui(cfg: dict):
    scale, win = _DEFAULT_UI_SCALE, _DEFAULT_WIN_SIZE
    col_widths = _DEFAULT_COL_WIDTHS
    try:
        scale = float(cfg["ui"]["scale"])
        size = cfg["ui"]["window_size"]
        win = (int(float(size[0])), int(float(size[1])))
        raw = cfg["ui"]["column_widths"]
        col_widths = tuple(float(v) for v in raw)
        if scale <= 0 or min(win) <= 0 or len(col_widths) != 2 or min(col_widths) <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        pass
    return scale, win, col_widths


_CONFIG = _load_config()
AXIS_LIMITS = _load_axis_limits(_CONFIG)
X_HOME, X_END, X_END_WITH_UV = _load_cycle_endpoints(_CONFIG)
UV_OFFSET_DEFAULT = _load_uv_offset(_CONFIG)
PASS_STOP_WAIT_SECONDS = _load_pass_stop_wait_seconds(_CONFIG)
PASS_STOP_WAIT_MS = int(round(PASS_STOP_WAIT_SECONDS * 1000))
JOG_STEPS = _load_jog_steps(_CONFIG)
X_CYCLE_COUNT_LIMIT, X_CYCLE_Z_STEP = _load_z_step(_CONFIG)
FLASH_INTERVAL_DEFAULT = _load_flash_interval(_CONFIG)
FLASH_PAUSE_MS = _load_flash_pause(_CONFIG)
FLASH_X_OFFSET = _load_flash_x_offset(_CONFIG)
PRESS_INK_Z_PRESET = _load_press_ink(_CONFIG)
PRESS_INK_DURATIONS = _load_press_ink_durations(_CONFIG)
UI_SCALE, WIN_SIZE, COL_WIDTHS = _load_ui(_CONFIG)

# 点动按钮文字: {轴: (负方向, 正方向)}，保留正负号
JOG_LABELS = {
    "X": ("-右", "+左"),
    "Y": ("-人后", "+人前"),
    "Z": ("-下", "+上"),
}

CMD_UV_LAMP = "D"
CMD_ROLLER = "G"
CMD_ESTOP = "q"  # 急停：打断所有运动指令

# 自动循环全部完成后的提示音
FINISH_SOUND = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "assets", "freesound_community-goodresult-82807.mp3")
# 自动压墨时循环播放的警告音
INK_WARNING_SOUND = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "assets", "warning.mp3")

# 状态帧: POS:X=+014.88,Y=+000.00,Z=+000.00,D=0,G=0
STATUS_PATTERN = re.compile(
    r"POS:X=([+-]?\d+(?:\.\d+)?),Y=([+-]?\d+(?:\.\d+)?),Z=([+-]?\d+(?:\.\d+)?)"
    r",D=(\d+),G=(\d+)")


def fmt_pos(value: float) -> str:
    """位置数值格式化：整数不带小数点"""
    return str(int(value)) if value == int(value) else str(value)


def fmt_duration(seconds: float) -> str:
    """秒数格式化为 HH:MM:SS"""
    s = int(round(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# ---------- TCP 收发器 ----------

class TcpSender:
    """TCP 连接 + 发送队列（指令间隔 >= 50ms）+ 接收线程（状态帧）"""

    def __init__(self, on_sent, on_state_change, on_receive):
        self.sock = None
        self.connected = False
        self._queue = queue.Queue()
        self._worker = None
        self._lock = threading.Lock()
        self.on_sent = on_sent                  # func(str) 指令已发出
        self.on_state_change = on_state_change  # func(bool, str)
        self.on_receive = on_receive            # func(str) 收到设备数据

    def connect(self, host: str, port: int) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            sock.connect((host, port))
            sock.settimeout(0.5)
            with self._lock:
                self.sock = sock
                self.connected = True
            self._worker = threading.Thread(target=self._send_loop, daemon=True)
            self._worker.start()
            threading.Thread(target=self._recv_loop, daemon=True).start()
            self.on_state_change(True, f"已连接 {host}:{port}")
            return True
        except Exception as e:
            self.on_state_change(False, f"连接失败: {e}")
            return False

    def disconnect(self):
        with self._lock:
            self.connected = False
            sock, self.sock = self.sock, None
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        self.on_state_change(False, "已断开连接")

    def send_command(self, cmd: str):
        """指令入队，由发送线程按间隔发出"""
        self._queue.put(cmd)

    def send_urgent(self, cmd: str):
        """急停指令：清空队列、立即发送，绕过 50ms 间隔"""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        with self._lock:
            sock = self.sock if self.connected else None
        if sock is None:
            self.on_state_change(False, f"未连接，急停指令未发送: {cmd}")
            return
        try:
            sock.sendall(cmd.encode("ascii"))
            self.on_sent(cmd)
        except OSError as e:
            self.on_state_change(False, f"急停发送失败: {e}")
            self.disconnect()

    def _send_loop(self):
        while True:
            cmd = self._queue.get()
            with self._lock:
                sock = self.sock if self.connected else None
            if sock is None:
                self.on_state_change(False, f"未连接，指令被丢弃: {cmd}")
                continue
            try:
                sock.sendall(cmd.encode("ascii"))
                self.on_sent(cmd)
            except OSError as e:
                self.on_state_change(False, f"发送失败: {e}")
                self.disconnect()
            time.sleep(MIN_INTERVAL)

    def _recv_loop(self):
        """接收设备周期性上报的状态帧"""
        buffer = ""
        while True:
            with self._lock:
                sock = self.sock if self.connected else None
            if sock is None:
                return
            try:
                data = sock.recv(4096)
                if not data:  # 对端关闭
                    self.disconnect()
                    return
                buffer += data.decode("ascii", errors="replace")
                # 状态帧无结束符，按完整帧匹配后从缓冲区移除
                while True:
                    m = STATUS_PATTERN.search(buffer)
                    if not m:
                        break
                    self.on_receive(m.group(0))
                    buffer = buffer[m.end():]
                # 防止异常数据无限堆积
                if len(buffer) > 4096:
                    buffer = buffer[-1024:]
            except socket.timeout:
                continue
            except OSError:
                self.disconnect()
                return


# ---------- 扫描服务器连接 ----------

class ScanClient:
    """与扫描服务器建连（127.0.0.1:19090），显示连接/就绪状态并收发命令"""

    def __init__(self, on_state_change, on_ready, on_response):
        self.sock = None
        self.connected = False
        self.ready = False
        self.on_state_change = on_state_change  # func(bool, str)
        self.on_ready = on_ready                # func() 收到就绪帧
        self.on_response = on_response          # func(str) 收到一行回复

    def connect(self, host: str, port: int) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            sock.connect((host, port))
            sock.settimeout(0.5)
            self.sock = sock
            self.connected = True
            self.on_state_change(True, f"扫描服务器已连接 {host}:{port}")
            threading.Thread(target=self._recv_loop, daemon=True).start()
            return True
        except Exception as e:
            self.on_state_change(False, f"扫描服务器连接失败: {e}")
            return False

    def disconnect(self):
        self.connected = False
        self.ready = False
        sock, self.sock = self.sock, None
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        self.on_state_change(False, "扫描服务器已断开")

    def send(self, data: str):
        """发送原始命令（调用方自行带 \r\n）"""
        sock = self.sock
        if sock is None or not self.connected:
            self.on_state_change(False, "扫描服务器未连接，命令未发送")
            return False
        try:
            sock.sendall(data.encode("ascii"))
            return True
        except OSError as e:
            self.on_state_change(False, f"扫描服务器发送失败: {e}")
            self.disconnect()
            return False

    def _recv_loop(self):
        buffer = ""
        while True:
            sock = self.sock
            if sock is None or not self.connected:
                return
            try:
                data = sock.recv(4096)
                if not data:
                    self.disconnect()
                    return
                buffer += data.decode("ascii", errors="replace")
                if not self.ready and SCAN_READY in buffer:
                    self.ready = True
                    self.on_ready()
                # 按行分割，逐行回调
                while "\r\n" in buffer or "\n" in buffer:
                    line, _, rest = buffer.partition("\n")
                    buffer = rest
                    line = line.rstrip("\r")
                    if line.strip():
                        self.on_response(line)
            except socket.timeout:
                continue
            except OSError:
                self.disconnect()
                return


# ---------- 主窗口 ----------

class MainWindow(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("设备远程控制")
        self.tk.call("tk", "scaling", UI_SCALE)
        self.geometry(f"{WIN_SIZE[0] * UI_SCALE:.0f}x{WIN_SIZE[1] * UI_SCALE:.0f}")

        # 位置/开关状态以设备周期性上报的状态帧为准
        self.positions = {axis: 0.0 for axis in AXIS_LIMITS}
        self.uv_on = False
        self.roller_on = False
        self.flash_on = False  # 闪喷状态，以服务器回送的 AFTER 为准

        # 运动中的等待目标 {轴: 目标位置}，某轴非空时只锁定该轴的控件
        self._pending = {}
        # 每轴独立的运动控件 + 自动循环控件（设备控制 UV/滚子 不参与锁定）
        self.axis_widgets = {axis: [] for axis in AXIS_LIMITS}
        self.auto_widgets = []
        # X 轴自动循环状态
        self._auto_active = False
        self._auto_mode = None       # 当前任务来源: "manual" / "server"
        self._auto_remaining = 0   # 本组内剩余循环次数
        self._inner_total = 0      # 每组内循环次数
        self._outer_remaining = 0  # 大循环剩余次数
        self._y_start = 0.0        # 内循环开始时的 Y 位置（大循环间要回到这里）
        self._job_current_layer = 0
        self._job_total_layers = 0
        # 已从事件队列正式消费、当前正在执行 PASS 的层号。
        # 与 _job_current_layer 分开，后者可能被提前到达的 LAYER_START 更新。
        self._active_layer = 0
        self._layer_listening = False
        self._pass_current = 0
        self._pass_total = 0
        self._auto_leg = "end"     # 当前段: "end"/"home"/"ystep"/"yback"
        self._auto_ystep = 10.0    # 循环间 Y 轴负向步进量
        self._uv_dist = UV_OFFSET_DEFAULT      # UV 灯开灯的 X 位置（水平偏移）
        self._server_zero_received = False
        # 打印中维护闪喷：X 到达终点累计计数（不管 Y step / 大循环，一直累加）
        self._flash_count = 0      # X 到达终点累计次数
        self._flash_interval = FLASH_INTERVAL_DEFAULT
        self._flash_pausing = False   # 是否处于闪喷暂停中
        self._flash_after = None      # 闪喷恢复定时器 id
        self._flash_wait_off = False  # 是否正在等待结束闪喷回送
        self._flash_wait_after = None # 结束闪喷回送超时定时器 id
        self._server_stop_after = None  # 每次 PASS_REMAINING_ZERO 急停后的 2s 等待
        self._auto_eta = 0.0          # 本次自动循环预计完成时间 (s)
        self.eta_var = tk.StringVar(value="")  # 预计完成时间显示
        self.auto_count_var = tk.StringVar(
            value="闪喷计数: 0")

        self.sender = TcpSender(
            on_sent=self._on_sent,
            on_state_change=self._on_state_change,
            on_receive=self._on_status,
        )

        self.scan = ScanClient(
            on_state_change=self._on_scan_state,
            on_ready=self._on_scan_ready,
            on_response=self._on_scan_response,
        )

        # socket EVENT 主动事件队列（服务器广播，保留最近 N 条）
        self.event_queue = deque(maxlen=200)

        # 左右两列布局：左侧连接/服务/设备/日志，右侧轴控制（加宽减高）
        # 列宽分别由 COL_WIDTHS 控制（minsize，逻辑像素），窗口拉宽时多余宽度给左列
        main = ttk.Frame(self)
        main.pack(fill="both", expand=True)
        main.grid_rowconfigure(0, weight=1)
        main.grid_columnconfigure(0, weight=1, minsize=int(COL_WIDTHS[0]))
        main.grid_columnconfigure(1, weight=0, minsize=int(COL_WIDTHS[1]))
        left_col = ttk.Frame(main)
        left_col.grid(row=0, column=0, sticky="nsew")
        right_col = ttk.Frame(main)
        right_col.grid(row=0, column=1, sticky="nsew")

        self._build_connection_frame(left_col)
        self._build_scan_frame(left_col)
        self._build_device_frame(left_col)
        self._build_log_frame(left_col)
        self._build_axis_frame(right_col)
        self._build_event_frame(right_col)
        self._build_status_bar()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # 启动后自动连接设备与扫描服务器
        self.after(300, self._auto_connect)

    def _auto_connect(self):
        if not self.sender.connected:
            self._toggle_connect()
        if not self.scan.connected:
            self._toggle_scan()

    # ---------- UI 构建 ----------

    def _build_connection_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="连接设置")
        frame.pack(fill="x", padx=8, pady=6)

        ttk.Label(frame, text="IP 地址:").grid(row=0, column=0, padx=4, pady=6)
        self.ip_var = tk.StringVar(value=DEFAULT_HOST)
        ttk.Entry(frame, textvariable=self.ip_var, width=15).grid(row=0, column=1, padx=4)

        ttk.Label(frame, text="端口:").grid(row=0, column=2, padx=4)
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        ttk.Entry(frame, textvariable=self.port_var, width=7).grid(row=0, column=3, padx=4)

        self.connect_btn = ttk.Button(frame, text="连接", command=self._toggle_connect)
        self.connect_btn.grid(row=0, column=4, padx=8)

    def _build_scan_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="打印服务进程")
        frame.pack(fill="x", padx=8, pady=4)

        ttk.Label(frame, text="IP 地址:").grid(row=0, column=0, padx=4, pady=6)
        self.scan_ip_var = tk.StringVar(value=SCAN_HOST)
        ttk.Entry(frame, textvariable=self.scan_ip_var, width=15).grid(row=0, column=1, padx=4)

        ttk.Label(frame, text="端口:").grid(row=0, column=2, padx=4)
        self.scan_port_var = tk.StringVar(value=str(SCAN_PORT))
        ttk.Entry(frame, textvariable=self.scan_port_var, width=7).grid(row=0, column=3, padx=4)

        self.scan_btn = ttk.Button(frame, text="连接", command=self._toggle_scan)
        self.scan_btn.grid(row=0, column=4, padx=8)

        self.scan_ready_var = tk.StringVar(value="未连接")
        ttk.Label(frame, textvariable=self.scan_ready_var,
                  foreground="gray").grid(row=1, column=0, columnspan=5, sticky="w", padx=4, pady=2)

    def _build_axis_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="轴控制（位置来自设备周期性上报）")
        frame.pack(fill="x", padx=8, pady=4, anchor="n")

        # 共享点动步长：不可手动输入，通过单选按钮切换（选项来自 config.toml）
        step_bar = ttk.Frame(frame)
        step_bar.grid(row=0, column=0, columnspan=7, sticky="w", pady=2)
        ttk.Label(step_bar, text="点动步长:").pack(side="left", padx=4)
        self.step_var = tk.StringVar(value=fmt_pos(JOG_STEPS[0]))
        for v in JOG_STEPS:
            tk.Radiobutton(step_bar, text=str(v), variable=self.step_var, value=str(v),
                           indicatoron=False, width=4,
                           selectcolor="#2e86de",        # 选中时底色（蓝色突出）
                           activebackground="#9ec7f0",   # 悬停底色
                           font=("", 10, "bold")).pack(side="left", padx=2)

        # 表头
        for col, text in enumerate(["轴", "当前位置", "目标位置", "", "点动", "", "行程范围"]):
            ttk.Label(frame, text=text).grid(row=1, column=col, padx=4, pady=2)

        self.pos_labels = {}
        self.target_vars = {}

        for row, axis in enumerate(AXIS_LIMITS, start=2):
            ttk.Label(frame, text=axis, font=("", 10, "bold")).grid(row=row, column=0, padx=4)

            lbl = ttk.Label(frame, text="0", width=10, anchor="center", relief="groove")
            lbl.grid(row=row, column=1, padx=4)
            self.pos_labels[axis] = lbl

            self.target_vars[axis] = tk.StringVar(value="0")
            ttk.Entry(frame, textvariable=self.target_vars[axis], width=8).grid(row=row, column=2, padx=2)
            move_btn = ttk.Button(frame, text="移动", width=6,
                                  command=lambda a=axis: self._move_absolute(a))
            move_btn.grid(row=row, column=3, padx=4)
            self.axis_widgets[axis].append(move_btn)

            jog_minus = ttk.Button(frame, text=JOG_LABELS[axis][0], width=6,
                                   command=lambda a=axis: self._jog(a, -1))
            jog_plus = ttk.Button(frame, text=JOG_LABELS[axis][1], width=6,
                                  command=lambda a=axis: self._jog(a, 1))
            if axis == "X":
                # X 轴点动先显示"左"再"右"（与视觉方向一致）
                jog_plus.grid(row=row, column=4, padx=2)
                jog_minus.grid(row=row, column=5, padx=2)
            else:
                jog_minus.grid(row=row, column=4, padx=2)
                jog_plus.grid(row=row, column=5, padx=2)
            self.axis_widgets[axis] += [jog_minus, jog_plus]

            lo, hi = AXIS_LIMITS[axis]
            ttk.Label(frame, text=f"[{fmt_pos(lo)}, {fmt_pos(hi)}]",
                      foreground="gray").grid(row=row, column=6, padx=6)

        # 快捷位置按钮（X / Z 各一行）
        quick_x = ttk.Frame(frame)
        quick_x.grid(row=5, column=0, columnspan=7, sticky="w", pady=2)
        ttk.Label(quick_x, text="X 轴预设:").pack(side="left", padx=4)
        for text, target in [("零点位置 (1)", 1),
                             (f"起始位置 ({fmt_pos(X_HOME)})", X_HOME),
                             (f"终点位置 ({fmt_pos(X_END)})", X_END)]:
            b = ttk.Button(quick_x, text=text, width=14,
                           command=lambda t=target: self._move_to("X", t))
            b.pack(side="left", padx=4)
            self.axis_widgets["X"].append(b)

        quick_z = ttk.Frame(frame)
        quick_z.grid(row=6, column=0, columnspan=7, sticky="w", pady=2)
        ttk.Label(quick_z, text="Z 轴预设:").pack(side="left", padx=4)
        for text, target in [("打印高度 (125)", 125),
                             ("调试高度 (-10)", -10),
                             (f"压墨高度 ({fmt_pos(PRESS_INK_Z_PRESET)})",
                              PRESS_INK_Z_PRESET)]:
            b = ttk.Button(quick_z, text=text, width=14,
                           command=lambda t=target: self._move_to("Z", t))
            b.pack(side="left", padx=4)
            self.axis_widgets["Z"].append(b)

        # 服务器事件驱动流程的计数显示。
        self.cycle_info_var = tk.StringVar(value="")
        self.eta_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.auto_count_var,
                  foreground="#1565c0").grid(row=7, column=0, columnspan=7, pady=8)

        self.last_report_var = tk.StringVar(value="等待设备上报...")
        ttk.Label(frame, textvariable=self.last_report_var,
                  foreground="gray").grid(row=8, column=0, columnspan=7, pady=6)

    def _build_event_frame(self, parent):
        """socket EVENT 主动事件队列显示区域（轴控制下方）"""
        frame = ttk.LabelFrame(parent, text="事件队列 (EVENT)")
        frame.pack(fill="both", expand=True, padx=8, pady=4)

        head = ttk.Frame(frame)
        head.pack(fill="x", padx=4, pady=2)
        self.event_count_var = tk.StringVar(value="0 条")
        ttk.Label(head, text="服务器主动事件（最新在底）：").pack(side="left")
        ttk.Label(head, textvariable=self.event_count_var,
                  foreground="gray").pack(side="left", padx=6)
        self.job_progress_var = tk.StringVar(value="打印进度: 0/0")
        ttk.Label(head, textvariable=self.job_progress_var,
                  foreground="#1565c0").pack(side="left", padx=(12, 0))
        self.pass_progress_var = tk.StringVar(value="PASS进度: 0/0")
        ttk.Label(head, textvariable=self.pass_progress_var,
                  foreground="#7b1fa2").pack(side="left", padx=(12, 0))
        ttk.Button(head, text="停止", width=6,
                   command=self._emergency_stop).pack(side="right")

        self.event_list = tk.Listbox(frame, height=8, font=("Consolas", 9),
                                     exportselection=False)
        self.event_list.pack(fill="both", expand=True, padx=4, pady=4)

    def _clear_event_queue(self):
        self.event_queue.clear()
        self.event_list.delete(0, "end")
        self.event_count_var.set("0 条")

    def _remove_event(self, fragment: str):
        """从事件队列中移除包含指定片段的条目并刷新显示"""
        before = len(self.event_queue)
        self.event_queue = deque(
            (ts, ev) for ts, ev in self.event_queue if fragment not in ev)
        removed = before - len(self.event_queue)
        if removed:
            self.event_list.delete(0, "end")
            for ts, ev in self.event_queue:
                self.event_list.insert("end", f"[{ts}] {ev}")
            self.event_list.see("end")
            self.event_count_var.set(f"{len(self.event_queue)} 条")

    def _enqueue_event(self, line: str):
        """把服务器主动 EVENT 压入队列并刷新显示"""
        stamp = time.strftime("%H:%M:%S")
        self.event_queue.append((stamp, line))
        self.event_list.delete(0, "end")
        for ts, ev in self.event_queue:
            self.event_list.insert("end", f"[{ts}] {ev}")
        self.event_list.see("end")
        self.event_count_var.set(f"{len(self.event_queue)} 条")

    def _build_device_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="设备控制")
        frame.pack(fill="x", padx=8, pady=4)

        row1 = ttk.Frame(frame)
        row1.pack(fill="x")

        self.uv_btn = ttk.Button(row1, text="UV 灯: 关", width=16, command=self._toggle_uv)
        self.uv_btn.pack(side="left", padx=16, pady=8)

        self.roller_btn = ttk.Button(row1, text="滚子: 停止", width=16, command=self._toggle_roller)
        self.roller_btn.pack(side="left", padx=16, pady=8)

        # 急停按钮始终可用（用 tk.Button 以便着色）
        self.estop_btn = tk.Button(row1, text="急停", width=10,
                                   bg="#d32f2f", fg="white",
                                   font=("", 11, "bold"),
                                   command=self._emergency_stop)
        self.estop_btn.pack(side="left", padx=16, pady=8)

        # 闪喷 / 压墨 按钮独立一行
        row1b = ttk.Frame(frame)
        row1b.pack(fill="x")
        self.flash_btn = ttk.Button(row1b, text="闪喷: 关", width=14, command=self._send_flash)
        self.flash_btn.pack(side="left", padx=12, pady=8)
        self.press_ink_btn = ttk.Button(row1b, text="压墨", width=12,
                                        command=self._send_press_ink)
        self.press_ink_btn.pack(side="left", padx=(12, 4), pady=8)
        # 压墨时长预设：类似点动步长的单选按钮（选项来自 config.toml）
        ttk.Label(row1b, text="时长:").pack(side="left", padx=4, pady=8)
        self.press_ink_time_var = tk.StringVar(value=fmt_pos(PRESS_INK_DURATIONS[0]))
        self.press_ink_seconds = float(self.press_ink_time_var.get())
        for v in PRESS_INK_DURATIONS:
            tk.Radiobutton(row1b, text=f"{fmt_pos(v)}s", variable=self.press_ink_time_var,
                           value=fmt_pos(v), indicatoron=False, width=3,
                           command=self._select_press_ink_time,
                           selectcolor="#2e86de",        # 选中时底色（蓝色突出）
                           activebackground="#9ec7f0",   # 悬停底色
                           font=("", 10, "bold")).pack(side="left", padx=2, pady=8)

        row2 = ttk.Frame(frame)
        row2.pack(fill="x")
        ttk.Label(row2, text="UV灯水平距离 (mm):").pack(side="left", padx=(16, 2), pady=4)
        self.uv_dist_var = tk.StringVar(value=fmt_pos(UV_OFFSET_DEFAULT))
        ttk.Entry(row2, textvariable=self.uv_dist_var, width=8).pack(side="left", padx=2)
        # 自动UV灯: 自动循环中 X 向终点移动时开灯，其他情况关灯
        self.auto_uv_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="自动UV灯", variable=self.auto_uv_var,
                        command=self._auto_uv_toggled).pack(side="left", padx=16)

        # 打印中维护闪喷：自动循环中 X 到达终点累计计数，达间隔时暂停闪喷
        row3 = ttk.Frame(frame)
        row3.pack(fill="x")
        self.flash_maintain_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row3, text="打印中维护闪喷", variable=self.flash_maintain_var
                        ).pack(side="left", padx=(16, 2), pady=4)

        # 自定义指令：用户输入原始指令发送到设备或扫描服务器
        row4 = ttk.Frame(frame)
        row4.pack(fill="x")
        ttk.Label(row4, text="自定义指令:").pack(side="left", padx=(16, 2), pady=4)
        self.custom_cmd_var = tk.StringVar()
        cmd_entry = ttk.Entry(row4, textvariable=self.custom_cmd_var, width=20)
        cmd_entry.pack(side="left", padx=2)
        cmd_entry.bind("<Return>", lambda e: self._send_custom_cmd())
        self.custom_target_var = tk.StringVar(value="设备")
        ttk.Combobox(row4, textvariable=self.custom_target_var,
                     values=["设备", "扫描服务器"], width=8,
                     state="readonly").pack(side="left", padx=2)
        ttk.Button(row4, text="发送", width=6,
                   command=self._send_custom_cmd).pack(side="left", padx=4)

    def _build_log_frame(self, parent):
        from tkinter import scrolledtext
        frame = ttk.LabelFrame(parent, text="指令日志")
        frame.pack(fill="both", expand=True, padx=8, pady=4)

        self.log_text = scrolledtext.ScrolledText(frame, state="disabled",
                                                  font=("Consolas", 10), height=8)
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)
        ttk.Button(frame, text="清空日志", command=self._clear_log).pack(anchor="e", padx=4, pady=2)

    def _build_status_bar(self):
        self.status_var = tk.StringVar(value="未连接")
        ttk.Label(self, textvariable=self.status_var, anchor="w",
                  relief="sunken").pack(fill="x", side="bottom")

    # ---------- 连接 ----------

    def _toggle_connect(self):
        if self.sender.connected:
            self.sender.disconnect()
        else:
            host = self.ip_var.get().strip()
            try:
                port = int(self.port_var.get().strip())
            except ValueError:
                messagebox.showerror("错误", "端口必须是数字")
                return
            threading.Thread(target=self.sender.connect,
                             args=(host, port), daemon=True).start()

    def _toggle_scan(self):
        if self.scan.connected:
            self.scan.disconnect()
        else:
            host = self.scan_ip_var.get().strip()
            try:
                port = int(self.scan_port_var.get().strip())
            except ValueError:
                messagebox.showerror("错误", "端口必须是数字")
                return
            threading.Thread(target=self.scan.connect,
                             args=(host, port), daemon=True).start()

    # ---------- 扫描服务器回调 ----------

    def _on_scan_state(self, connected: bool, msg: str):
        def update():
            self.scan_btn.config(text="断开" if connected else "连接")
            self.scan_ready_var.set("等待就绪帧..." if connected else "未连接")
            self._append_log(f"[扫描] {msg}\n")
        self.after(0, update)

    def _on_scan_ready(self):
        def update():
            self.scan_ready_var.set("就绪 (NETSCAN_SERVER_READY)")
            self._append_log("[扫描] 收到就绪帧: NETSCAN_SERVER_READY\n")
        self.after(0, update)

    def _send_flash(self):
        """发送闪喷命令 FLASH\r\n 到扫描服务器"""
        if not self.scan.connected:
            messagebox.showwarning("提示", "扫描服务器未连接")
            return
        if self.scan.send(f"{CMD_FLASH}\r\n"):
            self._append_log(f"[闪喷] 发送: {CMD_FLASH}\r\n")

    def _select_press_ink_time(self):
        """切换压墨时长预设按钮，更新当前时长"""
        self.press_ink_seconds = float(self.press_ink_time_var.get())
        self.press_ink_time_label_var.set(f"(时长 {fmt_pos(self.press_ink_seconds)}s)")

    def _send_custom_cmd(self):
        """发送用户输入的自定义指令"""
        cmd = self.custom_cmd_var.get().strip()
        if not cmd:
            return
        if self.custom_target_var.get() == "扫描服务器":
            if not self.scan.connected:
                messagebox.showwarning("提示", "扫描服务器未连接")
                return
            if self.scan.send(f"{cmd}\r\n"):
                self._append_log(f"[自定义] 发送: {cmd}\r\n")
        else:
            if not self.sender.connected:
                messagebox.showwarning("提示", "设备未连接")
                return
            self.sender.send_command(cmd)
            self._append_log(f"[自定义] 发送: {cmd}\r\n")

    def _send_press_ink(self):
        """手动发送压墨命令 PRESS_INK <秒数>\r\n 到扫描服务器"""
        if not self.scan.connected:
            messagebox.showwarning("提示", "扫描服务器未连接")
            return
        seconds = self.press_ink_seconds
        if self.scan.send(f"PRESS_INK {fmt_pos(seconds)}\r\n"):
            self._append_log(f"[压墨] 发送: PRESS_INK {fmt_pos(seconds)}\r\n")

    def _on_scan_response(self, line: str):
        def update():
            self._append_log(f"[扫描回复] {line}\n")
            if line.strip().startswith("EVENT "):
                # 服务器主动事件：压入事件队列并显示
                self._enqueue_event(line.strip())
            m = START_JOB_PATTERN.fullmatch(line.strip())
            if m:
                # 服务器事件：启动服务器广播驱动的打印任务（独立逻辑）
                total_layers = int(m.group(1))
                self._append_log(
                    f"[扫描] 收到开始任务事件，总层数 {total_layers}，启动任务循环\n")
                self._start_job(total_layers)
                return
            m = PRINT_JOB_COMPLETED_PATTERN.fullmatch(line.strip())
            if m:
                total_layers = int(m.group(1))
                self._append_log(
                    f"[扫描] 收到打印完成事件，总层数 {total_layers}\n")
                if not (self._auto_active and self._auto_mode == "server"):
                    self._append_log(
                        "[自动] 当前没有服务器打印任务，忽略打印完成事件\n")
                    return
                self._remove_event(line.strip())
                self._complete_server_job()
                return
            m = LAYER_START_PATTERN.match(line.strip())
            if m:
                layer = int(m.group(1))
                self._append_log(f"[图层] LAYER_START LAYER={layer}\n")
                if (self._auto_active and self._auto_mode == "server"):
                    # 新层事件优先级最高：丢弃上一层残留 PASS/ZERO，
                    # 立即切换到当前层定位流程。
                    self._pending.clear()
                    self._auto_leg = "wait_layer"
                    self.event_queue = deque(
                        (ts, ev) for ts, ev in self.event_queue
                        if not (PASS_READY_PATTERN.fullmatch(ev)
                                or PASS_REMAINING_ZERO_PATTERN.fullmatch(ev))
                    )
                    self.event_list.delete(0, "end")
                    for ts, ev in self.event_queue:
                        self.event_list.insert("end", f"[{ts}] {ev}")
                    self.event_count_var.set(f"{len(self.event_queue)} 条")
                    if 1 <= layer <= self._job_total_layers:
                        self._job_current_layer = layer
                        self._update_job_progress()
                        if layer == self._job_total_layers:
                            self._layer_listening = False
                            self._append_log(
                                f"[图层] 已监听到最后一层 {layer}/"
                                f"{self._job_total_layers}\n")
                    else:
                        self._append_log(
                            f"[图层] LAYER={layer} 超出任务总层数 "
                            f"{self._job_total_layers}，忽略进度更新\n")
                # 立即消费当前层并执行 Z/Y 定位，不受上一层状态影响
                self._try_start_layer(layer)
                return
            m = PASS_READY_PATTERN.match(line.strip())
            if m:
                cur, total, step, empty = (int(value) for value in m.groups())
                self._append_log(
                    f"[PASS] 就绪 CURRENT={cur} TOTAL={total} STEP={step} EMPTY={empty}\n")
                self._try_start_pass(line.strip(), cur, total, step, empty)
                return
            m = PASS_REMAINING_ZERO_PATTERN.fullmatch(line.strip())
            if m:
                layer, pass_no = (int(value) for value in m.groups())
                self._append_log(
                    f"[PASS] 剩余列数归零 LAYER={layer} PASS={pass_no}\n")
                handled = self._stop_x_on_pass_remaining_zero(line.strip(), layer, pass_no)
                if (not handled and self.auto_uv_var.get()
                        and self._auto_active and self._auto_mode == "server"
                        and self._auto_leg == "wait_pass_zero"):
                    # 自动 UV 模式下 X 已到位，ZERO 晚到时直接衔接
                    # 原急停后的计数、维护闪喷和等待逻辑。
                    self._auto_leg = "end"
                    self._auto_advance()
                elif (not handled and self._auto_active
                      and self._auto_mode == "server"
                      and self._auto_leg == "wait_pass_zero"):
                    # 事件可能因层号状态更新时序尚未同步；既然当前正等待
                    # ZERO，直接消费该事件并继续，不让流程永久等待。
                    self._remove_event(line.strip())
                    self._auto_leg = "end"
                    self._auto_advance()
                return
            if "PRESS_INK" in line:
                # 压墨完成/结果回送，仅记录日志
                self._append_log(f"[压墨] {line}\n")
                return
            m = FLASH_RESPONSE_PATTERN.fullmatch(line.strip())
            if m:
                before, after = m.group(1), m.group(2)
                self.flash_on = after == "ON"
                self.flash_btn.config(text=f"闪喷: {'开' if self.flash_on else '关'}")
                self._append_log(f"[闪喷] 状态: {before} -> {after}\n")
                if self._flash_wait_off and not self.flash_on:
                    # 结束闪喷已确认关闭，继续移动 X
                    if self._flash_wait_after is not None:
                        self.after_cancel(self._flash_wait_after)
                        self._flash_wait_after = None
                    self._flash_wait_off = False
                    self._append_log("[自动] 闪喷已结束，继续自动循环\n")
                    self._proceed_after_flash()
            elif FLASH_ACCEPTED in line:
                self._append_log(f"[闪喷] 已接受: {FLASH_ACCEPTED}\n")
            elif line.startswith("未知命令"):
                self._append_log(f"[闪喷] 未知命令回复: {line}\n")
        self.after(0, update)

    # ---------- 轴控制 ----------

    def _check_range(self, axis: str, target: float) -> bool:
        lo, hi = AXIS_LIMITS[axis]
        if not (lo <= target <= hi):
            messagebox.showwarning(
                "超出行程",
                f"{axis} 轴目标位置 {fmt_pos(target)} 超出行程范围 [{fmt_pos(lo)}, {fmt_pos(hi)}]")
            return False
        return True

    def _move_absolute(self, axis: str):
        try:
            target = float(self.target_vars[axis].get().strip())
        except ValueError:
            messagebox.showerror("错误", f"{axis} 轴目标位置必须是数字")
            return
        if not self._check_range(axis, target):
            return
        self.sender.send_command(f"{axis}={fmt_pos(target)}")
        self._wait_target(axis, target)

    def _move_to(self, axis: str, target: float):
        """移动到指定绝对位置（快捷按钮用）"""
        if not self._check_range(axis, target):
            return
        self.sender.send_command(f"{axis}={fmt_pos(target)}")
        self._wait_target(axis, target)

    def _jog(self, axis: str, direction: int):
        try:
            step = abs(float(self.step_var.get().strip()))
        except ValueError:
            messagebox.showerror("错误", "点动步长无效")
            return
        delta = step * direction
        target = self.positions[axis] + delta
        if not self._check_range(axis, target):
            return
        self.sender.send_command(f"{axis}{'+' if delta >= 0 else '-'}{fmt_pos(abs(delta))}")
        self._wait_target(axis, target)

    # ---------- 运动锁定（按轴独立） ----------

    def _wait_target(self, axis: str, target: float):
        """登记运动目标并锁定该轴的控件，直到设备上报到达"""
        self._pending[axis] = target
        self._set_axis_enabled(axis, False)
        if not self._auto_active:
            self.status_var.set(f"{axis} 轴运动中... 到达目标位置后才能再次操作该轴")

    def _set_axis_enabled(self, axis: str, enabled: bool):
        state = "normal" if enabled else "disabled"
        for w in self.axis_widgets[axis]:
            w.config(state=state)

    def _set_auto_widgets(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for w in self.auto_widgets:
            w.config(state=state)

    def _lock_auto_group(self):
        """自动循环期间锁定 X、Y、Z 三轴及循环参数控件（UV / 滚子不受影响）"""
        for axis in AXIS_LIMITS:
            self._set_axis_enabled(axis, False)
        self._set_auto_widgets(False)
        self.status_var.set("自动循环运行中... X/Y/Z 轴已锁定")

    def _unlock_auto_group(self):
        for axis in AXIS_LIMITS:
            self._set_axis_enabled(axis, True)
        self._set_auto_widgets(True)

    def _unlock_all(self):
        for axis in self.axis_widgets:
            self._set_axis_enabled(axis, True)
        self._set_auto_widgets(True)

    def _check_arrival(self):
        """每次收到状态帧后调用：到达的轴解锁；自动循环时推进下一段"""
        arrived = [a for a, t in self._pending.items()
                   if abs(self.positions[a] - t) <= POS_TOLERANCE]
        for a in arrived:
            del self._pending[a]
        if not arrived:
            return
        if self._auto_active:
            # 自动循环期间 X/Y/Z 三轴全部锁定，到达后不单独解锁
            if not self._pending:
                self._auto_advance()
                self._auto_uv_update()
                if not self._auto_active:
                    # 循环刚结束/中止
                    self._unlock_auto_group()
                    self.status_var.set(
                        f"已连接 {self.ip_var.get()}:{self.port_var.get()}（自动循环结束）")
            return  # 自动循环期间三轴保持锁定
        for a in arrived:
            self._set_axis_enabled(a, True)
        if not self._pending:
            self.status_var.set(f"已连接 {self.ip_var.get()}:{self.port_var.get()}（已到达目标位置）")

    # ---------- X 轴自动循环 ----------

    def _update_cycle_info(self):
        self.cycle_info_var.set(
            f"大循环剩余: {self._outer_remaining}，内循环剩余: {self._auto_remaining}")

    def _update_auto_counts(self):
        """刷新闪喷计数显示。"""
        self.auto_count_var.set(f"闪喷计数: {self._flash_count}")

    def _update_job_progress(self):
        """刷新服务器打印任务的当前层/总层数显示。"""
        self.job_progress_var.set(
            f"打印进度: {self._job_current_layer}/{self._job_total_layers}")

    def _update_pass_progress(self):
        """刷新服务器打印任务的当前 PASS/总 PASS 数显示。"""
        self.pass_progress_var.set(
            f"PASS进度: {self._pass_current}/{self._pass_total}")

    def _start_job(self, total_layers: int):
        """服务器 START_JOB 事件：新的任务逻辑，不复用 _start_auto。

        由服务器广播事件（START_JOB / LAYER_START / PASS_READY）驱动。
        当前仅锁定 X/Y/Z 三轴（循环参数不参与锁定，下方流程不会用到）。
        """
        # 每次 START_JOB 都视为新任务，清空上一任务的队列、目标和定时器。
        self._pending.clear()
        self._cancel_flash_pause()
        self.event_queue.clear()
        self.event_list.delete(0, "end")
        self.event_count_var.set("0 条")
        self._flash_count = 0
        self._update_auto_counts()
        self._auto_active = True
        self._auto_mode = "server"
        self._y_start = self.positions["Y"]
        self._job_current_layer = 0
        self._job_total_layers = total_layers
        self._active_layer = 0
        self._layer_listening = total_layers > 0
        self._pass_current = 0
        self._pass_total = 0
        self._update_job_progress()
        self._update_pass_progress()
        self.eta_var.set("")
        self.cycle_info_var.set("")
        self._append_log("[自动] 服务器开始任务，进入自动循环\n")
        # 仅锁定 X/Y/Z 三轴
        for axis in AXIS_LIMITS:
            self._set_axis_enabled(axis, False)
        # 锁定后先记录任务开始时的 Y 位置，再移动 X 到起始位置
        self._append_log(
            f"[自动] 记录 Y 初始位置 {fmt_pos(self._y_start)}\n")
        self._auto_leg = "prehome"
        self._append_log(f"[自动] 移动 X 到起始位置 {fmt_pos(X_HOME)}\n")
        self.sender.send_command(f"X={fmt_pos(X_HOME)}")
        self._wait_target("X", X_HOME)
        self._auto_uv_update()

    def _complete_server_job(self):
        """收到服务端 PRINT_JOB_COMPLETED 后清理任务，并清空 Y 初始位置。"""
        self._auto_active = False
        self._auto_mode = None
        self._auto_leg = "complete"
        self._layer_listening = False
        self._active_layer = 0
        self._job_current_layer = 0
        self._job_total_layers = 0
        self._pass_current = 0
        self._pass_total = 0
        self._outer_remaining = 0
        self._pending.clear()
        self._cancel_flash_pause()
        self._unlock_auto_group()
        self.cycle_info_var.set("")
        self.eta_var.set("")
        self._update_job_progress()
        self._update_pass_progress()
        if self.auto_uv_var.get():
            self._set_uv(False)
        self._y_start = 0.0
        self._play_finish_sound()
        self.status_var.set("打印完成，全部流程已结束")
        self._append_log(
            "[自动] 打印完成，已结束全部流程并清空 Y 初始位置\n")

    def _start_manual_auto(self):
        try:
            count = int(self.cycle_var.get().strip())
            if count < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("错误", "循环次数必须是正整数")
            return
        try:
            outer = int(self.outer_var.get().strip())
            if outer < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("错误", "大循环次数必须是正整数")
            return
        try:
            ystep = abs(float(self.ystep_var.get().strip()))
            if round(ystep, 2) != ystep:
                raise ValueError
        except ValueError:
            messagebox.showerror("错误", "Y 步进必须是非负数字，最多两位小数")
            return
        if "X" in self._pending or "Y" in self._pending:
            messagebox.showwarning("提示", "X 或 Y 轴正在运动中，请等待到达后再开始自动循环")
            return
        try:
            uv_dist = float(self.uv_dist_var.get().strip())
            if not (0 <= uv_dist <= X_END_WITH_UV):
                raise ValueError
        except ValueError:
            if self.auto_uv_var.get():
                messagebox.showerror("错误", f"UV灯水平偏移必须是 0 ~ {fmt_pos(X_END_WITH_UV)} 之间的数字")
                return
            uv_dist = X_END  # 未启用自动UV灯时该值无意义
        self._uv_dist = uv_dist
        # 闪喷间隔只由 config.toml 的 [flash].interval 配置，不在 UI 修改。
        self._flash_interval = FLASH_INTERVAL_DEFAULT
        self._flash_count = 0
        self._update_auto_counts()
        self._cancel_flash_pause()
        self._auto_ystep = ystep
        self._inner_total = count
        self._auto_remaining = count
        self._outer_remaining = outer
        self._y_start = self.positions["Y"]
        self._auto_active = True
        self._auto_mode = "manual"
        self._auto_eta = self._estimate_auto_time(count, outer, ystep)
        self.eta_var.set(f"预计完成时间: {fmt_duration(self._auto_eta)}")
        self._update_cycle_info()
        self._append_log(
            f"[自动] 开始: 大循环 {outer} 组 x 内循环 {count} 次"
            f"（X: {fmt_pos(X_HOME)} <-> {fmt_pos(X_END)}，循环间 Y "
            f"{'-' if Y_STEP_DIR < 0 else '+'}{fmt_pos(ystep)}，"
            f"Y 起始位置 {fmt_pos(self._y_start)}）\n")
        self._append_log(f"[自动] 预计完成时间: {fmt_duration(self._auto_eta)}\n")
        self._lock_auto_group()
        # 先回起始位置，避免当前 X 位置不确定
        self._auto_leg = "prehome"
        self._append_log(f"[自动] 先回起始位置 {fmt_pos(X_HOME)}\n")
        self.sender.send_command(f"X={fmt_pos(X_HOME)}")
        self._wait_target("X", X_HOME)
        self._auto_uv_update()

    def _stop_manual_auto(self):
        if not self._auto_active:
            return
        self._auto_active = False
        self._auto_mode = None
        self._outer_remaining = 0
        self._pending.clear()
        self._cancel_flash_pause()
        self._unlock_auto_group()
        self.cycle_info_var.set("")
        self.eta_var.set("")
        if self.auto_uv_var.get():
            self._set_uv(False)
        self._append_log("[自动] 已停止（当前这段运动会继续走完）\n")

    def _auto_abort(self, reason: str):
        """异常终止自动循环（如 Y 步进超出行程）"""
        self._auto_active = False
        self._auto_mode = None
        self._outer_remaining = 0
        self._pending.clear()
        self._cancel_flash_pause()
        self._unlock_auto_group()
        self.cycle_info_var.set("")
        self.eta_var.set("")
        if self.auto_uv_var.get():
            self._set_uv(False)
        self._append_log(f"[自动] 已中止: {reason}\n")
        messagebox.showwarning("自动循环中止", reason)

    def _flash_pause(self):
        """达到闪喷间隔时，先 X 正向偏移再执行维护闪喷。"""
        self._flash_pausing = True
        self._append_log(
            f"[自动] X 到达终点，维护闪喷（间隔 {self._flash_interval}），暂停\n")
        if (self._auto_mode == "server" and FLASH_X_OFFSET > 0):
            target = self.positions["X"] + FLASH_X_OFFSET
            if not (AXIS_LIMITS["X"][0] <= target <= AXIS_LIMITS["X"][1]):
                self._auto_abort(
                    f"维护闪喷前 X 正向偏移目标 {fmt_pos(target)} 超出行程 "
                    f"[{fmt_pos(AXIS_LIMITS['X'][0])}, {fmt_pos(AXIS_LIMITS['X'][1])}]")
                return
            self._auto_leg = "server_flash_offset"
            self._append_log(
                f"[自动] 维护闪喷前 X 正向偏移 {fmt_pos(FLASH_X_OFFSET)} "
                f"-> {fmt_pos(target)}，等待到位后开启闪喷\n")
            self.sender.send_command(f"X+{fmt_pos(FLASH_X_OFFSET)}")
            self._wait_target("X", target)
            self._auto_uv_update()
            return
        self._do_flash_pause()

    def _do_flash_pause(self):
        """压墨完成后（或未启用/失败），执行闪喷"""
        self._append_log("[自动] 暂停闪喷\n")
        self._send_flash()
        self.status_var.set("闪喷中... 2s 后结束闪喷")
        self._flash_after = self.after(FLASH_PAUSE_MS, self._flash_resume)

    def _flash_resume(self):
        """闪喷暂停结束：若闪喷仍开启则先发送结束命令，确认关闭后再移动 X"""
        self._flash_after = None
        self._flash_pausing = False
        if not self._auto_active:
            return
        if self.flash_on:
            self._flash_wait_off = True
            self._append_log("[自动] 发送结束闪喷命令\n")
            self._send_flash()
            self.status_var.set("结束闪喷中...")
            self._flash_wait_after = self.after(FLASH_PAUSE_MS, self._flash_wait_timeout)
            return
        self._proceed_after_flash()

    def _proceed_after_flash(self):
        """结束闪喷确认后，按手动/服务端任务类型继续对应流程。"""
        if not self._auto_active:
            return
        if (self._auto_mode == "server"
                and self._auto_leg == "server_flash_pause"):
            self._append_log(
                "[自动] 维护闪喷完成，开始急停后的配置等待\n")
            self._start_server_stop_wait()
            return
        self._auto_leg = "home"
        self.sender.send_command(f"X={fmt_pos(X_HOME)}")
        self._wait_target("X", X_HOME)
        self._auto_uv_update()

    def _flash_wait_timeout(self):
        """结束闪喷回送超时保护：超时后直接继续移动，避免卡住"""
        self._flash_wait_after = None
        if self._flash_wait_off:
            self._flash_wait_off = False
            self._append_log("[自动] 结束闪喷回送超时，继续移动\n")
            self._proceed_after_flash()

    def _cancel_flash_pause(self):
        """取消未执行的闪喷定时器并停止警告音（停止/中止/急停/断开时调用）"""
        for attr in ("_flash_after", "_flash_wait_after", "_server_stop_after"):
            timer = getattr(self, attr, None)
            if timer is not None:
                self.after_cancel(timer)
                setattr(self, attr, None)
        self._flash_pausing = False
        self._flash_wait_off = False

    def _z_step_down(self):
        """X 循环累计计数达阈值时，按配置下降 Z（不阻塞手动循环）。"""
        target = self.positions["Z"] - X_CYCLE_Z_STEP
        if not (AXIS_LIMITS["Z"][0] <= target <= AXIS_LIMITS["Z"][1]):
            self._append_log(
                f"[自动] Z 轴下降超出行程，跳过: 目标 {fmt_pos(target)}\n")
            return
        self.sender.send_command(f"Z-{fmt_pos(X_CYCLE_Z_STEP)}")
        self._append_log(
            f"[自动] X 循环计数达 {X_CYCLE_COUNT_LIMIT}，"
            f"Z 轴下降 {fmt_pos(X_CYCLE_Z_STEP)} -> {fmt_pos(target)}\n")

    def _start_server_layer_positioning(self):
        """服务端新层开始时先按 X 循环计数下降 Z，再让 Y 回初始位置。"""
        if not (self._auto_active and self._auto_mode == "server"):
            return
        layer = self._active_layer
        if layer > 0 and layer % X_CYCLE_COUNT_LIMIT == 0:
            target = self.positions["Z"] - X_CYCLE_Z_STEP
            if not (AXIS_LIMITS["Z"][0] <= target <= AXIS_LIMITS["Z"][1]):
                self._auto_abort(
                    f"打印层数 {layer} 达到 {X_CYCLE_COUNT_LIMIT} 的倍数，但 Z 下降目标 "
                    f"{fmt_pos(target)} 超出行程 "
                    f"[{fmt_pos(AXIS_LIMITS['Z'][0])}, {fmt_pos(AXIS_LIMITS['Z'][1])}]")
                return
            self._auto_leg = "layer_zstep"
            self.sender.send_command(f"Z-{fmt_pos(X_CYCLE_Z_STEP)}")
            self._wait_target("Z", target)
            self._append_log(
                f"[自动] 打印到第 {layer} 层（达到 {X_CYCLE_COUNT_LIMIT} 的倍数），"
                f"Z 下降 {fmt_pos(X_CYCLE_Z_STEP)} -> {fmt_pos(target)}\n")
            self._auto_uv_update()
            return
        self._start_server_layer_yhome()

    def _start_server_layer_yhome(self):
        """服务端新层开始时让 Y 回到任务初始位置并等待到位。"""
        if not (AXIS_LIMITS["Y"][0] <= self._y_start <= AXIS_LIMITS["Y"][1]):
            self._auto_abort(
                f"Y 初始位置 {fmt_pos(self._y_start)} 超出行程")
            return
        self._auto_leg = "layer_yhome"
        self._append_log(
            f"[自动] 新层开始，Y 移动到初始位置 {fmt_pos(self._y_start)}\n")
        self.sender.send_command(f"Y={fmt_pos(self._y_start)}")
        self._wait_target("Y", self._y_start)
        self._auto_uv_update()

    def _begin_layer_motion(self):
        """手动流程收到 LAYER_START 后，从起始位置开始向终点运动。"""
        if not self._auto_active or self._auto_leg != "wait_layer":
            return
        self._begin_x_end_motion()

    def _begin_x_end_motion(self):
        """按配置文件的 X_END 开始 X 运动。"""
        target = X_END_WITH_UV if (self._auto_mode == "server" and self.auto_uv_var.get()) else X_END
        self._auto_leg = "end"
        self._append_log(
            f"[自动] 使用配置文件的 X 终点位置 {fmt_pos(target)}\n")
        self.sender.send_command(f"X={fmt_pos(target)}")
        self._wait_target("X", target)
        # ZERO 可能在 Y 移动或层切换期间提前入队。X 段开始后立即检查，
        # 但只消费属于当前正式执行层/PASS 的事件。
        if self._auto_mode == "server":
            self._stop_x_on_pass_remaining_zero()

    def _try_start_pass(self, received_event=None, current=None, total=None,
                        step=None, empty=None):
        """wait_pass_ready 时消费一条合法 PASS_READY，并执行当前 PASS 的 X 运动。"""
        if not (self._auto_active and self._auto_mode == "server"
                and self._auto_leg == "wait_pass_ready"):
            return
        event = received_event
        if event is None:
            # 等待期间服务器可能已经推进了多个 PASS。使用最新事件与服务端
            # 当前状态对齐，避免回放旧 PASS 后令 ZERO 的 PASS 号永远错位。
            for _, queued_event in reversed(self.event_queue):
                match = PASS_READY_PATTERN.fullmatch(queued_event)
                if match:
                    current, total, step, empty = (
                        int(value) for value in match.groups())
                    event = queued_event
                    break
        if (event is None or current is None or total is None
                or total < 1 or not 1 <= current <= total):
            return
        queued_pass_events = [
            queued_event for _, queued_event in self.event_queue
            if PASS_READY_PATTERN.fullmatch(queued_event)
        ]
        stale_count = max(0, len(queued_pass_events) - 1)
        # 当前选中的是最新 PASS_READY；同一等待窗口内更早的 PASS_READY 已过期，
        # 一并移除，不能在后续 X 回起点后再次执行。
        for queued_event in dict.fromkeys(queued_pass_events):
            self._remove_event(queued_event)
        if stale_count:
            self._append_log(
                f"[自动] 已跳过 {stale_count} 条过期 PASS_READY，"
                f"与服务端最新 PASS {current}/{total} 对齐\n")
        self._pass_current = current
        self._pass_total = total
        self._update_pass_progress()
        self._append_log(
            f"[自动] 开始 PASS {current}/{total}（STEP={step}, EMPTY={empty}）\n")
        if empty == 1:
            self._append_log(
                f"[自动] PASS {current}/{total} EMPTY=1，跳过 Y/X 运动\n")
            self._finish_server_pass()
            return
        # STEP 单位为 μm；设备 Y 相对移动指令只接受两位小数。
        # 10 μm = 0.01 mm，使用整数运算实现精确的四舍五入（远离 0）。
        step_hundredths = (abs(step) + 5) // 10
        step_mm = step_hundredths / 100.0
        if step < 0:
            step_mm = -step_mm
        if step_mm == 0:
            self._append_log(
                f"[自动] PASS STEP={step} μm，四舍五入为 0.00 mm，跳过 Y 移动\n")
            self._begin_x_end_motion()
            return
        y_target = self.positions["Y"] + step_mm
        if not (AXIS_LIMITS["Y"][0] <= y_target <= AXIS_LIMITS["Y"][1]):
            self._auto_abort(
                f"PASS STEP 后 Y 目标 {fmt_pos(y_target)} 超出行程 "
                f"[{fmt_pos(AXIS_LIMITS['Y'][0])}, {fmt_pos(AXIS_LIMITS['Y'][1])}]")
            return
        sign = "+" if step_mm > 0 else "-"
        distance = f"{abs(step_mm):.2f}"
        self._auto_leg = "pass_ystep"
        self._append_log(
            f"[自动] PASS STEP={step} μm，四舍五入后 Y 相对移动 {sign}{distance} mm "
            f"-> {fmt_pos(y_target)}\n")
        self.sender.send_command(f"Y{sign}{distance}")
        self._wait_target("Y", y_target)
        self._auto_uv_update()

    def _finish_server_pass(self):
        """当前服务器 PASS 完成后，先让 X 回起点；到位后再判断是否还有 PASS。"""
        if not (self._auto_active and self._auto_mode == "server"):
            return
        self._auto_leg = "pass_return_home"
        self.status_var.set(
            f"PASS {self._pass_current}/{self._pass_total} 完成，X 返回起始位置")
        self._append_log(
            f"[自动] PASS {self._pass_current}/{self._pass_total} 完成，"
            f"X 返回起始位置 {fmt_pos(X_HOME)}，到位后判断 CURRENT < TOTAL\n")
        self.sender.send_command(f"X={fmt_pos(X_HOME)}")
        self._wait_target("X", X_HOME)
        self._auto_uv_update()

    def _stop_x_on_pass_remaining_zero(self, event=None, layer=None, pass_no=None):
        """只用当前执行层/PASS 的 ZERO 事件急停 X；早到事件保留在队列。"""
        if not (self._auto_active
                and self._auto_mode == "server"
                and self._auto_leg in ("end", "wait_pass_zero")):
            return False
        if event is None:
            for _, queued_event in self.event_queue:
                match = PASS_REMAINING_ZERO_PATTERN.fullmatch(queued_event)
                if not match:
                    continue
                queued_layer, queued_pass = (
                    int(value) for value in match.groups())
                if (queued_layer == self._active_layer
                        and queued_pass == self._pass_current):
                    event = queued_event
                    layer = queued_layer
                    pass_no = queued_pass
                    break
        # X 到位后处于 wait_pass_zero 时，层号可能因事件时序尚未同步；
        # 当前正在等待的 PASS 号优先，允许层号随后再更新。
        layer_ok = (layer == self._active_layer or self._auto_leg == "wait_pass_zero")
        if (event is None or not layer_ok or pass_no != self._pass_current):
            return False
        if self.auto_uv_var.get():
            # 自动 UV 模式不急停；事件到达后消费并衔接终点后的处理。
            self._remove_event(event)
            if self._auto_leg == "wait_pass_zero":
                self._auto_leg = "end"
                self._auto_advance()
            return True
        self._remove_event(event)
        self._pending.pop("X", None)
        self.sender.send_urgent(CMD_ESTOP)
        # 匹配事件被消费并真正发送 q 时才计数，避免早到、重复或不匹配事件误计。
        self._flash_count += 1
        self._update_auto_counts()
        self._auto_uv_update()
        self._append_log(
            f"[自动] LAYER={layer} PASS={pass_no} 剩余列数变为 0，"
            f"已发送 q 停止 X；闪喷计数={self._flash_count}\n")
        if (self.flash_maintain_var.get()
                and self._flash_count >= self._flash_interval):
            self._flash_count = 0
            self._update_auto_counts()
            self._auto_leg = "server_flash_pause"
            self.status_var.set(
                f"LAYER={layer} PASS={pass_no} 已急停，达到闪喷间隔，正在维护闪喷")
            self._append_log(
                f"[自动] 闪喷计数达到配置间隔 {self._flash_interval}，"
                "闪喷计数清零并执行维护闪喷\n")
            self._flash_pause()
            return True
        self._start_server_stop_wait()
        return True

    def _remove_matching_pass_zero(self):
        """自动 UV 模式下消费当前 PASS 的 ZERO 事件但不发送急停。"""
        for _, queued_event in list(self.event_queue):
            match = PASS_REMAINING_ZERO_PATTERN.fullmatch(queued_event)
            if match and int(match.group(1)) == self._active_layer and int(match.group(2)) == self._pass_current:
                self._remove_event(queued_event)
                return

    def _start_server_stop_wait(self):
        """进入服务端 PASS 急停后的配置等待。"""
        if not (self._auto_active and self._auto_mode == "server"):
            return
        self._auto_leg = "wait_after_pass_stop"
        wait_text = fmt_pos(PASS_STOP_WAIT_SECONDS)
        self.status_var.set(
            f"LAYER={self._active_layer} PASS={self._pass_current} 已急停，"
            f"等待 {wait_text} 秒")
        self._append_log(
            f"[自动] 急停后等待 {wait_text} 秒\n")
        self._server_stop_after = self.after(
            PASS_STOP_WAIT_MS, self._resume_after_pass_stop)

    def _resume_after_pass_stop(self):
        """每次 PASS 急停后等待配置时长，再完成当前 PASS。"""
        self._server_stop_after = None
        if not (self._auto_active and self._auto_mode == "server"
                and self._auto_leg == "wait_after_pass_stop"):
            return
        self._append_log(
            f"[自动] 急停后等待 {fmt_pos(PASS_STOP_WAIT_SECONDS)} 秒完成，"
            "继续 PASS 流程\n")
        self._finish_server_pass()

    def _try_start_layer(self, received_layer=None):
        """wait_layer 时，消费任意合法的 LAYER_START，并开始该层流程。

        若广播早于起始位置就位则从队列查找并消费；若广播尚未到达则保持
        wait_layer，待 LAYER_START 处理器入队后传入对应层号再次触发。
        """
        if not (self._auto_active and self._auto_leg == "wait_layer"):
            return
        max_layer = self._job_total_layers if self._auto_mode == "server" else 1
        layer = received_layer
        event = None
        if layer is not None:
            candidate = f"EVENT LAYER_START LAYER={layer}"
            if any(ev == candidate for _, ev in self.event_queue):
                event = candidate
        else:
            for _, queued_event in self.event_queue:
                match = LAYER_START_PATTERN.fullmatch(queued_event)
                if match:
                    candidate_layer = int(match.group(1))
                    if 1 <= candidate_layer <= max_layer:
                        layer = candidate_layer
                        event = queued_event
                        break
        if (event is None or layer is None
                or not 1 <= layer <= max_layer):
            return
        self._remove_event(event)
        if self._auto_mode == "server":
            self._active_layer = layer
            if self._job_current_layer != layer:
                self._job_current_layer = layer
                self._update_job_progress()
            if layer == self._job_total_layers:
                self._layer_listening = False
        self._append_log(
            f"[自动] 收到 LAYER_START LAYER={layer}\n")
        if self._auto_mode == "server":
            if self._auto_leg == "wait_layer":
                self._pass_current = 0
                self._pass_total = 0
                self._update_pass_progress()
                self._start_server_layer_positioning()
        else:
            self._begin_layer_motion()

    def _auto_advance(self) -> bool:
        """一段到达后推进自动循环，返回 True 表示已发出下一段"""
        if self._auto_leg == "prehome":
            # 手动流程在 prehome 到位后记录 Y；服务器流程已在移动 X 前记录，不能在此覆盖。
            if self._auto_mode == "manual":
                self._y_start = self.positions["Y"]
                self._append_log(
                    f"[自动] 记录 Y 初始位置 {fmt_pos(self._y_start)}\n")
            self._auto_leg = "wait_layer"
            # 从队列消费任意合法的 LAYER_START；存在则先让 Y 回任务初始位置。
            self._try_start_layer()
            return True
        if self._auto_leg == "wait_layer":
            # 等待 LAYER_START 事件触发层处理
            return True
        if self._auto_leg == "wait_pass_ready":
            # 等待 PASS_READY 事件触发当前 PASS 的 X 运动
            return True
        if self._auto_leg == "layer_zstep":
            # 达到配置阈值时先等待 Z 下降到位，再让 Y 回到任务初始位置。
            self._start_server_layer_yhome()
            return True
        if self._auto_leg == "server_flash_offset":
            # X 正向偏移到位后才开启维护闪喷。
            self._do_flash_pause()
            return True
        if self._auto_leg == "layer_yhome":
            # 每层开始时 Y 回到任务初始位置，到位后再进入 PASS 循环。
            self._auto_leg = "wait_pass_ready"
            self.status_var.set("Y 已回到初始位置，等待 PASS_READY")
            self._append_log("[自动] Y 已回到初始位置，等待 PASS_READY\n")
            self._try_start_pass()
            return True
        if self._auto_leg == "pass_ystep":
            # 当前 PASS 的 Y 相对移动到位后，再执行动态 X 终点流程。
            self._begin_x_end_motion()
            return True
        if self._auto_leg == "pass_return_home":
            # 每个 PASS 都先回 X_HOME；到位后才判断是否进入下一 PASS。
            if self._pass_total > 0 and self._pass_current < self._pass_total:
                self._auto_leg = "wait_pass_ready"
                self.status_var.set("X 已回到起始位置，等待下一条 PASS_READY")
                self._append_log(
                    "[自动] X 已回到起始位置，CURRENT < TOTAL，"
                    "等待下一条 PASS_READY\n")
                self._try_start_pass()
                return True
            self._pass_current = 0
            self._pass_total = 0
            self._update_pass_progress()
            self._auto_leg = "wait_layer"
            self.status_var.set("X 已回到起始位置，本层完成，等待下一层 LAYER_START")
            self._append_log(
                "[自动] X 已回到起始位置，CURRENT < TOTAL 为否，"
                "等待下一层 LAYER_START\n")
            self._try_start_layer()
            return True
        if self._auto_leg == "end":
            if self._auto_mode == "server":
                if self.auto_uv_var.get():
                    # 自动 UV 模式下不急停，完整移动至带 UV 终点后关闭 UV，
                    # 再执行原急停后的计数、维护闪喷及等待逻辑。
                    self._set_uv(False)
                    self._remove_matching_pass_zero()
                    self._flash_count += 1
                    self._update_auto_counts()
                    if (self.flash_maintain_var.get()
                            and self._flash_count >= self._flash_interval):
                        self._flash_count = 0
                        self._update_auto_counts()
                        self._auto_leg = "server_flash_pause"
                        self._flash_pause()
                    else:
                        self._start_server_stop_wait()
                    return True
                # X 先到终点时，ZERO 事件可能尚未到达；进入等待状态，
                # 由稍后收到的 PASS_REMAINING_ZERO 继续执行后续逻辑。
                self._auto_leg = "wait_pass_zero"
                self._append_log(
                    f"[自动] X 已到达终点 {fmt_pos(X_END)}，等待 PASS_REMAINING_ZERO 事件\n")
                self._stop_x_on_pass_remaining_zero()
                return True
            # 到达终点 -> 计数；启用维护闪喷且达间隔时，先暂停闪喷再返回起始
            self._flash_count += 1
            # X 到达终点累计计数达阈值时，Z 轴下降（独立于闪喷计数）
            if self.flash_maintain_var.get() and self._flash_count >= self._flash_interval:
                self._flash_count = 0
                self._update_auto_counts()
                self._flash_pause()
                return True
            self._update_auto_counts()
            self._auto_leg = "home"
            self.sender.send_command(f"X={fmt_pos(X_HOME)}")
            self._wait_target("X", X_HOME)
            return True
        if self._auto_leg == "wait_pass_zero":
            # 等待 PASS_REMAINING_ZERO 事件；事件处理器会触发后续流程。
            self._stop_x_on_pass_remaining_zero()
            return True
        if self._auto_leg == "ystep":
            # Y 步进完成 -> 开始下一次内循环，向终点运动
            self._auto_leg = "end"
            self.sender.send_command(f"X={fmt_pos(X_END)}")
            self._wait_target("X", X_END)
            return True
        if self._auto_leg == "yback":
            # Y 已回到起始位置 -> 开始新一组内循环
            self._auto_remaining = self._inner_total
            self._update_cycle_info()
            self._append_log(f"[自动] 开始新一组内循环（大循环剩余 {self._outer_remaining}）\n")
            self._auto_leg = "end"
            self.sender.send_command(f"X={fmt_pos(X_END)}")
            self._wait_target("X", X_END)
            return True
        # leg == "home": 回到起始 -> 完成一次内循环
        self._auto_remaining -= 1
        self._update_cycle_info()
        self._append_log(f"[自动] 完成一次内循环，本组剩余 {self._auto_remaining}\n")
        if self._auto_remaining > 0:
            if self._auto_ystep == 0:
                # Y 步进为 0：不移动 Y，直接开始下一次内循环
                self._auto_leg = "end"
                self.sender.send_command(f"X={fmt_pos(X_END)}")
                self._wait_target("X", X_END)
                return True
            # 下一次内循环之前，Y 轴按配置方向步进
            y_target = self.positions["Y"] + Y_STEP_DIR * self._auto_ystep
            if not (AXIS_LIMITS["Y"][0] <= y_target <= AXIS_LIMITS["Y"][1]):
                self._auto_abort(
                    f"Y 步进后目标 {fmt_pos(y_target)} 超出行程 "
                    f"[{fmt_pos(AXIS_LIMITS['Y'][0])}, {fmt_pos(AXIS_LIMITS['Y'][1])}]")
                return False
            self._auto_leg = "ystep"
            sign = "+" if Y_STEP_DIR > 0 else "-"
            self._append_log(f"[自动] Y 步进 {sign}{fmt_pos(self._auto_ystep)} -> {fmt_pos(y_target)}\n")
            self.sender.send_command(
                f"Y{sign}{fmt_pos(self._auto_ystep)}")
            self._wait_target("Y", y_target)
            return True
        # 本组内循环全部完成
        if self._outer_remaining > 1:
            # 还有大循环: Y 回到起始位置，再重复整组
            self._outer_remaining -= 1
            self._update_cycle_info()
            if not (AXIS_LIMITS["Y"][0] <= self._y_start <= AXIS_LIMITS["Y"][1]):
                self._auto_abort(f"Y 起始位置 {fmt_pos(self._y_start)} 超出行程")
                return False
            self._auto_leg = "yback"
            self._append_log(
                f"[自动] 本组完成，Y 回到起始位置 {fmt_pos(self._y_start)}"
                f"（大循环剩余 {self._outer_remaining}）\n")
            self.sender.send_command(f"Y={fmt_pos(self._y_start)}")
            self._wait_target("Y", self._y_start)
            return True
        # 全部完成
        self._auto_active = False
        self._auto_mode = None
        self._outer_remaining = 0
        self.cycle_info_var.set("")
        self.eta_var.set("")
        self._append_log("[自动] 全部循环完成\n")
        self._play_finish_sound()
        return False

    def _play_finish_sound(self):
        """循环完成提示音（Windows MCI 播放 MP3，异步不阻塞）"""
        try:
            winmm = ctypes.windll.winmm
            alias = "cycle_finish"
            winmm.mciSendStringW(f"close {alias}", None, 0, None)
            if winmm.mciSendStringW(f'open "{FINISH_SOUND}" alias {alias}', None, 0, None) == 0:
                winmm.mciSendStringW(f"play {alias}", None, 0, None)
            else:
                self._append_log(f"[提示] 音效文件无法播放: {FINISH_SOUND}\n")
        except Exception as e:
            self._append_log(f"[提示] 音效播放失败: {e}\n")

    def _refresh_position(self, axis: str):
        self.pos_labels[axis].config(text=fmt_pos(self.positions[axis]))

    # ---------- UV 灯 / 滚子 ----------

    def _toggle_uv(self):
        self.sender.send_command(CMD_UV_LAMP)
        self.uv_on = not self.uv_on
        self.uv_btn.config(text=f"UV 灯: {'开' if self.uv_on else '关'}")

    def _set_uv(self, on: bool):
        """按目标状态开关 UV 灯（状态已一致时不发指令）"""
        if self.uv_on == on:
            return
        self.sender.send_command(CMD_UV_LAMP)
        self.uv_on = on
        self.uv_btn.config(text=f"UV 灯: {'开' if on else '关'}")

    def _auto_uv_update(self):
        """自动UV灯: 非"向终点移动"的段一律关灯；向终点段的开灯由位置触发"""
        if not self.auto_uv_var.get():
            return
        if not (self._auto_active and self._auto_leg == "end"):
            self._set_uv(False)

    def _auto_uv_position_check(self, x: float):
        """向终点移动过程中，X 到达 UV 灯水平距离时开灯"""
        if (self.auto_uv_var.get() and self._auto_active
                and self._auto_leg == "end" and not self.uv_on
                and x >= self._uv_dist):
            self._set_uv(True)

    def _auto_uv_toggled(self):
        """勾选框状态改变: 取消勾选立即关灯，勾选则按当前循环段应用"""
        if self.auto_uv_var.get():
            self._auto_uv_update()
        else:
            self._set_uv(False)

    def _toggle_roller(self):
        self.sender.send_command(CMD_ROLLER)
        self.roller_on = not self.roller_on
        self.roller_btn.config(text=f"滚子: {'运行' if self.roller_on else '停止'}")

    def _emergency_stop(self):
        """强制停止打印：发送 q，清空服务器任务状态并解锁所有轴。"""
        self.sender.send_urgent(CMD_ESTOP)
        self._auto_active = False
        self._auto_mode = None
        self._auto_leg = "stopped"
        self._outer_remaining = 0
        self._layer_listening = False
        self._active_layer = 0
        self._job_current_layer = 0
        self._job_total_layers = 0
        self._pass_current = 0
        self._pass_total = 0
        self._pending.clear()
        self._cancel_flash_pause()
        self.event_queue.clear()
        self.event_list.delete(0, "end")
        self.event_count_var.set("0 条")
        self._update_job_progress()
        self._update_pass_progress()
        self.cycle_info_var.set("")
        self.eta_var.set("")
        self._y_start = 0.0
        self._unlock_all()
        if self.auto_uv_var.get():
            self._set_uv(False)
        self._append_log("[停止] 已发送 q，强制终止打印并清空任务事件\n")

    # ---------- 回调与日志 ----------

    def _on_sent(self, cmd: str):
        self.after(0, self._append_log, f"[发送] {cmd}\n")

    def _on_status(self, frame: str):
        """收到设备状态帧: POS:X=+014.88,Y=+000.00,Z=+000.00,D=0,G=0"""
        m = STATUS_PATTERN.fullmatch(frame)
        if not m:
            return
        x, y, z = float(m.group(1)), float(m.group(2)), float(m.group(3))
        uv, roller = int(m.group(4)) != 0, int(m.group(5)) != 0

        def update():
            self.positions.update({"X": x, "Y": y, "Z": z})
            for axis in self.positions:
                self._refresh_position(axis)
            self.uv_on = uv
            self.roller_on = roller
            self.uv_btn.config(text=f"UV 灯: {'开' if uv else '关'}")
            self.roller_btn.config(text=f"滚子: {'运行' if roller else '停止'}")
            self.last_report_var.set(
                f"最后上报: {time.strftime('%H:%M:%S')}  {frame}")
            self._check_arrival()
            self._auto_uv_position_check(x)
        self.after(0, update)

    def _on_state_change(self, connected: bool, msg: str):
        def update():
            self.status_var.set(msg)
            self.connect_btn.config(text="断开" if connected else "连接")
            self._append_log(f"[状态] {msg}\n")
            if not connected:
                # 断开后清空等待目标并解锁，避免卡死
                self._pending.clear()
                self._auto_active = False
                self._auto_mode = None
                self._outer_remaining = 0
                self._cancel_flash_pause()
                self.cycle_info_var.set("")
                self.eta_var.set("")
                self._unlock_all()
        self.after(0, update)

    def _append_log(self, text: str):
        self.log_text.config(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _on_close(self):
        self.sender.disconnect()
        self.scan.disconnect()
        self.destroy()


if __name__ == "__main__":
    app = MainWindow()
    app.mainloop()
