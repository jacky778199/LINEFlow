from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Dict
import yaml

def _require_env(name: str) -> str:
    """Read a required secret from the environment; raise clearly if missing."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Required environment variable '{name}' is not set. "
            "Copy .env.example to .env, fill in the value, and restart."
        )
    return value

@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    auth_token: str = ""  # Must be supplied via LINEFLOW_AUTH_TOKEN env var

@dataclass
class DatabaseConfig:
    path: str = "data/lineflow.db"

@dataclass
class InstanceConfig:
    enabled: bool = True
    adb_serial: str = "redroid_line1:5555"
    line_package: str = "jp.naver.line.android"
    poll_interval_sec: float = 2.0
    health_check_sec: float = 30.0

@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    instances: Dict[str, InstanceConfig] = field(default_factory=dict)

def load_config(config_path: str = "config.yaml") -> AppConfig:
    p = Path(config_path)
    if not p.exists():
        return AppConfig()
    with open(p, "r", encoding="utf-8") as f:
        raw_text = os.path.expandvars(f.read())
        data = yaml.safe_load(raw_text) or {}

    srv_data = data.get("server", {})
    server_cfg = ServerConfig(
        host=srv_data.get("host", "0.0.0.0"),
        port=srv_data.get("port", 8000),
        # Env var takes precedence over config file value.
        # Config file should NOT contain the real token.
        auth_token=os.environ.get("LINEFLOW_AUTH_TOKEN") or srv_data.get("auth_token") or _require_env("LINEFLOW_AUTH_TOKEN")
    )

    db_data = data.get("database", {})
    database_cfg = DatabaseConfig(
        path=db_data.get("path", "data/lineflow.db")
    )

    instances_data = data.get("instances", {})
    instances = {}
    for name, inst_val in instances_data.items():
        instances[name] = InstanceConfig(
            enabled=inst_val.get("enabled", True),
            adb_serial=inst_val.get("adb_serial", "redroid_line1:5555"),
            line_package=inst_val.get("line_package", "jp.naver.line.android"),
            poll_interval_sec=float(inst_val.get("poll_interval_sec", 2.0)),
            health_check_sec=float(inst_val.get("health_check_sec", 30.0))
        )

    return AppConfig(server=server_cfg, database=database_cfg, instances=instances)
