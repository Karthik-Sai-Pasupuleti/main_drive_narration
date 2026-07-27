from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field

from utils.utils import load_config

# 1x1 transparent JPEG, used when no RViz frame was captured so the image
# placeholder always has valid data (the prompt then narrates from text only).
_BLANK_IMAGE = ("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////"
                "////////////////////////////////////////////////////wgALCAABAAEB"
                "AREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=")


class Narration(BaseModel):
    """Structured narration reply the model must return as JSON."""

    scene: str = Field(description="a short description of the road / intersection "
                                   "seen in the image")
    action: str = Field(description="the given driving action, repeated in a few words")
    narration: str = Field(
        description="one warm, present-tense, first-person-plural sentence "
                    "for the passenger, at most 14 words, grounded in the scene")


class OllamaBot:
    """Ollama-backed narrator: renders a text + image prompt, returns structured JSON."""

    def __init__(self, config_path: str):
        """Build the prompt + model chain from a TOML config.

        Args:
            config_path (str): path to the TOML config holding model,
                temperature, system_prompt and user_template.
        """
        self.config = load_config(config_path)
        self.system_prompt = self.config["system_prompt"]
        self.user_prompt = self.config["user_template"]
        self.model = ChatOllama(model=self.config["model"],
                                temperature=self.config.get("temperature", 0.5))

        # The human turn is multimodal: the text template plus an image_url whose
        # base64 payload is the {image_data} placeholder, filled at invoke time.
        self.prompt_template = ChatPromptTemplate.from_messages([
            ("system", self.system_prompt),
            ("human", [
                {"type": "text", "text": self.user_prompt},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64,{image_data}"}},
            ]),
        ])
        self.chain = self.prompt_template | self.model.with_structured_output(Narration)

    def invoke(self, driving_action: str, *, drone_data: str = "none",
               infrastructure_data: str = "none",
               action_memory: str = "(no readings)",
               narration_memory: str = "(none yet)",
               image: str | None = None) -> Narration:
        """Render the prompt for one event and return the structured reply.

        Args:
            driving_action (str): the ground-truth action to narrate.
            drone_data (str, optional): latest drone report. Defaults to "none".
            infrastructure_data (str, optional): latest infrastructure report. Defaults to "none".
            action_memory (str, optional): recent HUD actions. Defaults to "(no readings)".
            narration_memory (str, optional): previous narrations. Defaults to "(none yet)".
            image (str | None, optional): base64 JPEG of the RViz view. Defaults to None.

        Returns:
            Narration: the parsed reply with .scene, .action and .narration fields.
        """
        return self.chain.invoke({
            "driving_action": driving_action,
            "drone_data": drone_data,
            "infrastructure_data": infrastructure_data,
            "action_memory": action_memory,
            "narration_memory": narration_memory,
            "image_data": image or _BLANK_IMAGE,
        })

    

    












