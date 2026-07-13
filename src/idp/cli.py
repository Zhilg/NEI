"""Operator command surface; durable batch commands arrive with persistence."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import typer

from idp.config import Settings
from idp.releases.build import ReleaseBuildSpec, build_bundle
from idp.releases.lifecycle import ReleaseManager
from idp.releases.manifest import (
    load_manifest,
    load_private_signing_key,
    load_public_verification_key,
    verify_bundle,
)
from idp.releases.validation import ProfileValidationError, validate_profile
from idp.runtime import run_controller, run_idle_worker

app = typer.Typer(no_args_is_help=True, help="Offline PDF batch pipeline operations.")
profile_app = typer.Typer(no_args_is_help=True, help="Pipeline profile operations.")
controller_app = typer.Typer(no_args_is_help=True, help="Controller operations.")
worker_app = typer.Typer(no_args_is_help=True, help="Worker operations.")
release_app = typer.Typer(no_args_is_help=True, help="Offline release bundle operations.")
app.add_typer(profile_app, name="profile")
app.add_typer(controller_app, name="controller")
app.add_typer(worker_app, name="worker")
app.add_typer(release_app, name="release")


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
def validate_profile_command(profile: str | None = None) -> None:
    """Verify an active/imported release and local target service prerequisites."""
    try:
        report = validate_profile(Settings(), profile)
    except ProfileValidationError as error:
        raise typer.Exit(code=_print_error(str(error))) from error
    typer.echo(json.dumps(report.__dict__, sort_keys=True))


@release_app.command("build")
def build_release(
    spec_path: Path = typer.Argument(..., exists=True, readable=True),
    output_directory: Path = typer.Argument(...),
    private_key_path: Path = typer.Option(..., "--private-key", exists=True, readable=True),
) -> None:
    """Build a signed bundle on the connected build host from an explicit JSON spec."""
    try:
        spec = ReleaseBuildSpec.model_validate_json(spec_path.read_text(encoding="utf-8"))
        manifest = build_bundle(spec, output_directory, load_private_signing_key(private_key_path))
    except (OSError, ValueError) as error:
        raise typer.Exit(code=_print_error(str(error))) from error
    typer.echo(json.dumps(manifest.model_dump(mode="json"), sort_keys=True))


@release_app.command("verify")
def verify_release(
    bundle_directory: Path = typer.Argument(..., exists=True, file_okay=False),
    public_key_path: Path = typer.Option(..., "--public-key", exists=True, readable=True),
) -> None:
    """Verify a transferred bundle without importing or activating it."""
    try:
        manifest = load_manifest(bundle_directory / "manifest.json")
        report = verify_bundle(bundle_directory, manifest, load_public_verification_key(public_key_path))
    except Exception as error:
        raise typer.Exit(code=_print_error(str(error))) from error
    typer.echo(json.dumps(report.__dict__, sort_keys=True))


@release_app.command("import")
def import_release(bundle_directory: Path = typer.Argument(..., exists=True, file_okay=False)) -> None:
    """Verify and copy a release into the managed target release directory."""
    settings = Settings()
    try:
        manager = ReleaseManager(
            settings.release_root,
            load_public_verification_key(settings.release_public_key_path),
            container_runtime=settings.container_runtime,
        )
        report = manager.import_bundle(bundle_directory)
    except Exception as error:
        raise typer.Exit(code=_print_error(str(error))) from error
    typer.echo(json.dumps(report.__dict__, sort_keys=True))


@release_app.command("activate")
def activate_release(release_id: str = typer.Argument(...)) -> None:
    """Verify an imported release again and atomically switch the active pointer."""
    settings = Settings()
    try:
        manager = ReleaseManager(
            settings.release_root,
            load_public_verification_key(settings.release_public_key_path),
            container_runtime=settings.container_runtime,
        )
        report = manager.activate(release_id)
    except Exception as error:
        raise typer.Exit(code=_print_error(str(error))) from error
    typer.echo(json.dumps(report.__dict__, sort_keys=True))


@release_app.command("rollback")
def rollback_release(release_id: str = typer.Argument(...)) -> None:
    """Atomically point runtime deployment back to a verified imported release."""
    activate_release(release_id)


def _print_error(message: str) -> int:
    typer.echo(json.dumps({"status": "error", "detail": message}, sort_keys=True), err=True)
    return 1


@controller_app.command("run")
def run_controller_process() -> None:
    """Start the controller recovery service and local Prometheus endpoint."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_controller(Settings())


@worker_app.command("run")
def run_worker_process() -> None:
    """Start the phase-two worker process without model stage handlers."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_idle_worker(Settings())
