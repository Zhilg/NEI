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
from idp.health import RuntimeHealthError, check_runtime_health
from idp.runtime import create_batch_service, register_default_profile, run_controller, run_worker

app = typer.Typer(no_args_is_help=True, help="Offline PDF batch pipeline operations.")
controller_app = typer.Typer(no_args_is_help=True, help="Controller operations.")
worker_app = typer.Typer(no_args_is_help=True, help="Worker operations.")
batch_app = typer.Typer(no_args_is_help=True, help="One-shot PDF batch operations.")
app.add_typer(controller_app, name="controller")
app.add_typer(worker_app, name="worker")
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


@app.command("healthcheck")
def healthcheck(include_models: bool = typer.Option(True, "--models/--no-models")) -> None:
    """Check the local Compose services used by this mounted deployment."""
    try:
        report = check_runtime_health(Settings(), include_models=include_models)
    except RuntimeHealthError as error:
        raise typer.Exit(code=_print_error(str(error))) from error
    typer.echo(json.dumps(report.__dict__, sort_keys=True))


@app.command("bootstrap")
def bootstrap() -> None:
    """Register the mounted runtime configuration as the default batch profile."""
    try:
        profile_hash = register_default_profile(Settings())
    except Exception as error:
        raise typer.Exit(code=_print_error(str(error))) from error
    typer.echo(json.dumps({"profile": "default", "profile_hash": profile_hash}, sort_keys=True))


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
    """Start the durable reconstruction, entity extraction, and publication worker."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_worker(Settings())
