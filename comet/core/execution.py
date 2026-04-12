import atexit
import os
from concurrent.futures import ThreadPoolExecutor

from comet.core.models import settings

app_executor = None
max_workers = settings.EXECUTOR_MAX_WORKERS
if max_workers is None or max_workers < 1:
    cpu_count = os.cpu_count() or 1
    max_workers = min(cpu_count, 4)


def setup_executor():
    global app_executor
    app_executor = ThreadPoolExecutor(max_workers=max_workers)


def shutdown_executor():
    global app_executor
    if app_executor:
        app_executor.shutdown(wait=True, cancel_futures=True)
        app_executor = None


atexit.register(shutdown_executor)


def get_executor():
    return app_executor
