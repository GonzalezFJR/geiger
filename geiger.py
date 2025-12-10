import os
import time
import math
import threading
from dataclasses import dataclass
from typing import Callable, Optional, List

# RPi.GPIO es el backend real porque YA te funciona
import RPi.GPIO as GPIO


@dataclass
class GeigerConfig:
    pin: int = 18
    verbose: bool = False
    mock: bool = False
    mock_rate: float = 5.0
    max_deltas: int = 2000
    max_series: int = 3600

    @classmethod
    def from_env(cls) -> "GeigerConfig":
        return cls(
            pin=int(os.getenv("GEIGER_PIN", "18")),
            verbose=os.getenv("GEIGER_VERBOSE", "0") == "1",
            mock=os.getenv("GEIGER_MOCK", "0") == "1",
            mock_rate=float(os.getenv("GEIGER_MOCK_RATE", "5.0")),
            max_deltas=int(os.getenv("GEIGER_MAX_DELTAS", "2000")),
            max_series=int(os.getenv("GEIGER_MAX_SERIES", "3600")),
        )


class GeigerState:
    def __init__(self, cfg: GeigerConfig):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.t0 = time.time()
            self.total = 0
            self.last_ts: Optional[float] = None
            self.deltas: List[float] = []
            self.per_second: List[int] = []
            self._current_second_count = 0

    def on_pulse(self, ts: float):
        with self.lock:
            self.total += 1
            if self.last_ts is not None:
                dt = ts - self.last_ts
                if dt >= 0:
                    self.deltas.append(dt)
                    if len(self.deltas) > self.cfg.max_deltas:
                        self.deltas = self.deltas[-self.cfg.max_deltas:]

            self.last_ts = ts
            self._current_second_count += 1

    def tick_second(self):
        with self.lock:
            self.per_second.append(self._current_second_count)
            self._current_second_count = 0
            if len(self.per_second) > self.cfg.max_series:
                self.per_second = self.per_second[-self.cfg.max_series:]

    def snapshot(self):
        with self.lock:
            now = time.time()
            elapsed = max(0.0, now - self.t0)

            if elapsed > 0:
                rate = self.total / elapsed
                err = math.sqrt(self.total) / elapsed if self.total > 0 else 0.0
            else:
                rate, err = 0.0, 0.0

            running_mean = []
            s = 0
            for i, c in enumerate(self.per_second, start=1):
                s += c
                running_mean.append(s / i)

            last_age = (now - self.last_ts) if self.last_ts else None

            return {
                "total": self.total,
                "elapsed": elapsed,
                "last_age": last_age,
                "per_second": list(self.per_second),
                "running_mean": running_mean,
                "rate_bq": rate,
                "rate_err": err,
                "deltas": list(self.deltas),
            }


class GeigerReader:
    """
    Lector de pulsos basado en RPi.GPIO edge detection.
    Este es el método que YA te funciona en geiger_print.py.
    """

    def __init__(self, cfg: GeigerConfig):
        self.cfg = cfg
        self._on_pulse: Optional[Callable[[float], None]] = None
        self._stop = threading.Event()
        self._mock_thread: Optional[threading.Thread] = None
        self._started = False

    def set_callback(self, cb: Callable[[float], None]):
        self._on_pulse = cb

    def _emit(self):
        ts = time.time()
        if self._on_pulse:
            self._on_pulse(ts)
        if self.cfg.verbose:
            print(f"[GEIGER] pulse @ {ts:.6f}")

    def start(self):
        if self._started:
            return
        self._started = True

        if self.cfg.mock:
            self._start_mock()
            return

        # Limpieza defensiva
        try:
            GPIO.cleanup()
        except Exception:
            pass

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.cfg.pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

        try:
            GPIO.remove_event_detect(self.cfg.pin)
        except Exception:
            pass

        try:
            GPIO.add_event_detect(
                self.cfg.pin,
                GPIO.RISING,
                callback=lambda ch: self._emit(),
                bouncetime=1
            )
        except RuntimeError as e:
            # Si esto falla aquí, fallaría igual que en tu script
            raise RuntimeError(f"Failed to add edge detection on GPIO{self.cfg.pin}") from e

        if self.cfg.verbose:
            print(f"[GEIGER] RPi.GPIO edge detect ON GPIO{self.cfg.pin} (RISING)")

    def stop(self):
        self._stop.set()
        try:
            GPIO.remove_event_detect(self.cfg.pin)
        except Exception:
            pass
        try:
            GPIO.cleanup()
        except Exception:
            pass

    def _start_mock(self):
        if self.cfg.verbose:
            print(f"[GEIGER] MOCK ON ~ {self.cfg.mock_rate} pps")

        import random

        def run():
            while not self._stop.is_set():
                lam = max(0.0001, self.cfg.mock_rate)
                dt = random.expovariate(lam)
                time.sleep(dt)
                self._emit()

        self._mock_thread = threading.Thread(target=run, daemon=True)
        self._mock_thread.start()

