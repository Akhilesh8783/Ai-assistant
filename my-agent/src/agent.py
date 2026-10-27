from __future__ import annotations

import logging

from pathlib import Path

from dotenv import load_dotenv

# Always locate .env.local relative to this project's root directory
env_path = Path(__file__).resolve().parent.parent / ".env.local"
load_dotenv(dotenv_path=env_path)
load_dotenv()

from google.genai import types as genai_types
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    TurnHandlingOptions,
    cli,
    inference,
    room_io,
)
from livekit.agents.beta.tools import EndCallTool
from livekit.plugins import ai_coustics, google

from browser import BrowserManager
from prompts import AGENT_INSTRUCTIONS
from tools import BrowserTools

logger = logging.getLogger("agent")


class Assistant(Agent):
    def __init__(self, browser: BrowserManager | None = None) -> None:
        self.browser = browser or BrowserManager(headless=False)
        self.browser_tools = BrowserTools(self.browser)
        self._end_call_tool = EndCallTool(
            extra_description=(
                "Only end the call after the user clearly says they are finished, "
                "says goodbye, or directly asks to end the call."
            ),
            end_instructions=(
                "Give Jarvis's brief, polite British-English farewell, then end the call."
            ),
        )
        super().__init__(
            llm=google.beta.realtime.RealtimeModel(
                model="gemini-3.1-flash-live-preview",
                voice="Enceladus",
                language="en-GB",
                tool_response_scheduling=genai_types.FunctionResponseScheduling.WHEN_IDLE,
            ),
            instructions=AGENT_INSTRUCTIONS,
            tools=[
                *self.browser_tools.tools,
                *self._end_call_tool.tools,
            ],
        )


server = AgentServer()


@server.rtc_session(agent_name="my-agent")
async def my_agent(ctx: JobContext):
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }

    # Headless is False so the user can see Jarvis open and interact with the browser
    browser = BrowserManager(headless=False)
    ctx.add_shutdown_callback(browser.close)

    # Gemini realtime handles speech-to-speech with turn handling and interruptions
    session = AgentSession(
        turn_handling=TurnHandlingOptions(
            turn_detection=inference.TurnDetector(),
            interruption={"mode": "adaptive"},
            preemptive_generation={"enabled": True},
        ),
    )

    # Start the session with audio and video support
    await session.start(
        agent=Assistant(browser),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            video_input=True,
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=ai_coustics.audio_enhancement(
                    model=ai_coustics.EnhancerModel.QUAIL_VF_S
                ),
            ),
        ),
    )

    # Join the room and connect to the user
    await ctx.connect()


if __name__ == "__main__":
    cli.run_app(server)
