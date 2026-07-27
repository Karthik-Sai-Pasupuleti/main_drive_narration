from pathlib import Path

from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama
from pydantic import AliasChoices, BaseModel, Field

from utils.utils import load_config

# 1x1 transparent JPEG, used when no RViz frame was captured so the image
# placeholder always has valid data (the prompt then narrates from text only).
_BLANK_IMAGE = ("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////"
                "////////////////////////////////////////////////////wgALCAABAAEB"
                "AREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=")


class Action_Based_Narration_Input(BaseModel):
    """Text inputs for the single-agent narration model (image passed separately).
    Fields match the actions_promt.toml placeholders."""

    driving_action: str = Field(description="the ground-truth driving action")
    drone_data: str = Field(default="none", description="latest drone report")
    infrastructure_data: str = Field(default="none", description="latest infrastructure report")
    action_memory: str = Field(default="(no readings)", description="recent HUD actions")
    narration_memory: str = Field(default="(none yet)", description="previous narrations")

class Action_Based_Narration_Output(BaseModel):
    """Structured narration reply the model must return as JSON."""

    narration: str = Field(description="one warm, present-tense, first-person-plural sentence "
                            "for the passenger, at most 14 words, grounded in the scene")
    

## multi-agent narration input and output classes

class Multi_Agent_Narration_Input(BaseModel):
    """Text inputs for the multi-agent narration model (the two scene
    descriptions come from the vision agents). Fields match the
    narration_prompt.toml placeholders."""

    driving_action: str = Field(description="the ground-truth driving action")
    road_geometry_description: str = Field(description="road-geometry agent output")
    obstacles_description: str = Field(description="obstacles agent output")
    drone_data: str = Field(default="none", description="latest drone report")
    infrastructure_data: str = Field(default="none", description="latest infrastructure report")
    action_memory: str = Field(default="(no readings)", description="recent HUD actions")
    narration_memory: str = Field(default="(none yet)", description="previous narrations")

class Road_Geometry_Input(BaseModel):
    """Bev image input for the model: structured narration reply the model must return as JSON."""

    Bev_image: str = Field(description="Base64 encoded BEV image of the road geometry")

class Road_Geometry_Output(BaseModel):
    """Bev image output for the model: structured narration reply the model must return as JSON."""

    road_geometry: str = Field(description="A short description of the road geometry seen in the BEV image")

class Obstacles_description_Input(BaseModel):
    """Front perspective image input for the model: structured narration reply the model must return as JSON."""

    front_perspective_image: str = Field(description="Base64 encoded front view image of the obstacles")

class Obstacles_description_Output(BaseModel):
    """Front perspective image output for the model: structured narration reply the model must return as JSON."""

    obstacles_description: str = Field(description="A short description of the obstacles seen in the front view image")

class Multi_Agent_Narration_Output(BaseModel):
    """Structured narration reply the model must return as JSON."""

    Narration: str = Field(description="one warm, present-tense, first-person-plural sentence "
                            "for the passenger, at most 14 words, grounded in the scene")

    
class AgentConfig(BaseModel):
    """One agent's model settings (from the pipeline config's section)."""

    provider: str = Field(default="ollama", description="'ollama' (local) or 'openai'")
    model: str = Field(description="model id / Ollama tag")
    temperature: float = Field(default=0.5)
    num_predict: int | None = Field(default=None, description="max tokens to generate")
    prompt: str = Field(description="path to the prompt TOML (system_prompt + user_template)")
    vision: bool = Field(default=False, description="send an image to the model")


class PromptConfig(BaseModel):
    """The prompt TOML: system prompt + user template (any model keys are ignored)."""

    system_prompt: str
    # accept either "user_template" or "user_prompt" as the key
    user_template: str = Field(
        validation_alias=AliasChoices("user_template", "user_prompt"))


def _make_model(cfg: AgentConfig):
    """Build the LangChain chat model for the configured provider."""
    if cfg.provider == "openai":
        from langchain_openai import ChatOpenAI  # needs OPENAI_API_KEY in the env
        return ChatOpenAI(model=cfg.model, temperature=cfg.temperature,
                          max_tokens=cfg.num_predict)
    return ChatOllama(model=cfg.model, temperature=cfg.temperature,
                      num_predict=cfg.num_predict)


class LLMBot:
    """Provider-agnostic agent: an Ollama or OpenAI model + a prompt, returning a
    structured Pydantic object. Model / temperature / provider come from the
    AgentConfig; the prompt file supplies system_prompt + user_template."""

    def __init__(self, cfg: AgentConfig, output_model: type[BaseModel],
                 prompts_dir: str = "") -> None:
        """Build the prompt + model chain.

        Args:
            cfg (AgentConfig): provider / model / temperature / prompt path / vision.
            output_model (type[BaseModel]): schema the model must return as JSON.
            prompts_dir (str): base dir the cfg.prompt path is resolved against.
        """
        self.vision = cfg.vision
        prompt_path = str(Path(prompts_dir) / cfg.prompt) if prompts_dir else cfg.prompt
        pcfg = PromptConfig(**load_config(prompt_path))
        model = _make_model(cfg)
        if cfg.vision:
            human = ("human", [
                {"type": "text", "text": pcfg.user_template},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64,{image_data}"}},
            ])
        else:
            human = ("human", pcfg.user_template)
        prompt = ChatPromptTemplate.from_messages([("system", pcfg.system_prompt), human])
        self.chain = prompt | model.with_structured_output(output_model)

    def invoke(self, inputs: BaseModel, *, image: str | None = None) -> BaseModel:
        """Run the agent from a typed input model and return its output model.

        Args:
            inputs (BaseModel): the agent's *_Input model; its fields fill the
                prompt placeholders. Any base64 image field (or the `image` arg)
                is routed to the vision image_data slot.
            image (str | None): base64 JPEG for a vision agent, if not carried in
                the input model; a blank frame is used when neither is given.

        Returns:
            BaseModel: an instance of this agent's output_model.
        """
        fields = inputs.model_dump()
        if self.vision:
            for key in [k for k in fields if "image" in k.lower()]:
                image = image or fields.pop(key)
            fields["image_data"] = image or _BLANK_IMAGE
        return self.chain.invoke(fields)

    

    












