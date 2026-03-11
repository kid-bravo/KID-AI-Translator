
# logger_setup.py
from __future__ import annotations
import os, sys, json, platform, socket, shutil, asyncio, logging, time
from logging.handlers import RotatingFileHandler
from datetime import datetime

_DEFAULT_KEYS = set(logging.makeLogRecord({}).__dict__.keys())

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
        data = {
            "ts": ts,
            "lvl": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
            "pid": os.getpid(),
            "service": getattr(record, "service", None) or os.getenv("SERVICE_NAME"),
        }
        for k, v in record.__dict__.items():
            if k not in _DEFAULT_KEYS and k not in ("msg","args","exc_info","exc_text","stack_info","stacklevel"):
                data[k] = v
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)

def _runtime_facts():
    cpu = os.cpu_count()
    mem_total = mem_avail = None
    try:
        with open("/proc/meminfo") as f:
            d = {}
            for line in f:
                k, rest = line.split(":",1)
                d[k] = int(rest.strip().split()[0]) # kB
        mem_total = round(d.get("MemTotal",0)*1024/(1024**3),2)
        mem_avail = round(d.get("MemAvailable",0)*1024/(1024**3),2)
    except Exception:
        pass
    def _du(path):
        try:
            t,u,f = shutil.disk_usage(path)
            return dict(total_gb=round(t/(1024**3),2), free_gb=round(f/(1024**3),2))
        except Exception:
            return {}
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "hostname": socket.gethostname(),
        "cpu_count": cpu,
        "mem_total_gb": mem_total,
        "mem_avail_gb": mem_avail,
        "home": _du("/home"),
        "tmp": _du("/tmp"),
        "WEBSITE_SITE_NAME": os.getenv("WEBSITE_SITE_NAME"),
        "WEBSITE_SKU": os.getenv("WEBSITE_SKU"),
        "WEBSITE_INSTANCE_ID": os.getenv("WEBSITE_INSTANCE_ID"),
        "REGION_NAME": os.getenv("REGION_NAME") or os.getenv("Location"),
    }

def _install_exception_hooks(logger: logging.Logger, service: str):
    def _excepthook(exc_type, exc, tb):
        logger.error("UNHANDLED_EXCEPTION", exc_info=(exc_type, exc, tb), extra={"service": service})
    sys.excepthook = _excepthook
    try:
        loop = asyncio.get_event_loop()
        def _asyncio_handler(loop, context):
            err = context.get("exception")
            msg = context.get("message", "")
            logger.error("ASYNCIO_UNHANDLED", exc_info=err if err else None,
                         extra={"service": service, "message": msg})
        loop.set_exception_handler(_asyncio_handler)
    except Exception:
        pass

def setup_logging(service: str = "app") -> logging.Logger:
    """Call once at startup:
       from logger_setup import setup_logging; logger = setup_logging(service="bot")"""
    if getattr(setup_logging, "_configured", False):
        return logging.getLogger(service)
    setup_logging._configured = True  # type: ignore

    level = os.getenv("LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    root.setLevel(level)

    fmt = JsonFormatter()
    sh = logging.StreamHandler(stream=sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(level)
    root.addHandler(sh)

    # optional rotating file (App Service)
    log_dir = os.getenv("APP_LOG_DIR", "/home/LogFiles/Application")
    try:
        os.makedirs(log_dir, exist_ok=True)
        fh = RotatingFileHandler(os.path.join(log_dir, f"{service}.log"),
                                 maxBytes=10*1024*1024, backupCount=5)
        fh.setFormatter(fmt)
        fh.setLevel(level)
        root.addHandler(fh)
    except Exception:
        pass

    # reduce noisy libs
    logging.getLogger("azure").setLevel(os.getenv("AZURE_SDK_LOG_LEVEL", "WARNING").upper())
    logging.getLogger("aiohttp.access").setLevel(os.getenv("AIOHTTP_LOG_LEVEL", "WARNING").upper())

    logger = logging.getLogger(service)
    _install_exception_hooks(logger, service)
    facts = _runtime_facts()
    small = (facts.get("cpu_count") or 1) <= 1 or (facts.get("mem_total_gb") or 0) < 6
    logger.info("SERVICE_START", extra={"service": service, "facts": facts, "engine_small": small})
    return logger
