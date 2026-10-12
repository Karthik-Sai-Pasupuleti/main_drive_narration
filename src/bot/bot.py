from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama
from pydantic import AliasChoices, BaseModel, Field

from utils.utils import load_config

# 1x1 transparent JPEG, used when no RViz frame was captured so the image
# placeholder always has valid data (the prompt then narrates from text only).
_BLANK_IMAGE = ("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////"
                "////////////////////////////////////////////////////wgALCAABAAEB"
                "AREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=")


class Narration_Input(BaseModel):
    """What the narration agent is given: the reporting camera's frame, which
    camera it is, and optionally that detector's object list. `image` is
    routed to the vision slot; the rest fill narration_prompt.toml."""

    image: str = Field(description="base64 JPEG frame from the reporting camera")
    source: str = Field(description="camera that reported it: mobile_pole, robot or drone")
    detections: str = Field(default="", description="optional object list from the "
                            "source's ROS detections")


class Narration_Output(BaseModel):
    """What the model returns: the spoken line, nothing else."""

    narration: str = Field(description="the passenger narration for this hazard")


class AgentConfig(BaseModel):
    """One agent's model settings (from the pipeline config's section)."""

    provider: str = Field(default="ollama", description="'ollama' (local) or 'openai'")
    model: str = Field(description="model id / Ollama tag")
    temperature: float = Field(default=0.5)
    num_predict: int | None = Field(default=None, description="max tokens to generate")
    prompt: str = Field(description="path to the prompt TOML (system_prompt + user_template)")
    vision: bool = Field(default=False, description="send an image to the model")
    keep_alive: str | int | None = Field(
        default="30m", description="ollama-only: how long to keep the model loaded in "
        "VRAM after a call (e.g. '30m', or -1 for indefinitely); ignored by openai")


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
                      num_predict=cfg.num_predict, keep_alive=cfg.keep_alive)


class LLMBot:
    """Provider-agnostic agent: an Ollama or OpenAI model + a prompt, returning a
    structured Pydantic object. Model / temperature / provider come from the
    AgentConfig; the prompt file supplies system_prompt + user_template."""

    def __init__(self, cfg: AgentConfig, output_model: type[BaseModel]) -> None:
        """Build the prompt + model chain.

        Args:
            cfg (AgentConfig): provider / model / temperature / prompt path / vision.
            output_model (type[BaseModel]): schema the model must return as JSON.
        """
        self.vision = cfg.vision
        pcfg = PromptConfig(**load_config(cfg.prompt))
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
