"""无 Tk/硬件依赖的编码器维护状态机；所有入口由 UI 主线程调用。"""
import re
import time

ACK_TIMEOUT_MS = 5000
MOTION_TIMEOUT_MS = 30000


class EncoderResetWorkflow:
    def __init__(self, send, move, stop, ready, schedule, unschedule, finished,
                 tolerance=0.1, clock=time.monotonic, token_factory=None,
                 ack_timeout_ms=ACK_TIMEOUT_MS, motion_timeout_ms=MOTION_TIMEOUT_MS):
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

    def _arm(self, phase, timeout):
        if self.timer is not None:
            self.unschedule(self.timer)
        self.phase = phase
        self.sent_at = None
        self.position_candidate = None
        self.deadline = self.clock() + timeout / 1000
        token = self.token
        self.timer = self.schedule(timeout, lambda: self._timeout(token, phase))

    def _timeout(self, token, phase):
        if self.token == token and self.phase == phase:
            self.abort(f"{phase} 超时")

    def _command(self, phase, command):
        self._arm(phase, self.ack_timeout_ms)
        if not self.ready() or not self.send(f"{command} TOKEN={self.token}\r\n"):
            self.abort(f"{command} 发送失败/连接失效")

    def _motion(self, phase, target):
        self._arm(phase, self.motion_timeout_ms)
        if not self.ready() or not self.move(target, self.token, phase):
            self.abort("PLC 运动发送失败/连接失效")

    def _check_live(self):
        if not self.ready():
            self.abort("连接失效")
            return False
        if self.clock() >= self.deadline:
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
        if self.phase == "to_zero" and abs(x) <= self.tolerance:
            self._command("reset", "RESET_ENCODER")
        elif self.phase == "to_return" and abs(x + 5) <= self.tolerance:
            self._command("end", "ENCODER_RESET_END")

    def response(self, line):
        m = re.fullmatch(
            r"(OK|ERR) (ENCODER_RESET_BEGIN|RESET_ENCODER|ENCODER_RESET_END|"
            r"ENCODER_RESET_CANCEL) TOKEN=([1-9][0-9]*)(?: (.*))?", line.strip())
        if not m:
            return False
        if not self.active or int(m[3]) != self.token:
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
            self._motion("to_zero", 0)
        elif self.phase == "reset":
            if m[4] == "ENC=1048576":
                self._motion("to_return", -5)
            else:
                self.abort("复位回复 ENC 无效")
        elif self.phase == "end" and m[4] is None:
            self._finish(True, "编码器重置完成，X=-5 已确认")
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
        self.deadline = None

    def _finish(self, success, reason):
        self._clear()
        self.finished(success, reason)
