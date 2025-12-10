import time
from dotenv import load_dotenv

from geiger import GeigerConfig, GeigerState, GeigerReader


def main():
    load_dotenv()

    cfg = GeigerConfig.from_env()
    state = GeigerState(cfg)
    reader = GeigerReader(cfg)

    def on_pulse(ts: float):
        state.on_pulse(ts)
        if cfg.verbose:
            print(f"[TEST] total={state.total}")

    reader.set_callback(on_pulse)
    reader.start()

    print(f"Testing Geiger on GPIO{cfg.pin} mock={cfg.mock}")
    print("Ctrl+C para salir.\n")

    t_last = time.time()
    try:
        while True:
            time.sleep(1)
            state.tick_second()
            now = time.time()
            if now - t_last >= 5:
                snap = state.snapshot()
                print(f"[TEST] 5s: total={snap['total']} rate={snap['rate_bq']:.2f} ± {snap['rate_err']:.2f} Bq")
                t_last = now
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
        print("Stopped.")


if __name__ == "__main__":
    main()

