"""无窗口、无设备：独立状态机 + AST 提取真实 UI/发送器方法。"""
import ast
import inspect
from pathlib import Path
import queue
import re
import socket
import threading
import time
import types
import unittest
from unittest import mock

from encoder_reset import EncoderResetWorkflow


SOURCE = Path(__file__).resolve().parents[1] / "device_control.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
NS = dict(tk=types.SimpleNamespace(Tk=object), threading=threading, queue=queue,
          time=time, re=re, socket=socket, POS_TOLERANCE=0.1, AXIS_LIMITS={"X": (-10, 230),
          "Y": (-5, 120), "Z": (-60, 125)}, CMD_ESTOP="q", MIN_INTERVAL=0,
          EncoderResetWorkflow=EncoderResetWorkflow, X_ZERO=0, X_HOME=-5, Y_HOME=0,
          fmt_pos=lambda value: f"{value:g}",
          SCAN_READY="NETSCAN_SERVER_READY")
for node in TREE.body:
    if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id.endswith("PATTERN")
            for target in node.targets):
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), NS)
    if isinstance(node, ast.ClassDef) and node.name in {"MainWindow", "TcpSender", "ScanClient"}:
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), NS)
Window = NS["MainWindow"]
TcpSender = NS["TcpSender"]
ScanClient = NS["ScanClient"]


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


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.events = []
        self.connected = True
        self.sent = True
        self.make_workflow()

    def make_workflow(self, **overrides):
        options = dict(zero_position=0, home_position=-5,
                       send=lambda line: self.events.append(("MFC", line.strip())) or self.sent)
        options.update(overrides)
        self.wf = EncoderResetWorkflow(
            move=lambda x, token, phase: self.events.append(("PLC", x)) or self.sent,
            stop=lambda: self.events.append(("PLC", "q")) if self.wf.motion_started else None,
            ready=lambda: self.connected, schedule=self.clock.schedule,
            unschedule=self.clock.cancel,
            finished=lambda ok, why: self.events.append(("DONE", ok)),
            clock=lambda: self.clock.now, token_factory=lambda: 7, **options)

    def ack(self, command, result="OK", extra="", token=None):
        token = self.wf.token if token is None else token
        self.wf.response(f"{result} {command} TOKEN={token}{extra}")

    def begin(self):
        self.assertTrue(self.wf.start())
        self.ack("ENCODER_RESET_BEGIN")

    def stable_positions(self, x=0, step=0.3):
        for _ in range(3):
            self.clock.advance(step)
            self.wf.position(x, self.clock.now)

    def zero(self):
        self.begin()
        self.wf.motion_sent(self.wf.token, "to_zero", self.clock.now)
        self.stable_positions()
        self.assertEqual(self.wf.phase, "reset")

    def returning(self):
        self.zero()
        self.clock.advance(2)
        self.assertEqual(self.wf.phase, "to_return")

    def ending(self):
        self.returning()
        self.wf.motion_sent(self.wf.token, "to_return", self.clock.now)
        self.clock.advance(0.01)
        self.wf.position(-5, self.clock.now)

    def test_target_arguments_are_required_keyword_only_and_wait_defaults_remain(self):
        parameters = inspect.signature(EncoderResetWorkflow).parameters
        for name in ("zero_position", "home_position"):
            with self.subTest(name=name):
                self.assertEqual(parameters[name].kind, inspect.Parameter.KEYWORD_ONLY)
                self.assertIs(parameters[name].default, inspect.Parameter.empty)
        for name, default in (("settle_ms", 500), ("settle_tolerance", 0.01),
                              ("reset_wait_ms", 2000), ("progress", None)):
            with self.subTest(name=name):
                self.assertEqual(parameters[name].default, default)

    def test_default_tokens_fit_mfc_long_and_increase(self):
        self.wf.token_factory = None
        self.assertTrue(self.wf.start())
        self.assertEqual(self.wf.token, 1)
        self.wf.abort("cancel")
        self.assertTrue(self.wf.start())
        self.assertEqual(self.wf.token, 2)

    def test_token_overflow_refuses_before_sending_any_command(self):
        self.wf.token_factory = lambda: 2147483648
        self.assertFalse(self.wf.start())
        self.assertFalse(self.wf.active)
        self.assertFalse(self.clock.timers)
        self.assertFalse(self.events)
        self.wf.token_factory = None
        self.wf.last_token = 2147483647
        self.assertFalse(self.wf.start())
        self.assertFalse(self.events)

    def test_normal_strict_sequence_and_end_ack_required(self):
        self.ending()
        self.assertTrue(self.wf.active)
        self.assertNotIn(("DONE", True), self.events)
        self.ack("ENCODER_RESET_END")
        self.assertEqual(self.events, [
            ("MFC", "ENCODER_RESET_BEGIN TOKEN=7"), ("PLC", 0),
            ("MFC", "RESET_ENCODER TOKEN=7"), ("PLC", -5),
            ("MFC", "ENCODER_RESET_END TOKEN=7"), ("DONE", True)])
        self.assertFalse(self.wf.active)
        self.assertFalse(self.clock.timers)

    def test_no_motion_before_begin_and_no_cached_or_presend_pos(self):
        self.wf.start()
        self.wf.position(0, self.clock.now)
        self.assertEqual(len(self.events), 1)
        self.ack("ENCODER_RESET_BEGIN")
        self.wf.position(0, self.clock.now)
        self.clock.advance(0.1)
        sent_at = self.clock.now
        self.wf.motion_sent(7, "to_zero", sent_at)
        for old in (0, sent_at - 0.1, sent_at):
            self.wf.position(0, old)
            self.assertEqual(self.wf.phase, "to_zero")
        self.clock.advance(0.1)
        self.wf.position(0.09, self.clock.now)  # 在运动容差内，但不满足停稳容差。
        self.assertEqual(self.wf.phase, "to_zero")
        self.stable_positions()
        self.assertEqual(self.wf.phase, "reset")

    def test_return_also_requires_sent_and_fresh_arrival(self):
        self.returning()
        self.wf.position(-5, self.clock.now)
        self.assertEqual(self.wf.phase, "to_return")
        self.clock.advance(0.1)
        sent_at = self.clock.now
        self.wf.motion_sent(7, "to_return", sent_at)
        self.wf.position(-5, sent_at - 0.1)
        self.clock.advance(0.1)
        self.wf.position(-4.8, self.clock.now)
        self.assertEqual(self.wf.phase, "to_return")
        self.clock.advance(0.1)
        self.wf.position(-5, self.clock.now)
        self.assertEqual(self.wf.phase, "end")

    def test_pos_before_sent_callback_needs_more_frames_only_at_zero(self):
        self.begin()
        sent_at = self.clock.now
        self.clock.advance(0.1)
        self.wf.position(0, self.clock.now)
        self.assertEqual(self.wf.position_candidate, (0, self.clock.now))
        self.wf.motion_sent(7, "to_zero", sent_at)
        self.assertEqual(self.wf.phase, "to_zero")
        self.assertIsNone(self.wf.position_candidate)
        self.stable_positions()
        self.assertEqual(self.wf.phase, "reset")
        self.clock.advance(2)
        sent_at = self.clock.now
        self.clock.advance(0.1)
        self.wf.position(-5, self.clock.now)
        self.wf.motion_sent(7, "to_return", sent_at)
        self.assertEqual(self.wf.phase, "end")
        self.assertIsNone(self.wf.position_candidate)
        self.ack("ENCODER_RESET_END")
        self.assertFalse(self.wf.active)

    def test_candidate_uses_latest_receipt_not_ui_order_and_clears_on_abort(self):
        self.begin()
        sent_at = self.clock.now
        self.clock.advance(0.1)
        older = self.clock.now
        self.clock.advance(0.1)
        self.wf.position(0, self.clock.now)
        self.clock.advance(0.1)
        self.wf.position(1, self.clock.now)
        self.wf.position(0, older)  # 更晚派发的旧帧不能替换最新真实位置。
        self.wf.motion_sent(7, "to_zero", sent_at)
        self.assertEqual(self.wf.phase, "to_zero")
        self.wf.abort("stop")
        self.assertIsNone(self.wf.position_candidate)
        self.wf.start()
        self.ack("ENCODER_RESET_BEGIN")
        self.wf.motion_sent(8, "to_zero", self.clock.now)
        self.assertEqual(self.wf.phase, "to_zero")
        self.stable_positions()
        self.assertEqual(self.wf.phase, "reset")

    def test_presend_candidate_and_wrong_sent_token_cannot_advance(self):
        self.begin()
        self.wf.position(0, self.clock.now)
        self.wf.motion_sent(6, "to_zero", self.clock.now)
        self.assertEqual(self.wf.phase, "to_zero")
        self.assertIsNone(self.wf.sent_at)
        self.clock.advance(0.1)
        self.wf.motion_sent(7, "to_zero", self.clock.now)
        self.assertEqual(self.wf.phase, "to_zero")
        self.assertIsNone(self.wf.position_candidate)
        self.stable_positions()
        self.assertEqual(self.wf.phase, "reset")

    def test_old_token_wrong_phase_and_cancel_ack_do_not_advance(self):
        self.wf.start()
        for command, token in (("ENCODER_RESET_BEGIN", 6), ("RESET_ENCODER", 7),
                               ("ENCODER_RESET_CANCEL", 7)):
            self.ack(command, token=token)
        self.assertEqual(len(self.events), 1)
        self.assertFalse(self.wf.start())
        self.assertEqual(len(self.events), 1)

    def test_begin_busy_refuses_motion_and_cancels(self):
        self.wf.start()
        self.ack("ENCODER_RESET_BEGIN", "ERR", " REASON=BUSY")
        self.assertFalse(self.wf.active)
        self.assertNotIn(("PLC", 0), self.events)
        self.assertEqual(self.events[-2:], [
                         ("MFC", "ENCODER_RESET_CANCEL TOKEN=7"), ("DONE", False)])
        self.assertNotIn(("PLC", "q"), self.events)

    def test_reset_errors_never_return(self):
        for reason in ("BUSY", "FAILED", "NOT_RESERVED", "STOPPING", "NOT_READY",
                       "NOT_RESET", "INVALID_TOKEN"):
            with self.subTest(reason=reason):
                self.setUp()
                self.zero()
                self.ack("RESET_ENCODER", "ERR", f" REASON={reason}")
                self.assertNotIn(("PLC", -5), self.events)
                self.assertFalse(self.wf.active)
                self.assertFalse(self.clock.timers)

    def test_matching_late_reset_err_aborts_return_and_end(self):
        for phase in ("to_return", "end"):
            with self.subTest(phase=phase):
                self.setUp()
                {"to_return": self.returning, "end": self.ending}[phase]()
                if phase == "to_return":
                    self.wf.motion_sent(7, "to_return", self.clock.now)
                stale_timer = next(iter(self.clock.timers.values()))[1]
                self.ack("RESET_ENCODER", "ERR", " REASON=FAILED")
                self.assertFalse(self.wf.active)
                self.assertFalse(self.clock.timers)
                self.assertIn(("PLC", "q"), self.events)
                self.assertNotIn(("DONE", True), self.events)
                self.assertEqual(self.events[-2:], [
                    ("MFC", "ENCODER_RESET_CANCEL TOKEN=7"), ("DONE", False)])
                previous = list(self.events)
                stale_timer()
                self.ack("RESET_ENCODER", extra=" ENC=1048576", token=7)
                self.ack("ENCODER_RESET_END", token=7)
                self.clock.advance(31)
                self.assertEqual(self.events, previous)
                self.assertFalse(self.clock.timers)

    def test_late_reset_ok_and_wrong_token_err_do_not_advance_return_or_end(self):
        for phase in ("to_return", "end"):
            with self.subTest(phase=phase):
                self.setUp()
                {"to_return": self.returning, "end": self.ending}[phase]()
                previous = list(self.events)
                timers = dict(self.clock.timers)
                self.ack("RESET_ENCODER", "ERR", " REASON=FAILED", token=6)
                for extra in ("", " ENC=0", " ENC=1048576", " ENC=garbage"):
                    self.ack("RESET_ENCODER", extra=extra)
                    self.assertTrue(self.wf.active)
                    self.assertEqual(self.wf.phase, phase)
                    self.assertEqual(self.events, previous)
                    self.assertEqual(self.clock.timers, timers)

    def test_reset_ok_with_any_encoder_is_ignored_until_fixed_wait(self):
        for extra in ("", " ENC=0", " ENC=1048576", " ENC=-42", " ENC=garbage"):
            with self.subTest(extra=extra):
                self.setUp()
                self.zero()
                timers = dict(self.clock.timers)
                self.ack("RESET_ENCODER", extra=extra)
                self.assertTrue(self.wf.active)
                self.assertEqual(self.wf.phase, "reset")
                self.assertEqual(self.clock.timers, timers)
                self.clock.advance(1.9)
                self.ack("RESET_ENCODER", extra=extra)
                self.assertNotIn(("PLC", -5), self.events)
                self.clock.advance(0.11)
                self.assertEqual(self.wf.phase, "to_return")
                self.assertEqual(self.events.count(("PLC", -5)), 1)

    def test_timeout_each_ack_or_motion_phase_cancels_and_clears_timer(self):
        for phase in ("begin", "to_zero", "to_return", "end"):
            with self.subTest(phase=phase):
                self.setUp()
                {"begin": self.wf.start, "to_zero": self.begin,
                 "to_return": self.returning, "end": self.ending}[phase]()
                self.clock.advance(31 if phase.startswith("to_") else 6)
                self.assertFalse(self.wf.active)
                self.assertFalse(self.clock.timers)
                self.assertEqual(self.events[-2:], [
                                 ("MFC", "ENCODER_RESET_CANCEL TOKEN=7"), ("DONE", False)])
                self.assertEqual(("PLC", "q") in self.events,
                                 phase in {"to_return", "end"})

    def test_stop_clears_state_and_late_callbacks_cannot_move(self):
        self.begin()
        stale_timer = next(iter(self.clock.timers.values()))[1]
        self.wf.abort("stop")
        previous = list(self.events)
        self.ack("ENCODER_RESET_BEGIN", token=7)
        self.ack("RESET_ENCODER", token=7, extra=" ENC=1048576")
        self.wf.motion_sent(7, "to_zero", 1)
        self.wf.position(0, self.clock.now)
        stale_timer()
        self.assertEqual(self.events, previous)
        self.assertFalse(self.clock.timers)
        self.wf.start()
        self.assertEqual(self.wf.token, 8)
        self.ack("ENCODER_RESET_BEGIN", token=7)
        self.assertEqual(self.wf.phase, "begin")

    def test_disconnect_on_position_aborts_and_best_effort_cancel(self):
        self.begin()
        self.connected = False
        self.wf.position(0, self.clock.now)
        self.assertFalse(self.wf.active)
        self.assertEqual(self.events[-2], ("MFC", "ENCODER_RESET_CANCEL TOKEN=7"))

    def test_send_failure_and_not_ready_start(self):
        self.connected = False
        self.assertFalse(self.wf.start())
        self.assertFalse(self.events)
        self.connected = True
        self.sent = False
        self.wf.start()
        self.assertFalse(self.wf.active)
        self.assertFalse(self.clock.timers)

    def test_reset_returns_without_any_reset_ack(self):
        self.zero()
        self.clock.advance(1.9)
        self.assertEqual(self.wf.phase, "reset")
        self.assertNotIn(("PLC", -5), self.events)
        self.clock.advance(0.11)
        self.assertEqual(self.wf.phase, "to_return")
        self.assertEqual(self.events.count(("PLC", -5)), 1)
        self.assertNotIn(("DONE", True), self.events)

    def test_reset_wait_starts_after_successful_send_returns(self):
        def slow_send(line):
            self.events.append(("MFC", line.strip()))
            if line.startswith("RESET_ENCODER "):
                self.clock.now += 0.75  # 模拟同步发送耗时，不派发 UI 定时器。
            return True

        self.make_workflow(send=slow_send)
        self.zero()
        reset_sent_at = self.clock.now
        self.assertEqual(len(self.clock.timers), 1)
        deadline = next(iter(self.clock.timers.values()))[0]
        self.assertAlmostEqual(deadline, reset_sent_at + 2)
        self.clock.advance(1.9)
        self.assertEqual(self.wf.phase, "reset")
        self.clock.advance(0.11)
        self.assertEqual(self.wf.phase, "to_return")

    def test_failed_reset_send_has_no_wait_or_return(self):
        self.begin()
        self.wf.motion_sent(7, "to_zero", self.clock.now)
        self.sent = False
        self.stable_positions()
        self.assertFalse(self.wf.active)
        self.assertFalse(self.clock.timers)
        self.clock.advance(3)
        self.assertNotIn(("PLC", -5), self.events)

    def test_reset_positions_do_not_apply_ack_timeout_or_shorten_wait(self):
        self.zero()
        # 模拟 Tk 延迟派发 2s 回调；reset 已过 5s，POS 不应按 ACK 超时失败。
        self.clock.now += 6
        for x in (0, 10, -5):
            self.wf.position(x, self.clock.now)
        self.assertTrue(self.wf.active)
        self.assertNotIn(("DONE", False), self.events)
        self.clock.advance(0)
        self.assertEqual(self.wf.phase, "to_return")
        self.assertEqual(self.events.count(("PLC", -5)), 1)

    def test_reset_err_wrong_token_ignored_but_matching_err_still_aborts(self):
        self.zero()
        self.ack("RESET_ENCODER", extra=" ENC=0")
        self.ack("RESET_ENCODER", "ERR", " REASON=FAILED", token=6)
        self.clock.advance(1.9)
        self.assertEqual(self.wf.phase, "reset")
        self.ack("RESET_ENCODER", "ERR", " REASON=FAILED")
        self.clock.advance(1)
        self.assertFalse(self.wf.active)
        self.assertFalse(self.clock.timers)
        self.assertNotIn(("PLC", -5), self.events)

    def test_reset_timer_is_cancelled_and_old_token_callback_cannot_advance(self):
        self.zero()
        stale_timer = next(iter(self.clock.timers.values()))[1]
        self.wf.abort("stop")
        self.assertFalse(self.clock.timers)
        previous = list(self.events)
        stale_timer()
        self.assertEqual(self.events, previous)
        self.assertTrue(self.wf.start())
        self.assertEqual(self.wf.token, 8)
        self.ack("ENCODER_RESET_BEGIN")
        self.wf.motion_sent(8, "to_zero", self.clock.now)
        self.stable_positions()
        self.assertEqual(self.wf.phase, "reset")
        self.clock.advance(1.2)  # 旧等待已到期，新 TOKEN 的等待仍未到期。
        timers = dict(self.clock.timers)
        previous = list(self.events)
        stale_timer()
        self.assertEqual(self.events, previous)
        self.assertEqual(self.clock.timers, timers)
        self.assertEqual(self.wf.phase, "reset")
        self.clock.advance(2)
        self.assertEqual(self.wf.phase, "to_return")

    def test_reset_callback_cannot_repeat_in_later_phase(self):
        self.zero()
        stale_timer = next(iter(self.clock.timers.values()))[1]
        self.clock.advance(2)
        previous = list(self.events)
        timers = dict(self.clock.timers)
        stale_timer()
        self.assertEqual(self.events, previous)
        self.assertEqual(self.clock.timers, timers)
        self.assertEqual(self.wf.phase, "to_return")

    def test_reset_timer_checks_connection_before_return_motion(self):
        self.zero()
        self.connected = False
        self.clock.advance(2)
        self.assertFalse(self.wf.active)
        self.assertFalse(self.clock.timers)
        self.assertNotIn(("PLC", -5), self.events)
        self.assertIn(("DONE", False), self.events)

    def test_configured_decimal_targets_and_custom_wait(self):
        self.make_workflow(zero_position=12.25, home_position=3.75,
                           settle_ms=100, reset_wait_ms=250)
        self.begin()
        self.assertEqual(self.events[-1], ("PLC", 12.25))
        self.wf.motion_sent(7, "to_zero", self.clock.now)
        self.stable_positions(12.25, step=0.06)
        self.assertEqual(self.wf.phase, "reset")
        self.clock.advance(0.24)
        self.assertNotIn(("PLC", 3.75), self.events)
        self.clock.advance(0.02)
        self.assertEqual(self.events[-1], ("PLC", 3.75))
        self.wf.motion_sent(7, "to_return", self.clock.now)
        self.clock.advance(0.01)
        self.wf.position(3.75, self.clock.now)
        self.assertEqual(self.wf.phase, "end")
        self.ack("ENCODER_RESET_END")
        self.assertEqual(self.events[-1], ("DONE", True))

    def test_zero_requires_three_frames_and_span_not_a_settle_timer(self):
        self.begin()
        self.wf.motion_sent(7, "to_zero", self.clock.now)
        for _ in range(2):
            self.clock.advance(0.1)
            self.wf.position(0, self.clock.now)
        self.clock.advance(0.6)
        self.assertEqual(self.wf.phase, "to_zero")
        self.assertNotIn(("MFC", "RESET_ENCODER TOKEN=7"), self.events)
        self.wf.position(0, self.clock.now)
        self.assertEqual(self.wf.phase, "reset")

    def test_no_pos_after_actual_send_never_assumes_stopped_and_motion_times_out(self):
        self.begin()
        self.wf.motion_sent(7, "to_zero", self.clock.now)
        self.clock.advance(29)
        self.assertEqual(self.wf.phase, "to_zero")
        self.assertNotIn(("MFC", "RESET_ENCODER TOKEN=7"), self.events)
        self.clock.advance(1.1)
        self.assertFalse(self.wf.active)
        self.assertFalse(self.clock.timers)
        self.assertNotIn(("MFC", "RESET_ENCODER TOKEN=7"), self.events)
        self.assertIn(("PLC", "q"), self.events)

    def test_three_fast_frames_still_need_settle_span_and_new_pos(self):
        self.begin()
        self.wf.motion_sent(7, "to_zero", self.clock.now)
        self.stable_positions(step=0.1)
        self.assertEqual(self.wf.phase, "to_zero")
        self.clock.advance(0.5)
        self.assertEqual(self.wf.phase, "to_zero")
        self.wf.position(0, self.clock.now)
        self.assertEqual(self.wf.phase, "reset")

    def test_duplicate_and_out_of_order_pos_do_not_count_as_new_frames(self):
        self.begin()
        self.wf.motion_sent(7, "to_zero", self.clock.now)
        self.clock.advance(0.1)
        stamp = self.clock.now
        self.wf.position(0, stamp)
        self.clock.advance(0.6)
        for stamp_again in (stamp, stamp - 0.01, stamp):
            self.wf.position(0, stamp_again)
            self.assertEqual(self.wf.last_position_at, stamp)
        self.assertEqual(self.wf.phase, "to_zero")
        self.wf.position(0, self.clock.now)  # 重复／反序已清空旧窗口，这是新首帧。
        self.assertEqual(self.wf.phase, "to_zero")
        self.clock.advance(0.1)
        self.wf.position(0, self.clock.now)
        self.assertEqual(self.wf.phase, "to_zero")
        self.assertNotIn(("MFC", "RESET_ENCODER TOKEN=7"), self.events)
        self.clock.advance(0.5)  # 新窗口共 3 帧、跨度 600ms。
        self.wf.position(0, self.clock.now)
        self.assertEqual(self.wf.phase, "reset")

    def test_out_of_order_off_zero_pos_clears_window_and_keeps_receipt_high_water(self):
        self.clock.now = 10.0
        self.begin()
        self.wf.motion_sent(7, "to_zero", self.clock.now)
        for stamp in (10.1, 10.35):
            self.clock.advance(stamp - self.clock.now)
            self.wf.position(0, stamp)
        self.assertEqual(self.wf.phase, "to_zero")
        self.wf.position(1, 10.2)  # 延迟派发的反序超差帧必须撤销旧停稳证据。
        self.assertEqual(self.wf.last_position_at, 10.35)
        self.assertEqual(self.wf.phase, "to_zero")
        self.clock.advance(10.6 - self.clock.now)
        self.wf.position(0, self.clock.now)
        self.assertEqual(self.wf.phase, "to_zero")
        self.assertNotIn(("MFC", "RESET_ENCODER TOKEN=7"), self.events)
        self.clock.advance(0.25)
        self.wf.position(0, self.clock.now)
        self.assertEqual(self.wf.phase, "to_zero")
        self.clock.advance(0.3)  # 从 10.6 重开后，第 3 帧跨度 550ms。
        self.wf.position(0, self.clock.now)
        self.assertEqual(self.wf.phase, "reset")
        self.assertEqual(self.events.count(("MFC", "RESET_ENCODER TOKEN=7")), 1)

    def test_stale_pos_and_gap_over_one_second_cannot_complete_old_window(self):
        self.begin()
        self.wf.motion_sent(7, "to_zero", self.clock.now)
        for _ in range(2):
            self.clock.advance(0.3)
            self.wf.position(0, self.clock.now)
        last = self.clock.now
        self.clock.advance(1.1)
        self.wf.position(0, last + 0.01)  # 时间严格递增，但帧年龄已超过 1s。
        self.assertEqual(self.wf.phase, "to_zero")
        self.wf.position(0, self.clock.now)  # 与上一有效帧间隔 >1s，必须重开窗口。
        self.assertEqual(self.wf.phase, "to_zero")
        self.clock.advance(0.3)
        self.wf.position(0, self.clock.now)
        self.assertEqual(self.wf.phase, "to_zero")
        self.clock.advance(0.3)
        self.wf.position(0, self.clock.now)
        self.assertEqual(self.wf.phase, "reset")

    def test_drift_or_leaving_zero_restarts_stable_window(self):
        for values in ((-0.009, -0.009, 0.009), (0, 0, 0.03)):
            with self.subTest(values=values):
                self.setUp()
                self.begin()
                self.wf.motion_sent(7, "to_zero", self.clock.now)
                for value in values:
                    self.clock.advance(0.3)
                    self.wf.position(value, self.clock.now)
                self.assertEqual(self.wf.phase, "to_zero")
                target = values[-1] if abs(values[-1]) <= 0.01 else 0
                self.clock.advance(0.3)
                self.wf.position(target, self.clock.now)
                self.assertEqual(self.wf.phase, "to_zero")
                self.clock.advance(0.3)
                self.wf.position(target, self.clock.now)
                # 离开零点的帧不能成为新窗口首帧。
                if values[-1] == 0.03:
                    self.assertEqual(self.wf.phase, "to_zero")
                    self.clock.advance(0.3)
                    self.wf.position(target, self.clock.now)
                self.assertEqual(self.wf.phase, "reset")

    def test_settle_span_error_and_coordinate_range_boundaries_are_inclusive(self):
        # 使用可精确二进制表示的数值，避免边界测试依赖浮点舍入。
        for values in ((0.125, 0.125, 0.125), (-0.0625, 0, 0.0625)):
            with self.subTest(values=values):
                self.setUp()
                self.make_workflow(tolerance=0.25, settle_tolerance=0.125)
                self.begin()
                self.wf.motion_sent(7, "to_zero", self.clock.now)
                for index, value in enumerate(values):
                    self.clock.advance(0.25)
                    self.wf.position(value, self.clock.now)
                    if index < 2:
                        self.assertEqual(self.wf.phase, "to_zero")
                self.assertEqual(self.wf.phase, "reset")

    def test_settle_error_uses_smaller_motion_and_settle_tolerance(self):
        for tolerance, settle_tolerance in ((0.1, 0.002), (0.002, 0.01)):
            with self.subTest(tolerance=tolerance, settle_tolerance=settle_tolerance):
                self.setUp()
                self.make_workflow(tolerance=tolerance, settle_tolerance=settle_tolerance)
                self.begin()
                self.wf.motion_sent(7, "to_zero", self.clock.now)
                self.stable_positions(0.005)
                self.assertEqual(self.wf.phase, "to_zero")
                self.stable_positions()
                self.assertEqual(self.wf.phase, "reset")

    def test_deadline_checked_even_if_ui_timeout_not_yet_dispatched(self):
        self.wf.start()
        self.clock.now += 6
        self.ack("ENCODER_RESET_BEGIN")
        self.assertNotIn(("PLC", 0), self.events)
        self.assertFalse(self.wf.active)


