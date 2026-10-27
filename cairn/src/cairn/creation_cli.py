"""Non-interactive JSON commands for agents that create and refine Cairn work."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar
from urllib.parse import quote, urlsplit

import click
from pydantic import BaseModel, ValidationError
import requests
import yaml

from cairn.server.models import CreateProjectRequest, CreateWorkflowRequest

MAX_INPUT_BYTES = 1024 * 1024
DEFAULT_SERVER = "http://127.0.0.1:8000"
REQUEST_TIMEOUT = 15
ModelType = TypeVar("ModelType", bound=BaseModel)


def emit(value) -> None:
    click.echo(json.dumps(value, ensure_ascii=False))


def fail(kind: str, message: str, **details) -> None:
    emit({"error": {"type": kind, "message": message, **details}})
    raise click.exceptions.Exit(1)


class JsonGroup(click.Group):
    @staticmethod
    def usage_error(exc):
        emit({"error": {"type": "usage", "message": exc.format_message()}})
        raise click.exceptions.Exit(exc.exit_code) from exc

    def parse_args(self, ctx, args):
        try:
            return super().parse_args(ctx, args)
        except click.ClickException as exc:
            self.usage_error(exc)

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except click.ClickException as exc:
            self.usage_error(exc)


def load_body(source: str) -> dict:
    try:
        if source == "-":
            content = click.get_binary_stream("stdin").read(MAX_INPUT_BYTES + 1)
        else:
            with Path(source).open("rb") as stream:
                content = stream.read(MAX_INPUT_BYTES + 1)
    except OSError:
        fail("input", "Could not read --file. Supply a readable JSON/YAML file or '-' for standard input.")
    if len(content) > MAX_INPUT_BYTES:
        fail("input", "Input exceeds the 1 MiB file limit. Supply a smaller project or workflow definition.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        fail("parse", "Input must be UTF-8 JSON or YAML.")
    try:
        # YAML safely accepts JSON as well, including when input comes from stdin.
        body = json.loads(text) if source.lower().endswith(".json") else yaml.safe_load(text)
    except json.JSONDecodeError as exc:
        fail("parse", "Invalid JSON input.", line=exc.lineno, column=exc.colno)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = {"line": mark.line + 1, "column": mark.column + 1} if mark is not None else {}
        fail("parse", "Invalid YAML or JSON input.", **location)
    if not isinstance(body, dict):
        fail("input", "Input must contain one object with the API fields, without a project/workflow wrapper.")
    return body


def validate_body(model: type[ModelType], body: dict, *, exclude_unset: bool = False) -> dict:
    try:
        return model.model_validate(body).model_dump(mode="json", by_alias=True, exclude_unset=exclude_unset)
    except ValidationError as exc:
        fail("validation", "The definition does not match the schema. Use the corresponding schema command.",
             issues=exc.errors(include_input=False, include_context=False, include_url=False))


def server_url(server: str) -> str:
    try:
        parts = urlsplit(server)
        _ = parts.port
    except ValueError:
        fail("usage", "--server must be a valid HTTP(S) base URL.")
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment or server != server.strip()):
        fail("usage", "--server must be an HTTP(S) base URL without credentials, query, or fragment.")
    return server.rstrip("/")


def request_json(method: str, server: str, path: str, body: dict) -> None:
    base = server_url(server)
    try:
        response = requests.request(method, base + path, json=body, timeout=REQUEST_TIMEOUT)
    except requests.Timeout:
        fail("network", "Cairn did not respond within 15 seconds. Check --server and inspect server state before retrying.")
    except requests.RequestException:
        # Request exception strings can expose URL credentials or proxy tokens.
        fail("network", "Could not reach Cairn. Check --server and inspect server state before retrying.")
    if not 200 <= response.status_code < 300:
        # Error bodies can echo input and credentials. The locally validated request
        # and HTTP status are enough for an agent to decide whether to correct or retry.
        message = ("Cairn encountered a server error. Check the server's health and state before retrying."
                   if response.status_code >= 500 else
                   "Cairn rejected the request. Check the input and the object's current state.")
        fail("server", message,
             status=response.status_code)
    try:
        result = response.json()
    except ValueError:
        fail("protocol", "Cairn returned a successful status without valid JSON.", status=response.status_code)
    if not isinstance(result, dict):
        fail("protocol", "Cairn returned JSON that is not a project or workflow object.", status=response.status_code)
    emit(result)


def create(model: type[BaseModel], resource: str, source: str | None, goal: str | None,
           title: str | None, origin: str | None, server: str, dry_run: bool) -> None:
    if source is not None and goal is not None:
        fail("usage", "Choose either --file or --goal, not both.")
    if source is None and goal is None:
        fail("usage", "Supply --file with a JSON/YAML definition or --goal for a project.")
    body = load_body(source) if source is not None else {"goal": goal}
    if title is not None:
        body["title"] = title
    if origin is not None:
        body["origin"] = origin
    validated = validate_body(model, body)
    if dry_run:
        emit({"dry_run": True, "resource": resource, "request": validated})
        return
    request_json("POST", server, f"/{resource}s", validated)


@click.group(cls=JsonGroup)
def project():
    """Create and refine projects without interactive prompts; data output is JSON."""


@click.group(cls=JsonGroup)
def workflow():
    """Create and refine staged workflows; data output is JSON."""


def creation_options(function):
    for option in reversed([
        click.option("--file", "source", help="Exact API object in JSON/YAML; use '-' for stdin. Maximum 1 MiB."),
        click.option("--title", help="Optional title; overrides the file's title when provided."),
        click.option("--origin", help="Optional starting context; overrides the file's origin when provided."),
        click.option("--server", default=DEFAULT_SERVER, show_default=True, help="Cairn server base URL."),
        click.option("--dry-run", is_flag=True, help="Validate and print the normalized definition without creating work."),
    ]):
        function = option(function)
    return function


@project.command("create")
@click.option("--goal", help="Create a project from its goal without an input file.")
@creation_options
def create_project(source, goal, title, origin, server, dry_run):
    """Create a project from a goal or an exact JSON/YAML API object."""
    create(CreateProjectRequest, "project", source, goal, title, origin, server, dry_run)


@workflow.command("create")
@creation_options
def create_workflow(source, title, origin, server, dry_run):
    """Create a workflow from a JSON/YAML API object containing stages."""
    if source is None:
        fail("usage", "Supply --file with a JSON/YAML workflow definition, or '-' for standard input.")
    create(CreateWorkflowRequest, "workflow", source, None, title, origin, server, dry_run)


@project.command("schema")
def project_schema():
    """Print the project creation API's JSON Schema; no server connection is needed."""
    emit(CreateProjectRequest.model_json_schema())


