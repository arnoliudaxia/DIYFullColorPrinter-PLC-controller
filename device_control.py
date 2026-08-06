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
  自动压墨：勾选后在自动循环闪喷前执行 压墨Z下降→PRESS_INK→循环播放warning.mp3并弹窗
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

# ---------- 配置文件 ----------

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml")

_DEFAULT_AXIS_LIMITS = {
    "X": (-10.0, 230.0),
    "Y": (-5.0, 120.0),
    "Z": (-60.0, 125.0),
}
_DEFAULT_X_HOME = 0.0
_DEFAULT_X_END = 220.0


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
    home, end = _DEFAULT_X_HOME, _DEFAULT_X_END
    try:
        home = float(cfg["auto_cycle"]["x_home"])
        end = float(cfg["auto_cycle"]["x_end"])
    except (KeyError, TypeError, ValueError):
        pass
    return home, end


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


def _load_flash_pause(cfg: dict) -> int:
    ms = _DEFAULT_FLASH_PAUSE_MS
    try:
        ms = int(float(cfg["flash"]["pause_ms"]))
        if ms < 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        pass
    return ms


_DEFAULT_PRESS_INK = (2.5, 80.0)  # (压墨秒数, 刮墨时 Z 下降/回升量)


def _load_press_ink(cfg: dict):
    seconds, z_drop = _DEFAULT_PRESS_INK
    try:
        seconds = float(cfg["press_ink"]["seconds"])
        z_drop = float(cfg["press_ink"]["z_drop"])
        if not (0 < seconds <= 3600) or z_drop <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        pass
    return seconds, z_drop


_DEFAULT_SPEEDS = (20.0, 10.0, 1.0)


def _load_speeds(cfg: dict):
    vx, vy, vz = _DEFAULT_SPEEDS
    try:
        vx = float(cfg["speeds"]["x"])
        vy = float(cfg["speeds"]["y"])
        vz = float(cfg["speeds"]["z"])
        if min(vx, vy, vz) <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        pass
    return vx, vy, vz


_DEFAULT_UI_SCALE = 1.6
_DEFAULT_WIN_SIZE = (400, 600)


def _load_ui(cfg: dict):
    scale, win = _DEFAULT_UI_SCALE, _DEFAULT_WIN_SIZE
    try:
        scale = float(cfg["ui"]["scale"])
        size = cfg["ui"]["window_size"]
        win = (int(float(size[0])), int(float(size[1])))
        if scale <= 0 or min(win) <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        pass
    return scale, win


_CONFIG = _load_config()
AXIS_LIMITS = _load_axis_limits(_CONFIG)
X_HOME, X_END = _load_cycle_endpoints(_CONFIG)
JOG_STEPS = _load_jog_steps(_CONFIG)
X_CYCLE_COUNT_LIMIT, X_CYCLE_Z_STEP = _load_z_step(_CONFIG)
FLASH_PAUSE_MS = _load_flash_pause(_CONFIG)
PRESS_INK_SECONDS, PRESS_INK_Z_DROP = _load_press_ink(_CONFIG)
VX_MMS, VY_MMS, VZ_MMS = _load_speeds(_CONFIG)
UI_SCALE, WIN_SIZE = _load_ui(_CONFIG)

