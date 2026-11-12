import asyncio
import json
import logging
import math
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import docker
import psutil
from docker.errors import DockerException
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from requests.exceptions import RequestException


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
# A file on the host root filesystem is sufficient for statvfs/disk_usage.
# Mounting the entire host root would expose secrets and bypass the Docker proxy.
HOST_ROOT_CANDIDATE = Path("/host/rootfs-anchor")
HOST_ROOT = HOST_ROOT_CANDIDATE if HOST_ROOT_CANDIDATE.exists() else Path("/")
HOST_SYS = Path("/host/sys")
SSH_SNAPSHOT_PATH = Path(os.environ.get("SSH_SNAPSHOT_PATH", "/host/ssh/ssh.json"))
SAMPLE_INTERVAL = float(os.environ.get("SAMPLE_INTERVAL_SECONDS", "10"))
if not 1 <= SAMPLE_INTERVAL <= 3600:
    raise ValueError("SAMPLE_INTERVAL_SECONDS must be between 1 and 3600")
logger = logging.getLogger("pi_monitor")
_docker_client: docker.DockerClient | None = None
_cache_lock = threading.Lock()
_snapshot: dict[str, Any] | None = None
_sampling_error: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _snapshot, _sampling_error
    with _cache_lock:
        _snapshot = None
        _sampling_error = None
    stop = threading.Event()
    worker = threading.Thread(target=_sampling_loop, args=(stop,), daemon=True,
                              name="metrics-sampler")
    worker.start()
    try:
        yield
    finally:
        stop.set()
        await asyncio.to_thread(worker.join)