class FakeSender:
    def __init__(self, clock, events):
        self.clock, self.events = clock, events
        self.sock = object()
        self.connected = True
        self.idle = True
        self.commands = []

    def send_command(self, cmd, safety=None, on_sent=None, sent_event=None):
        self.commands.append((cmd, safety, on_sent, self.sock, sent_event))
        return True

    def flush(self):
        for cmd, safety, callback, sock, sent_event in self.commands:
            if self.connected and sock is self.sock and (safety is None or safety.is_set()):
                if sent_event is not None:
                    sent_event.set()
                self.events.append(("PLC", cmd))
                if callback:
                    callback(self.clock.now)
        self.commands.clear()

    def revoke_guard(self, safety):
        safety.clear()

    def cancel_guarded_motion(self, safety, sent_event, expected_sock):
        safety.clear()
        if self.connected and self.sock is expected_sock and sent_event.is_set():
            self.events.append(("PLC", "q"))

    def send_urgent(self, cmd):
        self.commands.clear()
        self.events.append(("PLC", cmd))

    def disconnect(self):
        self.connected = False
        self.sock = None


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.events = []
        w = self.w = Window.__new__(Window)
        defaults = dict(_reset_workflow=None, _reset_safety=None, _reset_widget_states=[],
                        _reset_gate=threading.Event(), _reset_motion_sent=None, _encoder_token=6,
                        _reset_channel_guard=None, _scan_event_channel=None,
                        _maintenance_pending=set(), _pending={}, _retry_timers={},
                        _auto_active=False, flash_on=False, _press_ink_repeat_enabled=False,
                        _press_ink_repeat_after=None, _flash_pausing=False, _flash_wait_off=False,
                        _flash_after=None, _flash_wait_after=None, _server_stop_after=None,
                        positions={"X": 0, "Y": 0, "Z": 0}, _closing=False,
                        _ui_queue=queue.Queue(), _workflow_paused=False,
                        auto_uv_var=mock.Mock(get=lambda: False))
        for key, value in defaults.items():
            setattr(w, key, value)
        w.sender = FakeSender(self.clock, self.events)
        w._plc_status_socket = w.sender.sock
        w.scan = types.SimpleNamespace(sock=object(), connected=True, ready=True,
                                       _lock=threading.Lock())
        w._scan_event_channel = w.scan.sock
        w.scan._send_on_socket = lambda sock, line: (
            self.events.append(("MFC", line.strip())) or True
            if sock is w.scan.sock and w.scan.connected else False)
        w.scan.disconnect = mock.Mock(side_effect=lambda: setattr(w.scan, "connected", False))
        w.scan.close = w.scan.disconnect
        w.after = self.clock.schedule
        w.after_cancel = self.clock.cancel
        w._post_ui = lambda callback, *args: callback(*args)
        for name in ("_append_log", "_set_reset_lock", "_refresh_position",
                     "_enqueue_event", "_cancel_all_axis_retries", "_cancel_flash_pause",
                     "_reset_workflow_pause", "_unlock_all", "_set_press_ink_repeat",
                     "_update_job_progress", "_update_pass_progress", "_stop_http_api", "destroy",
                     "_update_auto_counts", "_set_axis_enabled", "_start_prehome",
                     "_unlock_auto_group", "_play_finish_sound", "_remove_event"):
            setattr(w, name, mock.Mock())
        for name in ("status_var", "uv_btn", "roller_btn", "last_report_var", "connect_btn",
                     "scan_btn", "scan_ready_var", "event_list", "event_count_var",
                     "cycle_info_var", "eta_var", "pause_btn"):
            setattr(w, name, mock.Mock())
        w.event_queue = []
        self.workflow_factory = mock.Mock(side_effect=lambda **kw:
            EncoderResetWorkflow(**{**kw, "clock": lambda: self.clock.now, "settle_ms": 100}))
        self.factory = mock.patch.dict(NS, EncoderResetWorkflow=self.workflow_factory)
        self.factory.start()
        self.addCleanup(self.factory.stop)

    def start(self):
        self.w._start_encoder_reset()
        self.assertTrue(self.w._encoder_active())

    def response(self, line, channel=None):
        self.w._on_scan_response(line, channel or self.w.scan.sock)

    def pos(self, x, received_at=None, channel=None):
        self.w._on_status(f"POS:X={x},Y=0,Z=0,D=0,G=0",
                          channel or self.w.sender.sock,
                          self.clock.now if received_at is None else received_at)

    def stable_pos(self, x=None):
        x = NS["X_ZERO"] if x is None else x
        for _ in range(3):
            self.clock.advance(0.06)
            self.pos(x)

    def enter_reset(self):
        self.start()
        self.response("OK ENCODER_RESET_BEGIN TOKEN=7")
        self.w.sender.flush()
        self.stable_pos()
        self.assertEqual(self.w._reset_workflow.phase, "reset")

    def test_full_integration_actual_send_then_new_pos(self):
        self.start()
        self.assertFalse(self.w.sender.commands)
        self.response("OK ENCODER_RESET_BEGIN TOKEN=7")
        self.assertEqual(self.events, [("MFC", "ENCODER_RESET_BEGIN TOKEN=7")])
        self.pos(0)
        self.assertEqual(self.w._reset_workflow.phase, "to_zero")
        self.w.sender.flush()
        self.pos(0, received_at=0.5)  # 延迟派发旧 POS
        self.assertEqual(self.w._reset_workflow.phase, "to_zero")
        self.stable_pos()
        self.response("OK RESET_ENCODER TOKEN=7 ENC=1048576")
        self.assertEqual(self.w._reset_workflow.phase, "reset")
        self.assertFalse(self.w.sender.commands)
        self.clock.advance(2)
        self.w.sender.flush()
        self.pos(-5, received_at=0.5)
        self.assertEqual(self.w._reset_workflow.phase, "to_return")
        self.clock.advance(0.1)
        self.pos(-5)
        self.assertTrue(self.w._encoder_active())
        self.response("OK ENCODER_RESET_END TOKEN=7")
        self.assertFalse(self.w._encoder_active())
        self.assertEqual(self.events, [("MFC", "ENCODER_RESET_BEGIN TOKEN=7"),
            ("PLC", "X=0"), ("MFC", "RESET_ENCODER TOKEN=7"), ("PLC", "X=-5"),
            ("MFC", "ENCODER_RESET_END TOKEN=7")])
        self.w._set_reset_lock.assert_has_calls([mock.call(True), mock.call(False)])
        self.assertFalse(self.clock.timers)

    def test_configured_targets_are_passed_as_keywords_and_formatted_for_motion(self):
        for zero, home, zero_cmd, home_cmd in (
                (2, -3, "X=2", "X=-3"),
                (12.25, 3.75, "X=12.25", "X=3.75")):
            with self.subTest(zero=zero, home=home):
                self.setUp()
                with mock.patch.dict(NS, X_ZERO=zero, X_HOME=home):
                    self.enter_reset()
                    kwargs = self.workflow_factory.call_args.kwargs
                    self.assertEqual(kwargs["zero_position"], zero)
                    self.assertEqual(kwargs["home_position"], home)
                    self.assertIn(("PLC", zero_cmd), self.events)
                    self.clock.advance(2)  # 无 RESET OK 也必须回程。
                    self.w.sender.flush()
                    self.assertIn(("PLC", home_cmd), self.events)
                    self.clock.advance(0.01)
                    self.pos(home)
                    self.assertEqual(self.w._reset_workflow.phase, "end")
                    self.response("OK ENCODER_RESET_END TOKEN=7")
                    self.assertFalse(self.w._encoder_active())
                    self.assertFalse(self.clock.timers)

    def test_configured_target_out_of_range_never_sends_begin(self):
        for zero, home in ((231, -5), (0, -11)):
            with self.subTest(zero=zero, home=home):
                with mock.patch.dict(NS, X_ZERO=zero, X_HOME=home):
                    self.w._start_encoder_reset()
                self.assertFalse(self.events)
                self.assertFalse(self.w.sender.commands)

    def test_reset_wait_without_ack_and_repeated_ok_cannot_queue_return_early(self):
        self.enter_reset()
        for extra in ("", " ENC=0", " ENC=999"):
            self.response(f"OK RESET_ENCODER TOKEN=7{extra}")
        self.clock.advance(1.9)
        self.assertEqual(self.w._reset_workflow.phase, "reset")
        self.assertFalse(self.w.sender.commands)
        self.clock.advance(0.11)
        self.assertEqual(self.w._reset_workflow.phase, "to_return")
        self.assertEqual(len(self.w.sender.commands), 1)
        self.assertEqual(self.w.sender.commands[0][0], "X=-5")

    def test_stop_during_reset_cancels_wait_and_stale_timer_cannot_queue_return(self):
        self.enter_reset()
        stale_timer = next(iter(self.clock.timers.values()))[1]
        self.w._emergency_stop()
        self.assertFalse(self.w._encoder_active())
        self.assertFalse(self.clock.timers)
        previous = list(self.events)
        stale_timer()
        self.clock.advance(3)
        self.w.sender.flush()
        self.assertEqual(self.events, previous)
        self.assertNotIn(("PLC", "X=-5"), self.events)

    def test_reset_wait_checks_ready_and_bound_channels_before_queueing_return(self):
        for change in ("plc_lost", "mfc_lost", "not_ready", "plc_replaced", "mfc_replaced"):
            with self.subTest(change=change):
                self.setUp()
                self.enter_reset()
                if change == "plc_lost":
                    self.w.sender.connected = False
                elif change == "mfc_lost":
                    self.w.scan.connected = False
                elif change == "not_ready":
                    self.w.scan.ready = False
                elif change == "plc_replaced":
                    self.w.sender.sock = object()
                else:
                    self.w.scan.sock = object()
                self.clock.advance(2)
                self.assertFalse(self.w._encoder_active())
                self.assertFalse(self.w.sender.commands)
                self.assertNotIn(("PLC", "X=-5"), self.events)
                self.assertFalse(self.clock.timers)

    def test_busy_gates_and_range_no_begin_or_motion(self):
        for key, value in (("_auto_active", True), ("_pending", {"Y": 1}),
                           ("_maintenance_pending", {"PRESS_INK"}), ("flash_on", True),
                           ("_press_ink_repeat_enabled", True), ("_flash_pausing", True),
                           ("_press_ink_repeat_after", 1), ("_flash_after", 1),
                           ("_server_stop_after", 1)):
            with self.subTest(key=key):
                old = getattr(self.w, key)
                setattr(self.w, key, value)
                self.w._start_encoder_reset()
                self.assertFalse(self.events)
                setattr(self.w, key, old)
        self.w.sender.idle = False
        self.w._start_encoder_reset()
        self.assertFalse(self.events)
        self.w.sender.idle = True
        with mock.patch.dict(NS, AXIS_LIMITS={"X": (-4, 230)}):
            self.w._start_encoder_reset()
        self.assertFalse(self.events)

    def test_connection_ready_and_real_pos_required_before_begin(self):
        for owner, key, value in ((self.w.sender, "connected", False),
                                  (self.w.scan, "connected", False),
                                  (self.w.scan, "ready", False),
                                  (self.w, "_plc_status_socket", None)):
            old = getattr(owner, key)
            setattr(owner, key, value)
            self.w._start_encoder_reset()
            self.assertFalse(self.events)
            setattr(owner, key, old)

    def test_manual_moves_and_pending_arrival_cannot_change_reset_target(self):
        self.start()
        self.response("OK ENCODER_RESET_BEGIN TOKEN=7")
        commands = list(self.w.sender.commands)
        self.w._move_absolute("X")
        self.w._move_to("Y", 50)
        self.w._jog("Z", 1)
        self.w._wait_target("X", 100)
        self.w._check_arrival()
        self.assertEqual(self.w.sender.commands, commands)
        self.assertFalse(self.w._pending)
        self.w._toggle_workflow_pause()
        self.assertFalse(self.w._workflow_paused)
        self.w._start_encoder_reset()
        self.assertEqual(len(self.events), 1)

    def test_print_events_abort_without_entering_print_state_machine(self):
        for line in ("EVENT START_JOB TOTAL_LAYERS=2", "EVENT LAYER_START LAYER=1",
                     "EVENT PASS_READY CURRENT=1 TOTAL=2 STEP=1000 EMPTY=0",
                     "EVENT PRINT_JOB_COMPLETED TOTAL_LAYERS=2"):
            with self.subTest(line=line):
                self.setUp()
                self.start()
                self.response("OK ENCODER_RESET_BEGIN TOKEN=7")
                self.w.sender.flush()
                self.response(line)
                self.assertFalse(self.w._encoder_active())
                self.assertFalse(self.w._auto_active)
                self.assertFalse(self.w._pending)
                self.w._enqueue_event.assert_not_called()
                self.assertEqual(self.events[-2:], [("PLC", "q"),
                                 ("MFC", "ENCODER_RESET_CANCEL TOKEN=7")])

    def test_direct_start_job_after_reservation_is_also_guarded(self):
        self.start()
        self.response("OK ENCODER_RESET_BEGIN TOKEN=7")
        self.w._start_job(2)
        self.assertFalse(self.w._auto_active)
        self.assertFalse(self.w._encoder_active())

    def test_start_job_before_begin_ack_yields_without_q_then_original_print_logic(self):
        self.start()
        self.assertTrue(self.w._reset_gate.is_set())
        self.response("EVENT START_JOB TOTAL_LAYERS=2")
        self.assertFalse(self.w._encoder_active())
        self.assertFalse(self.w._reset_gate.is_set())
        self.assertTrue(self.w._auto_active)
        self.assertEqual(self.w._auto_mode, "server")
        self.assertEqual(self.w._job_total_layers, 2)
        self.w._start_prehome.assert_called_once()
        self.assertNotIn(("PLC", "q"), self.events)
        self.assertFalse(self.w.sender.commands)
        self.assertEqual(self.events[-1], ("MFC", "ENCODER_RESET_CANCEL TOKEN=7"))
        self.response("OK ENCODER_RESET_BEGIN TOKEN=7")
        self.assertFalse(self.w.sender.commands)

    def test_direct_start_job_before_begin_ack_also_yields(self):
        self.start()
        self.w._start_job(2)
        self.assertTrue(self.w._auto_active)
        self.w._start_prehome.assert_called_once()
        self.assertNotIn(("PLC", "q"), self.events)

    def test_begin_error_or_timeout_never_q_and_reopens_ping_gate(self):
        for action in ("busy", "timeout", "disconnect"):
            with self.subTest(action=action):
                self.setUp()
                self.start()
                if action == "busy":
                    self.response("ERR ENCODER_RESET_BEGIN TOKEN=7 REASON=BUSY")
                elif action == "timeout":
                    self.clock.advance(6)
                else:
                    self.w.sender.connected = False
                    self.w._on_state_change(False, "lost")
                self.assertFalse(self.w._encoder_active())
                self.assertFalse(self.w._reset_gate.is_set())
                self.assertNotIn(("PLC", "q"), self.events)
                self.assertFalse(self.clock.timers)

    def test_reserved_but_unsent_start_job_is_rejected_without_q(self):
        self.start()
        self.response("OK ENCODER_RESET_BEGIN TOKEN=7")
        self.response("EVENT START_JOB TOTAL_LAYERS=2")
        self.w.sender.flush()
        self.assertFalse(self.w._auto_active)
        self.assertNotIn(("PLC", "q"), self.events)
        self.assertNotIn(("PLC", "X=0"), self.events)

    def test_sent_motion_callback_not_dispatched_still_q_on_abort(self):
        self.start()
        self.response("OK ENCODER_RESET_BEGIN TOKEN=7")
        self.w._post_ui = types.MethodType(Window._post_ui, self.w)
        self.w.sender.flush()  # 发送标记已写入，但 Tk 尚未消费发送回调。
        self.assertFalse(self.w._reset_workflow.motion_started)
        self.w._abort_encoder_reset("stop")
        self.assertIn(("PLC", "q"), self.events)
        self.assertFalse(self.clock.timers)

    def test_wrong_socket_reply_or_pos_never_advances(self):
        self.start()
        self.response("OK ENCODER_RESET_BEGIN TOKEN=7", channel=object())
        self.assertFalse(self.w.sender.commands)
        self.response("OK ENCODER_RESET_BEGIN TOKEN=7")
        self.w.sender.flush()
        wrong_channel = object()
        for _ in range(3):
            self.clock.advance(0.06)
            self.pos(0, channel=wrong_channel)
        self.assertEqual(self.w._reset_workflow.phase, "to_zero")
        self.assertNotIn(("MFC", "RESET_ENCODER TOKEN=7"), self.events)
        self.stable_pos()
        self.assertEqual(self.w._reset_workflow.phase, "reset")

    def test_queued_motion_is_cancelled_before_flush_on_stop(self):
        self.start()
        self.response("OK ENCODER_RESET_BEGIN TOKEN=7")
        safety = self.w._reset_safety
        self.w._emergency_stop()
        self.w.sender.flush()
        self.assertFalse(safety.is_set())
        self.assertNotIn(("PLC", "X=0"), self.events)
        self.assertFalse(self.w._encoder_active())
        self.assertFalse(self.clock.timers)
        self.response("OK RESET_ENCODER TOKEN=7 ENC=1048576")
        self.assertFalse(self.w.sender.commands)

    def test_disconnect_callbacks_and_manual_disconnect_cancel(self):
        for target in ("plc_callback", "mfc_callback", "plc_manual", "mfc_manual"):
            with self.subTest(target=target):
                self.setUp()
                self.start()
                self.response("OK ENCODER_RESET_BEGIN TOKEN=7")
                self.w.sender.flush()
                if target == "plc_callback":
                    self.w.sender.connected = False
                    self.w._on_state_change(False, "lost")
                elif target == "mfc_callback":
                    self.w.scan.connected = False
                    self.w._on_scan_state(False, "lost")
                elif target == "plc_manual":
                    self.w._toggle_connect()
                else:
                    self.w._toggle_scan()
                self.assertFalse(self.w._encoder_active())
                self.assertFalse(self.clock.timers)
                self.assertEqual(("PLC", "q") in self.events, target != "plc_callback")
                if target != "mfc_callback":
                    self.assertIn(("MFC", "ENCODER_RESET_CANCEL TOKEN=7"), self.events)

    def test_custom_and_maintenance_commands_blocked_during_reset(self):
        self.start()
        self.w.custom_cmd_var = mock.Mock(get=lambda: "X=100")
        self.w._send_custom_cmd()
        self.w._send_flash()
        self.w._send_press_ink()
        self.w._toggle_press_ink_repeat()
        self.assertEqual(len(self.events), 1)
        self.assertFalse(self.w.sender.commands)

    def test_close_aborts_and_clears_timer_before_destroy(self):
        self.start()
        self.w._on_close()
        self.assertFalse(self.w._encoder_active())
        self.assertTrue(self.w._closing)
        self.assertFalse(self.clock.timers)
        self.assertEqual(self.events[-1:], [("MFC", "ENCODER_RESET_CANCEL TOKEN=7")])
        self.assertNotIn(("PLC", "q"), self.events)
        self.w.destroy.assert_called_once()

    def test_new_workflow_keeps_monotonic_token_and_old_reply_cannot_move(self):
        self.start()
        self.w._abort_encoder_reset("cancel")
        self.start()
        self.assertEqual(self.w._reset_workflow.token, 8)
        self.response("OK ENCODER_RESET_BEGIN TOKEN=7")
        self.assertFalse(self.w.sender.commands)
        self.response("OK ENCODER_RESET_BEGIN TOKEN=8")
        self.assertEqual(len(self.w.sender.commands), 1)

    def drain_ui(self):
        while not self.w._ui_queue.empty():
            callback, args = self.w._ui_queue.get_nowait()
            callback(*args)

    def real_scan(self):
        self.w.scan = ScanClient(
            self.w._on_scan_state, self.w._on_scan_ready, self.w._on_scan_response,
            on_channel_state=self.w._on_scan_state,
            on_channel_invalidated=self.w._invalidate_reset_channel)
        self.w.scan.on_channel_response = self.w._on_scan_response
        sock = mock.Mock()
        self.w.scan.sock = sock
        self.w.scan.connected = self.w.scan.ready = True
        self.w._scan_event_channel = sock
        return sock

    def finite_sender(self):
        sender = self.w.sender
        items = []
        while not sender._queue.empty():
            items.append(sender._queue.get_nowait())
        sender._queue = mock.Mock()
        sender._queue.get.side_effect = items + [StopIteration]

        def run():
            try:
                sender._send_loop()
            except StopIteration:
                pass
        return run

    def test_eof_revokes_queued_x_before_tk_abort_without_tk_in_hook(self):
        self.w.sender = TcpSender(self.w._on_sent, self.w._on_state_change, self.w._on_status)
        plc = self.w.sender.sock = mock.Mock()
        self.w.sender.connected = True
        self.w._plc_status_socket = plc
        mfc = self.real_scan()
        self.start()
        self.response("OK ENCODER_RESET_BEGIN TOKEN=7")
        safety = self.w._reset_safety
        self.w._post_ui = types.MethodType(Window._post_ui, self.w)
        log_calls = self.w._append_log.call_count
        timers = dict(self.clock.timers)
        mfc.recv.return_value = b""
        sender_run = self.finite_sender()
        eof_done = threading.Event()

        def receive():
            self.w.scan._recv_loop(mfc)
            eof_done.set()

        def send():
            if eof_done.wait(2):
                sender_run()

        receiver = threading.Thread(target=receive)
        worker = threading.Thread(target=send)
        receiver.start()
        worker.start()
        receiver.join(2)
        worker.join(2)
        self.assertFalse(receiver.is_alive())
        self.assertFalse(worker.is_alive())
        self.assertTrue(eof_done.is_set())
        self.assertIsNone(self.w.scan.sock)
        self.assertTrue(self.w._encoder_active())  # Tk 尚未消费断开边界。
        self.assertFalse(safety.is_set())
        plc.sendall.assert_not_called()
        self.assertEqual(self.w._append_log.call_count, log_calls)
        self.assertEqual(self.clock.timers, timers)
        self.drain_ui()
        self.assertFalse(self.w._encoder_active())
        self.assertFalse(self.clock.timers)
        plc.sendall.assert_not_called()

    def test_two_threads_post_pos_before_sent_callback_still_require_stable_window(self):
        self.w.sender = TcpSender(self.w._on_sent, self.w._on_state_change, self.w._on_status)
        plc = self.w.sender.sock = mock.Mock()
        self.w.sender.connected = True
        self.w._plc_status_socket = plc
        self.start()
        self.response("OK ENCODER_RESET_BEGIN TOKEN=7")
        posted_pos, sent_known = threading.Event(), threading.Event()
        stamp = []
        post = types.MethodType(Window._post_ui, self.w)
        wf = self.w._reset_workflow

        def reversed_post(callback, *args):
            if callback == wf.motion_sent:
                stamp.append(args[2])
                sent_known.set()
                if not posted_pos.wait(2):
                    raise AssertionError("POS thread did not post")
            post(callback, *args)

        self.w._post_ui = reversed_post
        send_once = self.finite_sender()

        def sender_run():
            # 真实发送器和工作流使用同一 fake 单调时钟。
            with mock.patch.object(time, "monotonic", return_value=self.clock.now):
                send_once()

        def receive():
            if sent_known.wait(2):
                self.clock.now = stamp[0] + 0.001
                self.w._on_status("POS:X=0,Y=0,Z=0,D=0,G=0", plc, self.clock.now)
                posted_pos.set()

        receiver = threading.Thread(target=receive)
        worker = threading.Thread(target=sender_run)
        receiver.start()
        worker.start()
        receiver.join(2)
        worker.join(2)
        self.assertFalse(receiver.is_alive())
        self.assertFalse(worker.is_alive())
        self.assertTrue(posted_pos.is_set())
        self.assertIsNone(wf.sent_at)
        self.assertEqual(wf.phase, "to_zero")
        callback, args = self.w._ui_queue.get_nowait()
        callback(*args)  # 第一个真实 POS 先入 UI，暂存本阶段候选。
        self.assertIsNotNone(wf.position_candidate)
        self.assertEqual(wf.phase, "to_zero")
        self.drain_ui()
        self.assertEqual(wf.phase, "to_zero")
        self.assertNotIn(("MFC", "RESET_ENCODER TOKEN=7"), self.events)
        self.assertIsNone(wf.position_candidate)
        self.w._post_ui = lambda callback, *args: callback(*args)
        self.stable_pos()
        self.assertEqual(wf.phase, "reset")
        self.assertIn(("MFC", "RESET_ENCODER TOKEN=7"), self.events)

    def test_completion_immediately_before_eof_is_consumed_before_disconnect(self):
        mfc = self.real_scan()
        self.w._auto_active, self.w._auto_mode = True, "server"
        self.w._post_ui = types.MethodType(Window._post_ui, self.w)
        self.w._scan_event_channel = None
        self.w._on_scan_state(True, "connected", mfc)
        mfc.recv.side_effect = [b"EVENT PRINT_JOB_COMPLETED TOTAL_LAYERS=2\r\n", b""]
        worker = threading.Thread(target=self.w.scan._recv_loop, args=(mfc,))
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(self.w.scan.sock)
        self.assertTrue(self.w._auto_active)
        self.drain_ui()
        self.assertFalse(self.w._auto_active)
        self.assertIsNone(self.w._auto_mode)
        self.assertIsNone(self.w._scan_event_channel)
        self.w._play_finish_sound.assert_called_once()

    def test_fast_reconnect_orders_old_completion_before_new_start_and_filters_old_events(self):
        old = self.real_scan()
        self.w._auto_active, self.w._auto_mode = True, "server"
        self.w._post_ui = types.MethodType(Window._post_ui, self.w)
        self.w._scan_event_channel = None
        self.w._on_scan_state(True, "old connected", old)
        old.recv.side_effect = [b"EVENT PRINT_JOB_COMPLETED TOTAL_LAYERS=2\r\n", b""]
        self.w.scan._recv_loop(old)
        new = mock.Mock()
        with self.w.scan._lock:
            self.w.scan.sock = new
            self.w.scan.connected = self.w.scan.ready = True
            self.w.scan.on_channel_state(True, "new connected", new)
            self.w.scan.on_channel_response("EVENT START_JOB TOTAL_LAYERS=3", new)
        self.w._on_scan_state(False, "delayed old False", old)
        self.w._on_scan_response("EVENT PRINT_JOB_COMPLETED TOTAL_LAYERS=2", old)
        self.drain_ui()
        self.w._play_finish_sound.assert_called_once()
        self.w._start_prehome.assert_called_once()
        self.assertTrue(self.w._auto_active)
        self.assertEqual(self.w._job_total_layers, 3)
        self.assertIs(self.w._scan_event_channel, new)

    def test_complete_reset_ack_queued_before_eof_cannot_return_on_reconnect(self):
        old = self.real_scan()
        self.start()
        self.response("OK ENCODER_RESET_BEGIN TOKEN=7")
        self.w.sender.flush()
        self.stable_pos()
        self.assertEqual(self.w._reset_workflow.phase, "reset")
        stale_reset_timer = next(iter(self.clock.timers.values()))[1]
        self.w._post_ui = types.MethodType(Window._post_ui, self.w)
        old.recv.side_effect = [b"OK RESET_ENCODER TOKEN=7 ENC=1048576\r\n", b""]
        self.w.scan._recv_loop(old)
        new = mock.Mock()
        with self.w.scan._lock:
            self.w.scan.sock = new
            self.w.scan.connected = self.w.scan.ready = True
            self.w.scan.on_channel_state(True, "new connected", new)
        self.drain_ui()
        self.assertFalse(self.w._encoder_active())
        self.assertFalse(self.w.sender.commands)
        self.assertNotIn(("PLC", "X=-5"), self.events)
        self.assertFalse(self.clock.timers)
        stale_reset_timer()
        self.clock.advance(3)
        self.assertFalse(self.w.sender.commands)
        self.assertIs(self.w._scan_event_channel, new)
        self.w._post_ui = lambda callback, *args: callback(*args)
        self.start()
        self.response("OK ENCODER_RESET_BEGIN TOKEN=7", channel=old)
        self.assertFalse(self.w.sender.commands)
        self.response("OK ENCODER_RESET_BEGIN TOKEN=8", channel=new)
        self.assertEqual(len(self.w.sender.commands), 1)

    def test_ui_lock_covers_axis_parameters_and_maintenance_but_not_estop(self):
        class Widget:
            def __init__(self, state=None, children=()):
                self.state = state
                self.children = children

            def keys(self):
                return ["state"] if self.state is not None else []

            def cget(self, key):
                return self.state

            def configure(self, **kw):
                self.state = kw["state"]

            def winfo_children(self):
                return self.children

        controls = [Widget("normal") for _ in range(6)]
        controls.append(Widget("readonly"))
        self.w.estop_btn = Widget("normal")
        self.w.pause_btn = Widget("normal")
        self.w._axis_frame = Widget(children=controls[:3])
        self.w._device_frame = Widget(children=controls[3:] + [self.w.estop_btn])
        self.w._set_reset_lock = types.MethodType(Window._set_reset_lock, self.w)
        self.start()
        self.assertTrue(all(c.state == "disabled" for c in controls))
        self.assertEqual(self.w.estop_btn.state, "normal")
        self.assertEqual(self.w.pause_btn.state, "disabled")
        self.w._abort_encoder_reset("test")
        self.assertTrue(all(c.state == "normal" for c in controls[:-1]))
        self.assertEqual(controls[-1].state, "readonly")
        self.assertEqual(self.w.pause_btn.state, "normal")
        self.assertFalse(self.w._reset_widget_states)

    def test_thread_callbacks_only_enqueue_no_tk_calls(self):
        self.w._post_ui = types.MethodType(Window._post_ui, self.w)
        worker = threading.Thread(target=lambda: (
            self.w._on_scan_response("OK RESET_ENCODER TOKEN=7 ENC=1048576"),
            self.w._on_status("POS:X=0,Y=0,Z=0,D=0,G=0"),
            self.w._on_scan_state(True, "connected"), self.w._on_state_change(True, "connected"),
            self.w._on_sent("X=0")))
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.w._ui_queue.qsize(), 5)
        self.w.after = mock.Mock(side_effect=AssertionError("接收线程不能调用 Tk.after"))
        self.w._append_log.assert_not_called()
        self.w.status_var.set.assert_not_called()


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.sender = TcpSender(mock.Mock(), mock.Mock(), mock.Mock())
        self.sender.sock = mock.Mock()
        self.sender.connected = True

    def test_guarded_queue_is_not_a_send_ack_and_urgent_clears_it(self):
        safety = threading.Event()
        safety.set()
        callback = mock.Mock()
        self.assertTrue(self.sender.send_command("X=0", safety, callback))
        self.assertFalse(self.sender.idle)
        self.sender.sock.sendall.assert_not_called()
        callback.assert_not_called()
        safety.clear()
        self.sender.send_urgent("q")
        self.sender.sock.sendall.assert_called_once_with(b"q")
        self.assertTrue(self.sender.idle)
        callback.assert_not_called()

    def test_dequeued_cancelled_or_old_socket_motion_is_dropped(self):
        for mode in ("cancel", "socket", "valid"):
            with self.subTest(mode=mode):
                self.setUp()
                safety = threading.Event()
                safety.set()
                callback = mock.Mock()
                self.sender.send_command("X=0", safety, callback)
                if mode == "cancel":
                    safety.clear()
                elif mode == "socket":
                    self.sender.sock = mock.Mock()
                # 不开线程；单次运行真实 worker，第二次 get 退出测试。
                item = self.sender._queue.get_nowait()
                self.sender._queue = mock.Mock()
                self.sender._queue.get.side_effect = [item, StopIteration]
                with self.assertRaises(StopIteration):
                    self.sender._send_loop()
                if mode == "valid":
                    self.sender.sock.sendall.assert_called_once_with(b"X=0")
                    callback.assert_called_once()
                else:
                    self.sender.sock.sendall.assert_not_called()
                    callback.assert_not_called()
                self.assertTrue(self.sender.idle)

    def test_partial_pos_keeps_first_receipt_time_and_next_frame_is_fresh(self):
        sock = self.sender.sock
        sock.recv.side_effect = [b"POS:X=0,Y=0", b",Z=0,D=0,G=0\r\n",
                                 b"POS:X=-5,Y=0,Z=0,D=0,G=0\r\n", b""]
        with mock.patch.object(time, "monotonic", side_effect=[1, 2, 3, 4]):
            self.sender._recv_loop(sock)
        self.assertEqual(self.sender.on_receive.call_args_list, [
            mock.call("POS:X=0,Y=0,Z=0,D=0,G=0", sock, 1),
            mock.call("POS:X=-5,Y=0,Z=0,D=0,G=0", sock, 3)])

    def test_old_receive_eof_does_not_disconnect_new_plc_socket(self):
        old, current = self.sender.sock, mock.Mock()

        def recv(size):
            self.sender.sock = current
            return b""

        old.recv.side_effect = recv
        self.sender._recv_loop(old)
        self.assertIs(self.sender.sock, current)
        self.assertTrue(self.sender.connected)
        self.sender.on_state_change.assert_not_called()

    def test_cancel_guarded_does_not_stop_other_job_before_own_send(self):
        safety, sent = threading.Event(), threading.Event()
        safety.set()
        self.sender.send_command("X=0", safety=safety, sent_event=sent)
        self.sender.cancel_guarded_motion(safety, sent, self.sender.sock)
        self.sender.sock.sendall.assert_not_called()
        self.assertFalse(safety.is_set())
        sent.set()
        self.sender.cancel_guarded_motion(safety, sent, self.sender.sock)
        self.sender.sock.sendall.assert_called_once_with(b"q")

    def test_old_flow_cancel_neither_qs_new_plc_nor_clears_its_queue(self):
        old = self.sender.sock
        safety, sent, other = threading.Event(), threading.Event(), threading.Event()
        safety.set()
        sent.set()  # 旧 A 上已经发过本流程 X；还排有下一条 guarded X。
        other.set()
        self.sender.send_command("X=-5", safety=safety, sent_event=sent)
        self.sender.disconnect(expected_sock=old)
        new = self.sender.sock = mock.Mock()
        self.sender.connected = True
        self.sender.send_command("Y=3")
        self.sender.send_command("Z=4", safety=other)
        queued = list(self.sender._queue.queue)
        self.sender.cancel_guarded_motion(safety, sent, old)
        new.sendall.assert_not_called()
        old.sendall.assert_not_called()
        self.assertEqual(list(self.sender._queue.queue), queued)
        self.assertFalse(safety.is_set())
        self.assertTrue(other.is_set())
        self.sender._queue = mock.Mock()
        self.sender._queue.get.side_effect = queued + [StopIteration]
        with self.assertRaises(StopIteration):
            self.sender._send_loop()
        self.assertEqual(new.sendall.call_args_list, [mock.call(b"Y=3"), mock.call(b"Z=4")])
        self.assertTrue(self.sender.idle)

    def test_current_flow_cancel_only_qs_bound_socket_without_draining_other_items(self):
        safety, sent = threading.Event(), threading.Event()
        safety.set()
        sent.set()
        self.sender.send_command("X=-5", safety=safety, sent_event=sent)
        self.sender.send_command("Y=9")
        queued = list(self.sender._queue.queue)
        self.sender.cancel_guarded_motion(safety, sent, self.sender.sock)
        self.sender.sock.sendall.assert_called_once_with(b"q")
        self.assertEqual(list(self.sender._queue.queue), queued)
        self.sender._queue = mock.Mock()
        self.sender._queue.get.side_effect = queued + [StopIteration]
        with self.assertRaises(StopIteration):
            self.sender._send_loop()
        self.assertEqual(self.sender.sock.sendall.call_args_list,
                         [mock.call(b"q"), mock.call(b"Y=9")])
        self.assertTrue(self.sender.idle)

    def test_revoke_guard_waits_for_sender_lock_and_has_no_callbacks(self):
        safety = threading.Event()
        safety.set()
        started = threading.Event()

        def revoke():
            started.set()
            self.sender.revoke_guard(safety)

        with self.sender._lock:
            thread = threading.Thread(target=revoke)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertTrue(safety.is_set())
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(safety.is_set())
        self.sender.sock.sendall.assert_not_called()
        self.sender.on_sent.assert_not_called()
        self.sender.on_state_change.assert_not_called()

    def test_cancel_serializes_with_inflight_own_send_before_q(self):
        safety, sent = threading.Event(), threading.Event()
        safety.set()
        self.sender.send_command("X=0", safety=safety, sent_event=sent)
        item = self.sender._queue.get_nowait()
        self.sender._queue = mock.Mock()
        self.sender._queue.get.side_effect = [item, StopIteration]
        entered, release = threading.Event(), threading.Event()
        sends = []

        def send(data):
            sends.append(data)
            if data == b"X=0":
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test release timeout")

        self.sender.sock.sendall.side_effect = send

        def worker():
            try:
                self.sender._send_loop()
            except StopIteration:
                pass

        self.sender._queue.get_nowait.side_effect = queue.Empty
        thread = threading.Thread(target=worker)
        cancel = threading.Thread(target=lambda: self.sender.cancel_guarded_motion(
            safety, sent, self.sender.sock))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            cancel.start()
        finally:
            release.set()
            thread.join(2)
            if cancel.ident is not None:
                cancel.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(cancel.is_alive())
        self.assertEqual(sends, [b"X=0", b"q"])
        self.assertFalse(safety.is_set())

    def test_disconnected_or_cancelled_enqueue_is_rejected(self):
        safety = threading.Event()
        self.assertFalse(self.sender.send_command("X=0", safety))
        safety.set()
        self.sender.connected = False
        self.assertFalse(self.sender.send_command("X=0", safety))
        self.assertTrue(self.sender.idle)


if __name__ == "__main__":
    unittest.main()
