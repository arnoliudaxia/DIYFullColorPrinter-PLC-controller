"""无 Tk/硬件依赖的编码器维护状态机；所有入口由 UI 主线程调用。"""
import math
import re
import time

ACK_TIMEOUT_MS = 5000
MOTION_TIMEOUT_MS = 30000
SETTLE_MS = 500
SETTLE_MIN_SAMPLES = 3
SETTLE_TOLERANCE = 0.01
POSITION_MAX_AGE_SECONDS = 1.0
RESET_WAIT_MS = 2000


class EncoderResetWorkflow:
    def __init__(self, send, move, stop, ready, schedule, unschedule, finished,
                 tolerance=0.1, clock=time.monotonic, token_factory=None,
                 ack_timeout_ms=ACK_TIMEOUT_MS, motion_timeout_ms=MOTION_TIMEOUT_MS,
                 *, zero_position, home_position, settle_ms=SETTLE_MS,
                 settle_tolerance=SETTLE_TOLERANCE, reset_wait_ms=RESET_WAIT_MS,
                 progress=None):
        self.send = send
        self.move = move
        # stop 必须撤销队列，并只在本流程确已发送运动时 q；不能误停并发打印。
        self.stop = stop
        self.ready = ready
        self.schedule = schedule
        self.unschedule = unschedule
        self.finished = finished
        self.tolerance = tolerance
        self.clock = clock
        self.token_factory = token_factory
        self.ack_timeout_ms = ack_timeout_ms
        self.motion_timeout_ms = motion_timeout_ms
        self.zero_position = float(zero_position)
        self.home_position = float(home_position)
        self.settle_ms = settle_ms
        self.settle_tolerance = settle_tolerance
        self.reset_wait_ms = reset_wait_ms
        self.progress = progress
        if not all(math.isfinite(value) for value in (
                self.zero_position, self.home_position, tolerance, settle_tolerance,
                settle_ms, reset_wait_ms)) or min(
                    tolerance, settle_tolerance, settle_ms, reset_wait_ms) <= 0:
            raise ValueError("编码复位目标、容差及等待时间无效")
        self.last_position_at = None
        self.stable_since = None
        self.stable_min = None
        self.stable_max = None
        self.stable_samples = 0
        self.phase = None
        self.token = None
        self.timer = None
        self.sent_at = None
        self.position_candidate = None
        self.deadline = None
        self.last_token = 0
        self.motion_started = False

    @property
    def active(self):
        return self.phase is not None

    def start(self):
        if self.active or not self.ready():
            return False
        self.motion_started = False
        candidate = self.last_token + 1
        if self.token_factory is not None:
            candidate = max(candidate, int(self.token_factory()))
        # MFC TOKEN使用32位有符号LONG，不能使用time_ns等超范围编号。
        if not 1 <= candidate <= 2147483647:
            return False
        self.token = candidate
        self.last_token = self.token
        self._command("begin", "ENCODER_RESET_BEGIN")
        return True

    def _report(self, message):
        if self.progress is not None:
            self.progress(message)

    def _clear_stability(self):
        self.stable_since = None
        self.stable_min = None
        self.stable_max = None
        self.stable_samples = 0

    def _arm(self, phase, timeout):
        if self.timer is not None:
            self.unschedule(self.timer)
        self.phase = phase
        self.sent_at = None
        self.position_candidate = None
        self.last_position_at = None
        self._clear_stability()
        self.deadline = self.clock() + timeout / 1000
        token = self.token
        self.timer = self.schedule(timeout, lambda: self._timeout(token, phase))

    def _timeout(self, token, phase):
        if self.token != token or self.phase != phase:
            return
        if phase == "reset":
            # 这是固定等待完成，不是等待MFC成功回包；提前/迟到回调不能绕过2秒。
            if not self._check_live():
                return
            remaining = self.deadline - self.clock()
            if remaining > 0:
                self.timer = self.schedule(
                    max(1, math.ceil(remaining * 1000)),
                    lambda: self._timeout(token, phase))
                return
            self.timer = None
            self._motion("to_return", self.home_position)
        else:
            self.abort(f"{phase} 超时")

    def _command(self, phase, command):
        self._arm(phase, self.ack_timeout_ms)
        if not self.ready() or not self.send(f"{command} TOKEN={self.token}\r\n"):
            self.abort(f"{command} 发送失败/连接失效")
            return False
        return self.active

    def _reset_and_wait(self):
        if self._command("reset", "RESET_ENCODER") and self.phase == "reset":
            # send返回后才开始计时，不从排队/进入零点范围时算起。
            self._arm("reset", self.reset_wait_ms)
            self._report(
                f"复位命令已发送，等待 {self.reset_wait_ms / 1000:g} 秒后"
                f"返回起始位置 X={self.home_position:g}（不等待MFC成功回复）")

    def _motion(self, phase, target):
        self._arm(phase, self.motion_timeout_ms)
        if not self.ready() or not self.move(target, self.token, phase):
            self.abort("PLC 运动发送失败/连接失效")
        elif self.phase == phase:
            self._report(
                f"移动到零点位置 X={target:g}，等待连续新POS确认停稳"
                if phase == "to_zero" else f"返回起始位置 X={target:g}，等待到位")

    def _check_live(self):
        if not self.ready():
            self.abort("连接失效")
            return False
        if self.phase != "reset" and self.clock() >= self.deadline:
            self.abort(f"{self.phase} 超时")
            return False
        return True

    def motion_sent(self, token, phase, sent_at):
        if self.token == token and self.phase == phase and self._check_live():
            self.sent_at = sent_at
            self.motion_started = True
            candidate, self.position_candidate = self.position_candidate, None
            if candidate is not None:
                # 两个接收/发送线程入 UI 队列可反序，只重放本阶段实际接收的候选。
                self.position(*candidate)

    def position(self, x, received_at):
        if not self.active:
            return
        if not self._check_live():
            return
        # 只暂存本运动阶段真实接收的最新 POS，不读取 positions 缓存。
        if self.sent_at is None:
            if self.phase in ("to_zero", "to_return"):
                if (self.position_candidate is None
                        or received_at > self.position_candidate[1]):
                    self.position_candidate = (x, received_at)
            return
        # 缓存、初始化坐标、发送前接收但延迟派发的 POS 均不能推进。
        if received_at <= self.sent_at:
            return
        now = self.clock()
        if received_at > now or now - received_at > POSITION_MAX_AGE_SECONDS:
            self._clear_stability()
            return
        if self.last_position_at is not None:
            if received_at <= self.last_position_at:
                # 反序反馈可能揭示窗口内曾移动；废弃旧窗口但保留时间高水位。
                self._clear_stability()
                return
            if received_at - self.last_position_at > POSITION_MAX_AGE_SECONDS:
                self._clear_stability()
        self.last_position_at = received_at
        if not math.isfinite(x):
            self.abort("PLC位置上报无效")
            return
        if self.phase == "to_zero":
            # 协议没有运动停止位：用连续新反馈在目标附近保持稳定确认。
            zero_tolerance = min(self.tolerance, self.settle_tolerance)
            if abs(x - self.zero_position) > zero_tolerance + 1e-9:
                self._clear_stability()
                return
            if (self.stable_since is None
                    or max(self.stable_max, x) - min(self.stable_min, x)
                    > self.settle_tolerance + 1e-9):
                self.stable_since = received_at
                self.stable_min = self.stable_max = x
                self.stable_samples = 1
            else:
                self.stable_min = min(self.stable_min, x)
                self.stable_max = max(self.stable_max, x)
                self.stable_samples += 1
            if (self.stable_samples >= SETTLE_MIN_SAMPLES
                    and received_at - self.stable_since + 1e-9 >= self.settle_ms / 1000):
                self._report(f"零点位置 X={self.zero_position:g} 已通过连续POS停稳确认")
                self._reset_and_wait()
        elif self.phase == "to_return" and abs(x - self.home_position) <= self.tolerance:
            self._command("end", "ENCODER_RESET_END")

    def response(self, line):
        m = re.fullmatch(
            r"(OK|ERR) (ENCODER_RESET_BEGIN|RESET_ENCODER|ENCODER_RESET_END|"
            r"ENCODER_RESET_CANCEL) TOKEN=([1-9][0-9]*)(?: (.*))?", line.strip())
        if not m:
            return False
        if not self.active or int(m[3]) != self.token:
            return True
        if (m[1] == "ERR" and m[2] == "RESET_ENCODER"
                and self.phase in ("to_return", "end")):
            # 不等待成功回复，但返程期间迟到的明确失败也不能被吞掉。
            if self._check_live():
                self.abort(f"{m[2]} {m[4] or 'FAILED'}")
            return True
        expected = {"begin": "ENCODER_RESET_BEGIN", "reset": "RESET_ENCODER",
                    "end": "ENCODER_RESET_END"}.get(self.phase)
        if m[2] != expected:
            return True
        if not self._check_live():
            return True
        if m[1] == "ERR":
            self.abort(f"{m[2]} {m[4] or 'FAILED'}")
        elif self.phase == "begin" and m[4] is None:
            self._motion("to_zero", self.zero_position)
        elif self.phase == "reset":
            # 兼容现有MFC的OK回复，但不依赖它、也不让它提前触发返程。
            pass
        elif self.phase == "end" and m[4] is None:
            self._finish(True, f"编码器重置完成，起始位置 X={self.home_position:g} 已确认")
        else:
            self.abort("确认回复格式无效")
        return True

    def abort(self, reason):
        if not self.active:
            return
        token = self.token
        # 先失活，防止 stop/send 回调或迟到回包重入。
        self._clear()
        self.stop()
        self.send(f"ENCODER_RESET_CANCEL TOKEN={token}\r\n")
        self.finished(False, reason)

    def _clear(self):
        if self.timer is not None:
            self.unschedule(self.timer)
        self.timer = None
        self.phase = None
        self.token = None
        self.sent_at = None
        self.position_candidate = None
        self.last_position_at = None
        self._clear_stability()
        self.deadline = None

    def _finish(self, success, reason):
        self._clear()
        self.finished(success, reason)
