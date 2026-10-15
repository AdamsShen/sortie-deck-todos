from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from sortie_deck.artifacts import ArtifactPathError, LocalArtifactStore
from sortie_deck.models import ArtifactRef, utc_now


def _safe_key(prefix: str, relative_path: str) -> str:
    if not relative_path or relative_path.startswith(("/", "\\")) or ".." in relative_path.split("/"):
        raise ArtifactPathError("invalid artifact key")
    prefix = prefix.strip("/")
    return f"{prefix}/{relative_path}" if prefix else relative_path


class S3ArtifactStore:
    """S3-compatible artifact backend (MinIO / AWS). Requires optional `boto3` extra."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "artifacts",
        endpoint_url: str | None = None,
        region: str = "us-east-1",
        access_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError(
                "S3 artifact backend requires boto3 — pip install 'sortie-deck[s3]'"
            ) from exc
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        kwargs: dict[str, Any] = {"region_name": region}
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key
        self._client = boto3.client("s3", **kwargs)

    def write_text(
        self,
        initiative_id: str,
        stage: str,
        filename: str,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        if "/" in filename or "\\" in filename or filename in {".", ".."}:
            raise ArtifactPathError("filename must be a basename")
        rel = f"{initiative_id}/{stage}/{filename}"
        key = _safe_key(self.prefix, rel)
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )
        return ArtifactRef(
            kind=filename,
            path=rel,
            stage=stage,
            created_at=utc_now(),
            meta={**(meta or {}), "backend": "s3", "bucket": self.bucket, "key": key},
        )

    def write_json(
        self,
        initiative_id: str,
        stage: str,
        filename: str,
        data: Any,
        meta: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        return self.write_text(
            initiative_id,
            stage,
            filename,
            json.dumps(data, ensure_ascii=False, indent=2),
            meta=meta,
        )

    def read_text(self, relative_path: str) -> str:
        key = _safe_key(self.prefix, relative_path)
        try:
            obj = self._client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise FileNotFoundError(relative_path) from exc
        return obj["Body"].read().decode("utf-8")

    def read_json(self, relative_path: str) -> Any:
        return json.loads(self.read_text(relative_path))

    def clear(self, initiative_id: str) -> None:
        prefix = _safe_key(self.prefix, initiative_id) + "/"
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            contents = page.get("Contents") or []
            if not contents:
                continue
            self._client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": o["Key"]} for o in contents]},
            )

    def presigned_url(self, relative_path: str, expires: int = 3600) -> str:
        key = _safe_key(self.prefix, relative_path)
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires,
        )


def build_artifact_store(settings: Any) -> LocalArtifactStore | S3ArtifactStore:
    backend = str(getattr(settings, "artifact_backend", "local") or "local").lower()
    if backend in {"s3", "minio"}:
        bucket = getattr(settings, "s3_bucket", None) or ""
        if not bucket:
            raise RuntimeError("TDT_S3_BUCKET required when TDT_ARTIFACT_BACKEND=s3")
        return S3ArtifactStore(
            bucket=bucket,
            prefix=getattr(settings, "s3_prefix", "artifacts") or "artifacts",
            endpoint_url=getattr(settings, "s3_endpoint", None),
            region=getattr(settings, "s3_region", "us-east-1") or "us-east-1",
            access_key=getattr(settings, "s3_access_key", None),
            secret_key=getattr(settings, "s3_secret_key", None),
        )
    return LocalArtifactStore(settings.missions_dir, layout="mission")


def quote_path(path: str) -> str:
    return quote(path, safe="/")