app = FastAPI(
    title="Raspberry Pi System Monitor",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.get("/assets/raspberry-pi.png", include_in_schema=False)
def brand_icon() -> FileResponse:
    return FileResponse(
        BASE_DIR / "raspberry-pi.png",
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/assets/favicon.svg", include_in_schema=False)
def favicon() -> FileResponse:
    # The supplied white PNG sits on a dark tile so light browser tabs show it.
    return FileResponse(
        BASE_DIR / "favicon.svg",
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "不可用"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _format_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "不可用"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    return f"{days} 天 {hours} 小时 {minutes} 分钟"


templates.env.filters["bytes"] = _format_bytes
templates.env.filters["uptime"] = _format_uptime


def _round_or_none(value: float | int | None, digits: int = 1) -> float | None:
    return round(value, digits) if value is not None else None


def _get_cpu_usage() -> float | None:
    try:
        return _round_or_none(psutil.cpu_percent(interval=None))
    except (OSError, RuntimeError) as exc:
        logger.warning("Unable to read CPU usage: %s", exc)
        return None


def _get_memory() -> dict[str, Any]:
    result = {
        "total_bytes": None,
        "used_bytes": None,
        "available_bytes": None,
        "percent": None,
        "swap_total_bytes": None,
        "swap_used_bytes": None,
        "swap_percent": None,
    }
    try:
        memory = psutil.virtual_memory()
        result.update(
            total_bytes=memory.total,
            used_bytes=memory.used,
            available_bytes=memory.available,
            percent=_round_or_none(memory.percent),
        )
    except (OSError, RuntimeError) as exc:
        logger.warning("Unable to read memory usage: %s", exc)
    try:
        swap = psutil.swap_memory()
        result.update(
            swap_total_bytes=swap.total,
            swap_used_bytes=swap.used,
            swap_percent=_round_or_none(swap.percent),
        )
    except (OSError, RuntimeError) as exc:
        logger.warning("Unable to read swap usage: %s", exc)
    return result


def _get_load_average() -> tuple[float, float, float] | None:
    try:
        return tuple(round(value, 2) for value in psutil.getloadavg())
    except (OSError, AttributeError) as exc:
        logger.warning("Unable to read system load average: %s", exc)
        return None


def _get_host_uptime_seconds() -> int | None:
    try:
        return max(0, int(time.time() - psutil.boot_time()))
    except (OSError, RuntimeError) as exc:
        logger.warning("Unable to read host uptime: %s", exc)
        return None


def _get_disk() -> dict[str, Any]:
    try:
        disk = psutil.disk_usage(str(HOST_ROOT))
        return {
            "path": "/",
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
            "percent": _round_or_none(disk.percent),
            "source": str(HOST_ROOT),
        }
    except (OSError, PermissionError) as exc:
        logger.warning("Unable to read root filesystem usage at %s: %s", HOST_ROOT, exc)
        return {
            "path": "/",
            "total_bytes": None,
            "used_bytes": None,
            "free_bytes": None,
            "percent": None,
            "source": str(HOST_ROOT),
        }


def _get_temperature() -> float | None:
    candidates = (
        Path("/sys/class/thermal/thermal_zone0/temp"),
        HOST_SYS / "class/thermal/thermal_zone0/temp",
    )
    for path in dict.fromkeys(candidates):
        try:
            raw_value = path.read_text(encoding="ascii").strip()
            value = float(raw_value)
            # Linux thermal sysfs normally reports millidegrees Celsius.
            celsius = value / 1000 if abs(value) >= 1000 else value
            return _round_or_none(celsius)
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, ValueError) as exc:
            logger.warning("Unable to read SoC temperature from %s: %s", path, exc)
            continue
    logger.info("SoC thermal zone temperature is unavailable")
    return None


def _get_fan_rpm() -> int | None:
    bases = (
        Path("/sys/devices/platform/cooling_fan/hwmon"),
        HOST_SYS / "devices/platform/cooling_fan/hwmon",
        Path("/sys/class/hwmon"),
        HOST_SYS / "class/hwmon",
    )
    for base in dict.fromkeys(bases):
        try:
            for rpm_file in sorted(base.glob("hwmon*/fan1_input")):
                try:
                    rpm = int(rpm_file.read_text(encoding="ascii").strip())
                    if rpm >= 0:
                        return rpm
                except (OSError, UnicodeError, ValueError) as exc:
                    logger.warning("Unable to read fan speed from %s: %s", rpm_file, exc)
        except OSError as exc:
            logger.warning("Unable to inspect fan sensor path %s: %s", base, exc)
    return None


def _get_docker_client() -> docker.DockerClient:
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.DockerClient(
            base_url=os.environ.get("DOCKER_HOST", "unix:///run/docker-proxy/docker.sock"),
            timeout=3,
        )
    return _docker_client


def _get_ssh_activity() -> dict[str, Any]:
    empty = {"sessions": {"items": [], "updated_at": None, "error": None},
             "history": {"items": [], "updated_at": None, "error": None},
             "warning": None, "collected_at": None}
    try:
        with SSH_SNAPSHOT_PATH.open("rb") as file:
            raw = file.read(262145)
        if len(raw) > 262144:
            raise ValueError("SSH snapshot exceeds size limit")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("Invalid SSH snapshot version")
        timestamp = data.get("collected_at")
        if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or not 0 <= timestamp <= 253402300799:
            raise ValueError("Invalid SSH timestamp")
        result = dict(empty, collected_at=timestamp)
        for section, limit in (("sessions", 256), ("history", 20)):
            value = data.get(section)
            if not isinstance(value, dict) or not isinstance(value.get("items"), list):
                raise ValueError("Invalid SSH section")
            items = value["items"][:limit]
            if any(not isinstance(item, dict) for item in items):
                raise ValueError("Invalid SSH item")
            for item in items:
                for key in ("user", "source_ip", "terminal", "session_id", "method"):
                    if key in item and (not isinstance(item[key], str) or len(item[key]) > 1024):
                        raise ValueError("Invalid SSH field")
                login_at = item.get("login_at")
                if login_at is not None and (not isinstance(login_at, (int, float)) or
                    not math.isfinite(login_at) or not 0 <= login_at <= 253402300799):
                    raise ValueError("Invalid SSH login timestamp")
            updated_at = value.get("updated_at")
            if updated_at is not None and (not isinstance(updated_at, (int, float)) or not math.isfinite(updated_at) or not 0 <= updated_at <= 253402300799):
                raise ValueError("Invalid SSH update timestamp")
            error = value.get("error")
            if error is not None and (not isinstance(error, str) or len(error) > 1024):
                raise ValueError("Invalid SSH error")
            # Export only the fields consumed by the UI, never arbitrary log data.
            clean_items = []
            for item in items:
                clean_items.append({
                    **{key: item.get(key, "") for key in ("user", "source_ip", "terminal", "session_id", "method")},
                    "login_at": item.get("login_at"),
                })
            result[section] = {"items": clean_items, "updated_at": updated_at, "error": error}
        age = time.time() - timestamp
        if age > 45 or age < -60:
            result["warning"] = "SSH 采集数据已过期，请检查宿主机采集服务；以下为上次数据。"
        return result
    except FileNotFoundError:
        empty["warning"] = "尚未连接 SSH 采集服务，请按使用说明安装宿主机只读采集服务。"
    except (OSError, ValueError, TypeError):
        logger.warning("Unable to read host SSH snapshot", exc_info=True)
        empty["warning"] = "无法读取 SSH 信息，请检查采集文件和挂载配置。"
    return empty


def _format_timestamp(value: float | None) -> str:
    if value is None:
        return "—"
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


templates.env.filters["timestamp"] = _format_timestamp


def _get_containers() -> tuple[list[dict[str, Any]], str | None]:
    global _docker_client
    try:
        client = _get_docker_client()
        containers = client.containers.list(all=True)
        result = [
            {
                "name": container.name,
                "status": container.status,
                "description": (
                    (container.attrs.get("Config", {}).get("Labels") or {}).get(
                        "description", ""
                    ) or ""
                ).strip(),
                "restart_count": container.attrs.get("RestartCount", 0),
                "image": container.attrs.get("Config", {}).get("Image", "未知"),
            }
            for container in containers
        ]
        result.sort(key=lambda item: item["name"].lower())
        return result, None
    except (DockerException, RequestException) as exc:
        logger.warning("Docker socket is unavailable or Docker API failed: %s", exc)
        if _docker_client is not None:
            try:
                _docker_client.close()
            except DockerException as close_exc:
                logger.warning("Unable to close failed Docker client: %s", close_exc)
            _docker_client = None
        return [], "无法连接 Docker 只读代理，请检查 Docker 和 docker-proxy 服务。"


def _collect_snapshot() -> dict[str, Any]:
    containers, docker_warning = _get_containers()
    return {
        "cpu_percent": _get_cpu_usage(),
        "logical_cpu_count": psutil.cpu_count(logical=True) or 1,
        "load_average": _get_load_average(),
        "memory": _get_memory(),
        "disk": _get_disk(),
        "temperature_c": _get_temperature(),
        "fan_rpm": _get_fan_rpm(),
        "host_uptime_seconds": _get_host_uptime_seconds(),
        "containers": containers,
        "docker_warning": docker_warning,
        "ssh": _get_ssh_activity(),
        "host_root_mounted": HOST_ROOT == HOST_ROOT_CANDIDATE,
        "sampled_at": time.time(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _sampling_loop(stop: threading.Event) -> None:
    global _snapshot, _sampling_error, _docker_client
    try:
        # psutil keeps CPU baselines per thread. Prime and sample on this thread.
        _get_cpu_usage()
        stop.wait(0.1)
        while not stop.is_set():
            started = time.monotonic()
            try:
                snapshot = _collect_snapshot()
                with _cache_lock:
                    _snapshot = snapshot
                    _sampling_error = None
            except Exception:
                logger.exception("Background metrics sampling failed")
                with _cache_lock:
                    _sampling_error = "后台采集失败，正在显示上次采集的数据"
            stop.wait(max(0.1, SAMPLE_INTERVAL - (time.monotonic() - started)))
    finally:
        if _docker_client is not None:
            try:
                _docker_client.close()
            except DockerException:
                logger.exception("Unable to close Docker client")
            _docker_client = None


def _cached_data() -> dict[str, Any] | None:
    with _cache_lock:
        if _snapshot is None:
            return None
        data = dict(_snapshot)
        data["sampling_error"] = _sampling_error
    data["sample_interval_seconds"] = SAMPLE_INTERVAL
    data["snapshot_age_seconds"] = max(0, time.time() - data["sampled_at"])
    data["stale"] = bool(data["sampling_error"]) or (
        data["snapshot_age_seconds"] > max(30, SAMPLE_INTERVAL * 3)
    )
    return data


@app.get("/api/metrics", include_in_schema=False)
def metrics() -> JSONResponse:
    data = _cached_data()
    if data is None:
        return JSONResponse({"detail": "正在进行首次采集，请稍候"}, status_code=503,
                            headers={"Cache-Control": "no-store", "Retry-After": "1"})
    # Render the same fragments used on first load; requests only read the cache.
    data["resources_html"] = templates.env.get_template("resources.html").render(**data)
    data["containers_html"] = templates.env.get_template("containers.html").render(**data)
    data["ssh_html"] = templates.env.get_template("ssh.html").render(**data)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@app.get("/", include_in_schema=False)
def dashboard(request: Request):
    data = _cached_data()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={**(data or {}), "data": data, "sample_interval_seconds": SAMPLE_INTERVAL},
        headers={"Cache-Control": "no-store"},
    )
