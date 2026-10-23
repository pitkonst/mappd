"""The mappd CLI: development and rare operations.

Every command is non-interactive, prints JSON with --json, and exits 0 on success, 1 when the
operation or a check fails, 2 on bad input or invalid MAPPD_* settings. The stack commands are
grouped by MAPP-D layer: model (M), process (P), db (P, Postgres), workflow and action (D);
data and eval serve the evals.
"""

from typing import Annotated

import typer

from mappd import __version__
from mappd.cli import (
    action,
    data,
    db,
    doctor,
    evals,
    feedback,
    mail,
    model,
    process,
    serve,
    user,
    workflow,
)
from mappd.cli._common import JSON_MODE, configure_logging

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
)
app.command()(doctor.doctor)
app.command()(serve.serve)
app.command()(serve.mock)
app.add_typer(model.app, name="model")
app.add_typer(process.app, name="process")
app.add_typer(db.app, name="db")
app.add_typer(workflow.app, name="workflow")
app.add_typer(action.app, name="action")
app.add_typer(user.app, name="user")
app.add_typer(mail.app, name="mail")
app.add_typer(data.app, name="data")
app.add_typer(evals.app, name="eval")
app.add_typer(feedback.app, name="feedback")


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version, is_eager=True, help="Print the version and exit."
        ),
    ] = False,
) -> None:
    """MAPP-D: durable LLM business processes. Start with `mappd doctor`.

    The stack by layer: model (M), process and db (P), workflow and action (D), mail. The
    eval loop: data, eval, feedback. Every command takes --json; exit 0 ok, 1 failed, 2
    bad input or settings.
    """
    JSON_MODE.set(False)
    configure_logging()
