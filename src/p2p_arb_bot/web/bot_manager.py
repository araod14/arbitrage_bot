"""Gestión del bot como subproceso del dashboard (start/stop/restart/status).

El estado vive en un *pidfile* en disco para sobrevivir a reinicios del propio
dashboard: si el dashboard se reinicia pero el bot seguía corriendo, recupera el
control leyendo el PID. El bot se lanza siempre con ``NO_INPUT=true`` para que no
pida prompts interactivos.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time

logger = logging.getLogger(__name__)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # existe pero sin permiso para señalarlo
    return True


class BotManager:
    """Arranca/para el proceso del bot y reporta su estado."""

    def __init__(self, pidfile: str, log_path: str) -> None:
        self._pidfile = pidfile
        self._log_path = log_path
        self._proc: subprocess.Popen | None = None

    # --- pidfile ----------------------------------------------------------

    def _read_pidfile(self) -> dict | None:
        if not os.path.exists(self._pidfile):
            return None
        try:
            with open(self._pidfile, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def _write_pidfile(self, pid: int) -> None:
        data = {"pid": pid, "started_at": time.time()}
        with open(self._pidfile, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def _clear_pidfile(self) -> None:
        try:
            os.remove(self._pidfile)
        except FileNotFoundError:
            pass

    def _current_pid(self) -> int | None:
        """PID del bot si está vivo, limpiando el pidfile si quedó huérfano."""
        info = self._read_pidfile()
        if not info:
            return None
        pid = int(info.get("pid", 0))
        if pid and _pid_alive(pid):
            return pid
        self._clear_pidfile()
        return None

    # --- API pública ------------------------------------------------------

    def is_running(self) -> bool:
        return self._current_pid() is not None

    def status(self) -> dict:
        info = self._read_pidfile()
        pid = self._current_pid()
        running = pid is not None
        uptime = None
        if running and info and info.get("started_at"):
            uptime = int(time.time() - float(info["started_at"]))
        return {"running": running, "pid": pid, "uptime_s": uptime}

    def start(self) -> dict:
        if self.is_running():
            return {"ok": False, "message": "El bot ya está corriendo."}

        env = dict(os.environ)
        env["NO_INPUT"] = "true"
        # Heredan DB_PATH/LOG_PATH/STATUS_PATH/etc del entorno del dashboard.
        log_fh = open(self._log_path, "a", encoding="utf-8")  # noqa: SIM115
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "p2p_arb_bot.main"],
                env=env,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # propio grupo: no muere con el dashboard
            )
        finally:
            log_fh.close()
        self._proc = proc
        self._write_pidfile(proc.pid)
        logger.info("Bot arrancado (pid=%s)", proc.pid)
        return {"ok": True, "message": f"Bot arrancado (pid={proc.pid})."}

    def stop(self, timeout: float = 10.0) -> dict:
        pid = self._current_pid()
        if pid is None:
            return {"ok": False, "message": "El bot no está corriendo."}

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._clear_pidfile()
            return {"ok": True, "message": "El bot ya estaba detenido."}

        deadline = time.time() + timeout
        while time.time() < deadline:
            if not _pid_alive(pid):
                break
            time.sleep(0.2)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        self._clear_pidfile()
        self._proc = None
        logger.info("Bot detenido (pid=%s)", pid)
        return {"ok": True, "message": "Bot detenido."}

    def restart(self) -> dict:
        self.stop()
        return self.start()
