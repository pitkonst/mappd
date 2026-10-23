"""mappd model: the M layer, the model gateway."""

import asyncio
from dataclasses import asdict
from typing import Annotated

import typer

from mappd import gateway, telemetry
from mappd.cli._common import JsonFlag, emit, fail, load_settings, status

app = typer.Typer(help="M: the model gateway.", no_args_is_help=True)


@app.command("list")
def list_(as_json: JsonFlag = False) -> None:
    """List the models the gateway routes. Exit 1 if the gateway is unreachable.

    Example:
        mappd model list --json
    """
    settings = load_settings()
    try:
        names = gateway.list_models(settings)
    except gateway.GatewayError as e:
        fail(str(e))
    emit({"models": names}, as_json, names or ["no models routed"])


@app.command("try")
def try_(
    model: Annotated[str, typer.Argument(help="A gateway model name, e.g. fast.")],
    prompt: Annotated[str, typer.Option("--prompt", help="The text to send.")],
    as_json: JsonFlag = False,
) -> None:
    """Send one prompt to one model through the gateway.

    Prints the answer, token counts and the trace id.

    Example:
        mappd model try fast --prompt "Say hello" --json
    """
    settings = load_settings()
    provider = telemetry.setup(settings)
    try:
        with status(f"asking {model}…", as_json):
            result = asyncio.run(gateway.try_model(settings, model, prompt))
    except gateway.GatewayError as e:
        fail(str(e))
    finally:
        provider.shutdown()
    emit(
        asdict(result),
        as_json,
        [
            result.output,
            f"tokens: {result.input_tokens} in, {result.output_tokens} out",
            f"trace: {result.trace_id}",
        ],
    )