# 点动按钮文字: {轴: (负方向, 正方向)}，保留正负号
JOG_LABELS = {
    "X": ("-右", "+左"),
    "Y": ("-PASS", "+前"),
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
        self._auto_remaining = 0   # 本组内剩余循环次数
        self._inner_total = 0      # 每组内循环次数
        self._outer_remaining = 0  # 大循环剩余次数
        self._y_start = 0.0        # 内循环开始时的 Y 位置（大循环间要回到这里）
        self._auto_leg = "end"     # 当前段: "end"/"home"/"ystep"/"yback"
        self._auto_ystep = 10.0    # 循环间 Y 轴负向步进量
        self._uv_dist = 120.0      # UV 灯开灯的 X 位置（水平距离）
        # 打印中维护闪喷：X 到达终点累计计数（不管 Y step / 大循环，一直累加）
        self._flash_count = 0      # X 到达终点累计次数
        self._flash_interval = DEFAULT_FLASH_INTERVAL
        self._flash_pausing = False   # 是否处于闪喷暂停中
        self._flash_after = None      # 闪喷恢复定时器 id
        self._flash_wait_off = False  # 是否正在等待结束闪喷回送
        self._flash_wait_after = None # 结束闪喷回送超时定时器 id
        self._x_cycle_count = 0       # X 到达终点累计计数（达阈值清零）
        self._auto_eta = 0.0          # 本次自动循环预计完成时间 (s)
        self.eta_var = tk.StringVar(value="")  # 预计完成时间显示

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

        self._build_connection_frame()
        self._build_scan_frame()
        self._build_axis_frame()
        self._build_device_frame()
        self._build_log_frame()
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

    def _build_connection_frame(self):
        frame = ttk.LabelFrame(self, text="连接设置")
        frame.pack(fill="x", padx=8, pady=6)

        ttk.Label(frame, text="IP 地址:").grid(row=0, column=0, padx=4, pady=6)
        self.ip_var = tk.StringVar(value=DEFAULT_HOST)
        ttk.Entry(frame, textvariable=self.ip_var, width=15).grid(row=0, column=1, padx=4)

        ttk.Label(frame, text="端口:").grid(row=0, column=2, padx=4)
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        ttk.Entry(frame, textvariable=self.port_var, width=7).grid(row=0, column=3, padx=4)

        self.connect_btn = ttk.Button(frame, text="连接", command=self._toggle_connect)
        self.connect_btn.grid(row=0, column=4, padx=8)

    def _build_scan_frame(self):
        frame = ttk.LabelFrame(self, text="打印服务进程")
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

    def _build_axis_frame(self):
        frame = ttk.LabelFrame(self, text="轴控制（位置来自设备周期性上报）")
        frame.pack(fill="x", padx=8, pady=4)

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
        for text, target in [("打印高度 (125)", 125), ("调试高度 (-10)", -10)]:
            b = ttk.Button(quick_z, text=text, width=14,
                           command=lambda t=target: self._move_to("Z", t))
            b.pack(side="left", padx=4)
            self.axis_widgets["Z"].append(b)

        # 自动循环：X 在起始/终点之间往返，循环之间 Y 轴向 - 方向步进
        auto_row = ttk.Frame(frame)
        auto_row.grid(row=7, column=0, columnspan=7, sticky="w", pady=(12, 2))
        ttk.Label(auto_row, text="自动打印循环:").pack(side="left", padx=4)
        ttk.Label(auto_row, text="PASS数:").pack(side="left")
        self.cycle_var = tk.StringVar(value="1")
        self.cycle_entry = ttk.Entry(auto_row, textvariable=self.cycle_var, width=5)
        self.cycle_entry.pack(side="left", padx=2)
        ttk.Label(auto_row, text="Y 步进:").pack(side="left", padx=(8, 0))
        self.ystep_var = tk.StringVar(value="10")
        self.ystep_entry = ttk.Entry(auto_row, textvariable=self.ystep_var, width=5)
        self.ystep_entry.pack(side="left", padx=2)
        self.auto_widgets += [self.cycle_entry, self.ystep_entry]
        self.cycle_info_var = tk.StringVar(value="")

        # 大循环：整组 X-Y 循环重复的次数，组间 Y 回到起始位置
        outer_row = ttk.Frame(frame)
        outer_row.grid(row=8, column=0, columnspan=7, sticky="w", pady=2)
        ttk.Label(outer_row, text="大循环次数:").pack(side="left", padx=4)
        self.outer_var = tk.StringVar(value="1")
        self.outer_entry = ttk.Entry(outer_row, textvariable=self.outer_var, width=5)
        self.outer_entry.pack(side="left", padx=2)
        ttk.Label(outer_row, text="(每组结束后 Y 回到起始位置再重复)",
                  foreground="gray").pack(side="left", padx=8)
        self.auto_widgets.append(self.outer_entry)

        # 开始/停止按钮独立一行（大循环次数下方）
        btn_row = ttk.Frame(frame)
        btn_row.grid(row=9, column=0, columnspan=7, sticky="w", pady=(6, 2))
        start_btn = tk.Button(btn_row, text="开始", width=8, bg="#2e7d32", fg="white",
                              font=("", 10, "bold"), activebackground="#388e3c",
                              command=self._start_auto)
        start_btn.pack(side="left", padx=4)
        self.auto_widgets.append(start_btn)
        # 停止按钮始终可用（手动急停出口）
        tk.Button(btn_row, text="停止", width=8, bg="#c62828", fg="white",
                  font=("", 10, "bold"), activebackground="#d32f2f",
                  command=self._stop_auto).pack(side="left", padx=4)

        # 预计完成时间显示
        ttk.Label(frame, textvariable=self.eta_var,
                  foreground="#2e7d32").grid(row=10, column=0, columnspan=7, pady=2)

        # 循环状态显示（独立一行，避免超出窗口）
        ttk.Label(frame, textvariable=self.cycle_info_var,
                  foreground="gray").grid(row=11, column=0, columnspan=7)

        self.last_report_var = tk.StringVar(value="等待设备上报...")
        ttk.Label(frame, textvariable=self.last_report_var,
                  foreground="gray").grid(row=12, column=0, columnspan=7, pady=6)

    def _build_device_frame(self):
        frame = ttk.LabelFrame(self, text="设备控制")
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
        self.flash_btn = ttk.Button(row1b, text="闪喷: 关", width=16, command=self._send_flash)
        self.flash_btn.pack(side="left", padx=16, pady=8)
        self.press_ink_btn = ttk.Button(row1b, text="压墨", width=16,
                                        command=self._send_press_ink)
        self.press_ink_btn.pack(side="left", padx=16, pady=8)

        row2 = ttk.Frame(frame)
        row2.pack(fill="x")
        ttk.Label(row2, text="UV灯水平距离 (mm):").pack(side="left", padx=(16, 2), pady=4)
        self.uv_dist_var = tk.StringVar(value="120")
        ttk.Entry(row2, textvariable=self.uv_dist_var, width=8).pack(side="left", padx=2)
        # 自动UV灯: 自动循环中 X 向终点移动时开灯，其他情况关灯
        self.auto_uv_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="自动UV灯", variable=self.auto_uv_var,
                        command=self._auto_uv_toggled).pack(side="left", padx=16)

        # 打印中维护闪喷：自动循环中 X 到达终点累计计数，达间隔时暂停闪喷
        row3 = ttk.Frame(frame)
        row3.pack(fill="x")
        ttk.Label(row3, text="闪喷间隔:").pack(side="left", padx=(16, 2), pady=4)
        self.flash_interval_var = tk.StringVar(value=str(DEFAULT_FLASH_INTERVAL))
        ttk.Entry(row3, textvariable=self.flash_interval_var, width=8).pack(side="left", padx=2)
        self.flash_maintain_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row3, text="打印中维护闪喷", variable=self.flash_maintain_var
                        ).pack(side="left", padx=(16, 2))

        # 自动压墨：勾选后在自动循环的闪喷前执行压墨
        row4 = ttk.Frame(frame)
        row4.pack(fill="x")
        self.press_ink_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row4, text="打印中自动压墨", variable=self.press_ink_var
                        ).pack(side="left", padx=(16, 2), pady=4)
        ttk.Label(row4, text=f"(时长 {fmt_pos(PRESS_INK_SECONDS)}s)",
                  foreground="gray").pack(side="left", padx=2)

    def _build_log_frame(self):
        from tkinter import scrolledtext
        frame = ttk.LabelFrame(self, text="指令日志")
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

    def _send_press_ink(self):
        """手动发送压墨命令 PRESS_INK <秒数>\r\n 到扫描服务器"""
        if not self.scan.connected:
            messagebox.showwarning("提示", "扫描服务器未连接")
            return
        if self.scan.send(f"PRESS_INK {fmt_pos(PRESS_INK_SECONDS)}\r\n"):
            self._append_log(f"[压墨] 发送: PRESS_INK {fmt_pos(PRESS_INK_SECONDS)}\r\n")

    def _on_scan_response(self, line: str):
        def update():
            self._append_log(f"[扫描回复] {line}\n")
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
        """自动循环期间锁定 X、Y 及循环参数控件（Z / UV / 滚子不受影响）"""
        self._set_axis_enabled("X", False)
        self._set_axis_enabled("Y", False)
        self._set_auto_widgets(False)
        self.status_var.set("自动循环运行中... X/Y 轴已锁定")

    def _unlock_auto_group(self):
        self._set_axis_enabled("X", True)
        self._set_axis_enabled("Y", True)
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
            # 自动循环只锁定 X/Y，其他轴（如 Z）到达后正常解锁
            for a in arrived:
                if a not in self._pending and a not in ("X", "Y"):
                    self._set_axis_enabled(a, True)
            if not self._pending:
                self._auto_advance()
                self._auto_uv_update()
                if not self._auto_active:
                    # 循环刚结束/中止
                    self._unlock_auto_group()
                    self.status_var.set(
                        f"已连接 {self.ip_var.get()}:{self.port_var.get()}（自动循环结束）")
            return  # 自动循环期间 X/Y 保持锁定
        for a in arrived:
            self._set_axis_enabled(a, True)
        if not self._pending:
            self.status_var.set(f"已连接 {self.ip_var.get()}:{self.port_var.get()}（已到达目标位置）")

    # ---------- X 轴自动循环 ----------

    def _update_cycle_info(self):
        self.cycle_info_var.set(
            f"大循环剩余: {self._outer_remaining}，内循环剩余: {self._auto_remaining}")

    def _estimate_auto_time(self, count: int, outer: int, ystep: float) -> float:
        """按各轴速度估算整次自动循环完成时间（秒）"""
        total_x = count * outer                       # X 到达终点总次数
        x_dist = abs(X_END - X_HOME) * 2 * total_x    # 每内循环两次 X 行程
        x_dist += abs(self.positions["X"] - X_HOME)   # 回起始位置的预移动
        # Y: 每组内 (count-1) 次步进，组间回到起始位置
        y_dist = (2 * outer - 1) * (count - 1) * ystep if count > 1 else 0.0
        # Z: 每 200 次 X 循环下降 0.5
        z_time = (total_x // X_CYCLE_COUNT_LIMIT) * X_CYCLE_Z_STEP / VZ_MMS
        t = x_dist / VX_MMS + y_dist / VY_MMS + z_time
        if self.flash_maintain_var.get():
            flashes = total_x // self._flash_interval
            t += flashes * (FLASH_PAUSE_MS / 1000.0)
            if self.press_ink_var.get():
                t += flashes * PRESS_INK_SECONDS
        return t

    def _start_auto(self):
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
            if ystep == 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("错误", "Y 步进必须是正数")
            return
        if "X" in self._pending or "Y" in self._pending:
            messagebox.showwarning("提示", "X 或 Y 轴正在运动中，请等待到达后再开始自动循环")
            return
        try:
            uv_dist = float(self.uv_dist_var.get().strip())
            if not (0 <= uv_dist <= X_END):
                raise ValueError
        except ValueError:
            if self.auto_uv_var.get():
                messagebox.showerror("错误", f"UV灯水平距离必须是 0 ~ {fmt_pos(X_END)} 之间的数字")
                return
            uv_dist = X_END  # 未启用自动UV灯时该值无意义
        self._uv_dist = uv_dist
        if self.flash_maintain_var.get():
            try:
                flash_int = int(self.flash_interval_var.get().strip())
                if flash_int < 1:
                    raise ValueError
            except ValueError:
                messagebox.showerror("错误", "闪喷间隔必须是正整数")
                return
        else:
            flash_int = DEFAULT_FLASH_INTERVAL
        self._flash_interval = flash_int
        self._flash_count = 0
        self._x_cycle_count = 0
        self._cancel_flash_pause()
        self._auto_ystep = ystep
        self._inner_total = count
        self._auto_remaining = count
        self._outer_remaining = outer
        self._y_start = self.positions["Y"]
        self._auto_active = True
        self._auto_eta = self._estimate_auto_time(count, outer, ystep)
        self.eta_var.set(f"预计完成时间: {fmt_duration(self._auto_eta)}")
        self._update_cycle_info()
        self._append_log(
            f"[自动] 开始: 大循环 {outer} 组 x 内循环 {count} 次"
            f"（X: {fmt_pos(X_HOME)} <-> {fmt_pos(X_END)}，循环间 Y -{fmt_pos(ystep)}，"
            f"Y 起始位置 {fmt_pos(self._y_start)}）\n")
        self._append_log(f"[自动] 预计完成时间: {fmt_duration(self._auto_eta)}\n")
        self._lock_auto_group()
        # 先回起始位置，避免当前 X 位置不确定
        self._auto_leg = "prehome"
        self._append_log(f"[自动] 先回起始位置 {fmt_pos(X_HOME)}\n")
        self.sender.send_command(f"X={fmt_pos(X_HOME)}")
        self._wait_target("X", X_HOME)
        self._auto_uv_update()

    def _stop_auto(self):
        if not self._auto_active:
            return
        self._auto_active = False
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
        """X 到达终点时暂停循环：先压墨（若启用），再闪喷"""
        self._flash_pausing = True
        self._append_log(
            f"[自动] X 到达终点，维护闪喷（间隔 {self._flash_interval}），暂停\n")
        if self.press_ink_var.get():
            # 1. Z 下降
            self._z_drop_for_ink()
            # 2. 发送压墨命令
            self._append_log(
                f"[自动] 发送压墨命令 PRESS_INK {fmt_pos(PRESS_INK_SECONDS)}\n")
            self.scan.send(f"PRESS_INK {fmt_pos(PRESS_INK_SECONDS)}\r\n")
            # 3. 循环警告音 + 弹窗等待手动刮墨
            self._play_ink_warning_loop()
            self.status_var.set("请手动刮墨...")
            messagebox.showinfo("请手动刮墨", "请手动刮墨，完成后点击确定")
            # 4. 关闭警告音，Z 回升，继续闪喷
            self._stop_ink_warning()
            self._z_rise_for_ink()
        self._do_flash_pause()

    def _z_drop_for_ink(self):
        """刮墨前 Z 轴下降（增量指令，带行程校验）"""
        target = self.positions["Z"] - PRESS_INK_Z_DROP
        if not (AXIS_LIMITS["Z"][0] <= target <= AXIS_LIMITS["Z"][1]):
            self._append_log(
                f"[自动] 压墨 Z 下降超出行程，跳过: 目标 {fmt_pos(target)}\n")
            return
        self.sender.send_command(f"Z-{fmt_pos(PRESS_INK_Z_DROP)}")
        self._append_log(
            f"[自动] 压墨 Z 下降 {fmt_pos(PRESS_INK_Z_DROP)} -> {fmt_pos(target)}\n")

    def _z_rise_for_ink(self):
        """刮墨完成后 Z 轴回升（增量指令，带行程校验）"""
        target = self.positions["Z"] + PRESS_INK_Z_DROP
        if not (AXIS_LIMITS["Z"][0] <= target <= AXIS_LIMITS["Z"][1]):
            self._append_log(
                f"[自动] 压墨 Z 回升超出行程，跳过: 目标 {fmt_pos(target)}\n")
            return
        self.sender.send_command(f"Z+{fmt_pos(PRESS_INK_Z_DROP)}")
        self._append_log(
            f"[自动] 压墨 Z 回升 {fmt_pos(PRESS_INK_Z_DROP)} -> {fmt_pos(target)}\n")

    def _play_ink_warning_loop(self):
        """循环播放 assets/warning.mp3（MCI，不阻塞）"""
        try:
            winmm = ctypes.windll.winmm
            alias = "ink_warning"
            winmm.mciSendStringW(f"stop {alias}", None, 0, None)
            winmm.mciSendStringW(f"close {alias}", None, 0, None)
            if winmm.mciSendStringW(f'open "{INK_WARNING_SOUND}" alias {alias}',
                                    None, 0, None) == 0:
                winmm.mciSendStringW(f"play {alias} repeat", None, 0, None)
            else:
                self._append_log(f"[提示] 警告音效无法播放: {INK_WARNING_SOUND}\n")
        except Exception as e:
            self._append_log(f"[提示] 警告音效播放失败: {e}\n")

    def _stop_ink_warning(self):
        """停止并释放警告音"""
        try:
            winmm = ctypes.windll.winmm
            alias = "ink_warning"
            winmm.mciSendStringW(f"stop {alias}", None, 0, None)
            winmm.mciSendStringW(f"close {alias}", None, 0, None)
        except Exception:
            pass

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
        """结束闪喷确认后，继续返回起始位置"""
        if not self._auto_active:
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
        for attr in ("_flash_after", "_flash_wait_after"):
            timer = getattr(self, attr, None)
            if timer is not None:
                self.after_cancel(timer)
                setattr(self, attr, None)
        self._flash_pausing = False
        self._flash_wait_off = False
        self._stop_ink_warning()

    def _z_step_down(self):
        """X 循环累计计数达阈值时，Z 轴下降 0.5（不阻塞自动循环）"""
        target = self.positions["Z"] - X_CYCLE_Z_STEP
        if not (AXIS_LIMITS["Z"][0] <= target <= AXIS_LIMITS["Z"][1]):
            self._append_log(
                f"[自动] Z 轴下降超出行程，跳过: 目标 {fmt_pos(target)}\n")
            return
        self.sender.send_command(f"Z-{fmt_pos(X_CYCLE_Z_STEP)}")
        self._append_log(
            f"[自动] X 循环计数达 {X_CYCLE_COUNT_LIMIT}，"
            f"Z 轴下降 {fmt_pos(X_CYCLE_Z_STEP)} -> {fmt_pos(target)}\n")

    def _auto_advance(self) -> bool:
        """一段到达后推进自动循环，返回 True 表示已发出下一段"""
        if self._auto_leg == "prehome":
            # 已回到起始位置 -> 开始第一次向终点运动
            self._auto_leg = "end"
            self.sender.send_command(f"X={fmt_pos(X_END)}")
            self._wait_target("X", X_END)
            return True
        if self._auto_leg == "end":
            # 到达终点 -> 计数；启用维护闪喷且达间隔时，先暂停闪喷再返回起始
            self._flash_count += 1
            # X 到达终点累计计数达阈值时，Z 轴下降（独立于闪喷计数）
            self._x_cycle_count += 1
            if self._x_cycle_count >= X_CYCLE_COUNT_LIMIT:
                self._x_cycle_count = 0
                self._z_step_down()
            if self.flash_maintain_var.get() and self._flash_count >= self._flash_interval:
                self._flash_count = 0
                self._flash_pause()
                return True
            self._auto_leg = "home"
            self.sender.send_command(f"X={fmt_pos(X_HOME)}")
            self._wait_target("X", X_HOME)
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
            # 下一次内循环之前，Y 轴向 - 方向步进
            y_target = self.positions["Y"] - self._auto_ystep
            if not (AXIS_LIMITS["Y"][0] <= y_target <= AXIS_LIMITS["Y"][1]):
                self._auto_abort(
                    f"Y 步进后目标 {fmt_pos(y_target)} 超出行程 "
                    f"[{fmt_pos(AXIS_LIMITS['Y'][0])}, {fmt_pos(AXIS_LIMITS['Y'][1])}]")
                return False
            self._auto_leg = "ystep"
            self._append_log(f"[自动] Y 步进 -{fmt_pos(self._auto_ystep)} -> {fmt_pos(y_target)}\n")
            self.sender.send_command(f"Y-{fmt_pos(self._auto_ystep)}")
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
        """急停：立即发送 q，打断所有运动，终止自动循环并全部解锁"""
        self.sender.send_urgent(CMD_ESTOP)
        self._auto_active = False
        self._outer_remaining = 0
        self._pending.clear()
        self._cancel_flash_pause()
        self.cycle_info_var.set("")
        self.eta_var.set("")
        self._unlock_all()
        if self.auto_uv_var.get():
            self._set_uv(False)
        self._append_log("[急停] 已发送 q，打断所有指令\n")

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
            self._append_log(f"[上报] {frame}\n")
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
