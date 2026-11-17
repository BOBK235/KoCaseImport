"""Forward shutdown to both services and their children; fail on child exit."""
import os
import signal
import subprocess
import sys
import time

from .app import create_app


def main():
    os.umask(0o077)
    create_app()  # Validate keys, database and persistent mount before serving.
    children = []
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        children.append(subprocess.Popen([sys.executable, '-m', 'kocaseimport.worker'], start_new_session=True))
        children.append(subprocess.Popen([sys.executable, '-m', 'gunicorn', '--workers', '1',
                        '--bind', '0.0.0.0:8000', '--forwarded-allow-ips=*',
                        'kocaseimport.app:create_app()'], start_new_session=True))
        while all(p.poll() is None for p in children):
            time.sleep(0.5)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        def signal_child(child, sig):
            try:
                os.killpg(child.pid, sig)
            except ProcessLookupError:
                pass
        for p in children:
            # A dead parent may still have a running PDF/OCR child. Always
            # signal its process group, not just surviving parent processes.
            signal_child(p, signal.SIGTERM)
        deadline = time.monotonic() + 10
        for p in children:
            try:
                p.wait(timeout=max(0.1, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                signal_child(p, signal.SIGKILL)
                p.wait()
        for p in children:
            signal_child(p, signal.SIGKILL)


if __name__ == '__main__':
    sys.exit(main())
