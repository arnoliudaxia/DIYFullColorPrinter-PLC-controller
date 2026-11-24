# -*- coding: utf-8 -*-
"""绘制 X 电机位置和光栅尺记录。

用法：
    python position_log_plot.py
    python position_log_plot.py logs/x_motor_grating_20261123_012917.csv

光栅尺坐标按 CSV 第一个记录点与 X 电机位置对齐，1 mm 等于 1000 μm。
不依赖 matplotlib，使用 Tk Canvas 绘图。拖动时间滑块可以查看对应采样点，
也可以播放、暂停或回到开始位置。
"""

import bisect
import csv
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


MICROMETERS_PER_MM = 1000.0


class PositionLogPlotter(tk.Toplevel):
    def __init__(self, parent, path):
        super().__init__(parent)
        self.title(f"位置轨迹 - {Path(path).name}")
        self.geometry("1000x650")
        self.minsize(720, 480)
        self._path = Path(path)
        self._playing = False
        self._play_after = None
        self._data = self._load_data(self._path)
        self._times = [row["elapsed"] for row in self._data]
        self._duration = max(self._times[-1] if self._times else 0.0, 0.001)
        self._reference_x_mm = self._data[0]["x_motor"] if self._data else 0.0
        self._reference_grating_dec = self._data[0]["grating"] if self._data else 0
        self._window_seconds = min(10.0, self._duration)
        self._window_var = tk.StringVar(value=f"{self._window_seconds:.3f}")
        self._time_var = tk.DoubleVar(value=0.0)

        controls = ttk.Frame(self)
        controls.pack(fill="x", padx=8, pady=(8, 2))
        self._info_var = tk.StringVar()
        ttk.Label(controls, textvariable=self._info_var).pack(side="left")
        ttk.Label(controls, text="窗口(s):").pack(side="left", padx=(16, 2))
        ttk.Entry(controls, textvariable=self._window_var,
                  width=8).pack(side="left")
        ttk.Button(controls, text="应用窗口", width=9,
                   command=self._apply_window).pack(side="left", padx=4)
        self._play_btn = ttk.Button(
            controls, text="播放", width=8, command=self._toggle_play)
        self._play_btn.pack(side="right", padx=(4, 0))
        ttk.Button(controls, text="回到开始", width=9,
                   command=self._rewind).pack(side="right")

        self._scale = ttk.Scale(
            self, from_=0.0, to=self._duration, variable=self._time_var,
            command=self._on_slider)
        self._scale.pack(fill="x", padx=12, pady=(0, 4))
        scale_labels = ttk.Frame(self)
        scale_labels.pack(fill="x", padx=12)
        ttk.Label(scale_labels, text="0 s").pack(side="left")
        ttk.Label(scale_labels, text=f"{self._duration:.3f} s").pack(side="right")

        self._canvas = tk.Canvas(
            self, background="white", highlightthickness=1,
            highlightbackground="#bdbdbd")
        self._canvas.pack(fill="both", expand=True, padx=8, pady=8)
        self._canvas.bind("<Configure>", lambda _event: self._draw())
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._draw()

    @staticmethod
    def _load_data(path):
        rows = []
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                for row in csv.DictReader(stream):
                    try:
                        elapsed = float(row["elapsed_ms"]) / 1000.0
                        x_motor = float(row["x_motor_mm"])
                        grating = int(row["grating_dec"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    rows.append({
                        "elapsed": elapsed,
                        "x_motor": x_motor,
                        "grating": grating,
                        "timestamp": row.get("timestamp", ""),
                    })
        except (OSError, UnicodeError) as exc:
            messagebox.showerror("读取位置记录失败", f"{path}\n{exc}")
            return []
        rows.sort(key=lambda item: item["elapsed"])
        return rows

    @staticmethod
    def _range(values):
        low, high = min(values), max(values)
        if high == low:
            padding = max(abs(low) * 0.02, 1.0)
        else:
            padding = (high - low) * 0.08
        return low - padding, high + padding

    @staticmethod
    def _format_number(value):
        if abs(value) >= 100000:
            return f"{value:.0f}"
        return f"{value:.3f}".rstrip("0").rstrip(".")

    def _apply_window(self):
        try:
            seconds = float(self._window_var.get().strip())
            if seconds <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("错误", "时间窗口必须是正数")
            return
        self._window_seconds = seconds
        self._draw()

    def _on_slider(self, value):
        try:
            self._time_var.set(float(value))
        except (TypeError, ValueError):
            return
        self._draw()

    def _rewind(self):
        self._time_var.set(0.0)
        self._draw()

    def _toggle_play(self):
        if self._playing:
            self._playing = False
            if self._play_after is not None:
                self.after_cancel(self._play_after)
                self._play_after = None
            self._play_btn.config(text="播放")
            return
        if self._time_var.get() >= self._duration:
            self._time_var.set(0.0)
        self._playing = True
        self._play_btn.config(text="暂停")
        self._play_tick()

    def _play_tick(self):
        self._play_after = None
        if not self._playing:
            return
        current = min(self._time_var.get() + 0.05, self._duration)
        self._time_var.set(current)
        self._draw()
        if current >= self._duration:
            self._playing = False
            self._play_btn.config(text="播放")
        else:
            self._play_after = self.after(50, self._play_tick)

    def _draw(self):
        canvas = self._canvas
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 200 or height < 160:
            return
        if not self._data:
            canvas.create_text(
                width / 2, height / 2, text="没有可绘制的位置数据",
                fill="#555555", font=("", 14))
            self._info_var.set("无有效数据")
            return

        left, right, top, bottom = 78, 88, 42, 58
        x0, x1 = left, width - right
        y0, y1 = top, height - bottom
        window_seconds = min(self._window_seconds, self._duration)
        center_time = min(max(self._time_var.get(), 0.0), self._duration)
        window_start = max(
            0.0, min(center_time - window_seconds / 2,
                     self._duration - window_seconds))
        window_end = window_start + window_seconds
        start_index = bisect.bisect_left(self._times, window_start)
        end_index = bisect.bisect_right(self._times, window_end)
        display_rows = self._data[start_index:end_index]
        if not display_rows:
            display_rows = [self._data[min(
                bisect.bisect_left(self._times, center_time), len(self._data) - 1)]]
        # 以第一个记录点为基准对齐两套坐标：
        # 第一个光栅尺值对应第一个 X 电机位置，之后按光栅增量换算。
        x_values = [row["x_motor"] for row in display_rows]
        grating_um_values = [
            self._reference_x_mm * MICROMETERS_PER_MM
            + row["grating"] - self._reference_grating_dec
            for row in display_rows]
        common_values_mm = (
            x_values
            + [value / MICROMETERS_PER_MM for value in grating_um_values]
            + [0.0])
        y_low, y_high = self._range(common_values_mm)

        def map_x(seconds):
            return x0 + ((seconds - window_start) / window_seconds) * (x1 - x0)

        def map_y(value, low, high):
            return y1 - (value - low) / (high - low) * (y1 - y0)

        canvas.create_rectangle(x0, y0, x1, y1, outline="#777777")
        for tick in range(6):
            fraction = tick / 5
            y = y1 - fraction * (y1 - y0)
            canvas.create_line(x0, y, x1, y, fill="#e5e5e5")
            common_value_mm = y_low + fraction * (y_high - y_low)
            canvas.create_text(
                x0 - 8, y, text=self._format_number(common_value_mm),
                anchor="e", fill="#1565c0")
            canvas.create_text(
                x1 + 8,
                y,
                text=self._format_number(common_value_mm * MICROMETERS_PER_MM),
                anchor="w",
                fill="#d84315")
        for tick in range(6):
            fraction = tick / 5
            x = x0 + fraction * (x1 - x0)
            seconds = window_start + fraction * window_seconds
            canvas.create_line(x, y0, x, y1, fill="#eeeeee")
            canvas.create_text(
                x, y1 + 16, text=f"{seconds:.1f}s", anchor="n",
                fill="#555555")

        motor_points = []
        encoder_points = []
        for row in display_rows:
            x = map_x(row["elapsed"])
            grating_um = (
                self._reference_x_mm * MICROMETERS_PER_MM
                + row["grating"] - self._reference_grating_dec)
            grating_mm = grating_um / MICROMETERS_PER_MM
            motor_points += [x, map_y(row["x_motor"], y_low, y_high)]
            encoder_points += [x, map_y(grating_mm, y_low, y_high)]
        if len(motor_points) >= 4:
            canvas.create_line(
                *motor_points, fill="#1565c0", width=2, smooth=False)
            canvas.create_line(
                *encoder_points, fill="#d84315", width=2, smooth=False)
        else:
            canvas.create_oval(
                motor_points[0] - 2, motor_points[1] - 2,
                motor_points[0] + 2, motor_points[1] + 2,
                fill="#1565c0", outline="")
            canvas.create_oval(
                encoder_points[0] - 2, encoder_points[1] - 2,
                encoder_points[0] + 2, encoder_points[1] + 2,
                fill="#d84315", outline="")

        current_time = min(max(self._time_var.get(), 0.0), self._duration)
        index = min(
            bisect.bisect_left(self._times, current_time), len(self._data) - 1)
        row = self._data[index]
        cursor_x = map_x(row["elapsed"])
        canvas.create_line(cursor_x, y0, cursor_x, y1,
                           fill="#7b1fa2", width=2)
        current_grating_um = (
            self._reference_x_mm * MICROMETERS_PER_MM
            + row["grating"] - self._reference_grating_dec)
        current_grating_mm = current_grating_um / MICROMETERS_PER_MM
        motor_y = map_y(row["x_motor"], y_low, y_high)
        encoder_y = map_y(current_grating_mm, y_low, y_high)
        canvas.create_oval(
            cursor_x - 4, motor_y - 4, cursor_x + 4, motor_y + 4,
            fill="#1565c0", outline="white")
        canvas.create_oval(
            cursor_x - 4, encoder_y - 4, cursor_x + 4, encoder_y + 4,
            fill="#d84315", outline="white")

        canvas.create_text(
            (x0 + x1) / 2, 16, text="X 电机位置 / 光栅尺轨迹",
            fill="#222222", font=("", 12, "bold"))
        canvas.create_text(
            x0, y0 - 12, text="X 电机 (mm)", anchor="w", fill="#1565c0")
        canvas.create_text(
            x1, y0 - 12, text="光栅尺 (μm)", anchor="e", fill="#d84315")
        canvas.create_text(
            (x0 + x1) / 2, height - 12, text="经过时间", fill="#555555")
        self._info_var.set(
            f"时间 {row['elapsed']:.3f}s / {self._duration:.3f}s    "
            f"窗口 {window_start:.3f}~{window_end:.3f}s    "
            f"X={row['x_motor']:.4f} mm    "
            f"光栅尺对齐位置={current_grating_um:.0f} μm "
            f"(DEC={row['grating']}, {row['grating']:#010x})    "
            f"{row['timestamp']}    窗口数据 {len(display_rows)} 点")

    def _close(self):
        self._playing = False
        if self._play_after is not None:
            self.after_cancel(self._play_after)
            self._play_after = None
        self.destroy()


def main():
    root = tk.Tk()
    root.withdraw()
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        log_dir = Path(__file__).resolve().parent / "logs"
        selected = filedialog.askopenfilename(
            parent=root,
            title="选择位置记录文件",
            initialdir=str(log_dir if log_dir.exists() else Path.cwd()),
            filetypes=[("位置记录 CSV", "*.csv"), ("所有文件", "*.*")])
        if not selected:
            root.destroy()
            return
        path = Path(selected)
    PositionLogPlotter(root, path)
    root.mainloop()


if __name__ == "__main__":
    main()
