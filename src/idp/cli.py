"""Operator command surface; durable batch commands arrive with persistence."""

from __future__ import annotations

import json

import typer

from idp.config import Settings

app = typer.Typer(no_args_is_help=True, help="Offline PDF batch pipeline operations.")
profile_app = typer.Typer(no_args_is_help=True, help="Pipeline profile operations.")
app.add_typer(profile_app, name="profile")


@app.command("config")
def show_config() -> None:
    """Print validated foundation settings without exposing secrets."""
    settings = Settings()
    payload = {
        "allowed_roots": [str(path) for path in settings.allowed_roots],
        "minio_endpoint": settings.minio_endpoint,
        "minio_secure": settings.minio_secure,
        "metrics_port": settings.metrics_port,
        "offline_mode": settings.offline_mode,
        "telemetry_enabled": settings.telemetry_enabled,
    }
    typer.echo(json.dumps(payload, sort_keys=True))


@profile_app.command("validate")
def validate_profile(profile: str) -> None:
    """Reserve the required command; full verification is phase 3 work."""
    typer.echo(
        json.dumps(
            {
                "profile": profile,
                "status": "not_implemented",
                "detail": "Offline release validation is not part of the foundation phase.",
            },
            sort_keys=True,
        )
    )
