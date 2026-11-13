"""暂停手动调整回归：只编译真实 MainWindow AST，不导入/启动应用或连接设备。"""
import ast
from collections import deque
from pathlib import Path
import re
import types
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "device_control.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
NS = dict(tk=types.SimpleNamespace(Tk=object), re=re, deque=deque,
          POS_TOLERANCE=0.1, AXIS_LIMITS={"X": (-10, 230), "Y": (-5, 120),
                                      "Z": (-60, 125)},
          fmt_pos=lambda value: f"{value:g}", X_HOME=-5, Y_HOME=0,
          CMD_UV_LAMP="D", CMD_FLASH="FLASH", CMD_ESTOP="q",
          FLASH_WAIT_TIMEOUT_MS=2000, PASS_STOP_WAIT_MS=250,
          PASS_STOP_WAIT_SECONDS=0.25, DEVICE_HOST="fake", DEVICE_PORT=0)
for node in TREE.body:
    if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id.endswith("PATTERN")
            for target in node.targets):
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), NS)
    if isinstance(node, ast.ClassDef) and node.name == "MainWindow":
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), NS)
Window = NS["MainWindow"]


class Widget:
    """保留真实控件锁状态，而不是只检查 config 调用次数。"""
    def __init__(self, value="", state="disabled"):
        self.value = value
        self.options = {"state": state}

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

    def config(self, **options):
        self.options.update(options)

    configure = config


class Clock:
    def __init__(self):
        self.now = 1.0
        self.timers = {}
        self.serial = 0

    def schedule(self, ms, callback):
        self.serial += 1
        self.timers[self.serial] = (self.now + ms / 1000, callback)
        return self.serial

    def cancel(self, handle):
        self.timers.pop(handle, None)

    def advance(self, seconds):
        self.now += seconds
        for handle, (deadline, callback) in list(self.timers.items()):
            if handle in self.timers and deadline <= self.now:
                self.timers.pop(handle)
                callback()


class PauseMotionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.commands = []
        self.scan_commands = []
        # 所有时间均为假时间；没有 sleep、socket 或后台线程。
        patch = mock.patch.dict(NS, time=types.SimpleNamespace(
            monotonic=lambda: self.clock.now, strftime=lambda _: "00:00:01"),
            messagebox=mock.Mock())
        patch.start()
        self.addCleanup(patch.stop)
        w = self.w = Window.__new__(Window)
        defaults = dict(
            _reset_workflow=None, _auto_active=True, _auto_mode="server",
            _auto_leg="end", _workflow_paused=False, _pause_motion_unlocked=False,
            _paused_layer_start=None, _pending={}, _retry_timers={}, _closing=False,
            _maintenance_pending=set(), flash_on=False, _press_ink_repeat_enabled=False,
            _press_ink_repeat_after=None, _flash_pausing=False, _flash_wait_off=False,
            _flash_after=None, _flash_wait_after=None, _server_stop_after=None,
            positions={"X": 0, "Y": 0, "Z": 0}, uv_on=False, _uv_dist=5,
            _job_total_layers=3, _job_current_layer=1, _active_layer=1,
            _layer_listening=True, _pass_current=1, _pass_total=2,
            _prehome_pending_start=False, _y_start=0, event_queue=deque())
        for name, value in defaults.items():
            setattr(w, name, value)
        # idle 与位置反馈分别控制，不能将“命令已入队”误当成“设备已到位”。
        w.sender = types.SimpleNamespace(
            connected=True, sock=object(), idle=True,
            send_command=mock.Mock(side_effect=lambda cmd: self.commands.append(cmd) or True),
            send_urgent=mock.Mock(side_effect=self.commands.append))
        w.scan = types.SimpleNamespace(
            connected=True, ready=True, sock=object(),
            send=mock.Mock(side_effect=lambda cmd: self.scan_commands.append(cmd) or True))
        w._scan_event_channel = w.scan.sock
        w._plc_status_socket = w.sender.sock
        w._post_ui = lambda callback, *args: callback(*args)
        w.after, w.after_cancel = self.clock.schedule, self.clock.cancel
        w.axis_widgets = {axis: [Widget(), Widget(), Widget()] for axis in "XYZ"}
        w.auto_widgets = [Widget()]
        w.target_vars = {axis: Widget("0") for axis in "XYZ"}
        w.step_var = Widget("2")
        w.auto_uv_var = Widget(False)
        for name in ("status_var", "pause_btn", "encoder_reset_btn", "device_reset_btn",
                     "uv_btn", "roller_btn", "last_report_var", "flash_btn",
                     "event_count_var", "cycle_info_var", "eta_var"):
            setattr(w, name, Widget())
        w.event_list = mock.Mock()
        for name in ("_append_log", "_refresh_position", "_update_job_progress",
                     "_update_pass_progress", "_update_auto_counts", "_play_finish_sound",
                     "_auto_advance", "_start_server_layer_positioning"):
            setattr(w, name, mock.Mock())

    def assert_axes(self, enabled):
        expected = "normal" if enabled else "disabled"
        for axis, widgets in self.w.axis_widgets.items():
            for widget in widgets:
                self.assertEqual(widget.options["state"], expected, axis)

    def pause(self):
        self.w._toggle_workflow_pause()
        self.assertTrue(self.w._workflow_paused)

    def pos(self, x=0, y=0, z=0, uv=0):
        self.w._on_status(f"POS:X={x},Y={y},Z={z},D={uv},G=0",
                          self.w.sender.sock, self.clock.now)

    def response(self, line):
        self.w._on_scan_response(line, self.w.scan.sock)

    def test_pause_waits_for_all_axes_and_sender_idle_without_stopping_current_move(self):
        w = self.w
        w._pending = {"X": 10, "Z": 3}
        w.sender.idle = False
        self.pause()
        self.assert_axes(False)
        self.assertFalse(w._pause_motion_unlocked)
        self.pos(x=10)
        self.assertEqual(w._pending, {"Z": 3})
        self.assert_axes(False)
        self.pos(x=10, z=3)
        self.assertFalse(w._pending)
        self.assert_axes(False)  # 全部到位仍需等发送队列 idle。
        w.sender.idle = True
        self.pos(x=10, z=3)  # 无新 arrived 也必须刷新暂停锁。
        self.assertTrue(w._pause_motion_unlocked)
        self.assert_axes(True)
        self.assertEqual(w._auto_leg, "end")
        w._auto_advance.assert_not_called()
        self.assertEqual(self.commands, [])  # 暂停不能发 q 或替换现有运动。

    def test_pause_availability_gates_maintenance_and_manual_entry_points(self):
        w = self.w
        self.pause()
        blockers = {
            "_maintenance_pending": {"PRESS_INK"}, "flash_on": True,
            "_press_ink_repeat_enabled": True, "_press_ink_repeat_after": 1,
            "_flash_pausing": True, "_flash_wait_off": True, "_flash_after": 1,
            "_flash_wait_after": 1, "_server_stop_after": 1,
            "_reset_workflow": types.SimpleNamespace(active=True),
            "_auto_active": False, "_workflow_paused": False,
        }
        for name, value in blockers.items():
            with self.subTest(blocker=name), mock.patch.object(w, name, value):
                self.assertFalse(w._pause_motion_available())
        with mock.patch.object(w, "_closing", True):
            self.assertFalse(w._manual_motion_allowed("X"))
        with mock.patch.object(w.sender, "connected", False):
            self.assertFalse(w._manual_motion_allowed("X"))
        with mock.patch.object(w, "_workflow_paused", False):
            w._move_absolute("X")
            w._jog("X", 1)
            w._move_to("X", 2)
        self.assertEqual(self.commands, [])

    def test_absolute_jog_and_preset_register_targets_even_at_cached_position(self):
        w = self.w
        self.pause()
        for method, args, command, target in (
                ("_move_absolute", ("X",), "X=0", 0),  # 缓存恰好等于目标。
                ("_jog", ("X", 1), "X+2", 2),
                ("_move_to", ("X", 4), "X=4", 4)):
            with self.subTest(method=method):
                getattr(w, method)(*args)
                self.assertEqual(self.commands[-1], command)
                self.assertEqual(w._pending, {"X": target})
                self.assert_axes(False)
                # 手动运动期间其它轴也不能绕过全局暂停等待门禁。
                self.assertFalse(w._manual_motion_allowed("Y"))
                self.pos(x=target)
                self.assertFalse(w._pending)
                self.assert_axes(True)
                self.assertEqual(w._auto_leg, "end")
                w._auto_advance.assert_not_called()
        self.assertTrue(w._pause_motion_unlocked)
        self.assertEqual(w.encoder_reset_btn.options["state"], "disabled")
        self.assertEqual(w.device_reset_btn.options["state"], "disabled")
        before = list(self.commands)
        w._start_encoder_reset()
        w._send_device_reset()
        self.assertEqual(self.commands, before)
        self.assertEqual(self.scan_commands, [])
        self.assertIsNone(w._reset_workflow)

    def test_resume_refuses_pending_or_busy_queue_then_relocks_before_resume(self):
        w = self.w
        self.pause()
        w._move_to("X", 10)

        def resumed():
            self.assertFalse(w._workflow_paused)
            self.assertFalse(w._pause_motion_unlocked)
            self.assert_axes(False)

        with mock.patch.object(w, "_resume_workflow", side_effect=resumed) as resume:
            w._toggle_workflow_pause()
            self.assertTrue(w._workflow_paused)
            resume.assert_not_called()
            self.pos(x=10)
            w.sender.idle = False
            w._toggle_workflow_pause()
            self.assertTrue(w._workflow_paused)
            self.assertEqual(w.pause_btn.options["text"], "恢复")
            resume.assert_not_called()
            w.sender.idle = True
            w._toggle_workflow_pause()
            resume.assert_called_once_with()
        self.assertEqual(w.pause_btn.options["text"], "暂停")

    def test_uv_tracks_current_end_only_until_pause_manual_stage(self):
        w = self.w
        w.auto_uv_var.set(True)
        w._pending = {"X": 10}
        self.pause()
        self.pos(x=6)
        self.assertTrue(w.uv_on)  # 当前自动动作仍可按原 end 段开灯。
        self.pos(x=10, uv=1)
        self.assertTrue(w._pause_motion_unlocked)
        self.assertFalse(w.uv_on)
        self.assertEqual(self.commands, ["D", "D"])
        w._move_to("X", 20)
        self.pos(x=15)
        w._auto_uv_update()
        self.pos(x=20)
        self.assertFalse(w.uv_on)
        self.assertEqual(self.commands, ["D", "D", "X=20"])
        self.assertEqual(w._auto_leg, "end")
        w._auto_advance.assert_not_called()

    def test_paused_layer_preserves_motion_then_accepts_on_resume_and_keeps_new_pass(self):
        w = self.w
        old_pass = "EVENT PASS_READY CURRENT=1 TOTAL=2 STEP=10 EMPTY=0"
        new_pass = "EVENT PASS_READY CURRENT=1 TOTAL=3 STEP=20 EMPTY=0"
        old_zero = "EVENT PASS_REMAINING_ZERO LAYER=1 PASS=1 REMAINING=0"
        w.event_queue.extend(("old", event) for event in (old_pass, old_zero))
        self.pause()
        w._move_to("X", 10)  # 新层不能抢占暂停中已经下发的手动运动。
        with mock.patch.object(w, "_accept_layer_start", wraps=w._accept_layer_start) as accept:
            self.response("EVENT LAYER_START LAYER=2")
            self.response(new_pass)
            self.assertEqual(w._pending, {"X": 10})
            self.assertEqual(w._auto_leg, "end")
            self.assertEqual(w._active_layer, 1)
            self.assertEqual(w._paused_layer_start, 2)
            accept.assert_not_called()
            self.pos(x=10)
            accept.assert_not_called()
            w._auto_advance.assert_not_called()
            w._toggle_workflow_pause()
            accept.assert_called_once_with(2)
        self.assertIsNone(w._paused_layer_start)
        self.assertEqual(w._active_layer, 2)
        self.assertEqual(w._job_current_layer, 2)
        self.assertEqual([event for _, event in w.event_queue], [new_pass])
        w._start_server_layer_positioning.assert_called_once_with()
        self.assert_axes(False)

    def test_start_job_during_paused_motion_or_busy_queue_does_not_replace_job(self):
        w = self.w
        self.pause()
        for pending, idle in (({"X": 10}, True), ({}, False)):
            with self.subTest(pending=pending, idle=idle):
                w._pending = dict(pending)
                w.sender.idle = idle
                self.response("EVENT START_JOB TOTAL_LAYERS=99")
                self.assertEqual(w._pending, pending)
                self.assertEqual(w._job_total_layers, 3)
                self.assertEqual(w._auto_leg, "end")
                self.assertTrue(w._workflow_paused)
                self.assertEqual(self.commands, [])

    def test_print_completed_keeps_manual_pending_until_real_arrival(self):
        w = self.w
        self.pause()
        w._move_to("X", 10)
        self.response("EVENT PRINT_JOB_COMPLETED TOTAL_LAYERS=3")
        self.assertFalse(w._auto_active)
        self.assertFalse(w._workflow_paused)
        self.assertFalse(w._pause_motion_unlocked)
        self.assertEqual(w._pending, {"X": 10})
        self.assertEqual(w.axis_widgets["X"][0].options["state"], "disabled")
        self.assertFalse(w._manual_motion_allowed("X"))
        self.pos(x=5)
        self.assertEqual(w._pending, {"X": 10})
        self.pos(x=10)
        self.assertFalse(w._pending)
        self.assert_axes(True)
        w._auto_advance.assert_not_called()

    def test_flash_timer_finishes_maintenance_but_defers_motion_until_resume(self):
        w = self.w
        w._auto_leg = "server_flash_pause"
        w._flash_pausing = True
        w.flash_on = True
        w._flash_after = self.clock.schedule(10, w._flash_resume)
        self.pause()
        self.clock.advance(0.01)
        self.assertEqual(self.scan_commands, ["FLASH\r\n"])
        self.assertTrue(w._flash_wait_off)
        self.assert_axes(False)
        self.assertEqual(self.commands, [])
        self.response("OK FLASH BEFORE=ON AFTER=OFF")
        self.assertFalse(w._maintenance_pending)
        self.assertIsNone(w._flash_wait_after)
        self.assertTrue(w._pause_motion_unlocked)
        self.assert_axes(True)
        self.assertEqual(w._auto_leg, "server_flash_pause")
        self.assertEqual(self.commands, [])
        w._toggle_workflow_pause()
        self.assertEqual(w._auto_leg, "wait_after_pass_stop")
        self.assertIsNotNone(w._server_stop_after)
        self.assert_axes(False)

    def test_delayed_layer_blockers_do_not_recurse_and_late_flash_off_resumes(self):
        w = self.w
        w._paused_layer_start = 2
        w.event_queue.append(("new", "EVENT LAYER_START LAYER=2"))
        # 每个门禁单独验证真实 helper：未满足时既不消费也不启动定位。
        blockers = {
            "_workflow_paused": True, "_pending": {"X": 10},
            "_flash_pausing": True, "_flash_wait_off": True, "flash_on": True,
            "_flash_after": 1, "_flash_wait_after": 1, "_server_stop_after": 1,
        }
        for name, value in blockers.items():
            with self.subTest(blocker=name), mock.patch.object(w, name, value):
                self.assertFalse(w._consume_paused_layer_start())
                self.assertEqual(w._paused_layer_start, 2)
                self.assertEqual(w._active_layer, 1)
                self.assertEqual(w._auto_leg, "end")
                w._start_server_layer_positioning.assert_not_called()
                self.assertEqual(self.commands, [])

        w._auto_leg = "server_flash_pause"
        w.flash_on = True
        w._flash_wait_off = True
        w._flash_wait_after = self.clock.schedule(2000, w._flash_wait_timeout)
        with mock.patch.object(w, "_resume_workflow", wraps=w._resume_workflow) as resume, \
                mock.patch.object(w, "_consume_paused_layer_start",
                                  wraps=w._consume_paused_layer_start) as consume, \
                mock.patch.object(w, "_accept_layer_start",
                                  wraps=w._accept_layer_start) as accept, \
                mock.patch.object(w, "_finish_server_pass") as finish:
            self.clock.advance(2)  # 真实 timeout -> proceed -> helper；FLASH 仍为 ON。
            self.assertFalse(w._flash_wait_off)
            self.assertIsNone(w._flash_wait_after)
            consume.assert_called_once_with()
            resume.assert_not_called()  # proceed 不能回调 resume 形成环。
            w._resume_workflow()
            resume.assert_called_once_with()  # helper 拒绝后不能落入旧段分派。
            self.assertEqual(consume.call_count, 2)
            self.assertEqual(w._auto_leg, "server_flash_pause")
            w._proceed_after_flash()
            self.assertEqual(resume.call_count, 1)
            self.assertEqual(consume.call_count, 3)
            w._auto_leg = "wait_after_pass_stop"
            w._resume_after_pass_stop()
            self.assertEqual(resume.call_count, 1)
            self.assertEqual(consume.call_count, 4)
            finish.assert_not_called()
            accept.assert_not_called()
            self.assertEqual(w._paused_layer_start, 2)
            self.assertEqual(w._active_layer, 1)
            self.assertEqual(self.commands, [])
            self.assertEqual(self.scan_commands, [])

            # timeout 已清 wait_off，晚到 OFF 必须走 response 的补偿恢复入口。
            self.response("OK FLASH BEFORE=ON AFTER=OFF")
            self.assertEqual(resume.call_count, 2)
            accept.assert_called_once_with(2)
        self.assertFalse(w.flash_on)
        self.assertIsNone(w._paused_layer_start)
        self.assertEqual(w._active_layer, 2)
        w._start_server_layer_positioning.assert_called_once_with()
        self.assertEqual(self.commands, [])

    def test_layer_after_resume_waits_for_existing_maintenance_timer(self):
        w = self.w
        w._auto_leg = "wait_after_pass_stop"
        w._server_stop_after = self.clock.schedule(250, w._resume_after_pass_stop)
        timer = w._server_stop_after
        self.pause()
        with mock.patch.object(w, "_resume_workflow", wraps=w._resume_workflow) as resume, \
                mock.patch.object(w, "_accept_layer_start",
                                  wraps=w._accept_layer_start) as accept, \
                mock.patch.object(w, "_finish_server_pass") as finish:
            w._toggle_workflow_pause()
            self.assertFalse(w._workflow_paused)
            self.assertEqual(w._server_stop_after, timer)
            resume.assert_called_once_with()
            self.response("EVENT LAYER_START LAYER=2")
            self.assertEqual(w._paused_layer_start, 2)
            self.assertEqual(w._auto_leg, "wait_after_pass_stop")
            self.assertEqual(w._active_layer, 1)
            self.assertEqual(w._server_stop_after, timer)
            self.assertIn(timer, self.clock.timers)
            accept.assert_not_called()
            w._start_server_layer_positioning.assert_not_called()
            self.assertEqual(self.commands, [])
            self.clock.advance(0.25)  # 旧维护回调只经单向 helper 接管新层。
            accept.assert_called_once_with(2)
            resume.assert_called_once_with()
            finish.assert_not_called()
        self.assertIsNone(w._server_stop_after)
        self.assertIsNone(w._paused_layer_start)
        self.assertEqual(w._active_layer, 2)
        w._start_server_layer_positioning.assert_called_once_with()
        self.assert_axes(False)

    def test_multiple_paused_layers_choose_latest_and_filter_old_layers_and_passes(self):
        w = self.w
        old_pass = "EVENT PASS_READY CURRENT=1 TOTAL=2 STEP=10 EMPTY=0"
        middle_pass = "EVENT PASS_READY CURRENT=2 TOTAL=2 STEP=20 EMPTY=0"
        latest_pass = "EVENT PASS_READY CURRENT=1 TOTAL=3 STEP=30 EMPTY=0"
        self.pause()
        with mock.patch.object(w, "_accept_layer_start", wraps=w._accept_layer_start) as accept:
            self.response(old_pass)
            self.response("EVENT LAYER_START LAYER=2")
            self.response(middle_pass)
            self.response("EVENT PASS_REMAINING_ZERO LAYER=2 PASS=2 REMAINING=0")
            self.response("EVENT LAYER_START LAYER=3")
            self.response(latest_pass)
            self.assertEqual(w._paused_layer_start, 3)
            self.assertEqual(w._active_layer, 1)
            self.assertEqual(w._auto_leg, "end")
            self.assertIn("EVENT LAYER_START LAYER=2", [ev for _, ev in w.event_queue])
            accept.assert_not_called()
            w._toggle_workflow_pause()
            accept.assert_called_once_with(3)
            self.assertEqual(w._active_layer, 3)
            self.assertEqual(w._job_current_layer, 3)
            self.assertIsNone(w._paused_layer_start)
            self.assertEqual([ev for _, ev in w.event_queue], [latest_pass])
            # 保留真实 wait_layer 消费路径；再次查队列不能退回旧 LAYER_START。
            w._try_start_layer()
            w._resume_workflow()
            accept.assert_called_once_with(3)
        self.assertEqual(w._active_layer, 3)
        self.assertEqual([ev for _, ev in w.event_queue], [latest_pass])
        w._start_server_layer_positioning.assert_called_once_with()
        w._auto_advance.assert_not_called()
        self.assertEqual(self.commands, [])


if __name__ == "__main__":
    unittest.main()
