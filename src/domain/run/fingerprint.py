"""Canonical request fingerprints for idempotent run claims."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from src.domain.run.request import RunRequest


def request_fingerprint(request: RunRequest) -> str:
  payload: dict[str, Any] = {
      "trigger_id": request.trigger_id,
      "commit_hash": request.commit_hash,
      "action": request.action,
      "folder_mode": request.folder_mode,
      "folders": sorted(request.folders),
      "pipeline": request.pipeline,
      "pipeline_step": request.pipeline_step,
      "notification_target": request.notification_target.to_dict(),
      "ingress_source": request.ingress_source,
  }
  encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
  return hashlib.sha256(encoded).hexdigest()