@workflow.command("schema")
def workflow_schema():
    """Print the workflow creation API's JSON Schema; no server connection is needed."""
    emit(CreateWorkflowRequest.model_json_schema())


def metadata(resource, object_id, title, origin, server):
    from cairn.server.models import UpdateMetadataRequest

    body = {}
    if title is not None:
        body["title"] = title
    if origin is not None:
        body["origin"] = origin
    validated = validate_body(UpdateMetadataRequest, body, exclude_unset=True)
    request_json("PATCH", server, f"/{resource}s/{quote(object_id, safe='')}/metadata", validated)


def metadata_options(function):
    for option in reversed([
        click.argument("object_id"),
        click.option("--title", help="Replace the title with a nonempty title."),
        click.option("--origin", help="Refine starting context; completed historical stages are unchanged."),
        click.option("--server", default=DEFAULT_SERVER, show_default=True, help="Cairn server base URL."),
    ]):
        function = option(function)
    return function


@project.command("metadata")
@metadata_options
def project_metadata(object_id, title, origin, server):
    """Update project metadata after creation."""
    metadata("project", object_id, title, origin, server)


@workflow.command("metadata")
@metadata_options
def workflow_metadata(object_id, title, origin, server):
    """Update workflow metadata after creation."""
    metadata("workflow", object_id, title, origin, server)
