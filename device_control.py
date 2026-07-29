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
"""

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

AXIS_LIMITS = {
    "X": (-10.0, 230.0),
    "Y": (-5.0, 120.0),
    "Z": (-60.0, 125.0),
}

# 点动按钮文字: {轴: (负方向, 正方向)}，保留正负号
JOG_LABELS = {
    "X": ("-右", "+左"),
    "Y": ("-PASS", "+前"),
    "Z": ("-下", "+上"),
}

CMD_UV_LAMP = "D"
CMD_ROLLER = "G"
CMD_ESTOP = "q"  # 急停：打断所有运动指令

# X 轴快捷/自动循环的两个端点
X_HOME = 0.0   # 起始位置
X_END = 100.0  # 终点位置

# 状态帧: POS:X=+014.88,Y=+000.00,Z=+000.00,D=0,G=0
STATUS_PATTERN = re.compile(
    r"POS:X=([+-]?\d+(?:\.\d+)?),Y=([+-]?\d+(?:\.\d+)?),Z=([+-]?\d+(?:\.\d+)?)"
    r",D=(\d+),G=(\d+)")


def fmt_pos(value: float) -> str:
    """位置数值格式化：整数不带小数点"""
    return str(int(value)) if value == int(value) else str(value)


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


# ---------- 主窗口 ----------

class MainWindow(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("设备远程控制")
        self.geometry("560x600")

        # 位置/开关状态以设备周期性上报的状态帧为准
        self.positions = {axis: 0.0 for axis in AXIS_LIMITS}
        self.uv_on = False
        self.roller_on = False

        # 运动中的等待目标 {轴: 目标位置}，非空时锁定所有发指令控件
        self._pending = {}
        # 所有会发送指令的控件，统一 enable/disable
        self.cmd_widgets = []
        # X 轴自动循环状态
        self._auto_active = False
        self._auto_remaining = 0   # 本组内剩余循环次数
        self._inner_total = 0      # 每组内循环次数
        self._outer_remaining = 0  # 大循环剩余次数
        self._y_start = 0.0        # 内循环开始时的 Y 位置（大循环间要回到这里）
        self._auto_leg = "end"     # 当前段: "end"/"home"/"ystep"/"yback"
        self._auto_ystep = 10.0    # 循环间 Y 轴负向步进量

        self.sender = TcpSender(
            on_sent=self._on_sent,
            on_state_change=self._on_state_change,
            on_receive=self._on_status,
        )

        self._build_connection_frame()
        self._build_axis_frame()
        self._build_device_frame()
        self._build_log_frame()
        self._build_status_bar()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # 启动后自动连接设备
        self.after(300, self._auto_connect)

    def _auto_connect(self):
        if not self.sender.connected:
            self._toggle_connect()

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

    def _build_axis_frame(self):
        frame = ttk.LabelFrame(self, text="轴控制（位置来自设备周期性上报）")
        frame.pack(fill="x", padx=8, pady=4)

        # 共享点动步长：不可手动输入，通过 1/5/10 单选按钮切换，选中按钮呈按下状态
        step_bar = ttk.Frame(frame)
        step_bar.grid(row=0, column=0, columnspan=7, sticky="w", pady=2)
        ttk.Label(step_bar, text="点动步长:").pack(side="left", padx=4)
        self.step_var = tk.StringVar(value="1")
        for v in (1, 5, 10):
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
            self.cmd_widgets.append(move_btn)

            jog_minus = ttk.Button(frame, text=JOG_LABELS[axis][0], width=6,
                                   command=lambda a=axis: self._jog(a, -1))
            jog_minus.grid(row=row, column=4, padx=2)
            jog_plus = ttk.Button(frame, text=JOG_LABELS[axis][1], width=6,
                                  command=lambda a=axis: self._jog(a, 1))
            jog_plus.grid(row=row, column=5, padx=2)
            self.cmd_widgets += [jog_minus, jog_plus]

            lo, hi = AXIS_LIMITS[axis]
            ttk.Label(frame, text=f"[{fmt_pos(lo)}, {fmt_pos(hi)}]",
                      foreground="gray").grid(row=row, column=6, padx=6)

        # 快捷位置按钮（X / Z 各一行）
        quick_x = ttk.Frame(frame)
        quick_x.grid(row=5, column=0, columnspan=7, sticky="w", pady=2)
        ttk.Label(quick_x, text="X 轴快捷:").pack(side="left", padx=4)
        for text, target in [("零点位置 (1)", 1), ("起始位置 (0)", X_HOME), ("终点位置 (100)", X_END)]:
            b = ttk.Button(quick_x, text=text, width=14,
                           command=lambda t=target: self._move_to("X", t))
            b.pack(side="left", padx=4)
            self.cmd_widgets.append(b)

        quick_z = ttk.Frame(frame)
        quick_z.grid(row=6, column=0, columnspan=7, sticky="w", pady=2)
        ttk.Label(quick_z, text="Z 轴预设:").pack(side="left", padx=4)
        for text, target in [("打印高度 (125)", 125), ("调试高度 (-10)", -10)]:
            b = ttk.Button(quick_z, text=text, width=14,
                           command=lambda t=target: self._move_to("Z", t))
            b.pack(side="left", padx=4)
            self.cmd_widgets.append(b)

        # 自动循环：X 在起始/终点之间往返，循环之间 Y 轴向 - 方向步进
        auto_row = ttk.Frame(frame)
        auto_row.grid(row=7, column=0, columnspan=7, sticky="w", pady=2)
        ttk.Label(auto_row, text="自动X轴循环:").pack(side="left", padx=4)
        ttk.Label(auto_row, text="次数:").pack(side="left")
        self.cycle_var = tk.StringVar(value="1")
        self.cycle_entry = ttk.Entry(auto_row, textvariable=self.cycle_var, width=5)
        self.cycle_entry.pack(side="left", padx=2)
        ttk.Label(auto_row, text="Y 步进:").pack(side="left", padx=(8, 0))
        self.ystep_var = tk.StringVar(value="10")
        self.ystep_entry = ttk.Entry(auto_row, textvariable=self.ystep_var, width=5)
        self.ystep_entry.pack(side="left", padx=2)
        start_btn = ttk.Button(auto_row, text="开始", width=8, command=self._start_auto)
        start_btn.pack(side="left", padx=4)
        self.cmd_widgets += [start_btn, self.cycle_entry, self.ystep_entry]
        # 停止按钮始终可用（手动急停出口）
        ttk.Button(auto_row, text="停止", width=8, command=self._stop_auto).pack(side="left", padx=4)
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
        self.cmd_widgets.append(self.outer_entry)

        # 循环状态显示（独立一行，避免超出窗口）
        ttk.Label(frame, textvariable=self.cycle_info_var,
                  foreground="gray").grid(row=9, column=0, columnspan=7)

        self.last_report_var = tk.StringVar(value="等待设备上报...")
        ttk.Label(frame, textvariable=self.last_report_var,
                  foreground="gray").grid(row=10, column=0, columnspan=7, pady=6)

    def _build_device_frame(self):
        frame = ttk.LabelFrame(self, text="设备控制")
        frame.pack(fill="x", padx=8, pady=4)

        self.uv_btn = ttk.Button(frame, text="UV 灯: 关", width=16, command=self._toggle_uv)
        self.uv_btn.pack(side="left", padx=16, pady=8)

        self.roller_btn = ttk.Button(frame, text="滚子: 停止", width=16, command=self._toggle_roller)
        self.roller_btn.pack(side="left", padx=16, pady=8)

        # 急停按钮始终可用（用 tk.Button 以便着色）
        self.estop_btn = tk.Button(frame, text="急停", width=10,
                                   bg="#d32f2f", fg="white",
                                   font=("", 11, "bold"),
                                   command=self._emergency_stop)
        self.estop_btn.pack(side="left", padx=16, pady=8)

        self.cmd_widgets += [self.uv_btn, self.roller_btn]

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

    # ---------- 运动锁定 ----------

    def _wait_target(self, axis: str, target: float):
        """登记运动目标并锁定所有发指令控件，直到设备上报到达"""
        self._pending[axis] = target
        self._set_cmd_enabled(False)

    def _set_cmd_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for w in self.cmd_widgets:
            w.config(state=state)
        if not enabled:
            self.status_var.set("运动中... 到达目标位置后才能发送新指令")

    def _check_arrival(self):
        """每次收到状态帧后调用：所有等待轴都到达则解锁（自动循环时继续下一段）"""
        arrived = [a for a, t in self._pending.items()
                   if abs(self.positions[a] - t) <= POS_TOLERANCE]
        for a in arrived:
            del self._pending[a]
        if arrived and not self._pending:
            if self._auto_active and self._auto_advance():
                return  # 自动循环下一段已发出，保持锁定
            self._set_cmd_enabled(True)
            self.status_var.set(f"已连接 {self.ip_var.get()}:{self.port_var.get()}（已到达目标位置）")

    # ---------- X 轴自动循环 ----------

    def _update_cycle_info(self):
        self.cycle_info_var.set(
            f"大循环剩余: {self._outer_remaining}，内循环剩余: {self._auto_remaining}")

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
        self._auto_ystep = ystep
        self._inner_total = count
        self._auto_remaining = count
        self._outer_remaining = outer
        self._y_start = self.positions["Y"]
        self._auto_active = True
        self._auto_leg = "end"
        self._update_cycle_info()
        self._append_log(
            f"[自动] 开始: 大循环 {outer} 组 x 内循环 {count} 次"
            f"（X: {fmt_pos(X_HOME)} <-> {fmt_pos(X_END)}，循环间 Y -{fmt_pos(ystep)}，"
            f"Y 起始位置 {fmt_pos(self._y_start)}）\n")
        self.sender.send_command(f"X={fmt_pos(X_END)}")
        self._wait_target("X", X_END)

    def _stop_auto(self):
        if not self._auto_active:
            return
        self._auto_active = False
        self._outer_remaining = 0
        self._pending.clear()
        self._set_cmd_enabled(True)
        self.cycle_info_var.set("")
        self._append_log("[自动] 已停止（当前这段运动会继续走完）\n")

    def _auto_abort(self, reason: str):
        """异常终止自动循环（如 Y 步进超出行程）"""
        self._auto_active = False
        self._outer_remaining = 0
        self._pending.clear()
        self._set_cmd_enabled(True)
        self.cycle_info_var.set("")
        self._append_log(f"[自动] 已中止: {reason}\n")
        messagebox.showwarning("自动循环中止", reason)

    def _auto_advance(self) -> bool:
        """一段到达后推进自动循环，返回 True 表示已发出下一段"""
        if self._auto_leg == "end":
            # 到达终点 -> 返回起始
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
        self._append_log("[自动] 全部循环完成\n")
        return False

    def _refresh_position(self, axis: str):
        self.pos_labels[axis].config(text=fmt_pos(self.positions[axis]))

    # ---------- UV 灯 / 滚子 ----------

    def _toggle_uv(self):
        self.sender.send_command(CMD_UV_LAMP)
        self.uv_on = not self.uv_on
        self.uv_btn.config(text=f"UV 灯: {'开' if self.uv_on else '关'}")

    def _toggle_roller(self):
        self.sender.send_command(CMD_ROLLER)
        self.roller_on = not self.roller_on
        self.roller_btn.config(text=f"滚子: {'运行' if self.roller_on else '停止'}")

    def _emergency_stop(self):
        """急停：立即发送 q，打断所有运动，终止自动循环并解锁"""
        self.sender.send_urgent(CMD_ESTOP)
        self._auto_active = False
        self._pending.clear()
        self.cycle_info_var.set("")
        self._set_cmd_enabled(True)
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
                self.cycle_info_var.set("")
                self._set_cmd_enabled(True)
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
        self.destroy()


if __name__ == "__main__":
    app = MainWindow()
    app.mainloop()
