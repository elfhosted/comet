# comet/gunicorn_conf.py
from comet.utils.models import settings
from comet.utils.logger import logger

# Gunicorn config
bind = f"{settings.FASTAPI_HOST}:{settings.FASTAPI_PORT}"
workers = settings.FASTAPI_WORKERS
worker_class = "uvicorn.workers.UvicornWorker"
forwarded_allow_ips = "*"
proxy_allow_ips = "*"
proxy_protocol = True

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Performance settings
worker_connections = 1000
timeout = 120
keepalive = 5

def on_starting(server):
    """Log startup information when Gunicorn starts."""
    from comet.main import log_startup_info
    log_startup_info()

def worker_exit(server, worker):
    """Handle worker shutdown."""
    logger.log("COMET", f"Worker {worker.pid} exited")

def on_exit(server):
    """Handle server shutdown."""
    logger.log("COMET", "Server Shutdown")