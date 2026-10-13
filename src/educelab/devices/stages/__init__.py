"""Stages: devices that position things along linear or rotary axes."""

from educelab.devices.stages.base import Axis, AxisKind, Stage
from educelab.devices.stages.simulated import SimulatedStage

__all__ = ["Axis", "AxisKind", "SimulatedStage", "Stage"]
