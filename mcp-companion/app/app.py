from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
import uvicorn


OPTIONS_PATH = Path("/data/options.json")
CONFIG_ROOT = Path("/config")
BACKUP_ROOT = CONFIG_ROOT / ".mcp_companion_backups"
APP_VERSION = "0.1.3"


class CompanionOptions(BaseModel):
    companion_token: str = ""
    log_level: str = "info"
    allowed_config_roots: list[str] = Field(default_factory=lambda: ["/config"])


class FileReadRequest(BaseModel):
    path: str


class FileWriteRequest(BaseModel):
    path: str
    content: str
    expected_sha256: str | None = None
    create_missing: bool = False


class FileDeleteRequest(BaseModel):
    path: str
    expected_sha256: str


class BackupRequest(BaseModel):
    name: str | None = None
    full: bool = False
    compressed: bool = True


class ConfigCheckRequest(BaseModel):
    require_valid: bool = False


def create_app() -> FastAPI:
    app = FastAPI(title="Home Assistant MCP Companion", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "name": "mcp-companion", "version": APP_VERSION}

    @app.get("/capabilities")
    def capabilities(_: None = Depends(require_auth)) -> dict[str, Any]:
        options = load_options()
        return {
            "supervisor_token_present": bool(supervisor_token()),
            "config_root_exists": CONFIG_ROOT.exists(),
            "allowed_config_roots": options.allowed_config_roots,
            "supports": {
                "supervisor_backups": bool(supervisor_token()),
                "config_file_read": True,
                "config_file_write_with_backup": True,
                "config_file_delete_with_backup": True,
                "config_file_restore": True,
            },
        }

    @app.post("/config/read")
    def read_config_file(
        request: FileReadRequest,
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        path = resolve_allowed_path(request.path)
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="File not found.")
        content = path.read_text(encoding="utf-8")
        return {
            "path": str(path),
            "sha256": sha256_text(content),
            "content": content,
        }

    @app.post("/config/write")
    def write_config_file(
        request: FileWriteRequest,
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        path = resolve_allowed_path(request.path)
        if not path.exists() and not request.create_missing:
            raise HTTPException(status_code=404, detail="File not found.")
        if path.exists() and not path.is_file():
            raise HTTPException(status_code=400, detail="Path is not a file.")
        if path.exists() and request.expected_sha256:
            current = path.read_text(encoding="utf-8")
            if sha256_text(current) != request.expected_sha256:
                raise HTTPException(status_code=409, detail="Hash precondition failed.")

        backup_path = backup_file(path) if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(request.content, encoding="utf-8")
        return {
            "path": str(path),
            "sha256": sha256_text(request.content),
            "backup_path": str(backup_path) if backup_path else None,
        }

    @app.post("/config/delete")
    def delete_config_file(
        request: FileDeleteRequest,
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        path = resolve_allowed_path(request.path)
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="File not found.")
        current = path.read_text(encoding="utf-8")
        if sha256_text(current) != request.expected_sha256:
            raise HTTPException(status_code=409, detail="Hash precondition failed.")

        backup_path = backup_file(path)
        path.unlink()
        return {
            "path": str(path),
            "deleted": True,
            "backup_path": str(backup_path),
            "sha256": sha256_text(current),
        }

    @app.post("/config/restore")
    def restore_config_file(
        request: FileReadRequest,
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        backup_path = resolve_backup_path(request.path)
        if not backup_path.exists() or not backup_path.is_file():
            raise HTTPException(status_code=404, detail="Backup file not found.")
        metadata_path = backup_path.with_suffix(backup_path.suffix + ".json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        target = resolve_allowed_path(metadata["original_path"])
        shutil.copy2(backup_path, target)
        content = target.read_text(encoding="utf-8")
        return {
            "restored_to": str(target),
            "sha256": sha256_text(content),
            "backup_path": str(backup_path),
        }

    @app.post("/backup/create")
    def create_backup(
        request: BackupRequest,
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        if not supervisor_token():
            raise HTTPException(
                status_code=503,
                detail="SUPERVISOR_TOKEN is not available in this add-on.",
            )
        payload: dict[str, Any] = {"compressed": request.compressed}
        if request.name:
            payload["name"] = request.name
        path = "/backups/new/full" if request.full else "/backups/new/partial"
        if not request.full:
            payload["homeassistant"] = True
            payload["folders"] = ["homeassistant"]
        result = supervisor_request("POST", path, payload)
        return {"created": True, "full": request.full, "result": result}

    @app.get("/backup/list")
    def list_backups(_: None = Depends(require_auth)) -> dict[str, Any]:
        return supervisor_request("GET", "/backups")

    @app.post("/config/check")
    def check_config(
        request: ConfigCheckRequest,
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        result = supervisor_request("POST", "/core/check")
        if request.require_valid and result.get("result") == "error":
            raise HTTPException(status_code=409, detail=result)
        return result

    return app


def require_auth(authorization: str | None = Header(default=None)) -> None:
    options = load_options()
    if not options.companion_token:
        raise HTTPException(
            status_code=503,
            detail="companion_token must be configured before use.",
        )
    expected = f"Bearer {options.companion_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized.")


def load_options() -> CompanionOptions:
    if not OPTIONS_PATH.exists():
        return CompanionOptions()
    return CompanionOptions.model_validate_json(OPTIONS_PATH.read_text(encoding="utf-8"))


def resolve_allowed_path(path_value: str) -> Path:
    options = load_options()
    candidate = Path(path_value)
    if not candidate.is_absolute():
        candidate = CONFIG_ROOT / candidate
    resolved = candidate.resolve()
    roots = [Path(root).resolve() for root in options.allowed_config_roots]
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise HTTPException(status_code=403, detail="Path is outside allowed roots.")
    return resolved


def resolve_backup_path(path_value: str) -> Path:
    candidate = Path(path_value)
    if not candidate.is_absolute():
        candidate = BACKUP_ROOT / candidate
    resolved = candidate.resolve()
    backup_root = BACKUP_ROOT.resolve()
    if not (resolved == backup_root or backup_root in resolved.parents):
        raise HTTPException(status_code=403, detail="Path is outside backup root.")
    return resolved


def backup_file(path: Path) -> Path:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_id = f"{stamp}-{uuid4().hex[:12]}"
    backup_path = BACKUP_ROOT / backup_id / relative_allowed_path(path)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_path)
    metadata = {
        "backup_id": backup_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "original_path": str(path),
        "backup_path": str(backup_path),
        "sha256": sha256_file(path),
    }
    backup_path.with_suffix(backup_path.suffix + ".json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return backup_path


def relative_allowed_path(path: Path) -> Path:
    resolved = path.resolve()
    roots = [Path(root).resolve() for root in load_options().allowed_config_roots]
    for root in roots:
        if resolved == root or root in resolved.parents:
            return resolved.relative_to(root)
    raise HTTPException(status_code=403, detail="Path is outside allowed roots.")


def supervisor_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token = supervisor_token()
    if not token:
        raise HTTPException(status_code=503, detail="SUPERVISOR_TOKEN is unavailable.")

    body = None
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        f"http://supervisor{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            content = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=exc.code, detail=detail) from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=503, detail=str(exc.reason)) from exc

    if not content:
        return {}
    return json.loads(content.decode("utf-8"))


def supervisor_token() -> str:
    return os.getenv("SUPERVISOR_TOKEN") or os.getenv("HASSIO_TOKEN") or ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def run() -> None:
    uvicorn.run(create_app(), host="0.0.0.0", port=8099)
