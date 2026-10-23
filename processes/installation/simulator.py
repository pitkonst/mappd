"""Dialogue simulation: a simulated technician works through a scenario with the real engine.

One step of the script per turn: the technician reports what has just happened (in the
persona's words), the turn engine answers as in production — interpreter, state machine,
resolver over the scenario's systems — and the episode ends when the job is handed over, when
it blocks (the resolution is then in place; the dispatcher's side is not simulated), or after
`MAX_TURNS`. Photos follow the script: `yes` goes with the step's message, `late` follows in the
next message. The returned episode is what the metrics and the judge read.
"""

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pydantic_ai.models import Model

from . import domain, engine
from .agents import resolver
from .agents import simulator as technician
from .agents.interpreter import interpret as interpret_message
from .agents.interpreter import message_text as interpreter_text
from .engine import Interpret, JobView, Resolve, ToolCall
from .mocks import mock_llm
from .mocks.systems import Systems
from .mocks.systems import toolset as systems_toolset
from .schemas import Interpretation, Resolution, State
from .sim import EXCEPTIONS, Scenario, Step

MAX_TURNS = 16
PROGRESS: dict[str, State] = {s: s for s in domain.FLOW}

type Speak = Callable[[Scenario, Step, str, bool], Awaitable[str]]
type ResolverFor = Callable[[Systems], Resolve]


async def simulated(scenario: Scenario, step: Step, bot_reply: str, photo: bool) -> str:
    """The simulator model words the technician's message."""
    return await technician.speak(scenario, step, bot_reply, photo=photo)


async def scripted(scenario: Scenario, step: Step, bot_reply: str, photo: bool) -> str:
    """The script's line as written (no model; the baseline's technician)."""
    del scenario, bot_reply
    return f"{step.line} (photo attached)" if photo else step.line


def photo_description(step: Step) -> str:
    """What the vision model would say about the step's photo."""
    return f"A photo of the site after this step: {step.event.replace('_', ' ')}."


def model_resolver(model: Model | None) -> ResolverFor:
    """A resolver over each scenario's own systems (a plain agent: no workflow in evals)."""

    def resolver_for(systems: Systems) -> Resolve:
        agent = resolver.build([systems_toolset(systems)], durable_runs=False, model=model)

        async def resolve(
            job: JobView, reason: str, history: list[str]
        ) -> tuple[Resolution, list[ToolCall]]:
            return await resolver.run(agent, job, reason, history)

        return resolve

    return resolver_for


async def run(
    scenario: Scenario,
    *,
    speak: Speak = simulated,
    interpret: Interpret = interpret_message,
    resolver_for: ResolverFor | None = None,
    resolver_model: Model | None = None,
) -> dict[str, Any]:
    """One episode; the transcript and what the metrics need."""
    systems = Systems(scenario.facts)
    resolve = (resolver_for or model_resolver(resolver_model))(systems)
    job = JobView(
        external_id=scenario.job.external_id,
        state="assigned",
        skus=scenario.job.skus,
        site=scenario.job.site,
    )
    truth: State = "assigned"
    transcript: list[dict[str, Any]] = []
    states: list[str] = []
    true_states: list[str] = []
    revealed: int | None = None
    blocked: int | None = None
    resolution: Resolution | None = None
    reason: str | None = None
    calls: list[ToolCall] = []
    bot_reply, index, withheld = "", 0, False
    for turn in range(MAX_TURNS):
        if index >= len(scenario.script):
            break
        step = scenario.script[index]
        photo = step.photo == "yes" or (step.photo == "late" and withheld)
        text = await speak(scenario, step, bot_reply, photo)
        truth = PROGRESS.get(step.event, truth)
        if step.event in EXCEPTIONS and revealed is None:
            revealed = turn
        job = job.model_copy(update={"turn_key": str(turn)})
        result = await engine.turn(
            job,
            text,
            photo_description(step) if photo else None,
            interpret=interpret,
            resolve=resolve,
        )
        bot_reply = result.reply
        history = [*job.history, f"installer: {text}", f"bot: {bot_reply}"]
        job = job.model_copy(update={"state": result.state, "history": history})
        transcript.append(
            {
                "turn": turn,
                "installer": text,
                "photo": photo_description(step) if photo else None,
                "bot": bot_reply,
                "state": result.state,
            }
        )
        states.append(result.state)
        true_states.append(truth)
        if result.state == "blocked":
            blocked, resolution, calls = turn, result.resolution, result.tool_calls
            reason = result.blocked_reason
            break
        if result.state == "handed_over":
            break
        if step.photo == "late" and not withheld:
            withheld = True  # the same step again, now with its photo
        else:
            index, withheld = index + 1, False
    return {
        "transcript": transcript,
        "states": states,
        "true_states": true_states,
        "final_state": job.state,
        "exception": reason,
        "revealed_turn": revealed,
        "blocked_turn": blocked,
        "summary": resolution.summary if resolution else None,
        "proposals": [p.model_dump() for p in resolution.proposals] if resolution else [],
        "tool_calls": [c.model_dump(mode="json") for c in calls],
        "ticket": any(c.tool == "open_ticket" for c in calls),
    }


async def solve(case_input: dict[str, Any]) -> dict[str, Any]:
    """The eval solver: one simulated episode of a scenario."""
    return await run(Scenario.model_validate(case_input))


async def keyword_interpret(
    job: JobView, nxt: State | None, text: str, photo: str | None
) -> Interpretation:
    """Keyword rules instead of the interpreter model (the baseline)."""
    return Interpretation.model_validate(
        mock_llm.interpret(interpreter_text(job, nxt, text, photo))
    )


def _no_resolver(_systems: Systems) -> Resolve:
    async def resolve(
        job: JobView, reason: str, history: list[str]
    ) -> tuple[Resolution, list[ToolCall]]:
        del job, reason, history
        return Resolution(summary="", reply="A dispatcher will look into it.", proposals=[]), []

    return resolve


def keyword_baseline(case_input: dict[str, Any]) -> dict[str, Any]:
    """A floor without models: the script's lines, keyword reading, no resolver."""
    episode = run(
        Scenario.model_validate(case_input),
        speak=scripted,
        interpret=keyword_interpret,
        resolver_for=_no_resolver,
    )
    # Baselines are sync and called from the runner's event loop: run on a thread of our own.
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, episode).result()
