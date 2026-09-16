"""Typed inputs and outputs of the two LLM steps.

The input fields are the prompt placeholders (they fill `user_template` in the
matching TOML under config/prompts/), the output fields are the JSON the model
has to return.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

NOT_RECEIVED = "(not received yet)"


class TurnCommentaryInput(BaseModel):
    """Layer 1: what the turn-commentary VLM is told (the RViz frame is sent separately)."""

    driving_action: str = Field(description="the detected action, e.g. 'TURN LEFT (intersection)'")
    turn_direction: str = Field(description="'left' or 'right'")
    turn_phase: str = Field(default="upcoming", description="'upcoming' or 'executing'")
    narration_memory: str = Field(default="(none yet)",
                                  description="lines already spoken on this drive, oldest first")


class TurnCommentaryOutput(BaseModel):
    """Layer 1: the VLM's reading of the frame plus the line to speak."""

    scene: str = Field(description="a short factual description of the road/intersection "
                                   "actually visible in the frame; say so if it is unclear")
    narration: str = Field(description="one warm, present-tense, first-person-plural sentence "
                                       "for the passenger, at most 14 words, conveying the turn")


class SubscriberInput(BaseModel):
    """Layer 3: the three publisher slots, one per ROS node (unfilled ones say so)."""

    infrapole_report: str = Field(default=NOT_RECEIVED, description="ROS node 1 - infra pole")
    drone_report: str = Field(default=NOT_RECEIVED, description="ROS node 2 - drone")
    robot_report: str = Field(default=NOT_RECEIVED, description="ROS node 3 - robot")


class SubscriberOutput(BaseModel):
    """Layer 3: the refined context the single subscriber produces from those slots."""

    refined_context: str = Field(description="one or two plain sentences stating what is known "
                                             "so far and which node reported it")
    narration: str = Field(description="one warm, present-tense, first-person-plural sentence "
                                       "for the passenger, at most 20 words, naming the source "
                                       "and keeping its stated reason")
