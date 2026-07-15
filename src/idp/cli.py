"""Operator command surface; durable batch commands arrive with persistence."""

from __future__ import annotations

import json
import logging
import csv
import sys
from pathlib import Path
from uuid import UUID

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
from idp.runtime import create_batch_service, run_controller, run_idle_worker

app = typer.Typer(no_args_is_help=True, help="Offline PDF batch pipeline operations.")
profile_app = typer.Typer(no_args_is_help=True, help="Pipeline profile operations.")
controller_app = typer.Typer(no_args_is_help=True, help="Controller operations.")
worker_app = typer.Typer(no_args_is_help=True, help="Worker operations.")
release_app = typer.Typer(no_args_is_help=True, help="Offline release bundle operations.")
batch_app = typer.Typer(no_args_is_help=True, help="One-shot PDF batch operations.")
app.add_typer(profile_app, name="profile")
app.add_typer(controller_app, name="controller")
app.add_typer(worker_app, name="worker")
app.add_typer(release_app, name="release")
app.add_typer(batch_app, name="batch")


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


@batch_app.command("submit")
def submit_batch(
    roots: list[Path] = typer.Argument(...),
    profile: str = typer.Option(..., "--profile"),
) -> None:
    """Recursively snapshot allowlisted PDF roots and enqueue immutable source jobs."""
    try:
        result = create_batch_service(Settings()).submit(tuple(roots), profile)
    except Exception as error:
        raise typer.Exit(code=_print_error(str(error))) from error
    typer.echo(json.dumps(result.__dict__ | {"batch_id": str(result.batch_id)}, sort_keys=True))


@batch_app.command("status")
def batch_status(batch_id: UUID = typer.Argument(...)) -> None:
    """Show durable aggregate status without depending on a live worker process."""
    try:
        typer.echo(json.dumps(create_batch_service(Settings()).status(batch_id), sort_keys=True))
    except Exception as error:
        raise typer.Exit(code=_print_error(str(error))) from error


@batch_app.command("report")
def batch_report(
    batch_id: UUID = typer.Argument(...),
    output_format: str = typer.Option("json", "--format", case_sensitive=False),
) -> None:
    """Emit every batch path and disposition as deterministic JSON or CSV."""
    try:
        report = create_batch_service(Settings()).report(batch_id)
    except Exception as error:
        raise typer.Exit(code=_print_error(str(error))) from error
    if output_format == "json":
        typer.echo(json.dumps(report, sort_keys=True))
        return
    if output_format != "csv":
        raise typer.Exit(code=_print_error("report format must be json or csv"))
    fields = [
        "item_id",
        "root",
        "path",
        "state",
        "reason",
        "quality",
        "source_sha256",
        "attempts",
        "final_manifest_key",
    ]
    writer = csv.DictWriter(sys.stdout, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(report)


@batch_app.command("cancel")
def cancel_batch(batch_id: UUID = typer.Argument(...)) -> None:
    """Cancel pending work and request cooperative cancellation of running stages."""
    try:
        create_batch_service(Settings()).cancel(batch_id)
    except Exception as error:
        raise typer.Exit(code=_print_error(str(error))) from error
    typer.echo(json.dumps({"batch_id": str(batch_id), "state": "cancelled"}, sort_keys=True))


@batch_app.command("retry")
def retry_batch_item(item_id: UUID = typer.Argument(...)) -> None:
    """Retry one quarantined item from its already copied immutable source object."""
    try:
        job_id = create_batch_service(Settings()).retry(item_id)
    except Exception as error:
        raise typer.Exit(code=_print_error(str(error))) from error
    typer.echo(json.dumps({"item_id": str(item_id), "job_id": str(job_id)}, sort_keys=True))


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
